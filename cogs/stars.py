from __future__ import annotations
from typing import TYPE_CHECKING, Callable, Literal, Optional, Any, Union
from typing_extensions import Annotated

from discord.ext import commands, tasks
from discord import app_commands
from .utils import checks, cache
from .utils.formats import plural
from .utils.paginator import SimplePages

import discord
import datetime
import time
import asyncio
import asyncpg
import logging
import weakref
import re

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import GuildContext

    class StarboardContext(GuildContext):
        starboard: CompleteStarboardConfig

    StarableChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.Thread]


log = logging.getLogger(__name__)


class StarError(commands.CheckFailure):
    pass


def requires_starboard():
    async def predicate(ctx: StarboardContext) -> bool:
        if ctx.guild is None:
            return False

        cog: Stars = ctx.bot.get_cog('Stars')  # type: ignore

        ctx.starboard = await cog.get_starboard(ctx.guild.id)  # type: ignore
        if ctx.starboard.channel is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        return True

    return commands.check(predicate)


def MessageID(argument: str) -> int:
    try:
        return int(argument, base=10)
    except ValueError:
        raise StarError(f'"{argument}" is not a valid message ID. Use Developer Mode to get the Copy ID option.')


class StarboardConfig:
    __slots__ = ('bot', 'id', 'channel_id', 'threshold', 'locked', 'needs_migration', 'max_age')

    def __init__(self, *, guild_id: int, bot: RoboDanny, record: Optional[asyncpg.Record] = None):
        self.id: int = guild_id
        self.bot: RoboDanny = bot

        if record:
            self.channel_id: Optional[int] = record['channel_id']
            self.threshold: int = record['threshold']
            self.locked: bool = record['locked']
            self.needs_migration: bool = self.locked is None
            if self.needs_migration:
                self.locked = True

            self.max_age: datetime.timedelta = record['max_age']
        else:
            self.channel_id = None

    @property
    def channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.channel_id)  # type: ignore


if TYPE_CHECKING:

    class CompleteStarboardConfig(StarboardConfig):
        channel: discord.TextChannel


class Stars(commands.Cog):
    """A starboard to upvote posts obviously.

    There are two ways to make use of this feature, the first is
    via reactions, react to a message with \N{WHITE MEDIUM STAR} and
    the bot will automatically add (or remove) it to the starboard.

    The second way is via Developer Mode. Enable it under Settings >
    Appearance > Developer Mode and then you get access to Copy ID
    and using the star/unstar commands.
    """

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot

        # cache message objects to save Discord some HTTP requests.
        self._message_cache: dict[int, discord.Message] = {}
        self.clean_message_cache.start()
        self._about_to_be_deleted: set[int] = set()
        # guild_ids that need to have their star givers updated at some point
        self._stale_star_givers: set[int] = set()

        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self.spoilers = re.compile(r'\|\|(.+?)\|\|')

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{WHITE MEDIUM STAR}')

    def cog_unload(self):
        self.clean_message_cache.cancel()
        self.update_star_givers.stop()

    async def cog_command_error(self, ctx: StarboardContext, error: commands.CommandError):
        if isinstance(error, StarError):
            await ctx.send(str(error), ephemeral=True)

    @tasks.loop(hours=1.0)
    async def clean_message_cache(self):
        self._message_cache.clear()

    async def _update_star_givers(self) -> None:
        if not self._stale_star_givers:
            return

        query = """
            INSERT INTO star_givers (author_id, guild_id, total)
            SELECT starrers.author_id, entry.guild_id, COUNT(*)
            FROM starrers
            INNER JOIN starboard_entries AS entry ON entry.id=starrers.entry_id
            WHERE entry.guild_id = ANY($1::bigint[])
            GROUP BY starrers.author_id, entry.guild_id
            ON CONFLICT (author_id, guild_id) DO UPDATE SET total=EXCLUDED.total;
        """

        # There's actually asyncpg.protocol.NO_TIMEOUT but it's undocumented so..
        result = await self.bot.pool.execute(query, list(self._stale_star_givers), timeout=600.0)
        log.info('Updated star givers with %r as the result', result)
        self._stale_star_givers.clear()

    @tasks.loop(minutes=30.0)
    async def update_star_givers(self):
        await self._update_star_givers()

    @cache.cache()
    async def get_starboard(
        self, guild_id: int, *, connection: Optional[asyncpg.Pool | asyncpg.Connection] = None
    ) -> StarboardConfig:
        connection = connection or self.bot.pool
        query = "SELECT * FROM starboard WHERE id=$1;"
        record = await connection.fetchrow(query, guild_id)
        return StarboardConfig(guild_id=guild_id, bot=self.bot, record=record)

    def star_emoji(self, stars: int) -> str:
        if 5 > stars >= 0:
            return '\N{WHITE MEDIUM STAR}'
        elif 10 > stars >= 5:
            return '\N{GLOWING STAR}'
        elif 25 > stars >= 10:
            return '\N{DIZZY SYMBOL}'
        else:
            return '\N{SPARKLES}'

    def star_gradient_colour(self, stars: int) -> int:
        # We define as 13 stars to be 100% of the star gradient (half of the 26 emoji threshold)
        # So X / 13 will clamp to our percentage,
        # We start out with 0xfffdf7 for the beginning colour
        # Gradually evolving into 0xffc20c
        # rgb values are (255, 253, 247) -> (255, 194, 12)
        # To create the gradient, we use a linear interpolation formula
        # Which for reference is X = X_1 * p + X_2 * (1 - p)
        p = stars / 13
        if p > 1.0:
            p = 1.0

        red = 255
        green = int((194 * p) + (253 * (1 - p)))
        blue = int((12 * p) + (247 * (1 - p)))
        return (red << 16) + (green << 8) + blue

    def is_url_spoiler(self, text: str, url: str) -> bool:
        spoilers = self.spoilers.findall(text)
        for spoiler in spoilers:
            if url in spoiler:
                return True
        return False

    def get_emoji_message(self, message: discord.Message, stars: int) -> tuple[str, discord.Embed]:
        assert isinstance(message.channel, (discord.abc.GuildChannel, discord.Thread))
        emoji = self.star_emoji(stars)

        if stars > 1:
            content = f'{emoji} **{stars}** {message.channel.mention} ID: {message.id}'
        else:
            content = f'{emoji} {message.channel.mention} ID: {message.id}'

        embed = discord.Embed(description=message.content)
        if message.embeds:
            data = message.embeds[0]
            if data.type == 'image' and data.url and not self.is_url_spoiler(message.content, data.url):
                embed.set_image(url=data.url)

        if message.attachments:
            file = message.attachments[0]
            spoiler = file.is_spoiler()
            if not spoiler and file.filename.lower().endswith(('png', 'jpeg', 'jpg', 'gif', 'webp')):
                embed.set_image(url=file.url)
            elif spoiler:
                embed.add_field(name='Attachment', value=f'||[{file.filename}]({file.url})||', inline=False)
            else:
                embed.add_field(name='Attachment', value=f'[{file.filename}]({file.url})', inline=False)

        ref = message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            embed.add_field(name='Replying to...', value=f'[{ref.resolved.author}]({ref.resolved.jump_url})', inline=False)

        embed.add_field(name='Original', value=f'[Jump!]({message.jump_url})', inline=False)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.timestamp = message.created_at
        embed.colour = self.star_gradient_colour(stars)
        return content, embed

    async def get_message(self, channel: discord.abc.Messageable, message_id: int) -> Optional[discord.Message]:
        try:
            return self._message_cache[message_id]
        except KeyError:
            try:
                msg = await channel.fetch_message(message_id)
            except discord.HTTPException:
                return None
            else:
                self._message_cache[message_id] = msg
                return msg

    async def reaction_action(self, fmt: str, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != '\N{WHITE MEDIUM STAR}':
            return

        guild = self.bot.get_guild(payload.guild_id)  # type: ignore
        if guild is None:
            return

        channel = guild.get_channel_or_thread(payload.channel_id)
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            return

        method = getattr(self, f'{fmt}_message')

        user = payload.member or (await self.bot.get_or_fetch_member(guild, payload.user_id))
        if user is None or user.bot:
            return

        try:
            await method(channel, payload.message_id, payload.user_id, verify=True)
        except StarError:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.TextChannel):
            return

        starboard = await self.get_starboard(channel.guild.id)
        if starboard.channel is None or starboard.channel.id != channel.id:
            return

        # the starboard channel got deleted, so let's clear it from the database.
        async with self.bot.pool.acquire(timeout=300.0) as con:
            query = "DELETE FROM starboard WHERE id=$1;"
            await con.execute(query, channel.guild.id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.reaction_action('star', payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.reaction_action('unstar', payload)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id in self._about_to_be_deleted:
            # we triggered this deletion ourselves and
            # we don't need to drop it from the database
            self._about_to_be_deleted.discard(payload.message_id)
            return

        starboard = await self.get_starboard(payload.guild_id)
        if starboard.channel is None or starboard.channel.id != payload.channel_id:
            return

        # at this point a message got deleted in the starboard
        # so just delete it from the database
        async with self.bot.pool.acquire(timeout=300.0) as con:
            query = "DELETE FROM starboard_entries WHERE bot_message_id=$1;"
            await con.execute(query, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.message_ids <= self._about_to_be_deleted:
            # see comment above
            self._about_to_be_deleted.difference_update(payload.message_ids)
            return

        starboard = await self.get_starboard(payload.guild_id)
        if starboard.channel is None or starboard.channel.id != payload.channel_id:
            return

        async with self.bot.pool.acquire(timeout=300.0) as con:
            query = "DELETE FROM starboard_entries WHERE bot_message_id=ANY($1::bigint[]);"
            await con.execute(query, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)  # type: ignore
        if guild is None:
            return

        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is None or not isinstance(channel, (discord.Thread, discord.TextChannel)):
            return

        async with self.bot.pool.acquire(timeout=300.0) as con:
            starboard = await self.get_starboard(channel.guild.id, connection=con)
            if starboard.channel is None:
                return

            query = "DELETE FROM starboard_entries WHERE message_id=$1 RETURNING bot_message_id;"
            bot_message_id = await con.fetchrow(query, payload.message_id)

            if bot_message_id is None:
                return

            bot_message_id = bot_message_id[0]
            msg = await self.get_message(starboard.channel, bot_message_id)
            if msg is not None:
                await msg.delete()

    async def star_message(
        self,
        channel: StarableChannel,
        message_id: int,
        starrer_id: int,
        *,
        verify: bool = False,
    ) -> None:
        guild_id = channel.guild.id
        lock = self._locks.get(guild_id)
        if lock is None:
            self._locks[guild_id] = lock = asyncio.Lock()

        async with lock:
            async with self.bot.pool.acquire(timeout=300.0) as con:
                if verify:
                    config = self.bot.config_cog
                    if config:
                        plonked = await config.is_plonked(guild_id, starrer_id, channel=channel, connection=con)
                        if plonked:
                            return
                        perms = await config.get_command_permissions(guild_id, connection=con)
                        if perms.is_command_blocked('star', channel.id):
                            return

                await self._star_message(channel, message_id, starrer_id, connection=con)

    async def _star_message(
        self,
        channel: StarableChannel,
        message_id: int,
        starrer_id: int,
        *,
        connection: asyncpg.Connection,
    ) -> None:
        """Stars a message.

        Parameters
        ------------
        channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`Thread`]
            The channel that the starred message belongs to.
        message_id: int
            The message ID of the message being starred.
        starrer_id: int
            The ID of the person who starred this message.
        connection: asyncpg.Connection
            The connection to use.
        """
        record: Any
        guild_id = channel.guild.id
        starboard = await self.get_starboard(guild_id)
        starboard_channel = starboard.channel
        if starboard_channel is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        if starboard.locked:
            raise StarError('\N{NO ENTRY SIGN} Starboard is locked.')

        if channel.is_nsfw() and not starboard_channel.is_nsfw():
            raise StarError('\N{NO ENTRY SIGN} Cannot star NSFW in non-NSFW starboard channel.')

        if channel.id == starboard_channel.id:
            # special case redirection code goes here
            # ergo, when we add a reaction from starboard we want it to star
            # the original message

            query = "SELECT channel_id, message_id FROM starboard_entries WHERE bot_message_id=$1;"
            record = await connection.fetchrow(query, message_id)
            if record is None:
                raise StarError('Could not find message in the starboard.')

            ch = channel.guild.get_channel_or_thread(record['channel_id'])
            if ch is None:
                raise StarError('Could not find original channel.')

            return await self._star_message(ch, record['message_id'], starrer_id, connection=connection)  # type: ignore

        if not starboard_channel.permissions_for(starboard_channel.guild.me).send_messages:
            raise StarError('\N{NO ENTRY SIGN} Cannot post messages in starboard channel.')

        msg = await self.get_message(channel, message_id)

        if msg is None:
            raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')

        if msg.author.id == starrer_id:
            raise StarError('\N{NO ENTRY SIGN} You cannot star your own message.')

        empty_message = len(msg.content) == 0 and len(msg.attachments) == 0
        if empty_message or msg.type not in (discord.MessageType.default, discord.MessageType.reply):
            raise StarError('\N{NO ENTRY SIGN} This message cannot be starred.')

        oldest_allowed = discord.utils.utcnow() - starboard.max_age
        if msg.created_at < oldest_allowed:
            raise StarError('\N{NO ENTRY SIGN} This message is too old.')

        # check if this is freshly starred
        # originally this was a single query but it seems
        # WHERE ... = (SELECT ... in some_cte) is bugged
        # so I'm going to do two queries instead
        query = """WITH to_insert AS (
                       INSERT INTO starboard_entries AS entries (message_id, channel_id, guild_id, author_id)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (message_id) DO NOTHING
                       RETURNING entries.id
                   )
                   INSERT INTO starrers (author_id, entry_id)
                   SELECT $5, entry.id
                   FROM (
                       SELECT id FROM to_insert
                       UNION ALL
                       SELECT id FROM starboard_entries WHERE message_id=$1
                       LIMIT 1
                   ) AS entry
                   RETURNING entry_id;
                """

        try:
            record = await connection.fetchrow(
                query,
                message_id,
                channel.id,
                guild_id,
                msg.author.id,
                starrer_id,
            )
        except asyncpg.UniqueViolationError:
            raise StarError('\N{NO ENTRY SIGN} You already starred this message.')

        entry_id = record[0]

        query = "SELECT COUNT(*) FROM starrers WHERE entry_id=$1;"
        record = await connection.fetchrow(query, entry_id)

        self._stale_star_givers.add(guild_id)

        count = record[0]
        if count < starboard.threshold:
            # Unfortunately we have to track the count here too...
            # At least it should be "fast"
            await connection.execute("UPDATE starboard_entries SET total=$1 WHERE id=$2;", count, entry_id)
            return

        # at this point, we either edit the message or we create a message
        # with our star info
        content, embed = self.get_emoji_message(msg, count)

        # get the message ID to edit:
        query = "SELECT bot_message_id FROM starboard_entries WHERE id=$1;"
        record = await connection.fetchrow(query, entry_id)
        bot_message_id = record[0]

        if bot_message_id is None:
            new_msg = await starboard_channel.send(content, embed=embed)
            query = "UPDATE starboard_entries SET bot_message_id=$1, total=$2 WHERE id=$3;"
            await connection.execute(query, new_msg.id, count, entry_id)
        else:
            new_msg = await self.get_message(starboard_channel, bot_message_id)
            if new_msg is None:
                # deleted? might as well purge the data
                query = "DELETE FROM starboard_entries WHERE id=$1;"
                await connection.execute(query, entry_id)
            else:
                query = "UPDATE starboard_entries SET total=$1 WHERE id=$2;"
                await connection.execute(query, count, entry_id)
                await new_msg.edit(content=content, embed=embed)

    async def unstar_message(
        self,
        channel: StarableChannel,
        message_id: int,
        starrer_id: int,
        *,
        verify: bool = False,
    ) -> None:
        guild_id = channel.guild.id
        lock = self._locks.get(guild_id)
        if lock is None:
            self._locks[guild_id] = lock = asyncio.Lock()

        async with lock:
            async with self.bot.pool.acquire(timeout=300.0) as con:
                if verify:
                    config = self.bot.config_cog
                    if config:
                        plonked = await config.is_plonked(guild_id, starrer_id, channel=channel, connection=con)
                        if plonked:
                            return
                        perms = await config.get_command_permissions(guild_id, connection=con)
                        if perms.is_command_blocked('star', channel.id):
                            return

                await self._unstar_message(channel, message_id, starrer_id, connection=con)

    async def _unstar_message(
        self,
        channel: StarableChannel,
        message_id: int,
        starrer_id: int,
        *,
        connection: asyncpg.Connection,
    ) -> None:
        """Unstars a message.

        Parameters
        ------------
        channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`Thread`]
            The channel that the starred message belongs to.
        message_id: int
            The message ID of the message being unstarred.
        starrer_id: int
            The ID of the person who unstarred this message.
        connection: asyncpg.Connection
            The connection to use.
        """
        record: Any
        guild_id = channel.guild.id
        starboard = await self.get_starboard(guild_id)
        starboard_channel = starboard.channel
        if starboard_channel is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        if starboard.locked:
            raise StarError('\N{NO ENTRY SIGN} Starboard is locked.')

        if channel.id == starboard_channel.id:
            query = "SELECT channel_id, message_id FROM starboard_entries WHERE bot_message_id=$1;"
            record = await connection.fetchrow(query, message_id)
            if record is None:
                raise StarError('Could not find message in the starboard.')

            ch = channel.guild.get_channel_or_thread(record['channel_id'])
            if ch is None:
                raise StarError('Could not find original channel.')

            return await self._unstar_message(ch, record['message_id'], starrer_id, connection=connection)  # type: ignore

        if not starboard_channel.permissions_for(starboard_channel.guild.me).send_messages:
            raise StarError('\N{NO ENTRY SIGN} Cannot edit messages in starboard channel.')

        query = """DELETE FROM starrers USING starboard_entries entry
                   WHERE entry.message_id=$1
                   AND   entry.id=starrers.entry_id
                   AND   starrers.author_id=$2
                   RETURNING starrers.entry_id, entry.bot_message_id
                """

        record = await connection.fetchrow(query, message_id, starrer_id)
        if record is None:
            raise StarError('\N{NO ENTRY SIGN} You have not starred this message.')

        entry_id = record[0]
        bot_message_id = record[1]

        query = "SELECT COUNT(*) FROM starrers WHERE entry_id=$1;"
        record = await connection.fetchrow(query, entry_id)
        self._stale_star_givers.add(guild_id)
        count = record[0]

        if count == 0:
            # delete the entry if we have no more stars
            query = "DELETE FROM starboard_entries WHERE id=$1;"
            await connection.execute(query, entry_id)

        if bot_message_id is None:
            return

        bot_message = await self.get_message(starboard_channel, bot_message_id)
        if bot_message is None:
            return

        if count < starboard.threshold:
            self._about_to_be_deleted.add(bot_message_id)
            if count:
                # update the bot_message_id to be NULL in the table since we're deleting it
                query = "UPDATE starboard_entries SET bot_message_id=NULL, total=$1 WHERE id=$2;"
                await connection.execute(query, count, entry_id)

            await bot_message.delete()
        else:
            query = "UPDATE starboard_entries SET total=$1 WHERE id=$2;"
            await connection.execute(query, count, entry_id)

            msg = await self.get_message(channel, message_id)
            if msg is None:
                raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')

            content, embed = self.get_emoji_message(msg, count)
            await bot_message.edit(content=content, embed=embed)

    @commands.hybrid_group(fallback='create')
    @checks.is_manager()
    @app_commands.describe(name='The starboard channel name')
    async def starboard(self, ctx: GuildContext, *, name: str = 'starboard'):
        """Sets up the starboard for this server.

        This creates a new channel with the specified name
        and makes it into the server's "starboard". If no
        name is passed in then it defaults to "starboard".

        You must have Manage Server permission to use this.
        """

        await ctx.defer()

        # bypass the cache just in case someone used the star
        # reaction earlier before having it set up, or they
        # decided to use the ?star command
        self.get_starboard.invalidate(self, ctx.guild.id)

        starboard = await self.get_starboard(ctx.guild.id)
        if starboard.channel is not None:
            setattr(ctx, 'starboard', starboard)
            return await self.starboard_info(ctx)

        if hasattr(starboard, 'locked'):
            try:
                confirm = await ctx.prompt(
                    'Apparently, a previously configured starboard channel was deleted. Is this true?'
                )
            except RuntimeError as e:
                await ctx.send(str(e))
            else:
                if confirm:
                    await ctx.db.execute('DELETE FROM starboard WHERE id=$1;', ctx.guild.id)
                else:
                    return await ctx.send('Aborting starboard creation. Join the bot support server for more questions.')

        perms = ctx.channel.permissions_for(ctx.me)

        if not perms.manage_roles or not perms.manage_channels:
            return await ctx.send('\N{NO ENTRY SIGN} I do not have proper permissions (Manage Roles and Manage Channel)')

        overwrites = {
            ctx.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True, embed_links=True, read_message_history=True
            ),
            ctx.guild.default_role: discord.PermissionOverwrite(
                read_messages=True, send_messages=False, read_message_history=True
            ),
        }

        reason = f'{ctx.author} (ID: {ctx.author.id}) has created the starboard channel.'

        try:
            channel = await ctx.guild.create_text_channel(name=name, overwrites=overwrites, reason=reason)
        except discord.Forbidden:
            return await ctx.send('\N{NO ENTRY SIGN} I do not have permissions to create a channel.')
        except discord.HTTPException:
            return await ctx.send('\N{NO ENTRY SIGN} This channel name is bad or an unknown error happened.')

        query = "INSERT INTO starboard (id, channel_id) VALUES ($1, $2);"
        try:
            await ctx.db.execute(query, ctx.guild.id, channel.id)
        except:
            await channel.delete(reason='Failure to commit to create the ')
            await ctx.send('Could not create the channel due to an internal error. Join the bot support server for help.')
        else:
            self.get_starboard.invalidate(self, ctx.guild.id)
            await ctx.send(f'\N{GLOWING STAR} Starboard created at {channel.mention}.')

    @starboard.command(name='info')
    @requires_starboard()
    async def starboard_info(self, ctx: StarboardContext):
        """Shows meta information about the starboard."""
        starboard = ctx.starboard
        channel = starboard.channel
        data = []

        if channel is None:
            data.append('Channel: #deleted-channel')
        else:
            data.append(f'Channel: {channel.mention}')
            data.append(f'NSFW: {channel.is_nsfw()}')

        data.append(f'Locked: {starboard.locked}')
        data.append(f'Limit: {plural(starboard.threshold):star}')
        data.append(f'Max Age: {plural(starboard.max_age.days):day}')
        await ctx.send('\n'.join(data))

    @commands.hybrid_group(fallback='post', ignore_extra=False)
    @commands.guild_only()
    @app_commands.describe(message='The message ID to star')
    async def star(self, ctx: GuildContext, message: Annotated[int, MessageID]):
        """Stars a message via message ID.

        To star a message you should right click on the on a message and then
        click "Copy ID". You must have Developer Mode enabled to get that
        functionality.

        It is recommended that you react to a message with \N{WHITE MEDIUM STAR} instead.

        You can only star a message once.
        """
        await ctx.defer(ephemeral=True)
        try:
            await self.star_message(ctx.channel, message, ctx.author.id)
        except StarError as e:
            await ctx.send(str(e), ephemeral=True)
        else:
            if ctx.interaction is None:
                await ctx.message.delete()
            else:
                await ctx.send('Successfully starred message', ephemeral=True)

    @commands.command()
    @commands.guild_only()
    @app_commands.describe(message='The message ID to remove a star from')
    async def unstar(self, ctx: GuildContext, message: Annotated[int, MessageID]):
        """Unstars a message via message ID.

        To unstar a message you should right click on the on a message and then
        click "Copy ID". You must have Developer Mode enabled to get that
        functionality.
        """
        await ctx.defer(ephemeral=True)
        try:
            await self.unstar_message(ctx.channel, message, ctx.author.id, verify=True)
        except StarError as e:
            return await ctx.send(str(e), ephemeral=True)
        else:
            if ctx.interaction is None:
                await ctx.message.delete()
            else:
                await ctx.send('Successfully unstarred message', ephemeral=True)

    @star.command(name='clean')
    @checks.is_manager()
    @requires_starboard()
    @app_commands.describe(stars='Remove messages that have less than or equal to this number')
    async def star_clean(self, ctx: StarboardContext, stars: commands.Range[int, 1, None] = 1):
        """Cleans the starboard

        This removes messages in the starboard that only have less
        than or equal to the number of specified stars. This defaults to 1.

        Note that this only checks the last 100 messages in the starboard.

        This command requires the Manage Server permission.
        """

        await ctx.defer()
        stars = max(stars, 1)
        channel = ctx.starboard.channel

        last_messages = [m.id async for m in channel.history(limit=100)]

        query = """WITH bad_entries AS (
                       SELECT entry_id
                       FROM starrers
                       INNER JOIN starboard_entries
                       ON starboard_entries.id = starrers.entry_id
                       WHERE starboard_entries.guild_id=$1
                       AND   starboard_entries.bot_message_id = ANY($2::bigint[])
                       GROUP BY entry_id
                       HAVING COUNT(*) <= $3
                   )
                   DELETE FROM starboard_entries USING bad_entries
                   WHERE starboard_entries.id = bad_entries.entry_id
                   RETURNING starboard_entries.bot_message_id
                """

        to_delete = await ctx.db.fetch(query, ctx.guild.id, last_messages, stars)

        # we cannot bulk delete entries over 14 days old
        min_snowflake = int((time.time() - 14 * 24 * 60 * 60) * 1000.0 - 1420070400000) << 22
        to_delete = [discord.Object(id=r[0]) for r in to_delete if r[0] > min_snowflake]

        try:
            self._about_to_be_deleted.update(o.id for o in to_delete)
            await channel.delete_messages(to_delete)
        except discord.HTTPException:
            await ctx.send('Could not delete messages.')
        else:
            await ctx.send(f'\N{PUT LITTER IN ITS PLACE SYMBOL} Deleted {plural(len(to_delete)):message}.')

    @star.command(name='show')
    @requires_starboard()
    @app_commands.describe(message='The message ID to show star information of')
    async def star_show(self, ctx: StarboardContext, message: Annotated[int, MessageID]):
        """Shows a starred message via its ID.

        To get the ID of a message you should right click on the
        message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        You can only use this command once per 10 seconds.
        """

        await ctx.defer()
        query = """SELECT entry.channel_id,
                          entry.message_id,
                          entry.bot_message_id,
                          COUNT(*) OVER(PARTITION BY entry_id) AS "Stars"
                   FROM starrers
                   INNER JOIN starboard_entries entry
                   ON entry.id = starrers.entry_id
                   WHERE entry.guild_id=$1
                   AND (entry.message_id=$2 OR entry.bot_message_id=$2)
                   LIMIT 1
                """

        record = await ctx.db.fetchrow(query, ctx.guild.id, message)
        if record is None:
            return await ctx.send('This message has not been starred.')

        bot_message_id = record['bot_message_id']
        if bot_message_id is not None:
            # "fast" path, just redirect the message
            msg = await self.get_message(ctx.starboard.channel, bot_message_id)
            if msg is not None:
                embed = msg.embeds[0] if msg.embeds else None
                return await ctx.send(msg.content, embed=embed)
            else:
                # somehow it got deleted, so just delete the entry
                query = "DELETE FROM starboard_entries WHERE message_id=$1;"
                await ctx.db.execute(query, record['message_id'])
                return

        # slow path, try to fetch the content
        channel: Optional[discord.abc.Messageable] = ctx.guild.get_channel_or_thread(record['channel_id'])  # type: ignore
        if channel is None:
            return await ctx.send("The message's channel has been deleted.")

        msg = await self.get_message(channel, record['message_id'])
        if msg is None:
            return await ctx.send('The message has been deleted.')

        content, embed = self.get_emoji_message(msg, record['Stars'])
        await ctx.send(content, embed=embed)

    @star.command(name='who')
    @requires_starboard()
    @app_commands.describe(message='The message ID to show starrer information of')
    async def star_who(self, ctx: StarboardContext, message: Annotated[int, MessageID]):
        """Show who starred a message.

        The ID can either be the starred message ID
        or the message ID in the starboard channel.
        """

        await ctx.defer()
        query = """SELECT starrers.author_id
                   FROM starrers
                   INNER JOIN starboard_entries entry
                   ON entry.id = starrers.entry_id
                   WHERE entry.message_id = $1 OR entry.bot_message_id = $1
                """

        records = await ctx.db.fetch(query, message)
        if records is None or len(records) == 0:
            return await ctx.send('No one starred this message or this is an invalid message ID.')

        records = [r[0] for r in records]
        members = [str(member) async for member in self.bot.resolve_member_ids(ctx.guild, records)]

        p = SimplePages(entries=members, per_page=20, ctx=ctx)
        base = format(plural(len(records)), 'star')
        if len(records) > len(members):
            p.embed.title = f'{base} ({len(records) - len(members)} left server)'
        else:
            p.embed.title = base

        await p.start()

    @star.command(name='migrate', with_app_command=False)
    @requires_starboard()
    @checks.is_manager()
    async def star_migrate(self, ctx: StarboardContext):
        """Migrates the starboard to the newest version.

        While doing this, the starboard is locked.

        Note: This is an **incredibly expensive operation**.

        It will take a very long time.

        You must have Manage Server permissions to use this.
        """

        perms = ctx.starboard.channel.permissions_for(ctx.me)
        if not perms.read_message_history:
            return await ctx.send(f'Bot does not have Read Message History in {ctx.starboard.channel.mention}.')

        if ctx.starboard.locked:
            return await ctx.send('Starboard must be unlocked to migrate. It will be locked during the migration.')

        webhook = self.bot.stats_webhook

        start = time.time()
        guild_id = ctx.guild.id
        query = "UPDATE starboard SET locked=TRUE WHERE id=$1;"
        await ctx.db.execute(query, guild_id)
        self.get_starboard.invalidate(self, guild_id)

        await ctx.send('Starboard is now locked and migration will now begin.')

        valid_msg = re.compile(r'.+?<#(?P<channel_id>[0-9]{17,21})>\s*ID\:\s*(?P<message_id>[0-9]{17,21})')
        async with ctx.typing():
            fetched = 0
            updated = 0
            failed = 0

            # At the time of writing, the average server only had ~256 entries.
            async for message in ctx.starboard.channel.history(limit=1000):
                fetched += 1

                match = valid_msg.match(message.content)
                if match is None:
                    continue

                groups = match.groupdict()
                groups['guild_id'] = guild_id
                fmt = 'https://discord.com/channels/{guild_id}/{channel_id}/{message_id}'.format(**groups)
                if len(message.embeds) == 0:
                    continue

                embed = message.embeds[0]
                if len(embed.fields) == 0 or embed.fields[0].name == 'Attachments':
                    embed.add_field(name='Original', value=f'[Jump!]({fmt})', inline=False)
                    try:
                        await message.edit(embed=embed)
                    except discord.HTTPException:
                        failed += 1
                    else:
                        updated += 1

            delta = time.time() - start
            query = "UPDATE starboard SET locked = FALSE WHERE id=$1;"
            await ctx.db.execute(query, guild_id)
            self.get_starboard.invalidate(self, guild_id)

            m = await ctx.send(
                f'{ctx.author.mention}, we are done migrating!\n'
                'The starboard has been unlocked.\n'
                f'Updated {updated}/{fetched} entries to the new format.\n'
                f'Took {delta:.2f}s.'
            )

            e = discord.Embed(title='Starboard Migration', colour=discord.Colour.gold())
            e.add_field(name='Updated', value=updated)
            e.add_field(name='Fetched', value=fetched)
            e.add_field(name='Failed', value=failed)
            e.add_field(name='Name', value=ctx.guild.name)
            e.add_field(name='ID', value=guild_id)
            e.set_footer(text=f'Took {delta:.2f}s to migrate')
            e.timestamp = m.created_at
            await webhook.send(embed=e)

    def records_to_value(self, records: list[Any], fmt: Callable[[Any], str], default: str = 'None!') -> str:
        if not records:
            return default

        emoji_lookup = lambda i: '\N{SPORTS MEDAL}' if i >= 3 else chr(0x1F947 + i)  # :first_place:
        return '\n'.join(f'{emoji_lookup(i)}: {fmt(r)}' for i, r in enumerate(records))

    async def star_guild_stats(self, ctx: StarboardContext):
        e = discord.Embed()
        e.timestamp = ctx.starboard.channel.created_at
        e.set_footer(text='Adding stars since')
        e.set_author(name='Server Starboard Stats')
        query = "SELECT COUNT(*), SUM(total) FROM starboard_entries WHERE guild_id=$1;"
        record: Optional[tuple[int, int]] = await ctx.db.fetchrow(query, ctx.guild.id)
        assert record is not None
        total_messages, total_stars = record

        e.colour = discord.Colour.gold()

        query = """
            SELECT message_id, channel_id, total
            FROM starboard_entries
            WHERE bot_message_id IS NOT NULL AND guild_id=$1
            ORDER BY total DESC
            LIMIT 10;
        """
        top_posts = await ctx.db.fetch(query, ctx.guild.id)
        record_to_url = lambda r: f'https://discord.com/channels/{ctx.guild.id}/{r[1]}/{r[0]}'
        fmt = lambda r, url=record_to_url: f'[{r[0]}]({url(r)}) ({plural(r[2]):star})'
        e.title = 'Top Starred Posts'
        e.description = self.records_to_value(top_posts, fmt)

        query = """
            SELECT author_id, SUM(total)
            FROM starboard_entries
            WHERE author_id IS NOT NULL AND guild_id=$1
            GROUP BY author_id
            ORDER BY 2 DESC
            LIMIT 5;
        """
        to_mention = lambda r: f'<@{r[0]}> ({plural(r[1]):star})'
        top_star_receivers = await ctx.db.fetch(query, ctx.guild.id)
        e.add_field(
            name='Top Star Receivers',
            value=self.records_to_value(top_star_receivers, to_mention, default='No one!'),
            inline=False,
        )

        query = """
            SELECT author_id, total
            FROM star_givers
            WHERE guild_id=$1
            ORDER BY 2 DESC
            LIMIT 5;
        """
        top_givers = await ctx.db.fetch(query, ctx.guild.id)
        e.add_field(
            name='Top Star Givers',
            value=self.records_to_value(top_givers, to_mention, default='No one!'),
            inline=False,
        )

        # e.description = f'{plural(total_messages):message} starred with a total of {total_stars} stars.'
        e.add_field(name='Messages Starred', value=str(total_messages))
        e.add_field(name='Total Stars Given', value=str(total_stars))

        await ctx.send(embed=e)

    async def star_member_stats(self, ctx: StarboardContext, member: discord.Member):
        e = discord.Embed(colour=discord.Colour.gold())
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)

        # this query calculates
        # 1 - stars received,
        # 2 - stars given
        # The rest are the top 3 starred posts

        # Gets stars received
        query = "SELECT SUM(total) FROM starboard_entries WHERE guild_id=$1 AND author_id=$2;"
        record: Optional[tuple[int]] = await ctx.db.fetchrow(query, ctx.guild.id, member.id)
        assert record is not None
        received = record[0]

        # Gets stars given
        query = "SELECT total FROM star_givers WHERE guild_id=$1 AND author_id=$2;"
        record: Optional[tuple[int]] = await ctx.db.fetchrow(query, ctx.guild.id, member.id)
        given = record[0] if record is not None else 0

        # Gets the top 10 starred posts
        query = """
            SELECT message_id, channel_id, total
            FROM starboard_entries
            WHERE bot_message_id IS NOT NULL AND guild_id=$1 AND author_id=$2
            ORDER BY total DESC
            LIMIT 10;
        """
        records: list[tuple[int, int, int]] = await ctx.db.fetch(query, ctx.guild.id, member.id)
        record_to_url = lambda r: f'https://discord.com/channels/{ctx.guild.id}/{r[1]}/{r[0]}'
        fmt = lambda r, url=record_to_url: f'[{r[0]}]({url(r)}) ({plural(r[2]):star})'
        e.title = 'Top Starred Posts'
        e.description = self.records_to_value(records, fmt)

        # this query calculates how many of our messages were starred
        query = """SELECT COUNT(*) FROM starboard_entries WHERE guild_id=$1 AND author_id=$2;"""
        record: Optional[tuple[int]] = await ctx.db.fetchrow(query, ctx.guild.id, member.id)
        assert record is not None
        messages_starred = record[0]

        e.add_field(name='Messages Starred', value=messages_starred)
        e.add_field(name='Stars Received', value=received)
        e.add_field(name='Stars Given', value=given)

        await ctx.send(embed=e)

    @star.command(name='stats')
    @requires_starboard()
    @app_commands.describe(member='The member to show stats of, if not given then shows server stats')
    async def star_stats(self, ctx: StarboardContext, *, member: discord.Member = None):
        """Shows statistics on the starboard usage of the server or a member."""

        await ctx.defer()
        # Queue the guild for an update
        self._stale_star_givers.add(ctx.guild.id)

        if member is None:
            await self.star_guild_stats(ctx)
        else:
            await self.star_member_stats(ctx, member)

    @star.command(name='random')
    @requires_starboard()
    @app_commands.describe(member='The member to show random stars of, if not given then shows a random star in the server')
    async def star_random(self, ctx: StarboardContext, member: discord.User = None):
        """Shows a random starred message."""

        await ctx.defer()
        where_condition = 'WHERE guild_id=$1 AND bot_message_id IS NOT NULL'
        args: list[int] = [ctx.guild.id]
        if member is not None:
            where_condition += ' AND author_id=$2'
            args.append(member.id)

        query = f"""SELECT bot_message_id
                    FROM starboard_entries
                    {where_condition}
                    OFFSET FLOOR(RANDOM() * (
                        SELECT COUNT(*)
                        FROM starboard_entries
                        {where_condition}
                    ))
                    LIMIT 1
                """
        record = await ctx.db.fetchrow(query, *args)
        if record is None:
            return await ctx.send('Could not find anything.')

        message_id = record[0]
        message = await self.get_message(ctx.starboard.channel, message_id)
        if message is None:
            return await ctx.send(f'Message {message_id} has been deleted somehow.')

        if message.embeds:
            await ctx.send(message.content, embed=message.embeds[0])
        else:
            await ctx.send(message.content)

    @star_random.error
    async def star_random_error(self, ctx: StarboardContext, error: commands.CommandError):
        if isinstance(error, commands.UserNotFound):
            return await ctx.send('Could not find that member.')

    @star.command(name='lock')
    @checks.is_manager()
    @requires_starboard()
    async def star_lock(self, ctx: StarboardContext):
        """Locks the starboard from being processed.

        This is a moderation tool that allows you to temporarily
        disable the starboard to aid in dealing with star spam.

        When the starboard is locked, no new entries are added to
        the starboard as the bot will no longer listen to reactions or
        star/unstar commands.

        To unlock the starboard, use the unlock subcommand.

        To use this command you need Manage Server permission.
        """

        if ctx.starboard.needs_migration:
            return await ctx.send('Your starboard requires migration!')

        query = "UPDATE starboard SET locked=TRUE WHERE id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        self.get_starboard.invalidate(self, ctx.guild.id)

        await ctx.send('Starboard is now locked.')

    @star.command(name='unlock')
    @checks.is_manager()
    @requires_starboard()
    async def star_unlock(self, ctx: StarboardContext):
        """Unlocks the starboard for re-processing.

        To use this command you need Manage Server permission.
        """

        if ctx.starboard.needs_migration:
            return await ctx.send('Your starboard requires migration!')

        query = "UPDATE starboard SET locked=FALSE WHERE id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        self.get_starboard.invalidate(self, ctx.guild.id)

        await ctx.send('Starboard is now unlocked.')

    @star.command(name='limit', aliases=['threshold'])
    @checks.is_manager()
    @requires_starboard()
    @app_commands.describe(stars='The number of stars required before it shows up on the board')
    async def star_limit(self, ctx: StarboardContext, stars: int):
        """Sets the minimum number of stars required to show up.

        When this limit is set, messages must have this number
        or more to show up in the starboard channel.

        You cannot have a negative number and the maximum
        star limit you can set is 100.

        Note that messages that previously did not meet the
        limit but now do will still not show up in the starboard
        until starred again.

        You must have Manage Server permissions to use this.
        """

        if ctx.starboard.needs_migration:
            return await ctx.send('Your starboard requires migration!')

        stars = min(max(stars, 1), 100)
        query = "UPDATE starboard SET threshold=$2 WHERE id=$1;"
        await ctx.db.execute(query, ctx.guild.id, stars)
        self.get_starboard.invalidate(self, ctx.guild.id)

        await ctx.send(f'Messages now require {plural(stars):star} to show up in the starboard.')

    @star.command(name='age')
    @checks.is_manager()
    @requires_starboard()
    @app_commands.describe(
        number='The number of units to set the maximum age to',
        units='The unit of time to use for the number',
    )
    @app_commands.choices(
        units=[
            app_commands.Choice(name='Days', value='days'),
            app_commands.Choice(name='Weeks', value='weeks'),
            app_commands.Choice(name='Months', value='months'),
            app_commands.Choice(name='Years', value='years'),
        ]
    )
    async def star_age(
        self,
        ctx: StarboardContext,
        number: int,
        units: Literal['days', 'weeks', 'months', 'years', 'day', 'week', 'month', 'year'] = 'days',
    ):
        """Sets the maximum age of a message valid for starring.

        By default, the maximum age is 7 days. Any message older
        than this specified age is invalid of being starred.

        To set the limit you must specify a number followed by
        a unit. The valid units are "days", "weeks", "months",
        or "years". They do not have to be pluralized. The
        default unit is "days".

        The number cannot be negative, and it must be a maximum
        of 35. If the unit is years then the cap is 10 years.

        You cannot mix and match units.

        You must have Manage Server permissions to use this.
        """

        if units[-1] != 's':
            units = units + 's'  # type: ignore

        number = min(max(number, 1), 35)

        if units == 'years' and number > 10:
            return await ctx.send('The maximum is 10 years!')

        # the input is sanitised so this should be ok
        # only doing this because asyncpg requires a timedelta object but
        # generating that with these clamp units is overkill
        query = f"UPDATE starboard SET max_age='{number} {units}'::interval WHERE id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        self.get_starboard.invalidate(self, ctx.guild.id)

        if number == 1:
            age = f'1 {units[:-1]}'
        else:
            age = f'{number} {units}'

        await ctx.send(f'Messages must now be less than {age} old to be starred.')

    @star.command(hidden=True, with_app_command=False)
    @commands.is_owner()
    async def star_update_givers(self, ctx: GuildContext, guild_id: int = None):
        """Updates the star givers for all guilds."""

        if guild_id is None:
            args = ()
            subclause = ''
        else:
            args = (guild_id,)
            subclause = 'WHERE entry.guild_id=$1'

        query = f"""
            INSERT INTO star_givers (author_id, guild_id, total)
            SELECT starrers.author_id, entry.guild_id, COUNT(*)
            FROM starrers
            INNER JOIN starboard_entries entry ON entry.id = starrers.entry_id
            {subclause}
            GROUP BY starrers.author_id, entry.guild_id
            ON CONFLICT (author_id, guild_id) DO UPDATE SET total = EXCLUDED.total;
        """

        async with ctx.typing():
            status = await ctx.db.execute(query, *args, timeout=asyncpg.protocol.NO_TIMEOUT)

        await ctx.send(f'Done updating, {status!r}')

    @commands.command(hidden=True, with_app_command=False)
    @commands.is_owner()
    async def star_announce(self, ctx: GuildContext, *, message: str):
        """Announce stuff to every starboard."""
        query = "SELECT id, channel_id FROM starboard;"
        records = await ctx.db.fetch(query)

        to_send = []
        for guild_id, channel_id in records:
            guild = self.bot.get_guild(guild_id)
            if guild:
                channel = guild.get_channel(channel_id)
                if channel and channel.permissions_for(guild.me).send_messages:
                    to_send.append(channel)

        await ctx.send(f'Preparing to send to {len(to_send)} channels (out of {len(records)}).')

        success = 0
        start = time.time()
        for index, channel in enumerate(to_send):
            if index % 5 == 0:
                await asyncio.sleep(1)

            try:
                await channel.send(message)
            except:
                pass
            else:
                success += 1

        delta = time.time() - start
        await ctx.send(f'Successfully sent to {success} channels (out of {len(to_send)}) in {delta:.2f}s.')


async def setup(bot: RoboDanny):
    await bot.add_cog(Stars(bot))
