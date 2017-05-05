from discord.ext import commands
from collections import Counter

from .utils import checks

import logging
import discord
import datetime
import traceback
import psutil
import os

log = logging.getLogger()

LOGGING_CHANNEL = '309632009427222529'

class Stats:
    """Bot usage statistics."""

    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()

    async def on_command(self, command, ctx):
        self.bot.commands_used[ctx.command.qualified_name] += 1
        message = ctx.message
        destination = None
        if message.channel.is_private:
            destination = 'Private Message'
        else:
            destination = '#{0.channel.name} ({0.server.name})'.format(message)

        log.info('{0.timestamp}: {0.author.name} in {1}: {0.content}'.format(message, destination))

    async def on_socket_response(self, msg):
        self.bot.socket_stats[msg.get('t')] += 1

    @commands.command(hidden=True)
    @checks.is_owner()
    async def commandstats(self):
        p = commands.Paginator()
        counter = self.bot.commands_used
        width = len(max(counter, key=len))
        total = sum(counter.values())

        fmt = '{0:<{width}}: {1}'
        p.add_line(fmt.format('Total', total, width=width))
        for key, count in counter.most_common():
            p.add_line(fmt.format(key, count, width=width))

        for page in p.pages:
            await self.bot.say(page)

    @commands.command(hidden=True)
    async def socketstats(self):
        delta = datetime.datetime.utcnow() - self.bot.uptime
        minutes = delta.total_seconds() / 60
        total = sum(self.bot.socket_stats.values())
        cpm = total / minutes

        fmt = '%s socket events observed (%.2f/minute):\n%s'
        await self.bot.say(fmt % (total, cpm, self.bot.socket_stats))

    def get_bot_uptime(self, *, brief=False):
        now = datetime.datetime.utcnow()
        delta = now - self.bot.uptime
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)

        if not brief:
            if days:
                fmt = '{d} days, {h} hours, {m} minutes, and {s} seconds'
            else:
                fmt = '{h} hours, {m} minutes, and {s} seconds'
        else:
            fmt = '{h}h {m}m {s}s'
            if days:
                fmt = '{d}d ' + fmt

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @commands.command()
    async def uptime(self):
        """Tells you how long the bot has been up for."""
        await self.bot.say('Uptime: **{}**'.format(self.get_bot_uptime()))

    @commands.command(aliases=['stats'])
    async def about(self):
        """Tells you information about the bot itself."""
        cmd = r'git show -s HEAD~3..HEAD --format="[{}](https://github.com/Rapptz/RoboDanny/commit/%H) %s (%cr)"'
        if os.name == 'posix':
            cmd = cmd.format(r'\`%h\`')
        else:
            cmd = cmd.format(r'`%h`')

        revision = os.popen(cmd).read().strip()
        embed = discord.Embed(description='Latest Changes:\n' + revision)
        embed.title = 'Official Bot Server Invite'
        embed.url = 'https://discord.gg/0118rJdtd1rVJJfuI'
        embed.colour = 0x738bd7 # blurple

        try:
            owner = self._owner
        except AttributeError:
            owner = self._owner = await self.bot.get_user_info('80088516616269824')

        embed.set_author(name=str(owner), icon_url=owner.avatar_url)

        # statistics
        total_members = sum(len(s.members) for s in self.bot.servers)
        total_online  = sum(1 for m in self.bot.get_all_members() if m.status != discord.Status.offline)
        unique_members = set(self.bot.get_all_members())
        unique_online = sum(1 for m in unique_members if m.status != discord.Status.offline)
        channel_types = Counter(c.type for c in self.bot.get_all_channels())
        voice = channel_types[discord.ChannelType.voice]
        text = channel_types[discord.ChannelType.text]

        members = '%s total\n%s online\n%s unique\n%s unique online' % (total_members, total_online, len(unique_members), unique_online)
        embed.add_field(name='Members', value=members)
        embed.add_field(name='Channels', value='{} total\n{} text\n{} voice'.format(text + voice, text, voice))
        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        embed.add_field(name='Process', value='{:.2f} MiB\n{:.2f}% CPU'.format(memory_usage, cpu_usage))
        embed.set_footer(text='Made with discord.py', icon_url='http://i.imgur.com/5BFecvA.png')
        embed.timestamp = self.bot.uptime

        embed.add_field(name='Servers', value=len(self.bot.servers))
        embed.add_field(name='Commands Run', value=sum(self.bot.commands_used.values()))
        embed.add_field(name='Uptime', value=self.get_bot_uptime(brief=True))

        await self.bot.say(embed=embed)

    async def send_server_stat(self, e, server):
        e.add_field(name='Name', value=server.name)
        e.add_field(name='ID', value=server.id)
        e.add_field(name='Owner', value='{0} (ID: {0.id})'.format(server.owner))

        bots = sum(m.bot for m in server.members)
        total = server.member_count
        online = sum(m.status is discord.Status.online for m in server.members)
        e.add_field(name='Members', value=str(total))
        e.add_field(name='Bots', value='{} ({:.2%})'.format(bots, bots / total))
        e.add_field(name='Online', value='{} ({:.2%})'.format(online, online / total))
        if server.icon:
            e.set_thumbnail(url=server.icon_url)

        if server.me:
            e.timestamp = server.me.joined_at

        ch = self.bot.get_channel(LOGGING_CHANNEL)
        await self.bot.send_message(ch, embed=e)

    async def on_server_join(self, server):
        e = discord.Embed(colour=0x53dda4, title='New Server') # green colour
        await self.send_server_stat(e, server)

    async def on_server_remove(self, server):
        e = discord.Embed(colour=0xdd5f53, title='Left Server') # red colour
        await self.send_server_stat(e, server)

    async def on_command_error(self, error, ctx):
        ignored = (commands.NoPrivateMessage, commands.DisabledCommand, commands.CheckFailure,
                   commands.CommandNotFound, commands.UserInputError, discord.HTTPException)
        error = getattr(error, 'original', error)

        if isinstance(error, ignored):
            return

        e = discord.Embed(title='Command Error', colour=0xcc3366)
        e.add_field(name='Name', value=ctx.command.qualified_name)
        e.add_field(name='Author', value='{0} (ID: {0.id})'.format(ctx.message.author))

        if ctx.message.server:
            fmt = 'Channel: {0} (ID: {0.id})\nGuild: {1} (ID: {1.id})'
        else:
            fmt = 'Channel: {0} (ID: {0.id})'

        e.add_field(name='Location', value=fmt.format(ctx.message.channel, ctx.message.server), inline=False)

        exc = traceback.format_exception(type(error), error, error.__traceback__, chain=False)
        e.description = '```py\n%s\n```' % ''.join(exc)
        e.timestamp = datetime.datetime.utcnow()
        ch = self.bot.get_channel(LOGGING_CHANNEL)
        await self.bot.send_message(ch, embed=e)

old_on_error = commands.Bot.on_error

async def on_error(self, event, *args, **kwargs):
    e = discord.Embed(title='Event Error', colour=0xa32952)
    e.add_field(name='Event', value=event)
    e.description = '```py\n%s\n```' % traceback.format_exc()
    e.timestamp = datetime.datetime.utcnow()
    ch = self.get_channel(LOGGING_CHANNEL)
    try:
        await self.send_message(ch, embed=e)
    except:
        pass

def setup(bot):
    if not hasattr(bot, 'commands_used'):
        bot.commands_used = Counter()

    if not hasattr(bot, 'socket_stats'):
        bot.socket_stats = Counter()

    bot.add_cog(Stats(bot))
    commands.Bot.on_error = on_error

def teardown(bot):
    commands.Bot.on_error = old_on_error
