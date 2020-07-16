from discord.ext import commands, tasks
from .utils import checks, db, cache
from .utils.formats import plural, human_join
from .utils.paginator import Pages
from collections import Counter, defaultdict

import discord
import datetime
import time
import json
import random
import asyncio
import asyncpg
import logging
import weakref
import re

log = logging.getLogger(__name__)

class StarError(commands.CheckFailure):
    pass

def requires_starboard():
    async def predicate(ctx):
        if ctx.guild is None:
            return False

        cog = ctx.bot.get_cog('Stars')

        ctx.starboard = await cog.get_starboard(ctx.guild.id, connection=ctx.db)
        if ctx.starboard.channel is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        return True
    return commands.check(predicate)

def MessageID(argument):
    try:
        return int(argument, base=10)
    except ValueError:
        raise StarError(f'"{argument}" is not a valid message ID. Use Developer Mode to get the Copy ID option.')

class Starboard(db.Table):
    id = db.Column(db.Integer(big=True), primary_key=True)

    channel_id = db.Column(db.Integer(big=True))
    threshold = db.Column(db.Integer, default=1, nullable=False)
    locked = db.Column(db.Boolean, default=False)
    max_age = db.Column(db.Interval, default="'7 days'::interval", nullable=False)

class StarboardEntry(db.Table, table_name='starboard_entries'):
    id = db.PrimaryKeyColumn()

    bot_message_id = db.Column(db.Integer(big=True), index=True)
    message_id = db.Column(db.Integer(big=True), index=True, unique=True, nullable=False)
    channel_id = db.Column(db.Integer(big=True))
    author_id = db.Column(db.Integer(big=True))
    guild_id = db.Column(db.ForeignKey('starboard', 'id', sql_type=db.Integer(big=True)), index=True, nullable=False)

class Starrers(db.Table):
    id = db.PrimaryKeyColumn()
    author_id = db.Column(db.Integer(big=True), nullable=False)
    entry_id = db.Column(db.ForeignKey('starboard_entries', 'id'), index=True, nullable=False)

    @classmethod
    def create_table(cls, *, exists_ok=True):
        statement = super().create_table(exists_ok=exists_ok)
        sql = "CREATE UNIQUE INDEX IF NOT EXISTS starrers_uniq_idx ON starrers (author_id, entry_id);"
        return statement + '\n' + sql

class StarboardConfig:
    __slots__ = ('bot', 'id', 'channel_id', 'threshold', 'locked', 'needs_migration', 'max_age')

    def __init__(self, *, guild_id, bot, record=None):
        self.id = guild_id
        self.bot = bot

        if record:
            self.channel_id = record['channel_id']
            self.threshold = record['threshold']
            self.locked = record['locked']
            self.needs_migration = self.locked is None
            if self.needs_migration:
                self.locked = True

            self.max_age = record['max_age']
        else:
            self.channel_id = None

    @property
    def channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.channel_id)

class Stars(commands.Cog):
    """A starboard to upvote posts obviously.

    There are two ways to make use of this feature, the first is
    via reactions, react to a message with \N{WHITE MEDIUM STAR} and
    the bot will automatically add (or remove) it to the starboard.

    The second way is via Developer Mode. Enable it under Settings >
    Appearance > Developer Mode and then you get access to Copy ID
    and using the star/unstar commands.
    """

    def __init__(self, bot):
        self.bot = bot

        # cache message objects to save Discord some HTTP requests.
        self._message_cache = {}
        self.clean_message_cache.start()

        # if it's in this set,
        self._about_to_be_deleted = set()

        self._locks = weakref.WeakValueDictionary()
        self.spoilers = re.compile(r'\|\|(.+?)\|\|')

    def cog_unload(self):
        self.clean_message_cache.cancel()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, StarError):
            await ctx.send(error)

    @tasks.loop(hours=1.0)
    async def clean_message_cache(self):
        self._message_cache.clear()

    @cache.cache()
    async def get_starboard(self, guild_id, *, connection=None):
        connection = connection or self.bot.pool
        query = "SELECT * FROM starboard WHERE id=$1;"
        record = await connection.fetchrow(query, guild_id)
        return StarboardConfig(guild_id=guild_id, bot=self.bot, record=record)

    def star_emoji(self, stars):
        if 5 > stars >= 0:
            return '\N{WHITE MEDIUM STAR}'
        elif 10 > stars >= 5:
            return '\N{GLOWING STAR}'
        elif 25 > stars >= 10:
            return '\N{DIZZY SYMBOL}'
        else:
            return '\N{SPARKLES}'

    def star_gradient_colour(self, stars):
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

    def is_url_spoiler(self, text, url):
        spoilers = self.spoilers.findall(text)
        for spoiler in spoilers:
            if url in spoiler:
                return True
        return False

    def get_emoji_message(self, message, stars):
        emoji = self.star_emoji(stars)

        if stars > 1:
            content = f'{emoji} **{stars}** {message.channel.mention} ID: {message.id}'
        else:
            content = f'{emoji} {message.channel.mention} ID: {message.id}'


        embed = discord.Embed(description=message.content)
        if message.embeds:
            data = message.embeds[0]
            if data.type == 'image' and not self.is_url_spoiler(message.content, data.url):
                embed.set_image(url=data.url)

        if message.attachments:
            file = message.attachments[0]
            spoiler = file.is_spoiler()
            if not spoiler and file.url.lower().endswith(('png', 'jpeg', 'jpg', 'gif', 'webp')):
                embed.set_image(url=file.url)
            elif spoiler:
                embed.add_field(name='Attachment', value=f'||[{file.filename}]({file.url})||', inline=False)
            else:
                embed.add_field(name='Attachment', value=f'[{file.filename}]({file.url})', inline=False)

        embed.add_field(name='Original', value=f'[Jump!]({message.jump_url})', inline=False)
        embed.set_author(name=message.author.display_name, icon_url=message.author.avatar_url_as(format='png'))
        embed.timestamp = message.created_at
        embed.colour = self.star_gradient_colour(stars)
        return content, embed

    async def get_message(self, channel, message_id):
        try:
            return self._message_cache[message_id]
        except KeyError:
            try:
                o = discord.Object(id=message_id + 1)
                pred = lambda m: m.id == message_id
                # don't wanna use get_message due to poor rate limit (1/1s) vs (50/1s)
                msg = await channel.history(limit=1, before=o).next()

                if msg.id != message_id:
                    return None

                self._message_cache[message_id] = msg
                return msg
            except Exception:
                return None

    async def reaction_action(self, fmt, payload):
        if str(payload.emoji) != '\N{WHITE MEDIUM STAR}':
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        method = getattr(self, f'{fmt}_message')

        user = self.bot.get_user(payload.user_id)
        if user is None or user.bot:
            return

        try:
            await method(channel, payload.message_id, payload.user_id, verify=True)
        except StarError:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if not isinstance(channel, discord.TextChannel):
            return

        starboard = await self.get_starboard(channel.guild.id)
        if starboard.channel is None or starboard.channel.id != channel.id:
            return

        # the starboard channel got deleted, so let's clear it from the database.
        async with self.bot.pool.acquire() as con:
            query = "DELETE FROM starboard WHERE id=$1;"
            await con.execute(query, channel.guild.id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.reaction_action('star', payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.reaction_action('unstar', payload)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
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
        async with self.bot.pool.acquire() as con:
            query = "DELETE FROM starboard_entries WHERE bot_message_id=$1;"
            await con.execute(query, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.message_ids <= self._about_to_be_deleted:
            # see comment above
            self._about_to_be_deleted.difference_update(payload.message_ids)
            return

        starboard = await self.get_starboard(payload.guild_id)
        if starboard.channel is None or starboard.channel.id != payload.channel_id:
            return

        async with self.bot.pool.acquire() as con:
            query = "DELETE FROM starboard_entries WHERE bot_message_id=ANY($1::bigint[]);"
            await con.execute(query, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload):
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return

        async with self.bot.pool.acquire() as con:
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

    async def star_message(self, channel, message_id, starrer_id, *, verify=False):
        guild_id = channel.guild.id
        lock = self._locks.get(guild_id)
        if lock is None:
            self._locks[guild_id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            async with self.bot.pool.acquire() as con:
                if verify:
                    config = self.bot.get_cog('Config')
                    if config:
                        plonked = await config.is_plonked(guild_id, starrer_id, channel_id=channel.id, connection=con)
                        if plonked:
                            return
                        perms = await config.get_command_permissions(guild_id, connection=con)
                        if perms.is_command_blocked('star', channel.id):
                            return

                await self._star_message(channel, message_id, starrer_id, connection=con)

    async def _star_message(self, channel, message_id, starrer_id, *, connection):
        """Stars a message.

        Parameters
        ------------
        channel: :class:`TextChannel`
            The channel that the starred message belongs to.
        message_id: int
            The message ID of the message being starred.
        starrer_id: int
            The ID of the person who starred this message.
        connection: asyncpg.Connection
            The connection to use.
        """

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

            ch = channel.guild.get_channel(record['channel_id'])
            if ch is None:
                raise StarError('Could not find original channel.')

            return await self._star_message(ch, record['message_id'], starrer_id, connection=connection)

        if not starboard_channel.permissions_for(starboard_channel.guild.me).send_messages:
            raise StarError('\N{NO ENTRY SIGN} Cannot post messages in starboard channel.')

        msg = await self.get_message(channel, message_id)

        if msg is None:
            raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')

        if msg.author.id == starrer_id:
            raise StarError('\N{NO ENTRY SIGN} You cannot star your own message.')

        if (len(msg.content) == 0 and len(msg.attachments) == 0) or msg.type is not discord.MessageType.default:
            raise StarError('\N{NO ENTRY SIGN} This message cannot be starred.')

        oldest_allowed = datetime.datetime.utcnow() - starboard.max_age
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
            record = await connection.fetchrow(query, message_id, channel.id, guild_id, msg.author.id, starrer_id)
        except asyncpg.UniqueViolationError:
            raise StarError('\N{NO ENTRY SIGN} You already starred this message.')

        entry_id = record[0]

        query = "SELECT COUNT(*) FROM starrers WHERE entry_id=$1;"
        record = await connection.fetchrow(query, entry_id)

        count = record[0]
        if count < starboard.threshold:
            return

        # at this point, we either edit the message or we create a message
        # with our star info
        content, embed = self.get_emoji_message(msg, count)

        # get the message ID to edit:
        query = "SELECT bot_message_id FROM starboard_entries WHERE message_id=$1;"
        record = await connection.fetchrow(query, message_id)
        bot_message_id = record[0]

        if bot_message_id is None:
            new_msg = await starboard_channel.send(content, embed=embed)
            query = "UPDATE starboard_entries SET bot_message_id=$1 WHERE message_id=$2;"
            await connection.execute(query, new_msg.id, message_id)
        else:
            new_msg = await self.get_message(starboard_channel, bot_message_id)
            if new_msg is None:
                # deleted? might as well purge the data
                query = "DELETE FROM starboard_entries WHERE message_id=$1;"
                await connection.execute(query, message_id)
            else:
                await new_msg.edit(content=content, embed=embed)

    async def unstar_message(self, channel, message_id, starrer_id, *, verify=False):
        guild_id = channel.guild.id
        lock = self._locks.get(guild_id)
        if lock is None:
            self._locks[guild_id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            async with self.bot.pool.acquire() as con:
                if verify:
                    config = self.bot.get_cog('Config')
                    if config:
                        plonked = await config.is_plonked(guild_id, starrer_id, channel_id=channel.id, connection=con)
                        if plonked:
                            return
                        perms = await config.get_command_permissions(guild_id, connection=con)
                        if perms.is_command_blocked('star', channel.id):
                            return

                await self._unstar_message(channel, message_id, starrer_id, connection=con)

    async def _unstar_message(self, channel, message_id, starrer_id, *, connection):
        """Unstars a message.

        Parameters
        ------------
        channel: :class:`TextChannel`
            The channel that the starred message belongs to.
        message_id: int
            The message ID of the message being unstarred.
        starrer_id: int
            The ID of the person who unstarred this message.
        connection: asyncpg.Connection
            The connection to use.
        """

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

            ch = channel.guild.get_channel(record['channel_id'])
            if ch is None:
                raise StarError('Could not find original channel.')

            return await self._unstar_message(ch, record['message_id'], starrer_id, connection=connection)

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
        count = await connection.fetchrow(query, entry_id)
        count = count[0]

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
                query = "UPDATE starboard_entries SET bot_message_id=NULL WHERE id=$1;"
                await connection.execute(query, entry_id)

            await bot_message.delete()
        else:
            msg = await self.get_message(channel, message_id)
            if msg is None:
                raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')

            content, embed = self.get_emoji_message(msg, count)
            await bot_message.edit(content=content, embed=embed)

    @commands.group(invoke_without_command=True)
    @checks.is_mod()
    async def starboard(self, ctx, *, name='starboard'):
        """Sets up the starboard for this server.

        This creates a new channel with the specified name
        and makes it into the server's "starboard". If no
        name is passed in then it defaults to "starboard".

        You must have Manage Server permission to use this.
        """

        # bypass the cache just in case someone used the star
        # reaction earlier before having it set up, or they
        # decided to use the ?star command
        self.get_starboard.invalidate(self, ctx.guild.id)

        starboard = await self.get_starboard(ctx.guild.id, connection=ctx.db)
        if starboard.channel is not None:
            return await ctx.send(f'This server already has a starboard ({starboard.channel.mention}).')

        if hasattr(starboard, 'locked'):
            try:
                confirm = await ctx.prompt('Apparently, a previously configured starboard channel was deleted. Is this true?')
            except RuntimeError as e:
                await ctx.send(e)
            else:
                if confirm:
                    await ctx.db.execute('DELETE FROM starboard WHERE id=$1;', ctx.guild.id)
                else:
                    return await ctx.send('Aborting starboard creation. Join the bot support server for more questions.')

        perms = ctx.channel.permissions_for(ctx.me)

        if not perms.manage_roles or not perms.manage_channels:
            return await ctx.send('\N{NO ENTRY SIGN} I do not have proper permissions (Manage Roles and Manage Channel)')

        overwrites = {
            ctx.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True,
                                                embed_links=True, read_message_history=True),
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False,
                                                                read_message_history=True)
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
    async def starboard_info(self, ctx):
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

    @commands.group(invoke_without_command=True, ignore_extra=False)
    @commands.guild_only()
    async def star(self, ctx, message: MessageID):
        """Stars a message via message ID.

        To star a message you should right click on the on a message and then
        click "Copy ID". You must have Developer Mode enabled to get that
        functionality.

        It is recommended that you react to a message with \N{WHITE MEDIUM STAR} instead.

        You can only star a message once.
        """

        try:
            await self.star_message(ctx.channel, message, ctx.author.id)
        except StarError as e:
            await ctx.send(e)
        else:
            await ctx.message.delete()

    @commands.command()
    @commands.guild_only()
    async def unstar(self, ctx, message: MessageID):
        """Unstars a message via message ID.

        To unstar a message you should right click on the on a message and then
        click "Copy ID". You must have Developer Mode enabled to get that
        functionality.
        """
        try:
            await self.unstar_message(ctx.channel, message, ctx.author.id, verify=True)
        except StarError as e:
            return await ctx.send(e)
        else:
            await ctx.message.delete()

    @star.command(name='clean')
    @checks.is_mod()
    @requires_starboard()
    async def star_clean(self, ctx, stars=1):
        """Cleans the starboard

        This removes messages in the starboard that only have less
        than or equal to the number of specified stars. This defaults to 1.

        Note that this only checks the last 100 messages in the starboard.

        This command requires the Manage Server permission.
        """

        stars = max(stars, 1)
        channel = ctx.starboard.channel

        last_messages = await channel.history(limit=100).map(lambda m: m.id).flatten()

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
    async def star_show(self, ctx, message: MessageID):
        """Shows a starred message via its ID.

        To get the ID of a message you should right click on the
        message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        You can only use this command once per 10 seconds.
        """

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
        channel = ctx.guild.get_channel(record['channel_id'])
        if channel is None:
            return await ctx.send("The message's channel has been deleted.")

        msg = await self.get_message(channel, record['message_id'])
        if msg is None:
            return await ctx.send('The message has been deleted.')

        content, embed = self.get_emoji_message(msg, record['Stars'])
        await ctx.send(content, embed=embed)

    @star.command(name='who')
    @requires_starboard()
    async def star_who(self, ctx, message: MessageID):
        """Show who starred a message.

        The ID can either be the starred message ID
        or the message ID in the starboard channel.
        """

        query = """SELECT starrers.author_id
                   FROM starrers
                   INNER JOIN starboard_entries entry
                   ON entry.id = starrers.entry_id
                   WHERE entry.message_id = $1 OR entry.bot_message_id = $1
                """

        records = await ctx.db.fetch(query, message)
        if records is None or len(records) == 0:
            return await ctx.send('No one starred this message or this is an invalid message ID.')

        members = [str(ctx.guild.get_member(r[0]))
                   for r in records
                   if ctx.guild.get_member(r[0])]

        try:
            p = Pages(ctx, entries=members, per_page=20, show_entry_count=False)
            base = format(plural(len(records)), 'star')
            if len(records) > len(members):
                p.embed.title = f'{base} ({len(records) - len(members)} left server)'
            else:
                p.embed.title = base
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

    @star.command(name='migrate')
    @requires_starboard()
    @checks.is_mod()
    async def star_migrate(self, ctx):
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

        stats = self.bot.get_cog('Stats')
        if stats is None:
            return await ctx.send('Internal error occurred: Stats cog not loaded')

        webhook = stats.webhook

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
                fmt = 'https://discordapp.com/channels/{guild_id}/{channel_id}/{message_id}'.format(**groups)
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

            m = await ctx.send(f'{ctx.author.mention}, we are done migrating!\n' \
                                'The starboard has been unlocked.\n' \
                               f'Updated {updated}/{fetched} entries to the new format.\n' \
                               f'Took {delta:.2f}s.')

            e = discord.Embed(title='Starboard Migration', colour=discord.Colour.gold())
            e.add_field(name='Updated', value=updated)
            e.add_field(name='Fetched', value=fetched)
            e.add_field(name='Failed', value=failed)
            e.add_field(name='Name', value=ctx.guild.name)
            e.add_field(name='ID', value=guild_id)
            e.set_footer(text=f'Took {delta:.2f}s to migrate')
            e.timestamp = m.created_at
            await webhook.send(embed=e)


    def records_to_value(self, records, fmt=None, default='None!'):
        if not records:
            return default

        emoji = 0x1f947 # :first_place:
        fmt = fmt or (lambda o: o)
        return '\n'.join(f'{chr(emoji + i)}: {fmt(r["ID"])} ({plural(r["Stars"]):star})'
                         for i, r in enumerate(records))

    async def star_guild_stats(self, ctx):
        e = discord.Embed(title='Server Starboard Stats')
        e.timestamp = ctx.starboard.channel.created_at
        e.set_footer(text='Adding stars since')

        # messages starred
        query = "SELECT COUNT(*) FROM starboard_entries WHERE guild_id=$1;"

        record = await ctx.db.fetchrow(query, ctx.guild.id)
        total_messages = record[0]

        # total stars given
        query = """SELECT COUNT(*)
                   FROM starrers
                   INNER JOIN starboard_entries entry
                   ON entry.id = starrers.entry_id
                   WHERE entry.guild_id=$1;
                """

        record = await ctx.db.fetchrow(query, ctx.guild.id)
        total_stars = record[0]

        e.description = f'{plural(total_messages):message} starred with a total of {total_stars} stars.'
        e.colour = discord.Colour.gold()

        # this big query fetches 3 things:
        # top 3 starred posts (Type 3)
        # top 3 most starred authors  (Type 1)
        # top 3 star givers (Type 2)

        query = """WITH t AS (
                       SELECT
                           entry.author_id AS entry_author_id,
                           starrers.author_id,
                           entry.bot_message_id
                       FROM starrers
                       INNER JOIN starboard_entries entry
                       ON entry.id = starrers.entry_id
                       WHERE entry.guild_id=$1
                   )
                   (
                       SELECT t.entry_author_id AS "ID", 1 AS "Type", COUNT(*) AS "Stars"
                       FROM t
                       WHERE t.entry_author_id IS NOT NULL
                       GROUP BY t.entry_author_id
                       ORDER BY "Stars" DESC
                       LIMIT 3
                   )
                   UNION ALL
                   (
                       SELECT t.author_id AS "ID", 2 AS "Type", COUNT(*) AS "Stars"
                       FROM t
                       GROUP BY t.author_id
                       ORDER BY "Stars" DESC
                       LIMIT 3
                   )
                   UNION ALL
                   (
                       SELECT t.bot_message_id AS "ID", 3 AS "Type", COUNT(*) AS "Stars"
                       FROM t
                       WHERE t.bot_message_id IS NOT NULL
                       GROUP BY t.bot_message_id
                       ORDER BY "Stars" DESC
                       LIMIT 3
                   );
                """

        records = await ctx.db.fetch(query, ctx.guild.id)
        starred_posts = [r for r in records if r['Type'] == 3]
        e.add_field(name='Top Starred Posts', value=self.records_to_value(starred_posts), inline=False)

        to_mention = lambda o: f'<@{o}>'

        star_receivers = [r for r in records if r['Type'] == 1]
        value = self.records_to_value(star_receivers, to_mention, default='No one!')
        e.add_field(name='Top Star Receivers', value=value, inline=False)

        star_givers = [r for r in records if r['Type'] == 2]
        value = self.records_to_value(star_givers, to_mention, default='No one!')
        e.add_field(name='Top Star Givers', value=value, inline=False)

        await ctx.send(embed=e)

    async def star_member_stats(self, ctx, member):
        e = discord.Embed(colour=discord.Colour.gold())
        e.set_author(name=member.display_name, icon_url=member.avatar_url_as(format='png'))

        # this query calculates
        # 1 - stars received,
        # 2 - stars given
        # The rest are the top 3 starred posts

        query = """WITH t AS (
                       SELECT entry.author_id AS entry_author_id,
                              starrers.author_id,
                              entry.message_id
                       FROM starrers
                       INNER JOIN starboard_entries entry
                       ON entry.id=starrers.entry_id
                       WHERE entry.guild_id=$1
                   )
                   (
                       SELECT '0'::bigint AS "ID", COUNT(*) AS "Stars"
                       FROM t
                       WHERE t.entry_author_id=$2
                   )
                   UNION ALL
                   (
                       SELECT '0'::bigint AS "ID", COUNT(*) AS "Stars"
                       FROM t
                       WHERE t.author_id=$2
                   )
                   UNION ALL
                   (
                       SELECT t.message_id AS "ID", COUNT(*) AS "Stars"
                       FROM t
                       WHERE t.entry_author_id=$2
                       GROUP BY t.message_id
                       ORDER BY "Stars" DESC
                       LIMIT 3
                   )
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)
        received = records[0]['Stars']
        given = records[1]['Stars']
        top_three = records[2:]

        # this query calculates how many of our messages were starred
        query = """SELECT COUNT(*) FROM starboard_entries WHERE guild_id=$1 AND author_id=$2;"""
        record = await ctx.db.fetchrow(query, ctx.guild.id, member.id)
        messages_starred = record[0]

        e.add_field(name='Messages Starred', value=messages_starred)
        e.add_field(name='Stars Received', value=received)
        e.add_field(name='Stars Given', value=given)

        e.add_field(name='Top Starred Posts', value=self.records_to_value(top_three), inline=False)

        await ctx.send(embed=e)

    @star.command(name='stats')
    @requires_starboard()
    async def star_stats(self, ctx, *, member: discord.Member = None):
        """Shows statistics on the starboard usage of the server or a member."""

        if member is None:
            await self.star_guild_stats(ctx)
        else:
            await self.star_member_stats(ctx, member)

    @star.command(name='random')
    @requires_starboard()
    async def star_random(self, ctx):
        """Shows a random starred message."""

        query = """SELECT bot_message_id
                   FROM starboard_entries
                   WHERE guild_id=$1
                   AND bot_message_id IS NOT NULL
                   OFFSET FLOOR(RANDOM() * (
                       SELECT COUNT(*)
                       FROM starboard_entries
                       WHERE guild_id=$1
                       AND bot_message_id IS NOT NULL
                   ))
                   LIMIT 1
                """

        record = await ctx.db.fetchrow(query, ctx.guild.id)

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

    @star.command(name='lock')
    @checks.is_mod()
    @requires_starboard()
    async def star_lock(self, ctx):
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
    @checks.is_mod()
    @requires_starboard()
    async def star_unlock(self, ctx):
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
    @checks.is_mod()
    @requires_starboard()
    async def star_limit(self, ctx, stars: int):
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
    @checks.is_mod()
    @requires_starboard()
    async def star_age(self, ctx, number: int, units='days'):
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

        valid_units = ('days', 'weeks', 'months', 'years')

        if units[-1] != 's':
            units = units + 's'

        if units not in valid_units:
            return await ctx.send(f'Not a valid unit! I expect only {human_join(valid_units)}.')

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

    @commands.command(hidden=True)
    @commands.is_owner()
    async def star_announce(self, ctx, *, message):
        """Announce stuff to every starboard."""
        query = "SELECT id, channel_id FROM starboard;"
        records = await ctx.db.fetch(query)
        await ctx.release()

        to_send = []
        for guild_id, channel_id in records:
            guild = self.bot.get_guild(guild_id)
            if guild:
                channel = self.bot.get_channel(channel_id)
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

def setup(bot):
    bot.add_cog(Stars(bot))
