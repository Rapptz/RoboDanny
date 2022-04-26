from __future__ import annotations
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generator, Optional, TypedDict, Union
from typing_extensions import Annotated

from discord.ext import commands
from discord import app_commands
from .utils import fuzzy, cache, time
import asyncio
import datetime
import discord
import re
import zlib
import io
import os
import lxml.etree as etree

if TYPE_CHECKING:
    from .utils.context import Context, GuildContext
    from bot import RoboDanny
    from asyncpg import Record, Connection
    from cogs.reminder import Timer


DISCORD_API_ID = 81384788765712384
DISCORD_BOTS_ID = 110373943822540800
USER_BOTS_ROLE = 178558252869484544
CONTRIBUTORS_ROLE = 111173097888993280
DISCORD_PY_ID = 84319995256905728
DISCORD_PY_GUILD = 336642139381301249
DISCORD_PY_JP_CATEGORY = 490287576670928914
DISCORD_PY_JP_STAFF_ROLE = 490320652230852629
DISCORD_PY_PROF_ROLE = 381978395270971407
DISCORD_PY_HELPER_ROLE = 558559632637952010
DISCORD_PY_HELP_CHANNELS = (381965515721146390, 564950631455129636, 738572311107469354, 956435493296414720)

RTFM_PAGE_TYPES = {
    'stable': 'https://discordpy.readthedocs.io/en/stable',
    'stable-jp': 'https://discordpy.readthedocs.io/ja/stable',
    'latest': 'https://discordpy.readthedocs.io/en/latest',
    'latest-jp': 'https://discordpy.readthedocs.io/ja/latest',
    'python': 'https://docs.python.org/3',
    'python-jp': 'https://docs.python.org/ja/3',
}


class BotInfo(TypedDict):
    channel: int
    testing: tuple[int, ...]
    terms: str


BOT_LIST_INFO: dict[int, BotInfo] = {
    DISCORD_API_ID: {
        'channel': 580184108794380298,
        'testing': (
            381896832399310868,  # testing
            381896931724492800,  # playground
        ),
        'terms': 'By requesting to add your bot, you agree to not spam or do things without user input.',
    },
    DISCORD_PY_GUILD: {
        'channel': 579998326557114368,
        'testing': (
            381963689470984203,  # testing
            559455534965850142,  # playground
            568662293190148106,  # mod-testing
        ),
        'terms': 'By requesting to add your bot, you must agree to the guidelines presented in the <#381974649019432981>.',
    },
}


def in_testing(info: dict[int, BotInfo] = BOT_LIST_INFO):
    def predicate(ctx: GuildContext) -> bool:
        try:
            return ctx.channel.id in info[ctx.guild.id]['testing']
        except (AttributeError, KeyError):
            return False

    return commands.check(predicate)


def is_discord_py_helper(member: discord.Member) -> bool:
    guild_id = member.guild.id
    if guild_id != DISCORD_PY_GUILD:
        return False

    if member.guild_permissions.manage_roles:
        return False

    return member._roles.has(DISCORD_PY_HELPER_ROLE)


def can_use_block():
    def predicate(ctx: GuildContext) -> bool:
        if ctx.guild is None:
            return False

        if isinstance(ctx.channel, discord.Thread):
            return ctx.author.id == ctx.channel.owner_id or ctx.channel.permissions_for(ctx.author).manage_threads

        guild_id = ctx.guild.id
        if guild_id == DISCORD_API_ID:
            return ctx.channel.permissions_for(ctx.author).manage_roles
        elif guild_id == DISCORD_PY_GUILD:
            guild_level = ctx.author.guild_permissions
            return guild_level.manage_roles or (
                ctx.channel.category_id == DISCORD_PY_JP_CATEGORY and ctx.author._roles.has(DISCORD_PY_JP_STAFF_ROLE)
            )
        return False

    return commands.check(predicate)


def can_use_tempblock():
    def predicate(ctx: GuildContext) -> bool:
        if ctx.guild is None:
            return False

        if isinstance(ctx.channel, discord.Thread):
            return False

        guild_id = ctx.guild.id
        if guild_id == DISCORD_API_ID:
            return ctx.channel.permissions_for(ctx.author).manage_roles
        elif guild_id == DISCORD_PY_GUILD:
            guild_level = ctx.author.guild_permissions
            return (
                guild_level.manage_roles
                or (
                    ctx.channel.id in DISCORD_PY_HELP_CHANNELS
                    and (ctx.author._roles.has(DISCORD_PY_PROF_ROLE) or ctx.author._roles.has(DISCORD_PY_HELPER_ROLE))
                )
                or (ctx.channel.category_id == DISCORD_PY_JP_CATEGORY and ctx.author._roles.has(DISCORD_PY_JP_STAFF_ROLE))
            )
        return False

    return commands.check(predicate)


def contributor_or_higher():
    def predicate(ctx: GuildContext) -> bool:
        guild = ctx.guild
        if guild is None:
            return False

        role = discord.utils.find(lambda r: r.id == CONTRIBUTORS_ROLE, guild.roles)
        if role is None:
            return False

        return ctx.author.top_role >= role

    return commands.check(predicate)


class SphinxObjectFileReader:
    # Inspired by Sphinx's InventoryFileReader
    BUFSIZE = 16 * 1024

    def __init__(self, buffer: bytes):
        self.stream = io.BytesIO(buffer)

    def readline(self) -> str:
        return self.stream.readline().decode('utf-8')

    def skipline(self) -> None:
        self.stream.readline()

    def read_compressed_chunks(self) -> Generator[bytes, None, None]:
        decompressor = zlib.decompressobj()
        while True:
            chunk = self.stream.read(self.BUFSIZE)
            if len(chunk) == 0:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()

    def read_compressed_lines(self) -> Generator[str, None, None]:
        buf = b''
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b'\n')
            while pos != -1:
                yield buf[:pos].decode('utf-8')
                buf = buf[pos + 1 :]
                pos = buf.find(b'\n')


class BotUser(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        if not argument.isdigit():
            raise commands.BadArgument('Not a valid bot user ID.')
        try:
            user = await ctx.bot.fetch_user(int(argument))
        except discord.NotFound:
            raise commands.BadArgument('Bot user not found (404).')
        except discord.HTTPException as e:
            raise commands.BadArgument(f'Error fetching bot user: {e}')
        else:
            if not user.bot:
                raise commands.BadArgument('This is not a bot.')
            return user


class API(commands.Cog):
    """Discord API exclusive things."""

    faq_entries: dict[str, str]
    _rtfm_cache: dict[str, dict[str, str]]

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{PERSONAL COMPUTER}')

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != DISCORD_API_ID:
            return

        if member.bot:
            role = discord.Object(id=USER_BOTS_ROLE)
            await member.add_roles(role)

    def parse_object_inv(self, stream: SphinxObjectFileReader, url: str) -> dict[str, str]:
        # key: URL
        # n.b.: key doesn't have `discord` or `discord.ext.commands` namespaces
        result: dict[str, str] = {}

        # first line is version info
        inv_version = stream.readline().rstrip()

        if inv_version != '# Sphinx inventory version 2':
            raise RuntimeError('Invalid objects.inv file version.')

        # next line is "# Project: <name>"
        # then after that is "# Version: <version>"
        projname = stream.readline().rstrip()[11:]
        version = stream.readline().rstrip()[11:]

        # next line says if it's a zlib header
        line = stream.readline()
        if 'zlib' not in line:
            raise RuntimeError('Invalid objects.inv file, not z-lib compatible.')

        # This code mostly comes from the Sphinx repository.
        entry_regex = re.compile(r'(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)')
        for line in stream.read_compressed_lines():
            match = entry_regex.match(line.rstrip())
            if not match:
                continue

            name, directive, prio, location, dispname = match.groups()
            domain, _, subdirective = directive.partition(':')
            if directive == 'py:module' and name in result:
                # From the Sphinx Repository:
                # due to a bug in 1.1 and below,
                # two inventory entries are created
                # for Python modules, and the first
                # one is correct
                continue

            # Most documentation pages have a label
            if directive == 'std:doc':
                subdirective = 'label'

            if location.endswith('$'):
                location = location[:-1] + name

            key = name if dispname == '-' else dispname
            prefix = f'{subdirective}:' if domain == 'std' else ''

            if projname == 'discord.py':
                key = key.replace('discord.ext.commands.', '').replace('discord.', '')

            result[f'{prefix}{key}'] = os.path.join(url, location)

        return result

    async def build_rtfm_lookup_table(self):
        cache: dict[str, dict[str, str]] = {}
        for key, page in RTFM_PAGE_TYPES.items():
            cache[key] = {}
            async with self.bot.session.get(page + '/objects.inv') as resp:
                if resp.status != 200:
                    raise RuntimeError('Cannot build rtfm lookup table, try again later.')

                stream = SphinxObjectFileReader(await resp.read())
                cache[key] = self.parse_object_inv(stream, page)

        self._rtfm_cache = cache

    async def do_rtfm(self, ctx: Context, key: str, obj: Optional[str]):
        if obj is None:
            await ctx.send(RTFM_PAGE_TYPES[key])
            return

        if not hasattr(self, '_rtfm_cache'):
            await ctx.typing()
            await self.build_rtfm_lookup_table()

        obj = re.sub(r'^(?:discord\.(?:ext\.)?)?(?:commands\.)?(.+)', r'\1', obj)

        if key.startswith('latest'):
            # point the abc.Messageable types properly:
            q = obj.lower()
            for name in dir(discord.abc.Messageable):
                if name[0] == '_':
                    continue
                if q == name:
                    obj = f'abc.Messageable.{name}'
                    break

        cache = list(self._rtfm_cache[key].items())
        matches = fuzzy.finder(obj, cache, key=lambda t: t[0], lazy=False)[:8]

        e = discord.Embed(colour=discord.Colour.blurple())
        if len(matches) == 0:
            return await ctx.send('Could not find anything. Sorry.')

        e.description = '\n'.join(f'[`{key}`]({url})' for key, url in matches)
        await ctx.send(embed=e, reference=ctx.replied_reference)

        if ctx.guild and ctx.guild.id in (DISCORD_API_ID, DISCORD_PY_GUILD):
            query = 'INSERT INTO rtfm (user_id) VALUES ($1) ON CONFLICT (user_id) DO UPDATE SET count = rtfm.count + 1;'
            await ctx.db.execute(query, ctx.author.id)

    def transform_rtfm_language_key(self, ctx: Union[discord.Interaction, Context], prefix: str):
        if ctx.guild is not None:
            #                             日本語 category
            if ctx.channel.category_id == DISCORD_PY_JP_CATEGORY:  # type: ignore  # category_id is safe to access
                return prefix + '-jp'
            #                    d.py unofficial JP   Discord Bot Portal JP
            elif ctx.guild.id in (463986890190749698, 494911447420108820):
                return prefix + '-jp'
        return prefix

    async def rtfm_slash_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:

        # Degenerate case: not having built caching yet
        if not hasattr(self, '_rtfm_cache'):
            await interaction.response.autocomplete([])
            await self.build_rtfm_lookup_table()
            return []

        if not current:
            return []

        if len(current) < 3:
            return [app_commands.Choice(name=current, value=current)]

        assert interaction.command is not None
        key = interaction.command.name
        if key in ('stable', 'python'):
            key = self.transform_rtfm_language_key(interaction, key)
        elif key == 'jp':
            key = 'latest-jp'

        matches = fuzzy.finder(current, self._rtfm_cache[key], lazy=False)[:10]
        return [app_commands.Choice(name=m, value=m) for m in matches]

    @commands.hybrid_group(aliases=['rtfd'], fallback='stable')
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm(self, ctx: Context, *, entity: Optional[str] = None):
        """Gives you a documentation link for a discord.py entity.

        Events, objects, and functions are all supported through
        a cruddy fuzzy algorithm.
        """
        key = self.transform_rtfm_language_key(ctx, 'stable')
        await self.do_rtfm(ctx, key, entity)

    @rtfm.command(name='jp')
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_jp(self, ctx: Context, *, entity: Optional[str] = None):
        """Gives you a documentation link for a discord.py entity (Japanese)."""
        await self.do_rtfm(ctx, 'latest-jp', entity)

    @rtfm.command(name='python', aliases=['py'])
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_python(self, ctx: Context, *, entity: Optional[str] = None):
        """Gives you a documentation link for a Python entity."""
        key = self.transform_rtfm_language_key(ctx, 'python')
        await self.do_rtfm(ctx, key, entity)

    @rtfm.command(name='python-jp', aliases=['py-jp', 'py-ja'])
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_python_jp(self, ctx: Context, *, entity: Optional[str] = None):
        """Gives you a documentation link for a Python entity (Japanese)."""
        await self.do_rtfm(ctx, 'python-jp', entity)

    @rtfm.command(name='latest', aliases=['2.0', 'master'])
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_master(self, ctx: Context, *, entity: Optional[str] = None):
        """Gives you a documentation link for a discord.py entity (master branch)"""
        await self.do_rtfm(ctx, 'latest', entity)

    @rtfm.command(name='refresh')
    @commands.is_owner()
    async def rtfm_refresh(self, ctx: Context):
        """Refreshes the RTFM and FAQ cache"""

        async with ctx.typing():
            await self.build_rtfm_lookup_table()
            await self.refresh_faq_cache()

        await ctx.send('\N{THUMBS UP SIGN}')

    async def _member_stats(self, ctx: Context, member: discord.Member, total_uses: int):
        e = discord.Embed(title='RTFM Stats')
        e.set_author(name=str(member), icon_url=member.display_avatar.url)

        query = 'SELECT count FROM rtfm WHERE user_id=$1;'
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            count = 0
        else:
            count = record['count']

        e.add_field(name='Uses', value=count)
        e.add_field(name='Percentage', value=f'{count/total_uses:.2%} out of {total_uses}')
        e.colour = discord.Colour.blurple()
        await ctx.send(embed=e)

    @rtfm.command()
    @app_commands.describe(member='The member to look up stats for')
    async def stats(self, ctx: Context, *, member: discord.Member = None):
        """Shows statistics on RTFM usage on a member or the server."""
        query = 'SELECT SUM(count) AS total_uses FROM rtfm;'
        record: Record = await ctx.db.fetchrow(query)
        total_uses: int = record['total_uses']

        if member is not None:
            return await self._member_stats(ctx, member, total_uses)

        query = 'SELECT user_id, count FROM rtfm ORDER BY count DESC LIMIT 10;'
        records: list[Record] = await ctx.db.fetch(query)

        output = []
        output.append(f'**Total uses**: {total_uses}')

        # first we get the most used users
        if records:
            output.append(f'**Top {len(records)} users**:')

            for rank, (user_id, count) in enumerate(records, 1):
                user = self.bot.get_user(user_id) or (await self.bot.fetch_user(user_id))
                if rank != 10:
                    output.append(f'{rank}\u20e3 {user}: {count}')
                else:
                    output.append(f'\N{KEYCAP TEN} {user}: {count}')

        await ctx.send('\n'.join(output))

    def library_name(self, channel: Union[discord.abc.GuildChannel, discord.Thread]) -> str:
        # language_<name>
        name = channel.name
        index = name.find('_')
        if index != -1:
            name = name[index + 1 :]
        return name.replace('-', '.')

    def get_block_channels(self, guild: discord.Guild, channel: discord.abc.GuildChannel) -> list[discord.abc.GuildChannel]:
        if guild.id == DISCORD_PY_GUILD and channel.id in DISCORD_PY_HELP_CHANNELS:
            return [guild.get_channel(x) for x in DISCORD_PY_HELP_CHANNELS]  # type: ignore  # Not None
        return [channel]

    @commands.command()
    @can_use_block()
    async def block(self, ctx: GuildContext, *, member: discord.Member):
        """Blocks a user from your channel."""

        if member.top_role >= ctx.author.top_role:
            return

        reason = f'Block by {ctx.author} (ID: {ctx.author.id})'

        if isinstance(ctx.channel, discord.Thread):
            try:
                await ctx.channel.remove_user(member)
            except:
                await ctx.send('\N{THUMBS DOWN SIGN}')
            else:
                await ctx.send('\N{THUMBS UP SIGN}')

            return

        channels = self.get_block_channels(ctx.guild, ctx.channel)

        try:
            for channel in channels:
                await channel.set_permissions(
                    member,
                    send_messages=False,
                    add_reactions=False,
                    create_public_threads=False,
                    send_messages_in_threads=False,
                    reason=reason,
                )
        except:
            await ctx.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.send('\N{THUMBS UP SIGN}')

    @commands.command()
    @can_use_block()
    async def unblock(self, ctx: GuildContext, *, member: discord.Member):
        """Unblocks a user from your channel."""

        if member.top_role >= ctx.author.top_role:
            return

        reason = f'Unblock by {ctx.author} (ID: {ctx.author.id})'

        if isinstance(ctx.channel, discord.Thread):
            return await ctx.send('\N{THUMBS DOWN SIGN} Unblocking does not make sense for threads')

        channels = self.get_block_channels(ctx.guild, ctx.channel)

        try:
            for channel in channels:
                await channel.set_permissions(
                    member,
                    send_messages=None,
                    add_reactions=None,
                    create_public_threads=None,
                    send_messages_in_threads=None,
                    reason=reason,
                )
        except:
            await ctx.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.send('\N{THUMBS UP SIGN}')

    @commands.command()
    @can_use_tempblock()
    async def tempblock(self, ctx: GuildContext, duration: time.FutureTime, *, member: discord.Member):
        """Temporarily blocks a user from your channel.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2017-12-31".

        Note that times are in UTC.
        """

        if member.top_role >= ctx.author.top_role:
            return

        created_at = ctx.message.created_at
        if is_discord_py_helper(ctx.author) and duration.dt > (created_at + datetime.timedelta(minutes=60)):
            return await ctx.send('Helpers can only block for up to an hour.')

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        channels = self.get_block_channels(ctx.guild, ctx.channel)  # type: ignore  # Threads are disallowed
        timer = await reminder.create_timer(
            duration.dt,
            'tempblock',
            ctx.guild.id,
            ctx.author.id,
            ctx.channel.id,
            member.id,
            created=created_at,
        )

        reason = f'Tempblock by {ctx.author} (ID: {ctx.author.id}) until {duration.dt}'

        try:
            for channel in channels:
                await channel.set_permissions(
                    member,
                    send_messages=False,
                    add_reactions=False,
                    create_public_threads=False,
                    send_messages_in_threads=False,
                    reason=reason,
                )
        except:
            await ctx.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.send(f'Blocked {member} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempblock_timer_complete(self, timer: Timer):
        guild_id, mod_id, channel_id, member_id = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            # RIP x2
            return

        to_unblock = await self.bot.get_or_fetch_member(guild, member_id)
        if to_unblock is None:
            # RIP x3
            return

        moderator = await self.bot.get_or_fetch_member(guild, mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.fetch_user(mod_id)
            except:
                # request failed somehow
                moderator = f'Mod ID {mod_id}'
            else:
                moderator = f'{moderator} (ID: {mod_id})'
        else:
            moderator = f'{moderator} (ID: {mod_id})'

        reason = f'Automatic unblock from timer made on {timer.created_at} by {moderator}.'

        for ch in self.get_block_channels(guild, channel):
            try:
                await ch.set_permissions(
                    to_unblock,
                    send_messages=None,
                    add_reactions=None,
                    create_public_threads=None,
                    send_messages_in_threads=None,
                    reason=reason,
                )
            except:
                pass

    @cache.cache()
    async def get_feeds(self, channel_id: int, *, connection: Optional[Connection] = None) -> dict[str, int]:
        con = connection or self.bot.pool
        query = 'SELECT name, role_id FROM feeds WHERE channel_id=$1;'
        feeds = await con.fetch(query, channel_id)
        return {f['name']: f['role_id'] for f in feeds}

    @commands.group(name='feeds', invoke_without_command=True)
    @commands.guild_only()
    async def _feeds(self, ctx: GuildContext):
        """Shows the list of feeds that the channel has.

        A feed is something that users can opt-in to
        to receive news about a certain feed by running
        the `sub` command (and opt-out by doing the `unsub` command).
        You can publish to a feed by using the `publish` command.
        """

        feeds = await self.get_feeds(ctx.channel.id)

        if len(feeds) == 0:
            await ctx.send('This channel has no feeds.')
            return

        names = '\n'.join(f'- {r}' for r in feeds)
        await ctx.send(f'Found {len(feeds)} feeds.\n{names}')

    @_feeds.command(name='create')
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def feeds_create(self, ctx: GuildContext, *, name: str):
        """Creates a feed with the specified name.

        You need Manage Roles permissions to create a feed.
        """

        name = name.lower()

        if name in ('@everyone', '@here'):
            return await ctx.send('That is an invalid feed name.')

        query = 'SELECT role_id FROM feeds WHERE channel_id=$1 AND name=$2;'

        exists = await ctx.db.fetchrow(query, ctx.channel.id, name)
        if exists is not None:
            await ctx.send('This feed already exists.')
            return

        # create the role
        if ctx.guild.id == DISCORD_API_ID:
            role_name = self.library_name(ctx.channel) + ' ' + name
        else:
            role_name = name

        role = await ctx.guild.create_role(name=role_name, permissions=discord.Permissions.none())
        query = 'INSERT INTO feeds (role_id, channel_id, name) VALUES ($1, $2, $3);'
        await ctx.db.execute(query, role.id, ctx.channel.id, name)
        self.get_feeds.invalidate(self, ctx.channel.id)
        await ctx.send(f'{ctx.tick(True)} Successfully created feed.')

    @_feeds.command(name='delete', aliases=['remove'])
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def feeds_delete(self, ctx: GuildContext, *, feed: str):
        """Removes a feed from the channel.

        This will also delete the associated role so this
        action is irreversible.
        """

        query = 'DELETE FROM feeds WHERE channel_id=$1 AND name=$2 RETURNING *;'
        records = await ctx.db.fetch(query, ctx.channel.id, feed)
        self.get_feeds.invalidate(self, ctx.channel.id)

        if len(records) == 0:
            return await ctx.send('This feed does not exist.')

        for record in records:
            role = discord.utils.find(lambda r: r.id == record['role_id'], ctx.guild.roles)
            if role is not None:
                try:
                    await role.delete()
                except discord.HTTPException:
                    continue

        await ctx.send(f'{ctx.tick(True)} Removed feed.')

    async def do_subscription(self, ctx: GuildContext, feed: str, action: Callable[[discord.Role], Awaitable[Any]]):
        feeds = await self.get_feeds(ctx.channel.id)
        if len(feeds) == 0:
            await ctx.send('This channel has no feeds set up.')
            return

        if feed not in feeds:
            await ctx.send(f'This feed does not exist.\nValid feeds: {", ".join(feeds)}')
            return

        role_id = feeds[feed]
        role = discord.utils.find(lambda r: r.id == role_id, ctx.guild.roles)
        if role is not None:
            await action(role)
            await ctx.message.add_reaction(ctx.tick(True))
        else:
            await ctx.message.add_reaction(ctx.tick(False))

    @commands.command()
    @commands.guild_only()
    async def sub(self, ctx: GuildContext, *, feed: str):
        """Subscribes to the publication of a feed.

        This will allow you to receive updates from the channel
        owner. To unsubscribe, see the `unsub` command.
        """
        await self.do_subscription(ctx, feed, ctx.author.add_roles)

    @commands.command()
    @commands.guild_only()
    async def unsub(self, ctx: GuildContext, *, feed: str):
        """Unsubscribe to the publication of a feed.

        This will remove you from notifications of a feed you
        are no longer interested in. You can always sub back by
        using the `sub` command.
        """
        await self.do_subscription(ctx, feed, ctx.author.remove_roles)

    @commands.command()
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def publish(self, ctx: GuildContext, feed: str, *, content: str):
        """Publishes content to a feed.

        Everyone who is subscribed to the feed will be notified
        with the content. Use this to notify people of important
        events or changes.
        """
        feeds = await self.get_feeds(ctx.channel.id)
        feed = feed.lower()
        if feed not in feeds:
            await ctx.send('This feed does not exist.')
            return

        role = discord.utils.get(ctx.guild.roles, id=feeds[feed])
        if role is None:
            fmt = (
                'Uh.. a fatal error occurred here. The role associated with '
                'this feed has been removed or not found. '
                'Please recreate the feed.'
            )
            await ctx.send(fmt)
            return

        # delete the message we used to invoke it
        try:
            await ctx.message.delete()
        except:
            pass

        # make the role mentionable
        await role.edit(mentionable=True)

        # then send the message..
        mentions = discord.AllowedMentions(roles=[role])
        await ctx.send(f'{role.mention}: {content}'[:2000], allowed_mentions=mentions)

        # then make the role unmentionable
        await role.edit(mentionable=False)

    async def refresh_faq_cache(self):
        self.faq_entries = {}
        base_url = 'https://discordpy.readthedocs.io/en/latest/faq.html'
        async with self.bot.session.get(base_url) as resp:
            text = await resp.text(encoding='utf-8')

            root = etree.fromstring(text, etree.HTMLParser())
            nodes = root.findall(".//div[@id='questions']/ul[@class='simple']/li/ul//a")
            for node in nodes:
                self.faq_entries[''.join(node.itertext()).strip()] = base_url + node.get('href').strip()

    @commands.hybrid_command()
    @app_commands.describe(query='The FAQ entry to look up')
    async def faq(self, ctx: Context, *, query: Optional[str] = None):
        """Shows an FAQ entry from the discord.py documentation"""
        if not hasattr(self, 'faq_entries'):
            await self.refresh_faq_cache()

        if query is None:
            return await ctx.send('https://discordpy.readthedocs.io/en/latest/faq.html')

        matches = fuzzy.extract_matches(query, self.faq_entries, scorer=fuzzy.partial_ratio, score_cutoff=40)
        if len(matches) == 0:
            return await ctx.send('Nothing found...')

        paginator = commands.Paginator(suffix='', prefix='')
        for key, _, value in matches:
            paginator.add_line(f'**{key}**\n{value}')
        page = paginator.pages[0]
        await ctx.send(page, reference=ctx.replied_reference)

    @faq.autocomplete('query')
    async def faq_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not hasattr(self, 'faq_entries'):
            await interaction.response.autocomplete([])
            await self.refresh_faq_cache()
            return []

        if not current:
            choices = [app_commands.Choice(name=key, value=key) for key in self.faq_entries][:10]
            return choices

        matches = fuzzy.extract_matches(current, self.faq_entries, scorer=fuzzy.partial_ratio, score_cutoff=40)[:10]
        return [app_commands.Choice(name=key, value=key) for key, _, _, in matches][:10]

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id not in BOT_LIST_INFO.keys():
            return

        guild_id: int = payload.guild_id  # type: ignore  # It didn't understand the above narrowing
        emoji = str(payload.emoji)
        if emoji not in ('\N{WHITE HEAVY CHECK MARK}', '\N{CROSS MARK}', '\N{NO ENTRY SIGN}'):
            return

        channel_id = BOT_LIST_INFO[guild_id]['channel']
        if payload.channel_id != channel_id:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)

        if not isinstance(channel, (discord.Thread, discord.TextChannel, discord.VoiceChannel)):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (AttributeError, discord.HTTPException):
            return

        if len(message.embeds) != 1:
            return

        embed = message.embeds[0]
        # Already been handled.
        if embed.colour not in (discord.Colour.blurple(), discord.Colour.og_blurple()):
            return

        user = await self.bot.get_or_fetch_member(guild, payload.user_id)
        if user is None or user.bot:
            return

        author_id = int(embed.footer.text or '')
        bot_id = embed.author.name
        if emoji == '\N{WHITE HEAVY CHECK MARK}':
            to_send = f"Your bot, <@{bot_id}>, has been added to {guild.name}."
            colour = discord.Colour.dark_green()
        elif emoji == '\N{NO ENTRY SIGN}':
            to_send = (
                f"Your bot, <@{bot_id}>, could not be added to {guild.name}.\n"
                "This could be because it was private or required code grant. "
                "Please make your bot public and resubmit your application."
            )
            colour = discord.Colour.orange()
        else:
            to_send = f"Your bot, <@{bot_id}>, has been rejected from {guild.name}."
            colour = discord.Colour.dark_magenta()

        member = await self.bot.get_or_fetch_member(guild, author_id)
        try:
            await member.send(to_send)  # type: ignore
        except (AttributeError, discord.HTTPException):
            colour = discord.Colour.gold()

        embed.add_field(name='Responsible Moderator', value=f'{user} (ID: {user.id})', inline=False)
        embed.colour = colour
        message = channel.get_partial_message(payload.message_id)
        await message.edit(embed=embed)

    @commands.command()
    @in_testing()
    async def addbot(self, ctx: GuildContext, user: Annotated[discord.User, BotUser], *, reason: str):
        """Requests your bot to be added to the server.

        To request your bot you must pass your bot's user ID and a reason

        You will get a DM regarding the status of your bot, so make sure you
        have them on.
        """

        info = BOT_LIST_INFO[ctx.guild.id]
        confirm = None

        def terms_acceptance(msg):
            nonlocal confirm
            if msg.author.id != ctx.author.id:
                return False
            if msg.channel.id != ctx.channel.id:
                return False
            if msg.content in ('**I agree**', 'I agree'):
                confirm = True
                return True
            elif msg.content in ('**Abort**', 'Abort'):
                confirm = False
                return True
            return False

        msg = (
            f'{info["terms"]}. Moderators reserve the right to kick or reject your bot for any reason.\n\n'
            'If you agree, reply to this message with **I agree** within 1 minute. If you do not, reply with **Abort**.'
        )
        prompt = await ctx.send(msg)

        try:
            await self.bot.wait_for('message', check=terms_acceptance, timeout=60.0)
        except asyncio.TimeoutError:
            return await ctx.send('Took too long. Aborting.')
        finally:
            await prompt.delete()

        if not confirm:
            return await ctx.send('Aborting.')

        url = f'https://discord.com/oauth2/authorize?client_id={user.id}&scope=bot&guild_id={ctx.guild.id}'
        description = f'{reason}\n\n[Invite URL]({url})'
        embed = discord.Embed(title='Bot Request', colour=discord.Colour.blurple(), description=description)
        embed.add_field(name='Author', value=f'{ctx.author} (ID: {ctx.author.id})', inline=False)
        embed.add_field(name='Bot', value=f'{user} (ID: {user.id})', inline=False)
        embed.timestamp = ctx.message.created_at

        # data for the bot to retrieve later
        embed.set_footer(text=ctx.author.id)
        embed.set_author(name=user.id, icon_url=user.display_avatar.url)

        channel: discord.abc.Messageable = ctx.guild.get_channel(info['channel'])  # type: ignore
        try:
            msg = await channel.send(embed=embed)
            await msg.add_reaction('\N{WHITE HEAVY CHECK MARK}')
            await msg.add_reaction('\N{CROSS MARK}')
            await msg.add_reaction('\N{NO ENTRY SIGN}')
        except discord.HTTPException as e:
            return await ctx.send(f'Failed to request your bot somehow. Tell Danny, {str(e)!r}')

        await ctx.send('Your bot has been requested to the moderators. I will DM you the status of your request.')

    @addbot.error
    async def on_addbot_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            return await ctx.send(str(error))


async def setup(bot: RoboDanny):
    await bot.add_cog(API(bot))
