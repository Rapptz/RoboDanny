from discord.ext import commands
from .utils import checks, formats
import discord
from collections import OrderedDict, deque, Counter
import os, datetime
import re, asyncio

class TimeParser:
    def __init__(self, argument):
        compiled = re.compile(r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?")
        self.original = argument
        try:
            self.seconds = int(argument)
        except ValueError as e:
            match = compiled.match(argument)
            if match is None or not match.group(0):
                raise commands.BadArgument('Failed to parse time.') from e

            self.seconds = 0
            hours = match.group('hours')
            if hours is not None:
                self.seconds += int(hours) * 3600
            minutes = match.group('minutes')
            if minutes is not None:
                self.seconds += int(minutes) * 60
            seconds = match.group('seconds')
            if seconds is not None:
                self.seconds += int(seconds)

class Meta:
    """Commands for utilities related to Discord or the Bot itself."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def hello(self):
        """Displays my intro message."""
        await self.bot.say('Hello! I\'m a robot! I am currently **version 3.0.0**. Danny made me.')


    @commands.command(pass_context=True)
    async def timer(self, ctx, time : TimeParser, *, message=''):
        """Reminds you of something after a certain amount of time.

        The time can optionally be specified with units such as 'h'
        for hours, 'm' for minutes and 's' for seconds. If no unit
        is given then it is assumed to be seconds. You can also combine
        multiple units together, e.g. 2h4m10s.
        """

        author = ctx.message.author
        reminder = None
        completed = None

        if not message:
            reminder = 'Okay {0.mention}, I\'ll remind you in {1.seconds} seconds.'
            completed = 'Time is up {0.mention}! You asked to be reminded about something.'
        else:
            reminder = 'Okay {0.mention}, I\'ll remind you about "{2}" in {1.seconds} seconds.'
            completed = 'Time is up {0.mention}! You asked to be reminded about "{1}".'

        await self.bot.say(reminder.format(author, time, message))
        await asyncio.sleep(time.seconds)
        await self.bot.say(completed.format(author, message))

    @commands.command(name='quit')
    @checks.is_owner()
    async def _quit(self):
        """Quits the bot."""
        await self.bot.logout()

    @commands.command(pass_context=True)
    async def info(self, ctx, *, member : discord.Member = None):
        """Shows info about a member.

        This cannot be used in private messages. If you don't specify
        a member then the info returned will be yours.
        """
        channel = ctx.message.channel
        if channel.is_private:
            await self.bot.say('You cannot use this in PMs.')
            return

        if member is None:
            member = ctx.message.author

        roles = [role.name.replace('@', '@\u200b') for role in member.roles]
        shared = sum(1 for m in self.bot.get_all_members() if m.id == member.id)
        voice = member.voice_channel
        if voice is not None:
            voice = '{} with {} people'.format(voice, len(voice.voice_members))
        else:
            voice = 'Not connected.'

        entries = [
            ('Name', member.name),
            ('User ID', member.id),
            ('Joined', member.joined_at),
            ('Roles', ', '.join(roles)),
            ('Servers', '{} shared'.format(shared)),
            ('Channel', channel.name),
            ('Voice Channel', voice),
            ('Channel ID', channel.id),
            ('Avatar', member.avatar_url),
        ]

        await formats.entry_to_code(self.bot, entries)

    async def say_permissions(self, member, channel):
        permissions = channel.permissions_for(member)
        entries = []
        for attr in dir(permissions):
            is_property = isinstance(getattr(type(permissions), attr), property)
            if is_property:
                entries.append((attr.replace('_', ' ').title(), getattr(permissions, attr)))

        await formats.entry_to_code(self.bot, entries)

    @commands.command(pass_context=True, no_pm=True)
    async def permissions(self, ctx, *, member : discord.Member = None):
        """Shows a member's permissions.

        You cannot use this in private messages. If no member is given then
        the info returned will be yours.
        """
        channel = ctx.message.channel
        if member is None:
            member = ctx.message.author

        await self.say_permissions(member, channel)

    @commands.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_roles=True)
    async def botpermissions(self, ctx):
        """Shows the bot's permissions.

        This is a good way of checking if the bot has the permissions needed
        to execute the commands it wants to execute.

        To execute this command you must have Manage Roles permissions or
        have the Bot Admin role. You cannot use this in private messages.
        """
        channel = ctx.message.channel
        member = ctx.message.server.me
        await self.say_permissions(member, channel)

    def get_bot_uptime(self):
        now = datetime.datetime.utcnow()
        delta = now - self.bot.uptime
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        if days:
            fmt = '{d} days, {h} hours, {m} minutes, and {s} seconds'
        else:
            fmt = '{h} hours, {m} minutes, and {s} seconds'

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @commands.command()
    async def join(self, invite : discord.Invite):
        """Joins a server via invite."""
        await self.bot.accept_invite(invite)

    @commands.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def leave(self, ctx):
        """Leaves the server.

        To use this command you must have Manage Server permissions or have
        the Bot Admin role.
        """
        server = ctx.message.server
        try:
            await self.bot.leave_server(server)
        except:
            await self.bot.say('Could not leave..')


    @commands.command()
    async def uptime(self):
        """Tells you how long the bot has been up for."""
        await self.bot.say('Uptime: **{}**'.format(self.get_bot_uptime()))

    def format_message(self, message):
        return 'On {0.timestamp}, {0.author} said {0.content}'.format(message)

    @commands.command(pass_context=True)
    async def mentions(self, ctx, channel : discord.Channel = None, context : int = 3):
        """Tells you when you were mentioned in a channel.

        If a channel is not given, then it tells you when you were mentioned in a
        the current channel. The context is an integer that tells you how many messages
        before should be shown. The context cannot be greater than 5 or lower than 0.
        """
        if channel is None:
            channel = ctx.message.channel

        context = min(5, max(0, context))

        author = ctx.message.author
        previous = deque(maxlen=context)
        async for message in self.bot.logs_from(channel, limit=1000):
            previous.append(message)
            if author in message.mentions or message.mention_everyone:
                # we're mentioned so..
                try:
                    await self.bot.whisper('\n'.join(map(self.format_message, previous)))
                except discord.HTTPException:
                    await self.bot.whisper('An error happened while fetching mentions.')

    @commands.command()
    async def about(self):
        """Tells you information about the bot itself."""
        revision = os.popen(r'git show -s HEAD --format="%s (%cr)"').read().strip()
        result = ['**About Me:**']
        result.append('- Author: Danny (Discord ID: 80088516616269824)')
        result.append('- Library: discord.py (Python)')
        result.append('- Latest Change: {}'.format(revision))
        result.append('- Uptime: {}'.format(self.get_bot_uptime()))
        result.append('- Servers: {}'.format(len(self.bot.servers)))
        result.append('- Commands Run: {}'.format(self.bot.commands_executed))
        # statistics
        total_members = sum(len(s.members) for s in self.bot.servers)
        total_online  = sum(1 for m in self.bot.get_all_members() if m.status != discord.Status.offline)
        unique_members = set(self.bot.get_all_members())
        unique_online = sum(1 for m in unique_members if m.status != discord.Status.offline)
        channel_types = Counter(c.type for c in self.bot.get_all_channels())
        voice = channel_types[discord.ChannelType.voice]
        text = channel_types[discord.ChannelType.text]
        result.append('- Total Members: {} ({} online)'.format(total_members, total_online))
        result.append('- Unique Members: {} ({} online)'.format(len(unique_members), unique_online))
        result.append('- {} text channels, {} voice channels'.format(text, voice))
        await self.bot.say('\n'.join(result))

    @commands.command(rest_is_raw=True, hidden=True)
    @checks.is_owner()
    async def echo(self, *, content):
        await self.bot.say(content)

def setup(bot):
    bot.add_cog(Meta(bot))
