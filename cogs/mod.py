from discord.ext import commands, tasks
from .utils import checks, db, time, cache
from .utils.formats import plural
from collections import Counter, defaultdict
from inspect import cleandoc

import re
import json
import discord
import enum
import datetime
import asyncio
import argparse, shlex
import logging
import asyncpg
import io

log = logging.getLogger(__name__)

## Misc utilities

class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)

class RaidMode(enum.Enum):
    off = 0
    on = 1
    strict = 2

    def __str__(self):
        return self.name

## Tables

class GuildConfig(db.Table, table_name='guild_mod_config'):
    id = db.Column(db.Integer(big=True), primary_key=True)
    raid_mode = db.Column(db.Integer(small=True))
    broadcast_channel = db.Column(db.Integer(big=True))
    mention_count = db.Column(db.Integer(small=True))
    safe_mention_channel_ids = db.Column(db.Array(db.Integer(big=True)))
    mute_role_id = db.Column(db.Integer(big=True))
    muted_members = db.Column(db.Array(db.Integer(big=True)))

## Configuration

class ModConfig:
    __slots__ = ('raid_mode', 'id', 'bot', 'broadcast_channel_id', 'mention_count',
                 'safe_mention_channel_ids', 'mute_role_id', 'muted_members')

    @classmethod
    async def from_record(cls, record, bot):
        self = cls()

        # the basic configuration
        self.bot = bot
        self.raid_mode = record['raid_mode']
        self.id = record['id']
        self.broadcast_channel_id = record['broadcast_channel']
        self.mention_count = record['mention_count']
        self.safe_mention_channel_ids = set(record['safe_mention_channel_ids'] or [])
        self.muted_members = set(record['muted_members'] or [])
        self.mute_role_id = record['mute_role_id']
        return self

    @property
    def broadcast_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.broadcast_channel_id)

    @property
    def mute_role(self):
        guild = self.bot.get_guild(self.id)
        return guild and self.mute_role_id and guild.get_role(self.mute_role_id)

    def is_muted(self, member):
        return member.id in self.muted_members

    async def apply_mute(self, member, reason):
        if self.mute_role_id:
            await member.add_roles(discord.Object(id=self.mute_role_id), reason=reason)

## Converters

def can_execute_action(ctx, user, target):
    return user.id == ctx.bot.owner_id or \
           user == ctx.guild.owner or \
           user.top_role > target.top_role

class MemberNotFound(Exception):
    pass

async def resolve_member(guild, member_id):
    member = guild.get_member(member_id)
    if member is None:
        if guild.chunked:
            raise MemberNotFound()
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            raise MemberNotFound() from None
    return member

class MemberID(commands.Converter):
    async def convert(self, ctx, argument):
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                member_id = int(argument, base=10)
                m = await resolve_member(ctx.guild, member_id)
            except ValueError:
                raise commands.BadArgument(f"{argument} is not a valid member or member ID.") from None
            except MemberNotFound:
                # hackban case
                return type('_Hackban', (), {'id': member_id, '__str__': lambda s: f'Member ID {s.id}'})()

        if not can_execute_action(ctx, ctx.author, m):
            raise commands.BadArgument('You cannot do this action on this user due to role hierarchy.')
        return m

class BannedMember(commands.Converter):
    async def convert(self, ctx, argument):
        if argument.isdigit():
            member_id = int(argument, base=10)
            try:
                return await ctx.guild.fetch_ban(discord.Object(id=member_id))
            except discord.NotFound:
                raise commands.BadArgument('This member has not been banned before.') from None

        ban_list = await ctx.guild.bans()
        entity = discord.utils.find(lambda u: str(u.user) == argument, ban_list)

        if entity is None:
            raise commands.BadArgument('This member has not been banned before.')
        return entity

class ActionReason(commands.Converter):
    async def convert(self, ctx, argument):
        ret = f'{ctx.author} (ID: {ctx.author.id}): {argument}'

        if len(ret) > 512:
            reason_max = 512 - len(ret) + len(argument)
            raise commands.BadArgument(f'Reason is too long ({len(argument)}/{reason_max})')
        return ret

def safe_reason_append(base, to_append):
    appended = base + f'({to_append})'
    if len(appended) > 512:
        return base
    return appended

## Spam detector

# TODO: add this to d.py maybe
class CooldownByContent(commands.CooldownMapping):
    def _bucket_key(self, message):
        return (message.channel.id, message.content)

class SpamChecker:
    """This spam checker does a few things.

    1) It checks if a user has spammed more than 10 times in 12 seconds
    2) It checks if the content has been spammed 15 times in 17 seconds.
    3) It checks if new users have spammed 30 times in 35 seconds.
    4) It checks if "fast joiners" have spammed 10 times in 12 seconds.

    The second case is meant to catch alternating spam bots while the first one
    just catches regular singular spam bots.

    From experience these values aren't reached unless someone is actively spamming.
    """
    def __init__(self):
        self.by_content = CooldownByContent.from_cooldown(15, 17.0, commands.BucketType.member)
        self.by_user = commands.CooldownMapping.from_cooldown(10, 12.0, commands.BucketType.user)
        self.last_join = None
        self.new_user = commands.CooldownMapping.from_cooldown(30, 35.0, commands.BucketType.channel)

        # user_id flag mapping (for about 30 minutes)
        self.fast_joiners = cache.ExpiringCache(seconds=1800.0)
        self.hit_and_run = commands.CooldownMapping.from_cooldown(10, 12, commands.BucketType.channel)

    def is_new(self, member):
        now = datetime.datetime.utcnow()
        seven_days_ago = now - datetime.timedelta(days=7)
        ninety_days_ago = now - datetime.timedelta(days=90)
        return member.created_at > ninety_days_ago and member.joined_at > seven_days_ago

    def is_spamming(self, message):
        if message.guild is None:
            return False

        current = message.created_at.replace(tzinfo=datetime.timezone.utc).timestamp()

        if message.author.id in self.fast_joiners:
            bucket = self.hit_and_run.get_bucket(message)
            if bucket.update_rate_limit(current):
                return True

        if self.is_new(message.author):
            new_bucket = self.new_user.get_bucket(message)
            if new_bucket.update_rate_limit(current):
                return True

        user_bucket = self.by_user.get_bucket(message)
        if user_bucket.update_rate_limit(current):
            return True

        content_bucket = self.by_content.get_bucket(message)
        if content_bucket.update_rate_limit(current):
            return True

        return False

    def is_fast_join(self, member):
        joined = member.joined_at or datetime.datetime.utcnow()
        if self.last_join is None:
            self.last_join = joined
            return False
        is_fast = (joined - self.last_join).total_seconds() <= 2.0
        self.last_join = joined
        if is_fast:
            self.fast_joiners[member.id] = True
        return is_fast

## Checks

class NoMuteRole(commands.CommandError):
    def __init__(self):
        super().__init__('This server does not have a mute role set up.')

def can_mute():
    async def predicate(ctx):
        is_owner = await ctx.bot.is_owner(ctx.author)
        if ctx.guild is None:
            return False

        if not ctx.author.guild_permissions.manage_roles and not is_owner:
            return False

        # This will only be used within this cog.
        ctx.guild_config = config = await ctx.cog.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is None:
            raise NoMuteRole()
        return ctx.author.top_role > role
    return commands.check(predicate)

## The actual cog

class Mod(commands.Cog):
    """Moderation related commands."""

    def __init__(self, bot):
        self.bot = bot

        # guild_id: SpamChecker
        self._spam_check = defaultdict(SpamChecker)

        # guild_id: List[(member_id, insertion)]
        # A batch of data for bulk inserting mute role changes
        # True - insert, False - remove
        self._data_batch = defaultdict(list)
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._disable_lock = asyncio.Lock(loop=bot.loop)
        self.batch_updates.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_updates.start()

        # (guild_id, channel_id): List[str]
        # A batch list of message content for message
        self.message_batches = defaultdict(list)
        self._batch_message_lock = asyncio.Lock(loop=bot.loop)
        self.bulk_send_messages.start()

    def __repr__(self):
        return '<cogs.Mod>'

    def cog_unload(self):
        self.batch_updates.stop()
        self.bulk_send_messages.stop()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                await ctx.send('I do not have permission to execute this action.')
            elif isinstance(original, discord.NotFound):
                await ctx.send(f'This entity does not exist: {original.text}')
            elif isinstance(original, discord.HTTPException):
                await ctx.send('Somehow, an unexpected error occurred. Try again later?')
        elif isinstance(error, NoMuteRole):
            await ctx.send(error)

    async def bulk_insert(self):
        query = """UPDATE guild_mod_config
                   SET muted_members = x.result_array
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(guild_id BIGINT, result_array BIGINT[])
                   WHERE guild_mod_config.id = x.guild_id;
                """

        if not self._data_batch:
            return

        final_data = []
        for guild_id, data in self._data_batch.items():
            # If it's touched this function then chances are that this has hit cache before
            # so it's not actually doing a query, hopefully.
            config = await self.get_guild_config(guild_id)
            as_set = config.muted_members
            for member_id, insertion in data:
                func = as_set.add if insertion else as_set.discard
                func(member_id)

            final_data.append({
                'guild_id': guild_id,
                'result_array': list(as_set)
            })
            self.get_guild_config.invalidate(self, guild_id)

        await self.bot.pool.execute(query, final_data)
        self._data_batch.clear()

    @tasks.loop(seconds=15.0)
    async def batch_updates(self):
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=10.0)
    async def bulk_send_messages(self):
        async with self._batch_message_lock:
            for ((guild_id, channel_id), messages) in self.message_batches.items():
                guild = self.bot.get_guild(guild_id)
                channel = guild and guild.get_channel(channel_id)
                if channel is None:
                    continue

                paginator = commands.Paginator(suffix='', prefix='')
                for message in messages:
                    paginator.add_line(message)

                for page in paginator.pages:
                    try:
                        await channel.send(page)
                    except discord.HTTPException:
                        pass

            self.message_batches.clear()

    @cache.cache()
    async def get_guild_config(self, guild_id):
        query = """SELECT * FROM guild_mod_config WHERE id=$1;"""
        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            if record is not None:
                return await ModConfig.from_record(record, self.bot)
            return None

    async def check_raid(self, config, guild_id, member, message):
        if config.raid_mode != RaidMode.strict.value:
            return

        checker = self._spam_check[guild_id]
        if not checker.is_spamming(message):
            return

        try:
            await member.ban(reason='Auto-ban from spam (strict raid mode ban)')
        except discord.HTTPException:
            log.info(f'[Raid Mode] Failed to ban {member} (ID: {member.id}) from server {member.guild} via strict mode.')
        else:
            log.info(f'[Raid Mode] Banned {member} (ID: {member.id}) from server {member.guild} via strict mode.')

    @commands.Cog.listener()
    async def on_message(self, message):
        author = message.author
        if author.id in (self.bot.user.id, self.bot.owner_id):
            return

        if message.guild is None:
            return

        if not isinstance(author, discord.Member):
            return

        if author.bot:
            return

        # we're going to ignore members with roles
        if len(author.roles) > 1:
            return

        guild_id = message.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        # check for raid mode stuff
        await self.check_raid(config, guild_id, author, message)

        # auto-ban tracking for mention spams begin here
        if len(message.mentions) <= 3:
            return

        if not config.mention_count:
            return

        # check if it meets the thresholds required
        mention_count = sum(not m.bot and m.id != author.id for m in message.mentions)
        if mention_count < config.mention_count:
            return

        if message.channel.id in config.safe_mention_channel_ids:
            return

        try:
            await author.ban(reason=f'Spamming mentions ({mention_count} mentions)')
        except Exception as e:
            log.info(f'Failed to autoban member {author} (ID: {author.id}) in guild ID {guild_id}')
        else:
            to_send = f'Banned {author} (ID: {author.id}) for spamming {mention_count} mentions.'
            async with self._batch_message_lock:
                self.message_batches[(guild_id, message.channel.id)].append(to_send)

            log.info(f'Member {author} (ID: {author.id}) has been autobanned from guild ID {guild_id}')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild_id = member.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.is_muted(member):
            return await config.apply_mute(member, 'Member was previously muted.')

        if not config.raid_mode:
            return

        now = datetime.datetime.utcnow()

        is_new = member.created_at > (now - datetime.timedelta(days=7))
        checker = self._spam_check[guild_id]

        # Do the broadcasted message to the channel
        title = 'Member Joined'
        if checker.is_fast_join(member):
            colour = 0xdd5f53 # red
            if is_new:
                title = 'Member Joined (Very New Member)'
        else:
            colour = 0x53dda4 # green

            if is_new:
                colour = 0xdda453 # yellow
                title = 'Member Joined (Very New Member)'

        e = discord.Embed(title=title, colour=colour)
        e.timestamp = now
        e.set_author(name=str(member), icon_url=member.avatar_url)
        e.add_field(name='ID', value=member.id)
        e.add_field(name='Joined', value=member.joined_at)
        e.add_field(name='Created', value=time.human_timedelta(member.created_at), inline=False)

        if config.broadcast_channel:
            try:
                await config.broadcast_channel.send(embed=e)
            except discord.Forbidden:
                async with self._disable_lock:
                    await self.disable_raid_mode(guild_id)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # Comparing roles in memory is faster than potentially fetching from
        # database, even if there's a cache layer
        if before.roles == after.roles:
            return

        guild_id = after.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.mute_role_id is None:
            return

        # Use private API because d.py does not expose this yet
        before_has = before._roles.has(config.mute_role_id)
        after_has = after._roles.has(config.mute_role_id)

        # No change in the mute role
        # both didn't have it or both did have it
        if before_has == after_has:
            return

        async with self._batch_lock:
            # If `after_has` is true, then it's an insertion operation
            # if it's false, then the role for removed
            self._data_batch[guild_id].append((after.id, after_has))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        guild_id = role.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role_id != role.id:
            return

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)

    @commands.command(aliases=['newmembers'])
    @commands.guild_only()
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have
        joined.

        The count parameter can only be up to 25.
        """
        count = max(min(count, 25), 5)

        if not ctx.guild.chunked:
            await self.bot.request_offline_members(ctx.guild)

        members = sorted(ctx.guild.members, key=lambda m: m.joined_at, reverse=True)[:count]

        e = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            body = f'Joined {time.human_timedelta(member.joined_at)}\nCreated {time.human_timedelta(member.created_at)}'
            e.add_field(name=f'{member} (ID: {member.id})', value=body, inline=False)

        await ctx.send(embed=e)

    @commands.group(aliases=['raids'], invoke_without_command=True)
    @checks.is_mod()
    async def raid(self, ctx):
        """Controls raid mode on the server.

        Calling this command with no arguments will show the current raid
        mode information.

        You must have Manage Server permissions to use this command or
        its subcommands.
        """

        query = "SELECT raid_mode, broadcast_channel FROM guild_mod_config WHERE id=$1;"

        row = await ctx.db.fetchrow(query, ctx.guild.id)
        if row is None:
            fmt = 'Raid Mode: off\nBroadcast Channel: None'
        else:
            ch = f'<#{row[1]}>' if row[1] else None
            mode = RaidMode(row[0]) if row[0] is not None else RaidMode.off
            fmt = f'Raid Mode: {mode}\nBroadcast Channel: {ch}'

        await ctx.send(fmt)

    @raid.command(name='on', aliases=['enable', 'enabled'])
    @checks.is_mod()
    async def raid_on(self, ctx, *, channel: discord.TextChannel = None):
        """Enables basic raid mode on the server.

        When enabled, server verification level is set to table flip
        levels and allows the bot to broadcast new members joining
        to a specified channel.

        If no channel is given, then the bot will broadcast join
        messages on the channel this command was used in.
        """

        channel = channel or ctx.channel

        try:
            await ctx.guild.edit(verification_level=discord.VerificationLevel.high)
        except discord.HTTPException:
            await ctx.send('\N{WARNING SIGN} Could not set verification level.')

        query = """INSERT INTO guild_mod_config (id, raid_mode, broadcast_channel)
                   VALUES ($1, $2, $3) ON CONFLICT (id)
                   DO UPDATE SET
                        raid_mode = EXCLUDED.raid_mode,
                        broadcast_channel = EXCLUDED.broadcast_channel;
                """

        await ctx.db.execute(query, ctx.guild.id, RaidMode.on.value, channel.id)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Raid mode enabled. Broadcasting join messages to {channel.mention}.')

    async def disable_raid_mode(self, guild_id):
        query = """INSERT INTO guild_mod_config (id, raid_mode, broadcast_channel)
                   VALUES ($1, $2, NULL) ON CONFLICT (id)
                   DO UPDATE SET
                        raid_mode = EXCLUDED.raid_mode,
                        broadcast_channel = NULL;
                """

        await self.bot.pool.execute(query, guild_id, RaidMode.off.value)
        self._spam_check.pop(guild_id, None)
        self.get_guild_config.invalidate(self, guild_id)

    @raid.command(name='off', aliases=['disable', 'disabled'])
    @checks.is_mod()
    async def raid_off(self, ctx):
        """Disables raid mode on the server.

        When disabled, the server verification levels are set
        back to Low levels and the bot will stop broadcasting
        join messages.
        """

        try:
            await ctx.guild.edit(verification_level=discord.VerificationLevel.low)
        except discord.HTTPException:
            await ctx.send('\N{WARNING SIGN} Could not set verification level.')

        await self.disable_raid_mode(ctx.guild.id)
        await ctx.send('Raid mode disabled. No longer broadcasting join messages.')

    @raid.command(name='strict')
    @checks.is_mod()
    async def raid_strict(self, ctx, *, channel: discord.TextChannel = None):
        """Enables strict raid mode on the server.

        Strict mode is similar to regular enabled raid mode, with the added
        benefit of auto-banning members that are spamming. The threshold for
        spamming depends on a per-content basis and also on a per-user basis
        of 15 messages per 17 seconds.

        If this is considered too strict, it is recommended to fall back to regular
        raid mode.
        """
        channel = channel or ctx.channel

        perms = ctx.me.guild_permissions
        if not (perms.kick_members and perms.ban_members):
            return await ctx.send('\N{NO ENTRY SIGN} I do not have permissions to kick and ban members.')

        try:
            await ctx.guild.edit(verification_level=discord.VerificationLevel.high)
        except discord.HTTPException:
            await ctx.send('\N{WARNING SIGN} Could not set verification level.')

        query = """INSERT INTO guild_mod_config (id, raid_mode, broadcast_channel)
                   VALUES ($1, $2, $3) ON CONFLICT (id)
                   DO UPDATE SET
                        raid_mode = EXCLUDED.raid_mode,
                        broadcast_channel = EXCLUDED.broadcast_channel;
                """

        await ctx.db.execute(query, ctx.guild.id, RaidMode.strict.value, channel.id)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Raid mode enabled strictly. Broadcasting join messages to {channel.mention}.')

    async def _basic_cleanup_strategy(self, ctx, search):
        count = 0
        async for msg in ctx.history(limit=search, before=ctx.message):
            if msg.author == ctx.me:
                await msg.delete()
                count += 1
        return { 'Bot': count }

    async def _complex_cleanup_strategy(self, ctx, search):
        prefixes = tuple(self.bot.get_guild_prefixes(ctx.guild)) # thanks startswith

        def check(m):
            return m.author == ctx.me or m.content.startswith(prefixes)

        deleted = await ctx.channel.purge(limit=search, check=check, before=ctx.message)
        return Counter(m.author.display_name for m in deleted)

    @commands.command()
    @checks.has_permissions(manage_messages=True)
    async def cleanup(self, ctx, search=100):
        """Cleans up the bot's messages from the channel.

        If a search number is specified, it searches that many messages to delete.
        If the bot has Manage Messages permissions then it will try to delete
        messages that look like they invoked the bot as well.

        After the cleanup is completed, the bot will send you a message with
        which people got their messages deleted and their count. This is useful
        to see which users are spammers.

        You must have Manage Messages permission to use this.
        """

        strategy = self._basic_cleanup_strategy
        if ctx.me.permissions_in(ctx.channel).manage_messages:
            strategy = self._complex_cleanup_strategy

        spammers = await strategy(ctx, search)
        deleted = sum(spammers.values())
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'- **{author}**: {count}' for author, count in spammers)

        await ctx.send('\n'.join(messages), delete_after=10)

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def kick(self, ctx, member: MemberID, *, reason: ActionReason = None):
        """Kicks a member from the server.

        In order for this to work, the bot must have Kick Member permissions.

        To use this command you must have Kick Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.kick(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def ban(self, ctx, member: MemberID, *, reason: ActionReason = None):
        """Bans a member from the server.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def multiban(self, ctx, members: commands.Greedy[MemberID], *, reason: ActionReason = None):
        """Bans multiple members from the server.

        This only works through banning via ID.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        total_members = len(members)
        if total_members == 0:
            return await ctx.send('Missing members to ban.')

        confirm = await ctx.prompt(f'This will ban **{plural(total_members):member}**. Are you sure?', reacquire=False)
        if not confirm:
            return await ctx.send('Aborting.')

        failed = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                failed += 1

        await ctx.send(f'Banned {total_members - failed}/{total_members} members.')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def massban(self, ctx, *, args):
        """Mass bans multiple members from the server.

        This command has a powerful "command line" syntax. To use this command
        you and the bot must both have Ban Members permission. **Every option is optional.**

        Users are only banned **if and only if** all conditions are met.

        The following options are valid.

        `--channel` or `-c`: Channel to search for message history.
        `--reason` or `-r`: The reason for the ban.
        `--regex`: Regex that usernames must match.
        `--created`: Matches users whose accounts were created less than specified minutes ago.
        `--joined`: Matches users that joined less than specified minutes ago.
        `--joined-before`: Matches users who joined before the member ID given.
        `--joined-after`: Matches users who joined after the member ID given.
        `--no-avatar`: Matches users who have no avatar. (no arguments)
        `--no-roles`: Matches users that have no role. (no arguments)
        `--show`: Show members instead of banning them (no arguments).

        Message history filters (Requires `--channel`):

        `--contains`: A substring to search for in the message.
        `--starts`: A substring to search if the message starts with.
        `--ends`: A substring to search if the message ends with.
        `--match`: A regex to match the message content to.
        `--search`: How many messages to search. Default 100. Max 2000.
        `--after`: Messages must come after this message ID.
        `--before`: Messages must come before this message ID.
        `--files`: Checks if the message has attachments (no arguments).
        `--embeds`: Checks if the message has embeds (no arguments).
        """

        # For some reason there are cases due to caching that ctx.author
        # can be a User even in a guild only context
        # Rather than trying to work out the kink with it
        # Just upgrade the member itself.
        if not isinstance(ctx.author, discord.Member):
            try:
                author = await ctx.guild.fetch_member(ctx.author.id)
            except discord.HTTPException:
                return await ctx.send('Somehow, Discord does not seem to think you are in this server.')
        else:
            author = ctx.author

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('--channel', '-c')
        parser.add_argument('--reason', '-r')
        parser.add_argument('--search', type=int, default=100)
        parser.add_argument('--regex')
        parser.add_argument('--no-avatar', action='store_true')
        parser.add_argument('--no-roles', action='store_true')
        parser.add_argument('--created', type=int)
        parser.add_argument('--joined', type=int)
        parser.add_argument('--joined-before', type=int)
        parser.add_argument('--joined-after', type=int)
        parser.add_argument('--contains')
        parser.add_argument('--starts')
        parser.add_argument('--ends')
        parser.add_argument('--match')
        parser.add_argument('--show', action='store_true')
        parser.add_argument('--embeds', action='store_const', const=lambda m: len(m.embeds))
        parser.add_argument('--files', action='store_const', const=lambda m: len(m.attachments))
        parser.add_argument('--after', type=int)
        parser.add_argument('--before', type=int)

        try:
            args = parser.parse_args(shlex.split(args))
        except Exception as e:
            return await ctx.send(str(e))

        members = []

        if args.channel:
            channel = await commands.TextChannelConverter().convert(ctx, args.channel)
            before = args.before and discord.Object(id=args.before)
            after = args.after and discord.Object(id=args.after)
            predicates = []
            if args.contains:
                predicates.append(lambda m: args.contains in m.content)
            if args.starts:
                predicates.append(lambda m: m.content.startswith(args.starts))
            if args.ends:
                predicates.append(lambda m: m.content.endswith(args.ends))
            if args.match:
                try:
                    _match = re.compile(args.match)
                except re.error as e:
                    return await ctx.send(f'Invalid regex passed to `--match`: {e}')
                else:
                    predicates.append(lambda m, x=_match: x.match(m.content))
            if args.embeds:
                predicates.append(args.embeds)
            if args.files:
                predicates.append(args.files)

            async for message in channel.history(limit=min(max(1, args.search), 2000), before=before, after=after):
                if all(p(message) for p in predicates):
                    members.append(message.author)
        else:
            members = ctx.guild.members

        # member filters
        predicates = [
            lambda m: isinstance(m, discord.Member) and can_execute_action(ctx, author, m), # Only if applicable
            lambda m: not m.bot, # No bots
            lambda m: m.discriminator != '0000', # No deleted users
        ]

        async def _resolve_member(member_id):
            r = ctx.guild.get_member(member_id)
            if r is None:
                try:
                    return await ctx.guild.fetch_member(member_id)
                except discord.HTTPException as e:
                    raise commands.BadArgument(f'Could not fetch member by ID {member_id}: {e}') from None
            return r

        if args.regex:
            try:
                _regex = re.compile(args.regex)
            except re.error as e:
                return await ctx.send(f'Invalid regex passed to `--regex`: {e}')
            else:
                predicates.append(lambda m, x=_regex: x.match(m.name))

        if args.no_avatar:
            predicates.append(lambda m: m.avatar is None)
        if args.no_roles:
            predicates.append(lambda m: len(getattr(m, 'roles', [])) <= 1)

        now = datetime.datetime.utcnow()
        if args.created:
            def created(member, *, offset=now - datetime.timedelta(minutes=args.created)):
                return member.created_at > offset
            predicates.append(created)
        if args.joined:
            def joined(member, *, offset=now - datetime.timedelta(minutes=args.joined)):
                if isinstance(member, discord.User):
                    # If the member is a user then they left already
                    return True
                return member.joined_at and member.joined_at > offset
            predicates.append(joined)
        if args.joined_after:
            _joined_after_member = await _resolve_member(args.joined_after)
            def joined_after(member, *, _other=_joined_after_member):
                return member.joined_at and _other.joined_at and member.joined_at > _other.joined_at
            predicates.append(joined_after)
        if args.joined_before:
            _joined_before_member = await _resolve_member(args.joined_before)
            def joined_before(member, *, _other=_joined_before_member):
                return member.joined_at and _other.joined_at and member.joined_at < _other.joined_at
            predicates.append(joined_before)

        members = {m for m in members if all(p(m) for p in predicates)}
        if len(members) == 0:
            return await ctx.send('No members found matching criteria.')

        if args.show:
            members = sorted(members, key=lambda m: m.joined_at or now)
            fmt = "\n".join(f'{m.id}\tJoined: {m.joined_at}\tCreated: {m.created_at}\t{m}' for m in members)
            content = f'Current Time: {datetime.datetime.utcnow()}\nTotal members: {len(members)}\n{fmt}'
            file = discord.File(io.BytesIO(content.encode('utf-8')), filename='members.txt')
            return await ctx.send(file=file)

        if args.reason is None:
            return await ctx.send('--reason flag is required.')
        else:
            reason = await ActionReason().convert(ctx, args.reason)

        confirm = await ctx.prompt(f'This will ban **{plural(len(members)):member}**. Are you sure?')
        if not confirm:
            return await ctx.send('Aborting.')

        count = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await ctx.send(f'Banned {count}/{len(members)}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def softban(self, ctx, member: MemberID, *, reason: ActionReason = None):
        """Soft bans a member from the server.

        A softban is basically banning the member from the server but
        then unbanning the member as well. This allows you to essentially
        kick the member while removing their messages.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Kick Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.guild.unban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def unban(self, ctx, member: BannedMember, *, reason: ActionReason = None):
        """Unbans a member from the server.

        You can pass either the ID of the banned member or the Name#Discrim
        combination of the member. Typically the ID is easiest to use.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.unban(member.user, reason=reason)
        if member.reason:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}), previously banned for {member.reason}.')
        else:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}).')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def tempban(self, ctx, duration: time.FutureTime, member: MemberID, *, reason: ActionReason = None):
        """Temporarily bans a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.get_cog('Reminder')
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        until = f'until {duration.dt:%Y-%m-%dT%H:%M UTC}'
        heads_up_message = f'You have been banned from {ctx.guild.name} {until}. Reason: {reason}'

        try:
            await member.send(heads_up_message)
        except (AttributeError, discord.HTTPException):
            # best attempt, oh well.
            pass

        reason = safe_reason_append(reason, until)
        await ctx.guild.ban(member, reason=reason)
        timer = await reminder.create_timer(duration.dt, 'tempban', ctx.guild.id,
                                                                    ctx.author.id,
                                                                    member.id,
                                                                    connection=ctx.db,
                                                                    created=ctx.message.created_at)
        await ctx.send(f'Banned {member} for {time.human_timedelta(duration.dt, source=timer.created_at)}.')

    @commands.Cog.listener()
    async def on_tempban_timer_complete(self, timer):
        guild_id, mod_id, member_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        moderator = guild.get_member(mod_id)
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

        reason = f'Automatic unban from timer made on {timer.created_at} by {moderator}.'
        await guild.unban(discord.Object(id=member_id), reason=reason)

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def mentionspam(self, ctx, count: int=None):
        """Enables auto-banning accounts that spam mentions.

        If a message contains `count` or more mentions then the
        bot will automatically attempt to auto-ban the member.
        The `count` must be greater than 3. If the `count` is 0
        then this is disabled.

        This only applies for user mentions. Everyone or Role
        mentions are not included.

        To use this command you must have the Ban Members permission.
        """

        if count is None:
            query = """SELECT mention_count, COALESCE(safe_mention_channel_ids, '{}') AS channel_ids
                       FROM guild_mod_config
                       WHERE id=$1;
                    """

            row = await ctx.db.fetchrow(query, ctx.guild.id)
            if row is None or not row['mention_count']:
                return await ctx.send('This server has not set up mention spam banning.')

            ignores = ', '.join(f'<#{e}>' for e in row['channel_ids']) or 'None'
            return await ctx.send(f'- Threshold: {row["mention_count"]} mentions\n- Ignored Channels: {ignores}')

        if count == 0:
            query = """UPDATE guild_mod_config SET mention_count = NULL WHERE id=$1;"""
            await ctx.db.execute(query, ctx.guild.id)
            self.get_guild_config.invalidate(self, ctx.guild.id)
            return await ctx.send('Auto-banning members has been disabled.')

        if count <= 3:
            await ctx.send('\N{NO ENTRY SIGN} Auto-ban threshold must be greater than three.')
            return

        query = """INSERT INTO guild_mod_config (id, mention_count, safe_mention_channel_ids)
                   VALUES ($1, $2, '{}')
                   ON CONFLICT (id) DO UPDATE SET
                       mention_count = $2;
                """
        await ctx.db.execute(query, ctx.guild.id, count)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Now auto-banning members that mention more than {count} users.')

    @mentionspam.command(name='ignore', aliases=['bypass'])
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def mentionspam_ignore(self, ctx, *channels: discord.TextChannel):
        """Specifies what channels ignore mentionspam auto-bans.

        If a channel is given then that channel will no longer be protected
        by auto-banning from mention spammers.

        To use this command you must have the Ban Members permission.
        """

        query = """UPDATE guild_mod_config
                   SET safe_mention_channel_ids =
                       ARRAY(SELECT DISTINCT * FROM unnest(COALESCE(safe_mention_channel_ids, '{}') || $2::bigint[]))
                   WHERE id = $1;
                """

        if len(channels) == 0:
            return await ctx.send('Missing channels to ignore.')

        channel_ids = [c.id for c in channels]
        await ctx.db.execute(query, ctx.guild.id, channel_ids)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Mentions are now ignored on {", ".join(c.mention for c in channels)}.')

    @mentionspam.command(name='unignore', aliases=['protect'])
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def mentionspam_unignore(self, ctx, *channels: discord.TextChannel):
        """Specifies what channels to take off the ignore list.

        To use this command you must have the Ban Members permission.
        """

        if len(channels) == 0:
            return await ctx.send('Missing channels to protect.')

        query = """UPDATE guild_mod_config
                   SET safe_mention_channel_ids =
                       ARRAY(SELECT element FROM unnest(safe_mention_channel_ids) AS element
                             WHERE NOT(element = ANY($2::bigint[])))
                   WHERE id = $1;
                """

        await ctx.db.execute(query, ctx.guild.id, [c.id for c in channels])
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send('Updated mentionspam ignore list.')

    @commands.group(aliases=['purge'])
    @commands.guild_only()
    @checks.has_permissions(manage_messages=True)
    async def remove(self, ctx):
        """Removes messages that meet a criteria.

        In order to use this command, you must have Manage Messages permissions.
        Note that the bot needs Manage Messages as well. These commands cannot
        be used in a private message.

        When the command is done doing its work, you will get a message
        detailing which users got removed and how many messages got removed.
        """

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    async def do_removal(self, ctx, limit, predicate, *, before=None, after=None):
        if limit > 2000:
            return await ctx.send(f'Too many messages to search given ({limit}/2000)')

        if before is None:
            before = ctx.message
        else:
            before = discord.Object(id=before)

        if after is not None:
            after = discord.Object(id=after)

        try:
            deleted = await ctx.channel.purge(limit=limit, before=before, after=after, check=predicate)
        except discord.Forbidden as e:
            return await ctx.send('I do not have permissions to delete messages.')
        except discord.HTTPException as e:
            return await ctx.send(f'Error: {e} (try a smaller search?)')

        spammers = Counter(m.author.display_name for m in deleted)
        deleted = len(deleted)
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'**{name}**: {count}' for name, count in spammers)

        to_send = '\n'.join(messages)

        if len(to_send) > 2000:
            await ctx.send(f'Successfully removed {deleted} messages.', delete_after=10)
        else:
            await ctx.send(to_send, delete_after=10)

    @remove.command()
    async def embeds(self, ctx, search=100):
        """Removes messages that have embeds in them."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds))

    @remove.command()
    async def files(self, ctx, search=100):
        """Removes messages that have attachments in them."""
        await self.do_removal(ctx, search, lambda e: len(e.attachments))

    @remove.command()
    async def images(self, ctx, search=100):
        """Removes messages that have embeds or attachments."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds) or len(e.attachments))

    @remove.command(name='all')
    async def _remove_all(self, ctx, search=100):
        """Removes all messages."""
        await self.do_removal(ctx, search, lambda e: True)

    @remove.command()
    async def user(self, ctx, member: discord.Member, search=100):
        """Removes all messages by the member."""
        await self.do_removal(ctx, search, lambda e: e.author == member)

    @remove.command()
    async def contains(self, ctx, *, substr: str):
        """Removes all messages containing a substring.

        The substring must be at least 3 characters long.
        """
        if len(substr) < 3:
            await ctx.send('The substring length must be at least 3 characters.')
        else:
            await self.do_removal(ctx, 100, lambda e: substr in e.content)

    @remove.command(name='bot', aliases=['bots'])
    async def _bot(self, ctx, prefix=None, search=100):
        """Removes a bot user's messages and messages with their optional prefix."""

        def predicate(m):
            return (m.webhook_id is None and m.author.bot) or (prefix and m.content.startswith(prefix))

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='emoji', aliases=['emojis'])
    async def _emoji(self, ctx, search=100):
        """Removes all messages containing custom emoji."""
        custom_emoji = re.compile(r'<a?:[a-zA-Z0-9\_]+:([0-9]+)>')
        def predicate(m):
            return custom_emoji.search(m.content)

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='reactions')
    async def _reactions(self, ctx, search=100):
        """Removes all reactions from messages that have them."""

        if search > 2000:
            return await ctx.send(f'Too many messages to search for ({search}/2000)')

        total_reactions = 0
        async for message in ctx.history(limit=search, before=ctx.message):
            if len(message.reactions):
                total_reactions += sum(r.count for r in message.reactions)
                await message.clear_reactions()

        await ctx.send(f'Successfully removed {total_reactions} reactions.')

    @remove.command()
    async def custom(self, ctx, *, args: str):
        """A more advanced purge command.

        This command uses a powerful "command line" syntax.
        Most options support multiple values to indicate 'any' match.
        If the value has spaces it must be quoted.

        The messages are only deleted if all options are met unless
        the `--or` flag is passed, in which case only if any is met.

        The following options are valid.

        `--user`: A mention or name of the user to remove.
        `--contains`: A substring to search for in the message.
        `--starts`: A substring to search if the message starts with.
        `--ends`: A substring to search if the message ends with.
        `--search`: How many messages to search. Default 100. Max 2000.
        `--after`: Messages must come after this message ID.
        `--before`: Messages must come before this message ID.

        Flag options (no arguments):

        `--bot`: Check if it's a bot user.
        `--embeds`: Check if the message has embeds.
        `--files`: Check if the message has attachments.
        `--emoji`: Check if the message has custom emoji.
        `--reactions`: Check if the message has reactions
        `--or`: Use logical OR for all options.
        `--not`: Use logical NOT for all options.
        """
        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('--user', nargs='+')
        parser.add_argument('--contains', nargs='+')
        parser.add_argument('--starts', nargs='+')
        parser.add_argument('--ends', nargs='+')
        parser.add_argument('--or', action='store_true', dest='_or')
        parser.add_argument('--not', action='store_true', dest='_not')
        parser.add_argument('--emoji', action='store_true')
        parser.add_argument('--bot', action='store_const', const=lambda m: m.author.bot)
        parser.add_argument('--embeds', action='store_const', const=lambda m: len(m.embeds))
        parser.add_argument('--files', action='store_const', const=lambda m: len(m.attachments))
        parser.add_argument('--reactions', action='store_const', const=lambda m: len(m.reactions))
        parser.add_argument('--search', type=int)
        parser.add_argument('--after', type=int)
        parser.add_argument('--before', type=int)

        try:
            args = parser.parse_args(shlex.split(args))
        except Exception as e:
            await ctx.send(str(e))
            return

        predicates = []
        if args.bot:
            predicates.append(args.bot)

        if args.embeds:
            predicates.append(args.embeds)

        if args.files:
            predicates.append(args.files)

        if args.reactions:
            predicates.append(args.reactions)

        if args.emoji:
            custom_emoji = re.compile(r'<:(\w+):(\d+)>')
            predicates.append(lambda m: custom_emoji.search(m.content))

        if args.user:
            users = []
            converter = commands.MemberConverter()
            for u in args.user:
                try:
                    user = await converter.convert(ctx, u)
                    users.append(user)
                except Exception as e:
                    await ctx.send(str(e))
                    return

            predicates.append(lambda m: m.author in users)

        if args.contains:
            predicates.append(lambda m: any(sub in m.content for sub in args.contains))

        if args.starts:
            predicates.append(lambda m: any(m.content.startswith(s) for s in args.starts))

        if args.ends:
            predicates.append(lambda m: any(m.content.endswith(s) for s in args.ends))

        op = all if not args._or else any
        def predicate(m):
            r = op(p(m) for p in predicates)
            if args._not:
                return not r
            return r

        if args.after:
            if args.search is None:
                args.search = 2000

        if args.search is None:
            args.search = 100

        args.search = max(0, min(2000, args.search)) # clamp from 0-2000
        await self.do_removal(ctx, args.search, predicate, before=args.before, after=args.after)

    # Mute related stuff

    async def update_mute_role(self, ctx, config, role, *, merge=False):
        guild = ctx.guild
        if config and merge:
            members = config.muted_members
            # If the roles are being merged then the old members should get the new role
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id}): Merging mute roles'
            for member_id in members:
                member = guild.get_member(member_id)
                if member is not None and not member._roles.has(role.id):
                    try:
                        await member.add_roles(role, reason=reason)
                    except discord.HTTPException:
                        pass
        else:
            members = set()

        members.update(map(lambda m: m.id, role.members))
        query = """INSERT INTO guild_mod_config (id, mute_role_id, muted_members)
                   VALUES ($1, $2, $3::bigint[]) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id,
                       muted_members = EXCLUDED.muted_members
                """
        await self.bot.pool.execute(query, guild.id, role.id, list(members))
        self.get_guild_config.invalidate(self, guild.id)

    @staticmethod
    async def update_mute_role_permissions(role, guild, invoker):
        success = 0
        failure = 0
        skipped = 0
        reason = f'Action done by {invoker} (ID: {invoker.id})'
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.manage_roles:
                overwrite = channel.overwrites_for(role)
                overwrite.send_messages = False
                overwrite.add_reactions = False
                try:
                    await channel.set_permissions(role, overwrite=overwrite, reason=reason)
                except discord.HTTPException:
                    failure += 1
                else:
                    success += 1
            else:
                skipped += 1
        return success, failure, skipped

    @commands.group(name='mute', invoke_without_command=True)
    @can_mute()
    async def _mute(self, ctx, members: commands.Greedy[discord.Member], *, reason: ActionReason = None):
        """Mutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to mute.')

        failed = 0
        for member in members:
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Muted [{total - failed}/{total}]')

    @commands.command(name='unmute')
    @can_mute()
    async def _unmute(self, ctx, members: commands.Greedy[discord.Member], *, reason: ActionReason = None):
        """Unmutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to mute.')

        failed = 0
        for member in members:
            try:
                await member.remove_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Unmuted [{total - failed}/{total}]')


    @commands.command()
    @can_mute()
    async def tempmute(self, ctx, duration: time.FutureTime, member: discord.Member, *, reason: ActionReason = None):
        """Temporarily mutes a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC.

        This has the same permissions as the `mute` command.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.get_cog('Reminder')
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        role_id = ctx.guild_config.mute_role_id
        await member.add_roles(discord.Object(id=role_id), reason=reason)
        timer = await reminder.create_timer(duration.dt, 'tempmute', ctx.guild.id,
                                                                     ctx.author.id,
                                                                     member.id,
                                                                     role_id,
                                                                     created=ctx.message.created_at)
        delta = time.human_timedelta(duration.dt, source=timer.created_at)
        await ctx.send(f'Muted {discord.utils.escape_mentions(str(member))} for {delta}.')

    @commands.Cog.listener()
    async def on_tempmute_timer_complete(self, timer):
        guild_id, mod_id, member_id, role_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        member = guild.get_member(member_id)
        if member is None or not member._roles.has(role_id):
            # They left or don't have the role any more so it has to be manually changed in the SQL
            # if applicable, of course
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))
            return

        if mod_id != member_id:
            moderator = guild.get_member(mod_id)
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

            reason = f'Automatic unmute from timer made on {timer.created_at} by {moderator}.'
        else:
            reason = f'Expiring self-mute made on {timer.created_at} by {member}'

        try:
            await member.remove_roles(discord.Object(id=role_id), reason=reason)
        except discord.HTTPException:
            # if the request failed then just do it manually
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))

    @_mute.group(name='role', invoke_without_command=True)
    @checks.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def _mute_role(self, ctx):
        """Shows configuration of the mute role.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is not None:
            members = config.muted_members.copy()
            members.update(map(lambda r: r.id, role.members))
            total = len(members)
            role = f'{role} (ID: {role.id})'
        else:
            total = 0
        await ctx.send(f'Role: {role}\nMembers Muted: {total}')

    @_mute_role.command(name='set')
    @checks.has_guild_permissions(manage_guild=True, manage_roles=True)
    @commands.cooldown(1, 60.0, commands.BucketType.guild)
    async def mute_role_set(self, ctx, *, role: discord.Role):
        """Sets the mute role to a pre-existing role.

        This command can only be used once every minute.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        if role.is_default():
            return await ctx.send('Cannot use the @\u200beveryone role.')

        if role > ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send('This role is higher than your highest role.')

        if role > ctx.me.top_role:
            return await ctx.send('This role is higher than my highest role.')

        config = await self.get_guild_config(ctx.guild.id)
        has_pre_existing = config is not None and config.mute_role is not None
        merge = False
        author_id = ctx.author.id

        if has_pre_existing:
            if not ctx.channel.permissions_for(ctx.me).add_reactions:
                return await ctx.send('The bot is missing Add Reactions permission.')

            msg = '\N{WARNING SIGN} **There seems to be a pre-existing mute role set up.**\n\n' \
                  'If you want to abort the set-up process react with \N{CROSS MARK}.\n' \
                  'If you want to merge the pre-existing member data with the new member data react with \u2934.\n' \
                  'If you want to replace pre-existing member data with the new member data react with \U0001f504.\n\n' \
                  '**Note: Merging is __slow__. It will also add the role to every possible member that needs it.**'

            sent = await ctx.send(msg)
            emojis = {
               '\N{CROSS MARK}': ...,
               '\u2934': True,
               '\U0001f504': False,
            }

            def check(payload):
                nonlocal merge
                if payload.message_id != sent.id or payload.user_id != author_id:
                    return False

                codepoint = str(payload.emoji)
                try:
                    merge = emojis[codepoint]
                except KeyError:
                    return False
                else:
                    return True

            for emoji in emojis:
                await sent.add_reaction(emoji)

            try:
                await self.bot.wait_for('raw_reaction_add', check=check, timeout=120.0)
            except asyncio.TimeoutError:
                return await ctx.send('Took too long. Aborting.')
            finally:
                await sent.delete()
        else:
            muted_members = len(role.members)
            if muted_members > 0:
                msg = f'Are you sure you want to make this the mute role? It has {plural(muted_members):member}.'
                confirm = await ctx.prompt(msg, reacquire=False)
                if not confirm:
                    merge = ...

        if merge is ...:
            return await ctx.send('Aborting.')

        async with ctx.typing():
            await self.update_mute_role(ctx, config, role, merge=merge)
            escaped = discord.utils.escape_mentions(role.name)
            await ctx.send(f'Successfully set the {escaped} role as the mute role.\n\n'
                            '**Note: Permission overwrites have not been changed.**')

    @_mute_role.command(name='update', aliases=['sync'])
    @checks.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def mute_role_update(self, ctx):
        """Updates the permission overwrites of the mute role.

        This works by blocking the Send Messages and Add Reactions
        permission on every text channel that the bot can do.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is None:
            return await ctx.send('No mute role has been set up to update.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            total = success + failure + skipped
            await ctx.send(f'Attempted to update {total} channel permissions. '
                           f'[Updated: {success}, Failed: {failure}, Skipped: {skipped}]')

    @_mute_role.command(name='create')
    @checks.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def mute_role_create(self, ctx, *, name):
        """Creates a mute role with the given name.

        This also updates the channel overwrites accordingly
        if wanted.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is not None and config.mute_role is not None:
            return await ctx.send('A mute role already exists.')

        try:
            role = await ctx.guild.create_role(name=name, reason=f'Mute Role Created By {ctx.author} (ID: {ctx.author.id})')
        except discord.HTTPException as e:
            return await ctx.send(f'An error happened: {e}')

        query = """INSERT INTO guild_mod_config (id, mute_role_id)
                   VALUES ($1, $2) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id;
                """
        await ctx.db.execute(query, guild_id, role.id)
        self.get_guild_config.invalidate(self, guild_id)

        confirm = await ctx.prompt('Would you like to update the channel overwrites as well?', reacquire=False)
        if not confirm:
            return await ctx.send('Mute role successfully created.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            await ctx.send('Mute role successfully created. Overwrites: '
                           f'[Updated: {success}, Failed: {failure}, Skipped: {skipped}]')

    @_mute_role.command(name='unbind')
    @checks.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def mute_role_unbind(self, ctx):
        """Unbinds a mute role without deleting it.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role is None:
            return await ctx.send('No mute role has been set up.')

        muted_members = len(config.muted_members)
        if muted_members > 0:
            msg = f'Are you sure you want to unbind and unmute {plural(muted_members):member}?'
            confirm = await ctx.prompt(msg, reacquire=False)
            if not confirm:
                return await ctx.send('Aborting.')

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)
        await ctx.send('Successfully unbound mute role.')

    @commands.command()
    @commands.guild_only()
    async def selfmute(self, ctx, *, duration: time.ShortTime):
        """Temporarily mutes yourself for the specified duration.

        The duration must be in a short time form, e.g. 4h. Can
        only mute yourself for a maximum of 24 hours and a minimum
        of 5 minutes.

        Do not ask a moderator to unmute you.
        """

        reminder = self.bot.get_cog('Reminder')
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        config = await self.get_guild_config(ctx.guild.id)
        role_id = config and config.mute_role_id
        if role_id is None:
            raise NoMuteRole()

        if ctx.author._roles.has(role_id):
            return await ctx.send('Somehow you are already muted <:rooThink:596576798351949847>')

        created_at = ctx.message.created_at
        if duration.dt > (created_at + datetime.timedelta(days=1)):
            return await ctx.send('Duration is too long. Must be at most 24 hours.')

        if duration.dt < (created_at + datetime.timedelta(minutes=5)):
            return await ctx.send('Duration is too short. Must be at least 5 minutes.')

        delta = time.human_timedelta(duration.dt, source=created_at)
        warning = f'Are you sure you want to be muted for {delta}?\n**Do not ask the moderators to undo this!**'
        confirm = await ctx.prompt(warning, reacquire=False)
        if not confirm:
            return await ctx.send('Aborting', delete_after=5.0)

        reason = f'Self-mute for {ctx.author} (ID: {ctx.author.id}) for {delta}'
        await ctx.author.add_roles(discord.Object(id=role_id), reason=reason)
        timer = await reminder.create_timer(duration.dt, 'tempmute', ctx.guild.id,
                                                                     ctx.author.id,
                                                                     ctx.author.id,
                                                                     role_id,
                                                                     created=created_at)

        await ctx.send(f'\N{OK HAND SIGN} Muted for {delta}. Be sure not to bother anyone about it.')

    @selfmute.error
    async def on_selfmute_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('Missing a duration to selfmute for.')

def setup(bot):
    bot.add_cog(Mod(bot))
