from discord.ext import commands
from .utils import checks, formats
import discord
from collections import OrderedDict, deque, Counter
import os, datetime
import re, asyncio
import copy
import unicodedata

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

        if self.seconds < 0:
            raise commands.BadArgument('I don\'t do negative time.')

        if self.seconds > 604800: # 7 days
            raise commands.BadArgument('That\'s a bit too far in the future for me.')

class Meta:
    """Commands for utilities related to Discord or the Bot itself."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(hidden=True)
    async def hello(self):
        """Displays my intro message."""
        await self.bot.say('Hello! I\'m a robot! Danny#0007 made me.')

    @commands.command()
    async def charinfo(self, *, characters: str):
        """Shows you information about a number of characters.

        Only up to 15 characters at a time.
        """

        if len(characters) > 15:
            await self.bot.say('Too many characters ({}/15)'.format(len(characters)))
            return

        def to_string(c):
            digit = format(ord(c), 'x')
            name = unicodedata.name(c, 'Name not found.')
            return '`0x{0}`: {1} - {2} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{0}>'.format(digit, name, c)

        await self.bot.say('\n'.join(map(to_string, characters)))

    @commands.command()
    async def source(self, command : str = None):
        """Displays my full source code or for a specific command.

        To display the source code of a subcommand you have to separate it by
        periods, e.g. tag.create for the create subcommand of the tag command.
        """
        source_url = 'https://github.com/Rapptz/RoboDanny'
        if command is None:
            await self.bot.say(source_url)
            return

        code_path = command.split('.')
        obj = self.bot
        for cmd in code_path:
            try:
                obj = obj.get_command(cmd)
                if obj is None:
                    await self.bot.say('Could not find the command ' + cmd)
                    return
            except AttributeError:
                await self.bot.say('{0.name} command has no subcommands'.format(obj))
                return

        # since we found the command we're looking for, presumably anyway, let's
        # try to access the code itself
        src = obj.callback.__code__

        if not obj.callback.__module__.startswith('discord'):
            # not a built-in command
            location = os.path.relpath(src.co_filename).replace('\\', '/')
            final_url = '<{}/blob/master/{}#L{}>'.format(source_url, location, src.co_firstlineno)
        else:
            location = obj.callback.__module__.replace('.', '/') + '.py'
            base = 'https://github.com/Rapptz/discord.py'
            final_url = '<{}/blob/master/{}#L{}>'.format(base, location, src.co_firstlineno)

        await self.bot.say(final_url)

    @commands.command(pass_context=True, aliases=['reminder'])
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
        message = message.replace('@everyone', '@\u200beveryone')

        if not message:
            reminder = 'Okay {0.mention}, I\'ll remind you in {1.seconds} seconds.'
            completed = 'Time is up {0.mention}! You asked to be reminded about something.'
        else:
            reminder = 'Okay {0.mention}, I\'ll remind you about "{2}" in {1.seconds} seconds.'
            completed = 'Time is up {0.mention}! You asked to be reminded about "{1}".'

        await self.bot.say(reminder.format(author, time, message))
        await asyncio.sleep(time.seconds)
        await self.bot.say(completed.format(author, message))

    @timer.error
    async def timer_error(self, error, ctx):
        if type(error) is commands.BadArgument:
            await self.bot.say(str(error))

    @commands.command(name='quit', hidden=True)
    @checks.is_owner()
    async def _quit(self):
        """Quits the bot."""
        await self.bot.logout()

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def info(self, ctx, *, member : discord.Member = None):
        """Shows info about a member.

        This cannot be used in private messages. If you don't specify
        a member then the info returned will be yours.
        """
        channel = ctx.message.channel
        if member is None:
            member = ctx.message.author

        roles = [role.name.replace('@', '@\u200b') for role in member.roles]
        shared = sum(1 for m in self.bot.get_all_members() if m.id == member.id)
        voice = member.voice_channel
        if voice is not None:
            other_people = len(voice.voice_members) - 1
            voice_fmt = '{} with {} others' if other_people else '{} by themselves'
            voice = voice_fmt.format(voice.name, other_people)
        else:
            voice = 'Not connected.'

        entries = [
            ('Name', member.name),
            ('Tag', member.discriminator),
            ('ID', member.id),
            ('Joined', member.joined_at),
            ('Created', member.created_at),
            ('Roles', ', '.join(roles)),
            ('Servers', '{} shared'.format(shared)),
            ('Voice', voice),
            ('Avatar', member.avatar_url),
        ]

        await formats.indented_entry_to_code(self.bot, entries)

    @info.command(name='server', pass_context=True, no_pm=True)
    async def server_info(self, ctx):
        server = ctx.message.server
        roles = [role.name.replace('@', '@\u200b') for role in server.roles]

        secret_member = copy.copy(server.me)
        secret_member.id = '0'
        secret_member.roles = [server.default_role]

        # figure out what channels are 'secret'
        secret_channels = 0
        secret_voice = 0
        text_channels = 0
        for channel in server.channels:
            perms = channel.permissions_for(secret_member)
            is_text = channel.type == discord.ChannelType.text
            text_channels += is_text
            if is_text and not perms.read_messages:
                secret_channels += 1
            elif not is_text and (not perms.connect or not perms.speak):
                secret_voice += 1

        regular_channels = len(server.channels) - secret_channels
        voice_channels = len(server.channels) - text_channels
        member_by_status = Counter(str(m.status) for m in server.members)
        member_fmt = '{0} ({1[online]} online, {1[idle]} idle, {1[offline]} offline)'
        channels_fmt = '{} Text ({} secret) / {} Voice ({} locked)'
        channels = channels_fmt.format(text_channels, secret_channels, voice_channels, secret_voice)

        entries = [
            ('Name', server.name),
            ('ID', server.id),
            ('Channels', channels),
            ('Created', server.created_at),
            ('Members', member_fmt.format(len(server.members), member_by_status)),
            ('Owner', server.owner),
            ('Icon', server.icon_url),
            ('Roles', ', '.join(roles)),
        ]

        await formats.indented_entry_to_code(self.bot, entries)

    async def say_permissions(self, member, channel):
        permissions = channel.permissions_for(member)
        entries = [(attr.replace('_', ' ').title(), val) for attr, val in permissions]
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
    async def join(self):
        """Joins a server."""
        msg = 'It is no longer possible to ask me to join via invite. So use this URL instead.\n\n'
        perms = discord.Permissions.none()
        perms.read_messages = True
        perms.send_messages = True
        perms.manage_roles = True
        perms.ban_members = True
        perms.kick_members = True
        perms.manage_messages = True
        perms.embed_links = True
        perms.read_message_history = True
        perms.attach_files = True
        await self.bot.say(msg + discord.utils.oauth_url(self.bot.client_id, perms))

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

    @commands.command()
    async def about(self):
        """Tells you information about the bot itself."""
        revision = os.popen(r'git show -s HEAD --format="%s (%cr)"').read().strip()
        result = ['**About Me:**']
        result.append('- Author: Danny#0007 (Discord ID: 80088516616269824)')
        result.append('- Library: discord.py (Python)')
        result.append('- Latest Change: {}'.format(revision))
        result.append('- Uptime: {}'.format(self.get_bot_uptime()))
        result.append('- Servers: {}'.format(len(self.bot.servers)))
        result.append('- Commands Run: {}'.format(sum(self.bot.commands_used.values())))

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
        result.append('')
        result.append('"Official" R. Danny server: https://discord.gg/0118rJdtd1rVJJfuI')
        await self.bot.say('\n'.join(result))

    @commands.command(rest_is_raw=True, hidden=True)
    @checks.is_owner()
    async def echo(self, *, content):
        await self.bot.say(content)

    @commands.command(hidden=True)
    @checks.is_owner()
    async def commandstats(self):
        msg = 'Since startup, {} commands have been used.\n{}'
        counter = self.bot.commands_used
        await self.bot.say(msg.format(sum(counter.values()), counter))

    @commands.command(hidden=True)
    async def cud(self):
        """pls no spam"""

        for i in range(3):
            await self.bot.say(3 - i)
            await asyncio.sleep(1)

        await self.bot.say('go')

def setup(bot):
    bot.add_cog(Meta(bot))
