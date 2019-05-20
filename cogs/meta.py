from discord.ext import commands
from .utils import checks, formats
from .utils.paginator import Pages
import discord
from collections import OrderedDict, deque, Counter
import os, datetime
import asyncio
import copy
import unicodedata
import inspect
import itertools

class Prefix(commands.Converter):
    async def convert(self, ctx, argument):
        user_id = ctx.bot.user.id
        if argument.startswith((f'<@{user_id}>', f'<@!{user_id}>')):
            raise commands.BadArgument('That is a reserved prefix already in use.')
        return argument

class HelpPaginator(Pages):
    def __init__(self, help_command, ctx, entries, *, per_page=4):
        super().__init__(ctx, entries=entries, per_page=per_page)
        self.reaction_emojis.append(('\N{WHITE QUESTION MARK ORNAMENT}', self.show_bot_help))
        self.total = len(entries)
        self.help_command = help_command
        self.prefix = help_command.clean_prefix
        self.is_bot = False

    def get_bot_page(self, page):
        cog, description, commands = self.entries[page - 1]
        self.title = f'{cog} Commands'
        self.description = description
        return commands

    def prepare_embed(self, entries, page, *, first=False):
        self.embed.clear_fields()
        self.embed.description = self.description
        self.embed.title = self.title

        if self.is_bot:
            value ='For more help, join the official bot support server: https://discord.gg/DWEaqMy'
            self.embed.add_field(name='Support', value=value, inline=False)

        self.embed.set_footer(text=f'Use "{self.prefix}help command" for more info on a command.')

        for entry in entries:
            signature = f'{entry.qualified_name} {entry.signature}'
            self.embed.add_field(name=signature, value=entry.short_doc or "No help given", inline=False)

        if self.maximum_pages:
            self.embed.set_author(name=f'Page {page}/{self.maximum_pages} ({self.total} commands)')

    async def show_help(self):
        """shows this message"""

        self.embed.title = 'Paginator help'
        self.embed.description = 'Hello! Welcome to the help page.'

        messages = [f'{emoji} {func.__doc__}' for emoji, func in self.reaction_emojis]
        self.embed.clear_fields()
        self.embed.add_field(name='What are these reactions for?', value='\n'.join(messages), inline=False)

        self.embed.set_footer(text=f'We were on page {self.current_page} before this message.')
        await self.message.edit(embed=self.embed)

        async def go_back_to_current_page():
            await asyncio.sleep(30.0)
            await self.show_current_page()

        self.bot.loop.create_task(go_back_to_current_page())

    async def show_bot_help(self):
        """shows how to use the bot"""

        self.embed.title = 'Using the bot'
        self.embed.description = 'Hello! Welcome to the help page.'
        self.embed.clear_fields()

        entries = (
            ('<argument>', 'This means the argument is __**required**__.'),
            ('[argument]', 'This means the argument is __**optional**__.'),
            ('[A|B]', 'This means the it can be __**either A or B**__.'),
            ('[argument...]', 'This means you can have multiple arguments.\n' \
                              'Now that you know the basics, it should be noted that...\n' \
                              '__**You do not type in the brackets!**__')
        )

        self.embed.add_field(name='How do I use this bot?', value='Reading the bot signature is pretty simple.')

        for name, value in entries:
            self.embed.add_field(name=name, value=value, inline=False)

        self.embed.set_footer(text=f'We were on page {self.current_page} before this message.')
        await self.message.edit(embed=self.embed)

        async def go_back_to_current_page():
            await asyncio.sleep(30.0)
            await self.show_current_page()

        self.bot.loop.create_task(go_back_to_current_page())

class PaginatedHelpCommand(commands.HelpCommand):
    def __init__(self):
        super().__init__(command_attrs={
            'cooldown': commands.Cooldown(1, 3.0, commands.BucketType.member),
            'help': 'Shows help about the bot, a command, or a category'
        })

    async def on_help_command_error(self, ctx, error):
        if isinstance(error, commands.CommandInvokeError):
            await ctx.send(str(error.original))

    def get_command_signature(self, command):
        parent = command.full_parent_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            fmt = f'[{command.name}|{aliases}]'
            if parent:
                fmt = f'{parent} {fmt}'
            alias = fmt
        else:
            alias = command.name if not parent else f'{parent} {command.name}'
        return f'{alias} {command.signature}'

    async def send_bot_help(self, mapping):
        def key(c):
            return c.cog_name or '\u200bNo Category'

        bot = self.context.bot
        entries = await self.filter_commands(bot.commands, sort=True, key=key)
        nested_pages = []
        per_page = 9
        total = 0

        for cog, commands in itertools.groupby(entries, key=key):
            commands = sorted(commands, key=lambda c: c.name)
            if len(commands) == 0:
                continue

            total += len(commands)
            actual_cog = bot.get_cog(cog)
            # get the description if it exists (and the cog is valid) or return Empty embed.
            description = (actual_cog and actual_cog.description) or discord.Embed.Empty
            nested_pages.extend((cog, description, commands[i:i + per_page]) for i in range(0, len(commands), per_page))

        # a value of 1 forces the pagination session
        pages = HelpPaginator(self, self.context, nested_pages, per_page=1)

        # swap the get_page implementation to work with our nested pages.
        pages.get_page = pages.get_bot_page
        pages.is_bot = True
        pages.total = total
        await self.context.release()
        await pages.paginate()

    async def send_cog_help(self, cog):
        entries = await self.filter_commands(cog.get_commands(), sort=True)
        pages = HelpPaginator(self, self.context, entries)
        pages.title = f'{cog.qualified_name} Commands'
        pages.description = cog.description

        await self.context.release()
        await pages.paginate()

    def common_command_formatting(self, page_or_embed, command):
        page_or_embed.title = self.get_command_signature(command)
        if command.description:
            page_or_embed.description = f'{command.description}\n\n{command.help}'
        else:
            page_or_embed.description = command.help or 'No help found...'

    async def send_command_help(self, command):
        # No pagination necessary for a single command.
        embed = discord.Embed(colour=discord.Colour.blurple())
        self.common_command_formatting(embed, command)
        await self.context.send(embed=embed)

    async def send_group_help(self, group):
        subcommands = group.commands
        if len(subcommands) == 0:
            return await self.send_command_help(group)

        entries = await self.filter_commands(subcommands, sort=True)
        pages = HelpPaginator(self, self.context, entries)
        self.common_command_formatting(pages, group)

        await self.context.release()
        await pages.paginate()

class Meta(commands.Cog):
    """Commands for utilities related to Discord or the Bot itself."""

    def __init__(self, bot):
        self.bot = bot
        self.old_help_command = bot.help_command
        bot.help_command = PaginatedHelpCommand()
        bot.help_command.cog = self

    def cog_unload(self):
        self.bot.help_command = self.old_help_command

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    @commands.command(hidden=True)
    async def hello(self, ctx):
        """Displays my intro message."""
        await ctx.send('Hello! I\'m a robot! Danny#0007 made me.')

    @commands.command()
    async def charinfo(self, ctx, *, characters: str):
        """Shows you information about a number of characters.

        Only up to 25 characters at a time.
        """

        def to_string(c):
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'
        msg = '\n'.join(map(to_string, characters))
        if len(msg) > 2000:
            return await ctx.send('Output too long to display.')
        await ctx.send(msg)

    @commands.group(name='prefix', invoke_without_command=True)
    async def prefix(self, ctx):
        """Manages the server's custom prefixes.

        If called without a subcommand, this will list the currently set
        prefixes.
        """

        prefixes = self.bot.get_guild_prefixes(ctx.guild)

        # we want to remove prefix #2, because it's the 2nd form of the mention
        # and to the end user, this would end up making them confused why the
        # mention is there twice
        del prefixes[1]

        e = discord.Embed(title='Prefixes', colour=discord.Colour.blurple())
        e.set_footer(text=f'{len(prefixes)} prefixes')
        e.description = '\n'.join(f'{index}. {elem}' for index, elem in enumerate(prefixes, 1))
        await ctx.send(embed=e)

    @prefix.command(name='add', ignore_extra=False)
    @checks.is_mod()
    async def prefix_add(self, ctx, prefix: Prefix):
        """Appends a prefix to the list of custom prefixes.

        Previously set prefixes are not overridden.

        To have a word prefix, you should quote it and end it with
        a space, e.g. "hello " to set the prefix to "hello ". This
        is because Discord removes spaces when sending messages so
        the spaces are not preserved.

        Multi-word prefixes must be quoted also.

        You must have Manage Server permission to use this command.
        """

        current_prefixes = self.bot.get_raw_guild_prefixes(ctx.guild.id)
        current_prefixes.append(prefix)
        try:
            await self.bot.set_guild_prefixes(ctx.guild, current_prefixes)
        except Exception as e:
            await ctx.send(f'{ctx.tick(False)} {e}')
        else:
            await ctx.send(ctx.tick(True))

    @prefix_add.error
    async def prefix_add_error(self, ctx, error):
        if isinstance(error, commands.TooManyArguments):
            await ctx.send("You've given too many prefixes. Either quote it or only do it one by one.")

    @prefix.command(name='remove', aliases=['delete'], ignore_extra=False)
    @checks.is_mod()
    async def prefix_remove(self, ctx, prefix: Prefix):
        """Removes a prefix from the list of custom prefixes.

        This is the inverse of the 'prefix add' command. You can
        use this to remove prefixes from the default set as well.

        You must have Manage Server permission to use this command.
        """

        current_prefixes = self.bot.get_raw_guild_prefixes(ctx.guild.id)

        try:
            current_prefixes.remove(prefix)
        except ValueError:
            return await ctx.send('I do not have this prefix registered.')

        try:
            await self.bot.set_guild_prefixes(ctx.guild, current_prefixes)
        except Exception as e:
            await ctx.send(f'{ctx.tick(False)} {e}')
        else:
            await ctx.send(ctx.tick(True))

    @prefix.command(name='clear')
    @checks.is_mod()
    async def prefix_clear(self, ctx):
        """Removes all custom prefixes.

        After this, the bot will listen to only mention prefixes.

        You must have Manage Server permission to use this command.
        """

        await self.bot.set_guild_prefixes(ctx.guild, [])
        await ctx.send(ctx.tick(True))

    @commands.command()
    async def source(self, ctx, *, command: str = None):
        """Displays my full source code or for a specific command.

        To display the source code of a subcommand you can separate it by
        periods, e.g. tag.create for the create subcommand of the tag command
        or by spaces.
        """
        source_url = 'https://github.com/Rapptz/RoboDanny'
        if command is None:
            return await ctx.send(source_url)

        if command == 'help':
            src = type(self.bot.help_command)
            module = src.__module__
            filename = inspect.getsourcefile(src)
        else:
            obj = self.bot.get_command(command.replace('.', ' '))
            if obj is None:
                return await ctx.send('Could not find command.')

            # since we found the command we're looking for, presumably anyway, let's
            # try to access the code itself
            src = obj.callback.__code__
            module = obj.callback.__module__
            filename = src.co_filename

        lines, firstlineno = inspect.getsourcelines(src)
        if not module.startswith('discord'):
            # not a built-in command
            location = os.path.relpath(filename).replace('\\', '/')
        else:
            location = module.replace('.', '/') + '.py'
            source_url = 'https://github.com/Rapptz/discord.py'

        final_url = f'<{source_url}/blob/rewrite/{location}#L{firstlineno}-L{firstlineno + len(lines) - 1}>'
        await ctx.send(final_url)

    @commands.command(name='quit', hidden=True)
    @commands.is_owner()
    async def _quit(self, ctx):
        """Quits the bot."""
        await self.bot.logout()

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def info(self, ctx, *, member: discord.Member = None):
        """Shows info about a member.

        This cannot be used in private messages. If you don't specify
        a member then the info returned will be yours.
        """

        if member is None:
            member = ctx.author

        e = discord.Embed()
        roles = [role.name.replace('@', '@\u200b') for role in member.roles]
        shared = sum(1 for m in self.bot.get_all_members() if m.id == member.id)
        voice = member.voice
        if voice is not None:
            vc = voice.channel
            other_people = len(vc.members) - 1
            voice = f'{vc.name} with {other_people} others' if other_people else f'{vc.name} by themselves'
        else:
            voice = 'Not connected.'

        e.set_author(name=str(member))
        e.set_footer(text='Member since').timestamp = member.joined_at
        e.add_field(name='ID', value=member.id)
        e.add_field(name='Servers', value=f'{shared} shared')
        e.add_field(name='Created', value=member.created_at)
        e.add_field(name='Voice', value=voice)
        e.add_field(name='Roles', value=', '.join(roles) if len(roles) < 10 else f'{len(roles)} roles')
        e.colour = member.colour

        if member.avatar:
            e.set_thumbnail(url=member.avatar_url)

        await ctx.send(embed=e)

    @info.command(name='server', aliases=['guild'])
    @commands.guild_only()
    async def server_info(self, ctx):
        """Shows info about the current server."""

        guild = ctx.guild
        roles = [role.name.replace('@', '@\u200b') for role in guild.roles]

        # we're going to duck type our way here
        class Secret:
            pass

        secret_member = Secret()
        secret_member.id = 0
        secret_member.roles = [guild.default_role]

        # figure out what channels are 'secret'
        secret_channels = 0
        secret_voice = 0
        text_channels = 0
        for channel in guild.channels:
            perms = channel.permissions_for(secret_member)
            is_text = isinstance(channel, discord.TextChannel)
            text_channels += is_text
            if is_text and not perms.read_messages:
                secret_channels += 1
            elif not is_text and (not perms.connect or not perms.speak):
                secret_voice += 1

        regular_channels = len(guild.channels) - secret_channels
        voice_channels = len(guild.channels) - text_channels
        member_by_status = Counter(str(m.status) for m in guild.members)

        e = discord.Embed()
        e.title = 'Info for ' + guild.name
        e.add_field(name='ID', value=guild.id)
        e.add_field(name='Owner', value=guild.owner)
        if guild.icon:
            e.set_thumbnail(url=guild.icon_url)

        if guild.splash:
            e.set_image(url=guild.splash_url)

        info = []
        info.append(ctx.tick(len(guild.features) >= 3, 'Partnered'))

        sfw = guild.explicit_content_filter is not discord.ContentFilter.disabled
        info.append(ctx.tick(sfw, 'Scanning Images'))
        info.append(ctx.tick(guild.member_count > 100, 'Large'))

        e.add_field(name='Info', value='\n'.join(map(str, info)))

        fmt = f'Text {text_channels} ({secret_channels} secret)\nVoice {voice_channels} ({secret_voice} locked)'
        e.add_field(name='Channels', value=fmt)

        fmt = f'<:online:316856575413321728> {member_by_status["online"]} ' \
              f'<:idle:316856575098880002> {member_by_status["idle"]} ' \
              f'<:dnd:316856574868193281> {member_by_status["dnd"]} ' \
              f'<:offline:316856575501402112> {member_by_status["offline"]}\n' \
              f'Total: {guild.member_count}'

        e.add_field(name='Members', value=fmt)
        e.add_field(name='Roles', value=', '.join(roles) if len(roles) < 10 else f'{len(roles)} roles')
        e.set_footer(text='Created').timestamp = guild.created_at
        await ctx.send(embed=e)

    async def say_permissions(self, ctx, member, channel):
        permissions = channel.permissions_for(member)
        e = discord.Embed(colour=member.colour)
        allowed, denied = [], []
        for name, value in permissions:
            name = name.replace('_', ' ').replace('guild', 'server').title()
            if value:
                allowed.append(name)
            else:
                denied.append(name)

        e.add_field(name='Allowed', value='\n'.join(allowed))
        e.add_field(name='Denied', value='\n'.join(denied))
        await ctx.send(embed=e)

    @commands.command()
    @commands.guild_only()
    async def permissions(self, ctx, member: discord.Member = None, channel: discord.TextChannel = None):
        """Shows a member's permissions in a specific channel.

        If no channel is given then it uses the current one.

        You cannot use this in private messages. If no member is given then
        the info returned will be yours.
        """
        channel = channel or ctx.channel
        if member is None:
            member = ctx.author

        await self.say_permissions(ctx, member, channel)

    @commands.command()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_roles=True)
    async def botpermissions(self, ctx, *, channel: discord.TextChannel = None):
        """Shows the bot's permissions in a specific channel.

        If no channel is given then it uses the current one.

        This is a good way of checking if the bot has the permissions needed
        to execute the commands it wants to execute.

        To execute this command you must have Manage Roles permission.
        You cannot use this in private messages.
        """
        channel = channel or ctx.channel
        member = ctx.guild.me
        await self.say_permissions(ctx, member, channel)

    @commands.command(aliases=['invite'])
    async def join(self, ctx):
        """Joins a server."""
        perms = discord.Permissions.none()
        perms.read_messages = True
        perms.external_emojis = True
        perms.send_messages = True
        perms.manage_roles = True
        perms.manage_channels = True
        perms.ban_members = True
        perms.kick_members = True
        perms.manage_messages = True
        perms.embed_links = True
        perms.read_message_history = True
        perms.attach_files = True
        perms.add_reactions = True
        await ctx.send(f'<{discord.utils.oauth_url(self.bot.client_id, perms)}>')

    @commands.command(rest_is_raw=True, hidden=True)
    @commands.is_owner()
    async def echo(self, ctx, *, content):
        await ctx.send(content)

    @commands.command(hidden=True)
    async def cud(self, ctx):
        """pls no spam"""

        for i in range(3):
            await ctx.send(3 - i)
            await asyncio.sleep(1)

        await ctx.send('go')

def setup(bot):
    bot.add_cog(Meta(bot))
