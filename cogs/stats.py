from discord.ext import commands, tasks
from collections import Counter, defaultdict

from .utils import checks, db, time, formats
from .utils.paginator import CannotPaginate

import pkg_resources
import logging
import discord
import textwrap
import datetime
import traceback
import itertools
import typing
import asyncpg
import asyncio
import pygit2
import psutil
import json
import os
import re
import io
import gc

log = logging.getLogger(__name__)

LOGGING_CHANNEL = 309632009427222529

class GatewayHandler(logging.Handler):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(logging.INFO)

    def filter(self, record):
        return record.name == 'discord.gateway' or 'Shard ID' in record.msg or 'Websocket closed ' in record.msg

    def emit(self, record):
        self.cog.add_record(record)

class Commands(db.Table):
    id = db.PrimaryKeyColumn()

    guild_id = db.Column(db.Integer(big=True), index=True)
    channel_id = db.Column(db.Integer(big=True))
    author_id = db.Column(db.Integer(big=True), index=True)
    used = db.Column(db.Datetime, index=True)
    prefix = db.Column(db.String)
    command = db.Column(db.String, index=True)
    failed = db.Column(db.Boolean, index=True)

_INVITE_REGEX = re.compile(r'(?:https?:\/\/)?discord(?:\.gg|\.com|app\.com\/invite)?\/[A-Za-z0-9]+')

def censor_invite(obj, *, _regex=_INVITE_REGEX):
    return _regex.sub('[censored-invite]', str(obj))

def hex_value(arg):
    return int(arg, base=16)

def object_at(addr):
    for o in gc.get_objects():
        if id(o) == addr:
            return o
    return None

class Stats(commands.Cog):
    """Bot usage statistics."""

    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._data_batch = []
        self.bulk_insert_loop.add_exception_type(asyncpg.PostgresConnectionError)
        self.bulk_insert_loop.start()
        self._gateway_queue = asyncio.Queue(loop=bot.loop)
        self.gateway_worker.start()

    async def bulk_insert(self):
        query = """INSERT INTO commands (guild_id, channel_id, author_id, used, prefix, command, failed)
                   SELECT x.guild, x.channel, x.author, x.used, x.prefix, x.command, x.failed
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(guild BIGINT, channel BIGINT, author BIGINT, used TIMESTAMP, prefix TEXT, command TEXT, failed BOOLEAN)
                """

        if self._data_batch:
            await self.bot.pool.execute(query, self._data_batch)
            total = len(self._data_batch)
            if total > 1:
                log.info('Registered %s commands to the database.', total)
            self._data_batch.clear()

    def cog_unload(self):
        self.bulk_insert_loop.stop()
        self.gateway_worker.cancel()

    @tasks.loop(seconds=10.0)
    async def bulk_insert_loop(self):
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=0.0)
    async def gateway_worker(self):
        record = await self._gateway_queue.get()
        await self.notify_gateway_status(record)

    async def register_command(self, ctx):
        if ctx.command is None:
            return

        command = ctx.command.qualified_name
        self.bot.command_stats[command] += 1
        message = ctx.message
        destination = None
        if ctx.guild is None:
            destination = 'Private Message'
            guild_id = None
        else:
            destination = f'#{message.channel} ({message.guild})'
            guild_id = ctx.guild.id

        log.info(f'{message.created_at}: {message.author} in {destination}: {message.content}')
        async with self._batch_lock:
            self._data_batch.append({
                'guild': guild_id,
                'channel': ctx.channel.id,
                'author': ctx.author.id,
                'used': message.created_at.isoformat(),
                'prefix': ctx.prefix,
                'command': command,
                'failed': ctx.command_failed,
            })

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        await self.register_command(ctx)

    @commands.Cog.listener()
    async def on_socket_response(self, msg):
        self.bot.socket_stats[msg.get('t')] += 1

    @property
    def webhook(self):
        wh_id, wh_token = self.bot.config.stat_webhook
        hook = discord.Webhook.partial(id=wh_id, token=wh_token, adapter=discord.AsyncWebhookAdapter(self.bot.session))
        return hook

    async def log_error(self, *, ctx=None, extra=None):
        e = discord.Embed(title='Error', colour=0xdd5f53)
        e.description = f'```py\n{traceback.format_exc()}\n```'
        e.add_field(name='Extra', value=extra, inline=False)
        e.timestamp = datetime.datetime.utcnow()

        if ctx is not None:
            fmt = '{0} (ID: {0.id})'
            author = fmt.format(ctx.author)
            channel = fmt.format(ctx.channel)
            guild = 'None' if ctx.guild is None else fmt.format(ctx.guild)

            e.add_field(name='Author', value=author)
            e.add_field(name='Channel', value=channel)
            e.add_field(name='Guild', value=guild)

        await self.webhook.send(embed=e)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def commandstats(self, ctx, limit=20):
        """Shows command stats.

        Use a negative number for bottom instead of top.
        This is only for the current session.
        """
        counter = self.bot.command_stats
        width = len(max(counter, key=len))
        total = sum(counter.values())

        if limit > 0:
            common = counter.most_common(limit)
        else:
            common = counter.most_common()[limit:]

        output = '\n'.join(f'{k:<{width}}: {c}' for k, c in common)

        await ctx.send(f'```\n{output}\n```')

    @commands.command(hidden=True)
    async def socketstats(self, ctx):
        delta = datetime.datetime.utcnow() - self.bot.uptime
        minutes = delta.total_seconds() / 60
        total = sum(self.bot.socket_stats.values())
        cpm = total / minutes
        await ctx.send(f'{total} socket events observed ({cpm:.2f}/minute):\n{self.bot.socket_stats}')

    def get_bot_uptime(self, *, brief=False):
        return time.human_timedelta(self.bot.uptime, accuracy=None, brief=brief, suffix=False)

    @commands.command()
    async def uptime(self, ctx):
        """Tells you how long the bot has been up for."""
        await ctx.send(f'Uptime: **{self.get_bot_uptime()}**')

    def format_commit(self, commit):
        short, _, _ = commit.message.partition('\n')
        short_sha2 = commit.hex[0:6]
        commit_tz = datetime.timezone(datetime.timedelta(minutes=commit.commit_time_offset))
        commit_time = datetime.datetime.fromtimestamp(commit.commit_time).replace(tzinfo=commit_tz)

        # [`hash`](url) message (offset)
        offset = time.human_timedelta(commit_time.astimezone(datetime.timezone.utc).replace(tzinfo=None), accuracy=1)
        return f'[`{short_sha2}`](https://github.com/Rapptz/RoboDanny/commit/{commit.hex}) {short} ({offset})'

    def get_last_commits(self, count=3):
        repo = pygit2.Repository('.git')
        commits = list(itertools.islice(repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL), count))
        return '\n'.join(self.format_commit(c) for c in commits)

    @commands.command()
    async def about(self, ctx):
        """Tells you information about the bot itself."""

        revision = self.get_last_commits()
        embed = discord.Embed(description='Latest Changes:\n' + revision)
        embed.title = 'Official Bot Server Invite'
        embed.url = 'https://discord.gg/DWEaqMy'
        embed.colour = discord.Colour.blurple()

        owner = self.bot.get_user(self.bot.owner_id)
        embed.set_author(name=str(owner), icon_url=owner.avatar_url)

        # statistics
        total_members = 0
        total_unique = len(self.bot.users)

        text = 0
        voice = 0
        guilds = 0
        for guild in self.bot.guilds:
            guilds += 1
            total_members += guild.member_count
            for channel in guild.channels:
                if isinstance(channel, discord.TextChannel):
                    text += 1
                elif isinstance(channel, discord.VoiceChannel):
                    voice += 1

        embed.add_field(name='Members', value=f'{total_members} total\n{total_unique} unique')
        embed.add_field(name='Channels', value=f'{text + voice} total\n{text} text\n{voice} voice')

        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        embed.add_field(name='Process', value=f'{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU')

        version = pkg_resources.get_distribution('discord.py').version
        embed.add_field(name='Guilds', value=guilds)
        embed.add_field(name='Commands Run', value=sum(self.bot.command_stats.values()))
        embed.add_field(name='Uptime', value=self.get_bot_uptime(brief=True))
        embed.set_footer(text=f'Made with discord.py v{version}', icon_url='http://i.imgur.com/5BFecvA.png')
        embed.timestamp = datetime.datetime.utcnow()
        await ctx.send(embed=embed)

    def censor_object(self, obj):
        if not isinstance(obj, str) and obj.id in self.bot.blacklist:
            return '[censored]'
        return censor_invite(obj)

    async def show_guild_stats(self, ctx):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        embed = discord.Embed(title='Server Command Stats', colour=discord.Colour.blurple())

        # total command uses
        query = "SELECT COUNT(*), MIN(used) FROM commands WHERE guild_id=$1;"
        count = await ctx.db.fetchrow(query, ctx.guild.id)

        embed.description = f'{count[0]} commands used.'
        embed.set_footer(text='Tracking command usage since').timestamp = count[1] or datetime.datetime.utcnow()

        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id=$1
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Top Commands', value=value, inline=True)

        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands.'
        embed.add_field(name='Top Commands Today', value=value, inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)

        query = """SELECT author_id,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """


        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({uses} bot uses)'
                          for (index, (author_id, uses)) in enumerate(records)) or 'No bot users.'

        embed.add_field(name='Top Command Users', value=value, inline=True)

        query = """SELECT author_id,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """


        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({uses} bot uses)'
                          for (index, (author_id, uses)) in enumerate(records)) or 'No command users.'

        embed.add_field(name='Top Command Users Today', value=value, inline=True)
        await ctx.send(embed=embed)

    async def show_member_stats(self, ctx, member):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        embed = discord.Embed(title='Command Stats', colour=member.colour)
        embed.set_author(name=str(member), icon_url=member.avatar_url)

        # total command uses
        query = "SELECT COUNT(*), MIN(used) FROM commands WHERE guild_id=$1 AND author_id=$2;"
        count = await ctx.db.fetchrow(query, ctx.guild.id, member.id)

        embed.description = f'{count[0]} commands used.'
        embed.set_footer(text='First command used').timestamp = count[1] or datetime.datetime.utcnow()

        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id=$1 AND author_id=$2
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Most Used Commands', value=value, inline=False)

        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND author_id=$2
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Most Used Commands Today', value=value, inline=False)
        await ctx.send(embed=embed)

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 30.0, type=commands.BucketType.member)
    async def stats(self, ctx, *, member: discord.Member = None):
        """Tells you command usage stats for the server or a member."""
        async with ctx.typing():
            if member is None:
                await self.show_guild_stats(ctx)
            else:
                await self.show_member_stats(ctx, member)

    @stats.command(name='global')
    @commands.is_owner()
    async def stats_global(self, ctx):
        """Global all time command statistics."""

        query = "SELECT COUNT(*) FROM commands;"
        total = await ctx.db.fetchrow(query)

        e = discord.Embed(title='Command Stats', colour=discord.Colour.blurple())
        e.description = f'{total[0]} commands used.'

        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        query = """SELECT command, COUNT(*) AS "uses"
                   FROM commands
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)' for (index, (command, uses)) in enumerate(records))
        e.add_field(name='Top Commands', value=value, inline=False)

        query = """SELECT guild_id, COUNT(*) AS "uses"
                   FROM commands
                   GROUP BY guild_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = []
        for (index, (guild_id, uses)) in enumerate(records):
            if guild_id is None:
                guild = 'Private Message'
            else:
                guild = self.censor_object(self.bot.get_guild(guild_id) or f'<Unknown {guild_id}>')

            emoji = lookup[index]
            value.append(f'{emoji}: {guild} ({uses} uses)')

        e.add_field(name='Top Guilds', value='\n'.join(value), inline=False)

        query = """SELECT author_id, COUNT(*) AS "uses"
                   FROM commands
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = []
        for (index, (author_id, uses)) in enumerate(records):
            user = self.censor_object(self.bot.get_user(author_id) or f'<Unknown {author_id}>')
            emoji = lookup[index]
            value.append(f'{emoji}: {user} ({uses} uses)')

        e.add_field(name='Top Users', value='\n'.join(value), inline=False)
        await ctx.send(embed=e)

    @stats.command(name='today')
    @commands.is_owner()
    async def stats_today(self, ctx):
        """Global command statistics for the day."""

        query = "SELECT failed, COUNT(*) FROM commands WHERE used > (CURRENT_TIMESTAMP - INTERVAL '1 day') GROUP BY failed;"
        total = await ctx.db.fetch(query)
        failed = 0
        success = 0
        question = 0
        for state, count in total:
            if state is False:
                success += count
            elif state is True:
                failed += count
            else:
                question += count

        e = discord.Embed(title='Last 24 Hour Command Stats', colour=discord.Colour.blurple())
        e.description = f'{failed + success + question} commands used today. ' \
                        f'({success} succeeded, {failed} failed, {question} unknown)'

        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        query = """SELECT command, COUNT(*) AS "uses"
                   FROM commands
                   WHERE used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = '\n'.join(f'{lookup[index]}: {command} ({uses} uses)' for (index, (command, uses)) in enumerate(records))
        e.add_field(name='Top Commands', value=value, inline=False)

        query = """SELECT guild_id, COUNT(*) AS "uses"
                   FROM commands
                   WHERE used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY guild_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = []
        for (index, (guild_id, uses)) in enumerate(records):
            if guild_id is None:
                guild = 'Private Message'
            else:
                guild = self.censor_object(self.bot.get_guild(guild_id) or f'<Unknown {guild_id}>')
            emoji = lookup[index]
            value.append(f'{emoji}: {guild} ({uses} uses)')

        e.add_field(name='Top Guilds', value='\n'.join(value), inline=False)

        query = """SELECT author_id, COUNT(*) AS "uses"
                   FROM commands
                   WHERE used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query)
        value = []
        for (index, (author_id, uses)) in enumerate(records):
            user = self.censor_object(self.bot.get_user(author_id) or f'<Unknown {author_id}>')
            emoji = lookup[index]
            value.append(f'{emoji}: {user} ({uses} uses)')

        e.add_field(name='Top Users', value='\n'.join(value), inline=False)
        await ctx.send(embed=e)

    async def send_guild_stats(self, e, guild):
        e.add_field(name='Name', value=guild.name)
        e.add_field(name='ID', value=guild.id)
        e.add_field(name='Shard ID', value=guild.shard_id or 'N/A')
        e.add_field(name='Owner', value=f'{guild.owner} (ID: {guild.owner.id})')

        bots = sum(m.bot for m in guild.members)
        total = guild.member_count
        online = sum(m.status is discord.Status.online for m in guild.members)
        e.add_field(name='Members', value=str(total))
        e.add_field(name='Bots', value=f'{bots} ({bots/total:.2%})')
        e.add_field(name='Online', value=f'{online} ({online/total:.2%})')

        if guild.icon:
            e.set_thumbnail(url=guild.icon_url)

        if guild.me:
            e.timestamp = guild.me.joined_at

        await self.webhook.send(embed=e)

    @stats_today.before_invoke
    @stats_global.before_invoke
    async def before_stats_invoke(self, ctx):
        await ctx.trigger_typing()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        e = discord.Embed(colour=0x53dda4, title='New Guild') # green colour
        await self.send_guild_stats(e, guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        e = discord.Embed(colour=0xdd5f53, title='Left Guild') # red colour
        await self.send_guild_stats(e, guild)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        await self.register_command(ctx)
        if not isinstance(error, (commands.CommandInvokeError, commands.ConversionError)):
            return

        error = error.original
        if isinstance(error, (discord.Forbidden, discord.NotFound, CannotPaginate)):
            return

        e = discord.Embed(title='Command Error', colour=0xcc3366)
        e.add_field(name='Name', value=ctx.command.qualified_name)
        e.add_field(name='Author', value=f'{ctx.author} (ID: {ctx.author.id})')

        fmt = f'Channel: {ctx.channel} (ID: {ctx.channel.id})'
        if ctx.guild:
            fmt = f'{fmt}\nGuild: {ctx.guild} (ID: {ctx.guild.id})'

        e.add_field(name='Location', value=fmt, inline=False)
        e.add_field(name='Content', value=textwrap.shorten(ctx.message.content, width=512))

        exc = ''.join(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
        e.description = f'```py\n{exc}\n```'
        e.timestamp = datetime.datetime.utcnow()
        await self.webhook.send(embed=e)

    def add_record(self, record):
        # if self.bot.config.debug:
        #     return
        self._gateway_queue.put_nowait(record)

    async def notify_gateway_status(self, record):
        attributes = {
            'INFO': '\N{INFORMATION SOURCE}',
            'WARNING': '\N{WARNING SIGN}'
        }

        emoji = attributes.get(record.levelname, '\N{CROSS MARK}')
        dt = datetime.datetime.utcfromtimestamp(record.created)
        msg = f'{emoji} `[{dt:%Y-%m-%d %H:%M:%S}] {record.message}`'
        await self.webhook.send(msg, username='Gateway', avatar_url='https://i.imgur.com/4PnCKB3.png')

    @commands.command(hidden=True)
    @commands.is_owner()
    async def bothealth(self, ctx):
        """Various bot health monitoring tools."""

        # This uses a lot of private methods because there is no
        # clean way of doing this otherwise.

        HEALTHY = discord.Colour(value=0x43B581)
        UNHEALTHY = discord.Colour(value=0xF04947)
        WARNING = discord.Colour(value=0xF09E47)
        total_warnings = 0

        embed = discord.Embed(title='Bot Health Report', colour=HEALTHY)

        # Check the connection pool health.
        pool = self.bot.pool
        total_waiting = len(pool._queue._getters)
        current_generation = pool._generation

        description = [
            f'Total `Pool.acquire` Waiters: {total_waiting}',
            f'Current Pool Generation: {current_generation}',
            f'Connections In Use: {len(pool._holders) - pool._queue.qsize()}'
        ]

        questionable_connections = 0
        connection_value = []
        for index, holder in enumerate(pool._holders, start=1):
            generation = holder._generation
            in_use = holder._in_use is not None
            is_closed = holder._con is None or holder._con.is_closed()
            display = f'gen={holder._generation} in_use={in_use} closed={is_closed}'
            questionable_connections += any((in_use, generation != current_generation))
            connection_value.append(f'<Holder i={index} {display}>')

        joined_value = '\n'.join(connection_value)
        embed.add_field(name='Connections', value=f'```py\n{joined_value}\n```', inline=False)

        spam_control = self.bot.spam_control
        being_spammed = [
            str(key) for key, value in spam_control._cache.items()
            if value._tokens == 0
        ]

        description.append(f'Current Spammers: {", ".join(being_spammed) if being_spammed else "None"}')
        description.append(f'Questionable Connections: {questionable_connections}')

        total_warnings += questionable_connections
        if being_spammed:
            embed.colour = WARNING
            total_warnings += 1

        try:
            task_retriever = asyncio.Task.all_tasks
        except AttributeError:
            # future proofing for 3.9 I guess
            task_retriever = asyncio.all_tasks
        else:
            all_tasks = task_retriever(loop=self.bot.loop)

        event_tasks = [
            t for t in all_tasks
            if 'Client._run_event' in repr(t) and not t.done()
        ]

        cogs_directory = os.path.dirname(__file__)
        tasks_directory = os.path.join('discord', 'ext', 'tasks', '__init__.py')
        inner_tasks = [
            t for t in all_tasks
            if cogs_directory in repr(t) or tasks_directory in repr(t)
        ]

        bad_inner_tasks = ", ".join(hex(id(t)) for t in inner_tasks if t.done() and t._exception is not None)
        total_warnings += bool(bad_inner_tasks)
        embed.add_field(name='Inner Tasks', value=f'Total: {len(inner_tasks)}\nFailed: {bad_inner_tasks or "None"}')
        embed.add_field(name='Events Waiting', value=f'Total: {len(event_tasks)}', inline=False)

        command_waiters = len(self._data_batch)
        is_locked = self._batch_lock.locked()
        description.append(f'Commands Waiting: {command_waiters}, Batch Locked: {is_locked}')

        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        embed.add_field(name='Process', value=f'{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU', inline=False)

        global_rate_limit = not self.bot.http._global_over.is_set()
        description.append(f'Global Rate Limit: {global_rate_limit}')

        if command_waiters >= 8:
            total_warnings += 1
            embed.colour = WARNING

        if global_rate_limit or total_warnings >= 9:
            embed.colour = UNHEALTHY

        embed.set_footer(text=f'{total_warnings} warning(s)')
        embed.description = '\n'.join(description)
        await ctx.send(embed=embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def gateway(self, ctx):
        """Gateway related stats."""

        yesterday = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        identifies = {
            shard_id: sum(1 for dt in dates if dt > yesterday)
            for shard_id, dates in self.bot.identifies.items()
        }
        resumes = {
            shard_id: sum(1 for dt in dates if dt > yesterday)
            for shard_id, dates in self.bot.resumes.items()
        }

        total_identifies = sum(identifies.values())

        builder = [
            f'Total RESUMEs: {sum(resumes.values())}',
            f'Total IDENTIFYs: {total_identifies}'
        ]

        shard_count = len(self.bot.shards)
        if total_identifies > (shard_count * 10):
            issues = 2 + (total_identifies // 10) - shard_count
        else:
            issues = 0

        for shard_id, shard in self.bot.shards.items():
            badge = None
            # Shard WS closed
            # Shard Task failure
            # Shard Task complete (no failure)
            if shard._task.done():
                exc = shard._task.exception()
                if exc is not None:
                    badge = '\N{FIRE}'
                    issues += 1
                else:
                    badge = '\U0001f504'
            elif not shard.ws.open:
                badge = '<:offline:316856575501402112>'
                issues += 1

            if badge is None:
                badge = '<:online:316856575413321728>'

            stats = []
            identify = identifies.get(shard_id, 0)
            resume = resumes.get(shard_id, 0)
            if resume != 0:
                stats.append(f'R: {resume}')
            if identify != 0:
                stats.append(f'ID: {identify}')

            if stats:
                builder.append(f'Shard ID {shard_id}: {badge} ({", ".join(stats)})')
            else:
                builder.append(f'Shard ID {shard_id}: {badge}')

        if issues == 0:
            colour = 0x43B581
        elif issues < len(self.bot.shards) // 4:
            colour = 0xF09E47
        else:
            colour = 0xF04947

        embed = discord.Embed(colour=colour, title='Gateway (last 24 hours)')
        embed.description = '\n'.join(builder)
        embed.set_footer(text=f'{issues} warnings')
        await ctx.send(embed=embed)

    @commands.command(hidden=True, aliases=['cancel_task'])
    @commands.is_owner()
    async def debug_task(self, ctx, memory_id: hex_value):
        """Debug a task by a memory location."""
        task = object_at(memory_id)
        if task is None or not isinstance(task, asyncio.Task):
            return await ctx.send(f'Could not find Task object at {hex(memory_id)}.')

        if ctx.invoked_with == 'cancel_task':
            task.cancel()
            return await ctx.send(f'Cancelled task object {task!r}.')

        paginator = commands.Paginator(prefix='```py')
        fp = io.StringIO()
        frames = len(task.get_stack())
        paginator.add_line(f'# Total Frames: {frames}')
        task.print_stack(file=fp)

        for line in fp.getvalue().splitlines():
            paginator.add_line(line)

        for page in paginator.pages:
            await ctx.send(page)

    async def tabulate_query(self, ctx, query, *args):
        records = await ctx.db.fetch(query, *args)

        if len(records) == 0:
            return await ctx.send('No results found.')

        headers = list(records[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in records)
        render = table.render()

        fmt = f'```\n{render}\n```'
        if len(fmt) > 2000:
            fp = io.BytesIO(fmt.encode('utf-8'))
            await ctx.send('Too many results...', file=discord.File(fp, 'results.txt'))
        else:
            await ctx.send(fmt)

    @commands.group(hidden=True, invoke_without_command=True)
    @commands.is_owner()
    async def command_history(self, ctx):
        """Command history."""
        query = """SELECT
                        CASE failed
                            WHEN TRUE THEN command || ' [!]'
                            ELSE command
                        END AS "command",
                        to_char(used, 'Mon DD HH12:MI:SS AM') AS "invoked",
                        author_id,
                        guild_id
                   FROM commands
                   ORDER BY used DESC
                   LIMIT 15;
                """
        await self.tabulate_query(ctx, query)

    @command_history.command(name='for')
    @commands.is_owner()
    async def command_history_for(self, ctx, days: typing.Optional[int] = 7, *, command: str):
        """Command history for a command."""

        query = """SELECT *, t.success + t.failed AS "total"
                   FROM (
                       SELECT guild_id,
                              SUM(CASE WHEN failed THEN 0 ELSE 1 END) AS "success",
                              SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS "failed"
                       FROM commands
                       WHERE command=$1
                       AND used > (CURRENT_TIMESTAMP - $2::interval)
                       GROUP BY guild_id
                   ) AS t
                   ORDER BY "total" DESC
                   LIMIT 30;
                """

        await self.tabulate_query(ctx, query, command, datetime.timedelta(days=days))

    @command_history.command(name='guild', aliases=['server'])
    @commands.is_owner()
    async def command_history_guild(self, ctx, guild_id: int):
        """Command history for a guild."""

        query = """SELECT
                        CASE failed
                            WHEN TRUE THEN command || ' [!]'
                            ELSE command
                        END AS "command",
                        channel_id,
                        author_id,
                        used
                   FROM commands
                   WHERE guild_id=$1
                   ORDER BY used DESC
                   LIMIT 15;
                """
        await self.tabulate_query(ctx, query, guild_id)

    @command_history.command(name='user', aliases=['member'])
    @commands.is_owner()
    async def command_history_user(self, ctx, user_id: int):
        """Command history for a user."""

        query = """SELECT
                        CASE failed
                            WHEN TRUE THEN command || ' [!]'
                            ELSE command
                        END AS "command",
                        guild_id,
                        used
                   FROM commands
                   WHERE author_id=$1
                   ORDER BY used DESC
                   LIMIT 20;
                """
        await self.tabulate_query(ctx, query, user_id)

    @command_history.command(name='log')
    @commands.is_owner()
    async def command_history_log(self, ctx, days=7):
        """Command history log for the last N days."""

        query = """SELECT command, COUNT(*)
                   FROM commands
                   WHERE used > (CURRENT_TIMESTAMP - $1::interval)
                   GROUP BY command
                   ORDER BY 2 DESC
                """

        all_commands = {
            c.qualified_name: 0
            for c in self.bot.walk_commands()
        }

        records = await ctx.db.fetch(query, datetime.timedelta(days=days))
        for name, uses in records:
            if name in all_commands:
                all_commands[name] = uses

        as_data = sorted(all_commands.items(), key=lambda t: t[1], reverse=True)
        table = formats.TabularData()
        table.set_columns(['Command', 'Uses'])
        table.add_rows(tup for tup in as_data)
        render = table.render()

        embed = discord.Embed(title='Summary', colour=discord.Colour.green())
        embed.set_footer(text='Since').timestamp = datetime.datetime.utcnow() - datetime.timedelta(days=days)

        top_ten = '\n'.join(f'{command}: {uses}' for command, uses in records[:10])
        bottom_ten = '\n'.join(f'{command}: {uses}' for command, uses in records[-10:])
        embed.add_field(name='Top 10', value=top_ten)
        embed.add_field(name='Bottom 10', value=bottom_ten)

        unused = ', '.join(name for name, uses in as_data if uses == 0)
        if len(unused) > 1024:
            unused = 'Way too many...'

        embed.add_field(name='Unused', value=unused, inline=False)

        await ctx.send(embed=embed, file=discord.File(io.BytesIO(render.encode()), filename='full_results.txt'))

    @command_history.command(name='cog')
    @commands.is_owner()
    async def command_history_cog(self, ctx, days: typing.Optional[int] = 7, *, cog: str = None):
        """Command history for a cog or grouped by a cog."""

        interval = datetime.timedelta(days=days)
        if cog is not None:
            cog = self.bot.get_cog(cog)
            if cog is None:
                return await ctx.send(f'Unknown cog: {cog}')

            query = """SELECT *, t.success + t.failed AS "total"
                       FROM (
                           SELECT command,
                                  SUM(CASE WHEN failed THEN 0 ELSE 1 END) AS "success",
                                  SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS "failed"
                           FROM commands
                           WHERE command = any($1::text[])
                           AND used > (CURRENT_TIMESTAMP - $2::interval)
                           GROUP BY command
                       ) AS t
                       ORDER BY "total" DESC
                       LIMIT 30;
                    """
            return await self.tabulate_query(ctx, query, [c.qualified_name for c in cog.walk_commands()], interval)

        # A more manual query with a manual grouper.
        query = """SELECT *, t.success + t.failed AS "total"
                   FROM (
                       SELECT command,
                              SUM(CASE WHEN failed THEN 0 ELSE 1 END) AS "success",
                              SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS "failed"
                       FROM commands
                       WHERE used > (CURRENT_TIMESTAMP - $1::interval)
                       GROUP BY command
                   ) AS t;
                """

        class Count:
            __slots__ = ('success', 'failed', 'total')
            def __init__(self):
                self.success = 0
                self.failed = 0
                self.total = 0

            def add(self, record):
                self.success += record['success']
                self.failed += record['failed']
                self.total += record['total']

        data = defaultdict(Count)
        records = await ctx.db.fetch(query, interval)
        for record in records:
            command = self.bot.get_command(record['command'])
            if command is None or command.cog is None:
                data['No Cog'].add(record)
            else:
                data[command.cog.qualified_name].add(record)

        table = formats.TabularData()
        table.set_columns(['Cog', 'Success', 'Failed', 'Total'])
        data = sorted([
            (cog, e.success, e.failed, e.total)
            for cog, e in data.items()
        ], key=lambda t: t[-1], reverse=True)

        table.add_rows(data)
        render = table.render()
        await ctx.safe_send(f'```\n{render}\n```')

old_on_error = commands.AutoShardedBot.on_error

async def on_error(self, event, *args, **kwargs):
    e = discord.Embed(title='Event Error', colour=0xa32952)
    e.add_field(name='Event', value=event)
    e.description = f'```py\n{traceback.format_exc()}\n```'
    e.timestamp = datetime.datetime.utcnow()

    args_str = ['```py']
    for index, arg in enumerate(args):
        args_str.append(f'[{index}]: {arg!r}')
    args_str.append('```')
    e.add_field(name='Args', value='\n'.join(args_str), inline=False)
    hook = self.get_cog('Stats').webhook
    try:
        await hook.send(embed=e)
    except:
        pass

def setup(bot):
    if not hasattr(bot, 'command_stats'):
        bot.command_stats = Counter()

    if not hasattr(bot, 'socket_stats'):
        bot.socket_stats = Counter()

    cog = Stats(bot)
    bot.add_cog(cog)
    bot._stats_cog_gateway_handler = handler = GatewayHandler(cog)
    logging.getLogger().addHandler(handler)
    commands.AutoShardedBot.on_error = on_error

def teardown(bot):
    commands.AutoShardedBot.on_error = old_on_error
    logging.getLogger().removeHandler(bot._stats_cog_gateway_handler)
    del bot._stats_cog_gateway_handler
