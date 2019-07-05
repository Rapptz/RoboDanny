from discord.ext import commands, tasks
from .utils import db, checks

from collections import Counter, defaultdict

import discord
import asyncio
import asyncpg
import datetime
import logging
import yarl
import re
import io

log = logging.getLogger(__name__)

BLOB_GUILD_ID = 272885620769161216
EMOJI_REGEX = re.compile(r'<a?:.+?:([0-9]{15,21})>')
EMOJI_NAME_REGEX = re.compile(r'[0-9a-zA-Z\_]{2,32}')

class BlobEmoji(commands.Converter):
    async def convert(self, ctx, argument):
        guild = ctx.bot.get_guild(BLOB_GUILD_ID)
        emojis = {e.id: e for e in guild.emojis}

        m = EMOJI_REGEX.match(argument)
        if m is not None:
            emoji = emojis.get(int(m.group(1)))
        elif argument.isdigit():
            emoji = emojis.get(int(argument))
        else:
            emoji = discord.utils.find(lambda e: e.name == argument, emojis.values())

        if emoji is None:
            raise commands.BadArgument('Not a valid blob emoji.')
        return emoji

def partial_emoji(argument, *, regex=EMOJI_REGEX):
    if argument.isdigit():
        # assume it's an emoji ID
        return int(argument)

    m = regex.match(argument)
    if m is None:
        raise commands.BadArgument("That's not a custom emoji...")
    return int(m.group(1))

def emoji_name(argument, *, regex=EMOJI_NAME_REGEX):
    m = regex.match(argument)
    if m is None:
        raise commands.BadArgument('Invalid emoji name.')
    return argument

class EmojiURL:
    def __init__(self, *, animated, url):
        self.url = url
        self.animated = animated

    @classmethod
    async def convert(cls, ctx, argument):
        try:
            partial = await commands.PartialEmojiConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                url = yarl.URL(argument)
                if url.scheme not in ('http', 'https'):
                    raise RuntimeError
                path = url.path.lower()
                if not path.endswith(('.png', '.jpeg', '.jpg', '.gif')):
                    raise RuntimeError
                return cls(animated=url.path.endswith('.gif'), url=url)
            except Exception:
                raise commands.BadArgument('Not a valid or supported emoji URL.') from None
        else:
            return cls(animated=partial.animated, url=str(partial.url))

def usage_per_day(dt, usages):
    tracking_started = datetime.datetime(2017, 3, 31)
    now = datetime.datetime.utcnow()
    if dt < tracking_started:
        base = tracking_started
    else:
        base = dt

    days = (now - base).total_seconds() / 86400 # 86400 seconds in a day
    if int(days) == 0:
        return usages
    return usages / days

class EmojiStats(db.Table, table_name='emoji_stats'):
    id = db.Column(db.Integer(big=True, auto_increment=True), primary_key=True)

    guild_id = db.Column(db.Integer(big=True), index=True)
    emoji_id = db.Column(db.Integer(big=True), index=True)
    total = db.Column(db.Integer, default=0)

    @classmethod
    def create_table(cls, *, exists_ok=True):
        statement = super().create_table(exists_ok=exists_ok)

        # create the indexes
        sql = "CREATE UNIQUE INDEX IF NOT EXISTS emoji_stats_uniq_idx ON emoji_stats (guild_id, emoji_id);"
        return statement + '\n' + sql

class Emoji(commands.Cog):
    """Custom emoji tracking"""

    def __init__(self, bot):
        self.bot = bot
        self._batch_of_data = defaultdict(Counter)
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self.bulk_insert.add_exception_type(asyncpg.PostgresConnectionError)
        self.bulk_insert.start()

    def cog_unload(self):
        self.bulk_insert.stop()

    async def cog_command_error(self, ctx, error):
       if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    @tasks.loop(seconds=60.0)
    async def bulk_insert(self):
        query = """INSERT INTO emoji_stats (guild_id, emoji_id, total)
                   SELECT x.guild, x.emoji, x.added
                   FROM jsonb_to_recordset($1::jsonb) AS x(guild BIGINT, emoji BIGINT, added INT)
                   ON CONFLICT (guild_id, emoji_id) DO UPDATE
                   SET total = emoji_stats.total + excluded.total;
                """

        async with self._batch_lock:
            transformed = [
                {'guild': guild_id, 'emoji': emoji_id, 'added': count}
                for guild_id, data in self._batch_of_data.items()
                for emoji_id, count in data.items()
            ]
            self._batch_of_data.clear()
            await self.bot.pool.execute(query, transformed)

    async def do_redirect(self, message):
        if len(message.attachments) == 0:
            return

        data = io.BytesIO()
        await message.attachments[0].save(data)
        data.seek(0)

        ch = self.bot.get_channel(305838206119575552)
        if ch is not None:
            fmt = f'Suggestion from {message.author}: {message.clean_content}'
            await ch.send(fmt, file=discord.File(data, message.attachments[0].filename))

    def find_all_emoji(self, message, *, regex=EMOJI_REGEX):
        return regex.findall(message.content)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None:
            return

        if message.author.bot:
            return # no bots.

        # handle the redirection from #suggestions
        if message.channel.id == 295012914564169728:
            return await self.do_redirect(message)

        matches = EMOJI_REGEX.findall(message.content)
        if not matches:
            return

        async with self._batch_lock:
            self._batch_of_data[message.guild.id].update(map(int, matches))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        # we only care when an emoji is added
        lookup = { e.id for e in before }
        added = [e for e in after if e.id not in lookup and len(e.roles) == 0]
        if len(added) == 0:
            return

        log.info('Server %s has added %s emojis.', guild, len(added))
        if guild.id != BLOB_GUILD_ID:
            return # not the guild we care about

        # this is the backup channel
        channel = self.bot.get_channel(305841865293430795)
        if channel is None:
            return

        for emoji in added:
            async with self.bot.session.get(emoji.url) as resp:
                if resp.status != 200:
                    continue

                data = io.BytesIO(await resp.read())
                await channel.send(emoji.name, file=discord.File(data, f'{emoji.name}.png'))
                await asyncio.sleep(1)

    async def get_all_blob_stats(self, ctx):
        blob_guild = self.bot.get_guild(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis if len(e.roles) == 0 }

        query = "SELECT COALESCE(SUM(total), 0) FROM emoji_stats;"
        total_usage = await ctx.db.fetchrow(query)

        query = """SELECT emoji_id, COALESCE(SUM(total), 0) AS "Count"
                   FROM emoji_stats
                   WHERE emoji_id = ANY($1::bigint[])
                   GROUP BY emoji_id
                   ORDER BY "Count" DESC;
                """

        blob_usage = await ctx.db.fetch(query, list(blob_ids.keys()))

        e = discord.Embed(title='Blob Statistics', colour=0xf1c40f)

        total_count = sum(r['Count'] for r in blob_usage)
        global_usage = total_usage[0]
        e.add_field(name='Total Usage', value=f'{total_count} ({total_count / global_usage:.2%})')

        def elem_to_string(key, count):
            elem = blob_ids.get(key)
            per_day = usage_per_day(elem.created_at, count)
            return f'{elem}: {count} times, {per_day:.2f}/day ({count / total_count:.2%})'

        top = [elem_to_string(key, count) for key, count in blob_usage[0:7]]
        bottom = [elem_to_string(key, count) for key, count in blob_usage[-7:]]
        e.add_field(name='Most Common', value='\n'.join(top), inline=False)
        e.add_field(name='Least Common', value='\n'.join(bottom), inline=False)
        await ctx.send(embed=e)

    async def get_stats_for(self, ctx, emoji):
        e = discord.Embed(colour=0xf1c40f, title='Statistics')

        query = """SELECT COALESCE(SUM(total), 0) AS "Count"
                   FROM emoji_stats
                   WHERE emoji_id=$1
                   GROUP BY emoji_id;
                """

        usage = await ctx.db.fetchrow(query, emoji.id)
        usage = usage[0]

        e.add_field(name='Emoji', value=emoji)
        e.add_field(name='Usage', value=f'{usage}, {usage_per_day(emoji.created_at, usage):.2f}/day')
        await ctx.send(embed=e)

    @commands.group(hidden=True, invoke_without_command=True)
    async def blobstats(self, ctx, *, emoji: BlobEmoji = None):
        """Usage statistics of blobs."""
        if emoji is None:
            await self.get_all_blob_stats(ctx)
        else:
            await self.get_stats_for(ctx, emoji)

    @commands.command(aliases=['blobpost'], hidden=True)
    @checks.is_in_guilds(BLOB_GUILD_ID)
    @checks.is_admin()
    async def blobsort(self, ctx):
        """Sorts the blob post."""
        emojis = sorted([e.name for e in ctx.guild.emojis if len(e.roles) == 0])
        fp = io.BytesIO()
        pages = [emojis[i:i + 30] for i in range(0, len(emojis), 30)]

        for number, page in enumerate(pages, 1):
            fmt = f'Page {number}\n'
            fp.write(fmt.encode('utf-8'))
            for emoji in page:
                fmt = f':{emoji}: = `:{emoji}:`\n'
                fp.write(fmt.encode('utf-8'))

            fp.write(b'\n')

        fp.seek(0)
        await ctx.send(file=discord.File(fp, 'blob_posts.txt'))

    def emoji_fmt(self, emoji_id, count, total):
        emoji = self.bot.get_emoji(emoji_id)
        if emoji is None:
            name = f'[\N{WHITE QUESTION MARK ORNAMENT}](https://cdn.discordapp.com/emojis/{emoji_id}.png)'
            emoji = discord.Object(id=emoji_id)
        else:
            name = str(emoji)

        per_day = usage_per_day(emoji.created_at, count)
        p = count / total
        return f'{name}: {count} uses ({p:.1%}), {per_day:.1f} uses/day.'

    async def get_guild_stats(self, ctx):
        e = discord.Embed(title='Emoji Leaderboard', colour=discord.Colour.blurple())

        query = """SELECT
                       COALESCE(SUM(total), 0) AS "Count",
                       COUNT(*) AS "Emoji"
                   FROM emoji_stats
                   WHERE guild_id=$1
                   GROUP BY guild_id;
                """
        record = await ctx.db.fetchrow(query, ctx.guild.id)
        if record is None:
            return await ctx.send('This server has no emoji stats...')

        total = record['Count']
        emoji_used = record['Emoji']
        per_day = usage_per_day(ctx.me.joined_at, total)
        e.set_footer(text=f'{total} uses over {emoji_used} emoji for {per_day:.2f} uses per day.')

        query = """SELECT emoji_id, total
                   FROM emoji_stats
                   WHERE guild_id=$1
                   ORDER BY total DESC
                   LIMIT 10;
                """

        top = await ctx.db.fetch(query, ctx.guild.id)

        e.description = '\n'.join(f'{i}. {self.emoji_fmt(emoji, count, total)}' for i, (emoji, count) in enumerate(top, 1))
        await ctx.send(embed=e)

    async def get_emoji_stats(self, ctx, emoji_id):
        e = discord.Embed(title='Emoji Stats')
        cdn = f'https://cdn.discordapp.com/emojis/{emoji_id}.png'

        # first verify it's a real ID
        async with ctx.session.get(cdn) as resp:
            if resp.status == 404:
                e.description = "This isn't a valid emoji."
                e.set_thumbnail(url='https://this.is-serious.business/09e106.jpg')
                return await ctx.send(embed=e)

        e.set_thumbnail(url=cdn)

        # valid emoji ID so let's use it
        query = """SELECT guild_id, SUM(total) AS "Count"
                   FROM emoji_stats
                   WHERE emoji_id=$1
                   GROUP BY guild_id;
                """

        records = await ctx.db.fetch(query, emoji_id)
        transformed = {k: v for k, v in records}
        total = sum(transformed.values())

        dt = discord.utils.snowflake_time(emoji_id)

        # get the stats for this guild in particular
        try:
            count = transformed[ctx.guild.id]
            per_day = usage_per_day(dt, count)
            value = f'{count} uses ({count / total:.2%} of global uses), {per_day:.2f} uses/day'
        except KeyError:
            value = 'Not used here.'

        e.add_field(name='Server Stats', value=value, inline=False)

        # global stats
        per_day = usage_per_day(dt, total)
        value = f'{total} uses, {per_day:.2f} uses/day'
        e.add_field(name='Global Stats', value=value, inline=False)
        e.set_footer(text='These statistics are for servers I am in')
        await ctx.send(embed=e)

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def emojistats(self, ctx, *, emoji: partial_emoji = None):
        """Shows you statistics about the emoji usage in this server.

        If no emoji is given, then it gives you the top 10 emoji used.
        """

        if emoji is None:
            await self.get_guild_stats(ctx)
        else:
            await self.get_emoji_stats(ctx, emoji)

    @emojistats.command(name='server', aliases=['guild'])
    @commands.guild_only()
    async def emojistats_guild(self, ctx):
        """Shows you statistics about the local server emojis in this server."""
        emoji_ids = [e.id for e in ctx.guild.emojis]

        if not emoji_ids:
            await ctx.send('This guild has no custom emoji.')

        query = """SELECT emoji_id, total
                   FROM emoji_stats
                   WHERE guild_id=$1 AND emoji_id = ANY($2::bigint[])
                   ORDER BY total DESC
                """

        e = discord.Embed(title='Emoji Leaderboard', colour=discord.Colour.blurple())
        records = await ctx.db.fetch(query, ctx.guild.id, emoji_ids)

        total = sum(a for _, a in records)
        emoji_used = len(records)
        per_day = usage_per_day(ctx.me.joined_at, total)
        e.set_footer(text=f'{total} uses over {emoji_used} emoji for {per_day:.2f} uses per day.')
        top = records[:10]
        value = '\n'.join(self.emoji_fmt(emoji, count, total) for (emoji, count) in top)
        e.add_field(name=f'Top {len(top)}', value=value or 'Nothing...')

        record_count = len(records)
        if record_count > 10:
            bottom = records[-10:] if record_count >= 20 else records[-record_count + 10:]
            value = '\n'.join(self.emoji_fmt(emoji, count, total) for (emoji, count) in bottom)
            e.add_field(name=f'Bottom {len(bottom)}', value=value)

        await ctx.send(embed=e)

    @commands.group(name='emoji')
    @commands.guild_only()
    @checks.has_guild_permissions(manage_emoji=True)
    async def _emoji(self, ctx):
        """Emoji management commands."""
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)

    @_emoji.command(name='create')
    async def _emoji_create(self, ctx, name: emoji_name, *, emoji: EmojiURL):
        """Create an emoji for the server under the given name.

        You must have Manage Emoji permission to use this.
        The bot must have this permission too.
        """
        if not ctx.me.guild_permissions.manage_emojis:
            return await ctx.send('Bot does not have permission to add emoji.')

        reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        emoji_count = sum(e.animated == emoji.animated for e in ctx.guild.emojis)
        if emoji_count >= ctx.guild.emoji_limit:
            return await ctx.send('There are no more emoji slots in this server.')

        async with self.bot.session.get(emoji.url) as resp:
            if resp.status >= 400:
                return await ctx.send('Could not fetch the image.')
            if int(resp.headers['Content-Length']) >= (256 * 1024):
                return await ctx.send('Image is too big.')
            data = await resp.read()
            coro = ctx.guild.create_custom_emoji(name=name, image=data, reason=reason)
            async with ctx.typing():
                try:
                    created = await asyncio.wait_for(coro, timeout=10.0)
                except asyncio.TimeoutError:
                    return await ctx.send('Sorry, the bot is rate limited or it took too long.')
                except discord.HTTPException as e:
                    return await ctx.send(f'Failed to create emoji somehow: {e}')
                else:
                    return await ctx.send(f'Created {created}')

def setup(bot):
    bot.add_cog(Emoji(bot))
