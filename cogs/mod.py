from discord.ext import commands
from .utils import checks, db, time, cache
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

## Configuration

class ModConfig:
    __slots__ = ('raid_mode', 'id', 'bot', 'broadcast_channel_id', 'mention_count', 'safe_mention_channel_ids')

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
        return self

    @property
    def broadcast_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.broadcast_channel_id)

## Converters

class MemberID(commands.Converter):
    async def convert(self, ctx, argument):
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                return int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(f"{argument} is not a valid member or member ID.") from None
        else:
            can_execute = ctx.author.id == ctx.bot.owner_id or \
                          ctx.author == ctx.guild.owner or \
                          ctx.author.top_role > m.top_role

            if not can_execute:
                raise commands.BadArgument('You cannot do this action on this user due to role hierarchy.')
            return m.id

class BannedMember(commands.Converter):
    async def convert(self, ctx, argument):
        ban_list = await ctx.guild.bans()
        try:
            member_id = int(argument, base=10)
            entity = discord.utils.find(lambda u: u.user.id == member_id, ban_list)
        except ValueError:
            entity = discord.utils.find(lambda u: str(u.user) == argument, ban_list)

        if entity is None:
            raise commands.BadArgument("Not a valid previously-banned member.")
        return entity

class ActionReason(commands.Converter):
    async def convert(self, ctx, argument):
        ret = f'{ctx.author} (ID: {ctx.author.id}): {argument}'

        if len(ret) > 512:
            reason_max = 512 - len(ret) - len(argument)
            raise commands.BadArgument(f'reason is too long ({len(argument)}/{reason_max})')
        return ret

## The actual cog

class Mod:
    """Moderation related commands."""

    def __init__(self, bot):
        self.bot = bot

        # guild_id: set(user_id)
        self._recently_kicked = defaultdict(set)

    def __repr__(self):
        return '<cogs.Mod>'

    async def __error(self, ctx, error):
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

    @cache.cache()
    async def get_guild_config(self, guild_id):
        query = """SELECT * FROM guild_mod_config WHERE id=$1;"""
        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            if record is not None:
                return await ModConfig.from_record(record, self.bot)
            return None

    async def check_raid(self, config, guild, member, timestamp):
        if config.raid_mode != RaidMode.strict.value:
            return

        delta  = (member.joined_at - member.created_at).total_seconds() // 60

        # they must have created their account at most 30 minutes before they joined.
        if delta > 30:
            return

        delta = (timestamp - member.joined_at).total_seconds() // 60

        # check if this is their first action in the 30 minutes they joined
        if delta > 30:
            return

        try:
            fmt = f"""Howdy. The server {guild.name} is currently in a raid mode lockdown.

                   A raid is when a server is being bombarded with trolls or low effort posts.
                   Unfortunately, what this means is that you have been automatically kicked for
                   meeting the suspicious thresholds currently set.

                   **Do not worry though, as you will be able to join again in the future!**
                   """

            fmt = cleandoc(fmt)
            await member.send(fmt)
        except discord.HTTPException:
            pass

        # kick anyway
        try:
            await member.kick(reason='Strict raid mode')
        except discord.HTTPException:
            log.info(f'[Raid Mode] Failed to kick {member} (ID: {member.id}) from server {member.guild} via strict mode.')
        else:
            log.info(f'[Raid Mode] Kicked {member} (ID: {member.id}) from server {member.guild} via strict mode.')
            self._recently_kicked[guild.id].add(member.id)

    async def on_message(self, message):
        author = message.author
        if author.id in (self.bot.user.id, self.bot.owner_id):
            return

        if message.guild is None:
            return

        if not isinstance(author, discord.Member):
            return

        # we're going to ignore members with roles
        if len(author.roles) > 1:
            return

        guild_id = message.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        # check for raid mode stuff
        await self.check_raid(config, message.guild, author, message.created_at)

        # auto-ban tracking for mention spams begin here
        if len(message.mentions) <= 3:
            return

        if not config.mention_count:
            return

        # check if it meets the thresholds required
        mention_count = sum(not m.bot for m in message.mentions)
        if mention_count < config.mention_count:
            return

        if message.channel.id in config.safe_mention_channel_ids:
            return

        try:
            await author.ban(reason=f'Spamming mentions ({mention_count} mentions)')
        except Exception as e:
            log.info(f'Failed to autoban member {author} (ID: {author.id}) in guild ID {guild_id}')
        else:
            await message.channel.send(f'Banned {author} (ID: {author.id}) for spamming {mention_count} mentions.')
            log.info(f'Member {author} (ID: {author.id}) has been autobanned from guild ID {guild_id}')

    async def on_voice_state_update(self, user, before, after):
        if not isinstance(user, discord.Member):
            return

        # joined a voice channel
        if before.channel is None and after.channel is not None:
            config = await self.get_guild_config(user.guild.id)
            if config is None:
                return

            await self.check_raid(config, user.guild, user, datetime.datetime.utcnow())

    async def on_member_join(self, member):
        config = await self.get_guild_config(member.guild.id)
        if config is None or not config.raid_mode:
            return

        now = datetime.datetime.utcnow()

        # these are the dates in minutes
        created = (now - member.created_at).total_seconds() // 60
        was_kicked = False

        if config.raid_mode == RaidMode.strict.value:
            was_kicked = self._recently_kicked.get(member.guild.id)
            if was_kicked is not None:
                try:
                    was_kicked.remove(member.id)
                except KeyError:
                    was_kicked = False
                else:
                    was_kicked = True

        # Do the broadcasted message to the channel
        if was_kicked:
            title = 'Member Re-Joined'
            colour = 0xdd5f53 # red
        else:
            title = 'Member Joined'
            colour = 0x53dda4 # green

            if created < 30:
                colour = 0xdda453 # yellow

        e = discord.Embed(title=title, colour=colour)
        e.timestamp = now
        e.set_footer(text='Created')
        e.set_author(name=str(member), icon_url=member.avatar_url)
        e.add_field(name='ID', value=member.id)
        e.add_field(name='Joined', value=member.joined_at)
        e.add_field(name='Created', value=time.human_timedelta(member.created_at), inline=False)

        if config.broadcast_channel:
            await config.broadcast_channel.send(embed=e)

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
            body = f'joined {time.human_timedelta(member.joined_at)}, created {time.human_timedelta(member.created_at)}'
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
            fmt = f'Raid Mode: {RaidMode(row[0])}\nBroadcast Channel: {ch}'

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

        query = """INSERT INTO guild_mod_config (id, raid_mode, broadcast_channel)
                   VALUES ($1, $2, NULL) ON CONFLICT (id)
                   DO UPDATE SET
                        raid_mode = EXCLUDED.raid_mode,
                        broadcast_channel = NULL;
                """

        await ctx.db.execute(query, ctx.guild.id, RaidMode.off.value)
        self._recently_kicked.pop(ctx.guild.id, None)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send('Raid mode disabled. No longer broadcasting join messages.')

    @raid.command(name='strict')
    @checks.is_mod()
    async def raid_strict(self, ctx, *, channel: discord.TextChannel = None):
        """Enables strict raid mode on the server.

        Strict mode is similar to regular enabled raid mode, with the added
        benefit of auto-kicking members that meet the following requirements:

        - Account creation date and join date are at most 30 minutes apart.
        - First message recorded on the server is 30 minutes apart from join date.
        - Joining a voice channel within 30 minutes of joining.

        Members who meet these requirements will get a private message saying that the
        server is currently in lock down.

        If this is considered too strict, it is recommended to fall back to regular
        raid mode.
        """
        channel = channel or ctx.channel

        if not ctx.me.guild_permissions.kick_members:
            return await ctx.send('\N{NO ENTRY SIGN} I do not have permissions to kick members.')

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

        await ctx.db.execute(query, ctx.guild.id, RaidMode.strict.value, ctx.channel.id)
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
    async def kick(self, ctx, member: discord.Member, *, reason: ActionReason = None):
        """Kicks a member from the server.

        In order for this to work, the bot must have Kick Member permissions.

        To use this command you must have Kick Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await member.kick(reason=reason)
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

        await ctx.guild.ban(discord.Object(id=member), reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def massban(self, ctx, reason: ActionReason, *members: MemberID):
        """Mass bans multiple members from the server.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        Note that unlike the ban command, the reason comes first
        and is not optional.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        for member_id in members:
            await ctx.guild.ban(discord.Object(id=member_id), reason=reason)

        await ctx.send('\N{OK HAND SIGN}')

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

        obj = discord.Object(id=member)
        await ctx.guild.ban(obj, reason=reason)
        await ctx.guild.unban(obj, reason=reason)
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
        such as "2017-12-31".

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

        await ctx.guild.ban(discord.Object(id=member), reason=reason)
        timer = await reminder.create_timer(duration.dt, 'tempban', ctx.guild.id, ctx.author.id, member, connection=ctx.db)
        await ctx.send(f'Banned ID {member} for {time.human_timedelta(duration.dt)}.')

    async def on_tempban_timer_complete(self, timer):
        guild_id, mod_id, member_id = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        moderator = guild.get_member(mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.get_user_info(mod_id)
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
            help_cmd = self.bot.get_command('help')
            await ctx.invoke(help_cmd, command='remove')

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

    @remove.command(name='bot')
    async def _bot(self, ctx, prefix=None, search=100):
        """Removes a bot user's messages and messages with their optional prefix."""

        def predicate(m):
            return m.author.bot or (prefix and m.content.startswith(prefix))

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='emoji')
    async def _emoji(self, ctx, search=100):
        """Removes all messages containing custom emoji."""
        custom_emoji = re.compile(r'<:(\w+):(\d+)>')
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
        parser.add_argument('--search', type=int, default=100)
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

        args.search = max(0, min(2000, args.search)) # clamp from 0-2000
        await self.do_removal(ctx, args.search, predicate, before=args.before, after=args.after)

def setup(bot):
    bot.add_cog(Mod(bot))
