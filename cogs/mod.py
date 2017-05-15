from discord.ext import commands
from .utils import config, checks
from .utils.formats import human_timedelta
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

class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)

class RaidMode(enum.Enum):
    off = 0
    on = 1
    strict = 2

    def __str__(self):
        return self.name

def object_hook(obj):
    if '__raids__' in obj:
        raids = obj['__raids__']
        obj['__raids__'] = { k: (RaidMode(mode), v) for k, (mode, v) in raids.items() }
        return obj
    else:
        return obj

class RaidModeEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, RaidMode):
            return o.value

        super().default(o)

class Mod:
    """Moderation related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('mod.json', loop=bot.loop, object_hook=object_hook, encoder=RaidModeEncoder)

        # guild_id: set(user_id)
        self._recently_kicked = defaultdict(set)

    def bot_user(self, message):
        return message.server.me if message.channel.is_private else self.bot.user

    def is_plonked(self, server, member):
        db = self.config.get('plonks', {}).get(server.id, [])
        bypass_ignore = member.server_permissions.manage_server
        if not bypass_ignore and member.id in db:
            return True
        return False

    def __check(self, ctx):
        msg = ctx.message
        if checks.is_owner_check(msg):
            return True

        # user is bot banned
        if msg.server:
            if self.is_plonked(msg.server, msg.author):
                return False

        # check if the channel is ignored
        # but first, resolve their permissions

        perms = msg.channel.permissions_for(msg.author)
        bypass_ignore = perms.administrator

        # now we can finally realise if we can actually bypass the ignore.

        if not bypass_ignore and msg.channel.id in self.config.get('ignored', []):
            return False

        return True

    async def check_raid(self, guild, member, timestamp):
        if not isinstance(member, discord.Member):
            return

        raids = self.config.get('__raids__', {}).get(guild.id, (RaidMode.off, None))
        if raids[0] is not RaidMode.strict:
            return

        delta  = (member.joined_at - member.created_at).total_seconds() // 60

        # they must have created their account at most 30 minutes before they joined.
        if delta > 30:
            return

        delta = (timestamp - member.joined_at).total_seconds() // 60

        # check if this is their first action in the 30 minutes they joined
        if delta > 30:
            return

        fmt = """Howdy. The server {0.name} is currently in a raid mode lockdown.

              A raid is when a server is being bombarded with trolls or low effort posts.
              Unfortunately, what this means is that you have been automatically kicked for
              meeting the suspicious thresholds currently set.

              **Do not worry though, as you will be able to join again in the future!**

              Please use this invite in an hour or so when things have cooled down! {1}
              """

        try:
            invite = await self.bot.create_invite(guild, max_uses=1, unique=True)
            fmt = cleandoc(fmt).format(guild, invite)
            await self.bot.send_message(member, fmt)
        except discord.HTTPException:
            pass

        # kick anyway
        channel = self.bot.get_channel(raids[1])
        try:
            await self.bot.kick(member)
        except discord.HTTPException:
            log.info('[Raid Mode] Failed to kick {0} (ID: {0.id}) from server {0.server} via strict mode.'.format(member))
            await self.bot.send_message(channel, 'Failed to kick {0} (ID: {0.id}) from the server via strict raid mode.'.format(member))
        else:
            log.info('[Raid Mode] Kicked {0} (ID: {0.id}) from server {0.server} via strict mode.'.format(member))
            await self.bot.send_message(channel, 'Kicked {0} (ID: {0.id}) from the server via strict raid mode.'.format(member))
            self._recently_kicked[guild.id].add(member.id)

    async def on_message(self, message):
        if message.author == self.bot.user or checks.is_owner_check(message):
            return

        if message.server is None:
            return

        # check for raid mode stuff
        await self.check_raid(message.server, message.author, message.timestamp)

        # auto-ban tracking for mention spams begin here

        if len(message.mentions) <= 3:
            return

        counts = self.config.get('mentions', {})
        settings = counts.get(message.server.id)
        if settings is None:
            return

        # check if it meets the thresholds required
        mention_count = sum(not m.bot for m in message.mentions)
        if mention_count < settings['count']:
            return

        if message.channel.id in settings.get('ignored', []):
            return

        try:
            await self.bot.ban(message.author)
        except Exception as e:
            log.info('Failed to autoban member {0.author} (ID: {0.author.id}) in server {0.server}'.format(message))
        else:
            fmt = '{0} (ID: {0.id}) has been banned for spamming mentions ({1} mentions).'
            await self.bot.send_message(message.channel, fmt.format(message.author, len(message.mentions)))
            log.info('Member {0.author} (ID: {0.author.id}) has been autobanned from server {0.server}'.format(message))

    async def on_voice_state_update(self, before, after):
        # joined a voice channel
        if before.voice_channel is None and after.voice_channel is not None:
            await self.check_raid(after.server, after, datetime.datetime.utcnow())

    async def on_member_join(self, member):
        raids = self.config.get('__raids__', {})
        data = raids.get(member.server.id, (RaidMode.off, None))
        if data[0] is RaidMode.off:
            return

        now = datetime.datetime.utcnow()

        # these are the dates in minutes
        created = (now - member.created_at).total_seconds() // 60
        was_kicked = False

        if data[0] is RaidMode.strict:
            was_kicked = self._recently_kicked.get(member.server.id)
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
        e.timestamp = member.created_at
        e.set_footer(text='Created')
        e.set_author(name=str(member), icon_url=member.avatar_url or member.default_avatar_url)
        e.add_field(name='Created', value=human_timedelta(member.created_at))
        e.add_field(name='ID', value=member.id)
        e.add_field(name='Joined', value=member.joined_at)
        channel = self.bot.get_channel(data[1])
        await self.bot.send_message(channel, embed=e)

    @commands.command(pass_context=True, no_pm=True, aliases=['newmembers'])
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have
        joined.

        The count parameter can only be up to 25.
        """
        guild = ctx.message.server
        count = max(min(count, 25), 5)

        members = sorted(guild.members, key=lambda m: m.joined_at, reverse=True)[:count]

        e = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            body = 'joined {0}, created {1}'.format(human_timedelta(member.joined_at),
                                                    human_timedelta(member.created_at))
            e.add_field(name='{0} (ID: {0.id})'.format(member), value=body, inline=False)

        await self.bot.say(embed=e)

    @commands.group(aliases=['raids'], pass_context=True, invoke_without_command=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def raid(self, ctx):
        """Controls raid mode on the server.

        Calling this command with no arguments will show the current raid
        mode information.

        You must have Manage Server permissions or have the Bot Admin role
        to use this command or its subcommands.
        """

        raids = self.config.get('__raids__', {})
        # Raid data is [type, channel_id?], i.e. a tuple
        data = raids.get(ctx.message.server.id, (RaidMode.off, None))

        fmt = 'Raid Mode: {}\nBroadcast Channel: {}'
        ch = '<#%s>' % data[1] if data[1] else None
        await self.bot.say(fmt.format(data[0], ch))

    @raid.command(name='on', aliases=['enable', 'enabled'], pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def raid_on(self, ctx, *, channel: discord.Channel = None):
        """Enables basic raid mode on the server.

        When enabled, server verification level is set to table flip
        levels and allows the bot to broadcast new members joining
        to a specified channel.

        If no channel is given, then the bot will broadcast join
        messages on the channel this command was used in.
        """

        if channel is None:
            channel = ctx.message.channel

        if channel.type is not discord.ChannelType.text:
            return await self.bot.say('That is not a text channel.')

        guild = ctx.message.server
        try:
            await self.bot.edit_server(guild, verification_level=discord.VerificationLevel.high)
        except discord.HTTPException:
            await self.bot.say('\N{WARNING SIGN} Could not set verification level.')

        raids = self.config.get('__raids__', {})
        raids[guild.id] = (RaidMode.on, channel.id)
        await self.config.put('__raids__', raids)
        fmt = 'Raid mode enabled. Broadcasting join messages to %s.' % channel.mention
        await self.bot.say(fmt)

    @raid.command(name='off', aliases=['disable', 'disabled'], pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def raid_off(self, ctx):
        """Disables raid mode on the server.

        When disabled, the server verification levels are set
        back to Low levels and the bot will stop broadcasting
        join messages.
        """

        guild = ctx.message.server

        try:
            await self.bot.edit_server(guild, verification_level=discord.VerificationLevel.low)
        except discord.HTTPException:
            await self.bot.say('\N{WARNING SIGN} Could not set verification level.')

        raids = self.config.get('__raids__', {})
        raids[guild.id] = (RaidMode.off, None)
        self._recently_kicked.pop(guild.id, None)
        await self.config.put('__raids__', raids)
        await self.bot.say('Raid mode disabled. No longer broadcasting join messages.')

    @raid.command(name='strict', pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def raid_strict(self, ctx, *, channel: discord.Channel = None):
        """Enables strict raid mode on the server.

        Strict mode is similar to regular enabled raid mode, with the added
        benefit of auto-kicking members that meet the following requirements:

        - Account creation date and join date are at most 30 minutes apart.
        - First message recorded on the server is 30 minutes apart from join date.
        - Joining a voice channel within 30 minutes of joining.

        Members who meet these requirements will get a private message saying that the
        server is currently in lock down and if they are legitimate to join back at a
        later time via a created one-time use invite.

        If this is considered too strict, it is recommended to fall back to regular
        raid mode.
        """

        if channel is None:
            channel = ctx.message.channel

        if channel.type is not discord.ChannelType.text:
            return await self.bot.say('That is not a text channel.')

        guild = ctx.message.server
        my_permissions = guild.default_channel.permissions_for(guild.me)

        if not (my_permissions.kick_members and my_permissions.create_instant_invite):
            return await self.bot.say('\N{NO ENTRY SIGN} I do not have permissions to kick members or create invites.')

        try:
            await self.bot.edit_server(guild, verification_level=discord.VerificationLevel.high)
        except discord.HTTPException:
            await self.bot.say('\N{WARNING SIGN} Could not set verification level.')

        raids = self.config.get('__raids__', {})
        raids[guild.id] = (RaidMode.strict, channel.id)
        await self.config.put('__raids__', raids)

        fmt = 'Raid mode enabled strictly. Broadcasting join messages to %s.' % channel.mention
        await self.bot.say(fmt)

    @commands.group(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_channels=True)
    async def ignore(self, ctx):
        """Handles the bot's ignore lists.

        To use these commands, you must have the Bot Admin role or have
        Manage Channels permissions. These commands are not allowed to be used
        in a private message context.

        Users with Manage Roles or Bot Admin role can still invoke the bot
        in ignored channels.
        """
        if ctx.invoked_subcommand is None:
            await self.bot.say('Invalid subcommand passed: {0.subcommand_passed}'.format(ctx))

    @ignore.command(name='list', pass_context=True)
    async def ignore_list(self, ctx):
        """Tells you what channels are currently ignored in this server."""

        ignored = self.config.get('ignored', [])
        channel_ids = set(c.id for c in ctx.message.server.channels)
        result = []
        for channel in ignored:
            if channel in channel_ids:
                result.append('<#{}>'.format(channel))

        if result:
            await self.bot.say('The following channels are ignored:\n\n{}'.format(', '.join(result)))
        else:
            await self.bot.say('I am not ignoring any channels here.')

    @ignore.command(name='channel', pass_context=True)
    async def channel_cmd(self, ctx, *, channel : discord.Channel = None):
        """Ignores a specific channel from being processed.

        If no channel is specified, the current channel is ignored.
        If a channel is ignored then the bot does not process commands in that
        channel until it is unignored.
        """

        if channel is None:
            channel = ctx.message.channel

        ignored = self.config.get('ignored', [])
        if channel.id in ignored:
            await self.bot.say('That channel is already ignored.')
            return

        ignored.append(channel.id)
        await self.config.put('ignored', ignored)
        await self.bot.say('\U0001f44c')

    @ignore.command(name='all', pass_context=True)
    @checks.admin_or_permissions(manage_server=True)
    async def _all(self, ctx):
        """Ignores every channel in the server from being processed.

        This works by adding every channel that the server currently has into
        the ignore list. If more channels are added then they will have to be
        ignored by using the ignore command.

        To use this command you must have Manage Server permissions along with
        Manage Channels permissions. You could also have the Bot Admin role.
        """

        ignored = self.config.get('ignored', [])
        channels = ctx.message.server.channels
        ignored.extend(c.id for c in channels if c.type == discord.ChannelType.text)
        await self.config.put('ignored', list(set(ignored))) # make unique
        await self.bot.say('\U0001f44c')

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.admin_or_permissions(manage_channels=True)
    async def unignore(self, ctx, *channels: discord.Channel):
        """Unignores channels from being processed.

        If no channels are specified, it unignores the current channel.

        To use this command you must have the Manage Channels permission or have the
        Bot Admin role.
        """

        if len(channels) == 0:
            channels = (ctx.message.channel,)

        # a set is the proper data type for the ignore list
        # however, JSON only supports arrays and objects not sets.
        ignored = self.config.get('ignored', [])
        for channel in channels:
            try:
                ignored.remove(channel.id)
            except ValueError:
                pass

        await self.config.put('ignored', ignored)
        await self.bot.say('\N{OK HAND SIGN}')

    @unignore.command(name='all', pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_channels=True)
    async def unignore_all(self, ctx):
        """Unignores all channels in this server from being processed.

        To use this command you must have the Manage Channels permission or have the
        Bot Admin role.
        """
        channels = [c for c in ctx.message.server.channels if c.type is discord.ChannelType.text]
        await ctx.invoke(self.unignore, *channels)

    @commands.command(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def cleanup(self, ctx, search : int = 100):
        """Cleans up the bot's messages from the channel.

        If a search number is specified, it searches that many messages to delete.
        If the bot has Manage Messages permissions, then it will try to delete
        messages that look like they invoked the bot as well.

        After the cleanup is completed, the bot will send you a message with
        which people got their messages deleted and their count. This is useful
        to see which users are spammers.

        To use this command you must have Manage Messages permission or have the
        Bot Mod role.
        """

        spammers = Counter()
        channel = ctx.message.channel
        prefixes = self.bot.command_prefix
        if callable(prefixes):
            prefixes = prefixes(self.bot, ctx.message)

        def is_possible_command_invoke(entry):
            valid_call = any(entry.content.startswith(prefix) for prefix in prefixes)
            return valid_call and not entry.content[1:2].isspace()

        can_delete = channel.permissions_for(channel.server.me).manage_messages

        if not can_delete:
            api_calls = 0
            async for entry in self.bot.logs_from(channel, limit=search, before=ctx.message):
                if api_calls and api_calls % 5 == 0:
                    await asyncio.sleep(1.1)

                if entry.author == self.bot.user:
                    await self.bot.delete_message(entry)
                    spammers['Bot'] += 1
                    api_calls += 1

                if is_possible_command_invoke(entry):
                    try:
                        await self.bot.delete_message(entry)
                    except discord.Forbidden:
                        continue
                    else:
                        spammers[entry.author.display_name] += 1
                        api_calls += 1
        else:
            predicate = lambda m: m.author == self.bot.user or is_possible_command_invoke(m)
            deleted = await self.bot.purge_from(channel, limit=search, before=ctx.message, check=predicate)
            spammers = Counter(m.author.display_name for m in deleted)

        deleted = sum(spammers.values())
        messages = ['%s %s removed.' % (deleted, 'message was' if deleted == 1 else 'messages were')]
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(map(lambda t: '- **{0[0]}**: {0[1]}'.format(t), spammers))

        await self.bot.say('\n'.join(messages), delete_after=10)

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(kick_members=True)
    async def kick(self, *, member : discord.Member):
        """Kicks a member from the server.

        In order for this to work, the bot must have Kick Member permissions.

        To use this command you must have Kick Members permission or have the
        Bot Admin role.
        """

        try:
            await self.bot.kick(member)
        except discord.Forbidden:
            await self.bot.say('The bot does not have permissions to kick this member.')
        except discord.HTTPException:
            await self.bot.say('Kicking failed.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(ban_members=True)
    async def ban(self, *, member : discord.Member):
        """Bans a member from the server.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission or have the
        Bot Admin role.
        """

        try:
            await self.bot.ban(member)
        except discord.Forbidden:
            await self.bot.say('The bot does not have permissions to ban this member.')
        except discord.HTTPException:
            await self.bot.say('Banning failed.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True, pass_context=True)
    @checks.admin_or_permissions(ban_members=True)
    async def hackban(self, ctx, *member_ids: int):
        """Bans a member via their ID.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission or have the
        Bot Admin role.
        """

        if not ctx.message.server.me.server_permissions.ban_members:
            return await self.bot.say('The bot does not have permissions to ban members.')

        for member_id in member_ids:
            try:
                await self.bot.http.ban(member_id, ctx.message.server.id)
            except discord.HTTPException:
                pass

        await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(kick_members=True)
    async def softban(self, *, member : discord.Member):
        """Soft bans a member from the server.

        A softban is basically banning the member from the server but
        then unbanning the member as well. This allows you to essentially
        kick the member while removing their messages.

        To use this command you must have Kick Members permissions or have
        the Bot Admin role. Note that the bot must have the permission as well.
        """

        try:
            await self.bot.ban(member)
            await self.bot.unban(member.server, member)
        except discord.Forbidden:
            await self.bot.say('The bot does not have permissions to ban this member.')
        except discord.HTTPException:
            await self.bot.say('Banning failed.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True, pass_context=True)
    @checks.admin_or_permissions(manage_server=True)
    async def plonk(self, ctx, *, member: discord.Member):
        """Bans a user from using the bot.

        This bans a person from using the bot in the current server.
        There is no concept of a global ban. This ban can be bypassed
        by having the Manage Server permission.

        To use this command you must have the Manage Server permission
        or have a Bot Admin role.
        """

        plonks = self.config.get('plonks', {})
        guild_id = ctx.message.server.id
        db = plonks.get(guild_id, [])

        if member.id in db:
            await self.bot.say('That user is already bot banned in this server.')
            return

        db.append(member.id)
        plonks[guild_id] = db
        await self.config.put('plonks', plonks)
        await self.bot.say('%s has been banned from using the bot in this server.' % member)

    @commands.command(no_pm=True, pass_context=True)
    @checks.admin_or_permissions(manage_server=True)
    async def plonks(self, ctx):
        """Shows members banned from the bot."""
        plonks = self.config.get('plonks', {})
        guild = ctx.message.server
        db = plonks.get(guild.id, [])
        members = ', '.join(map(str, filter(None, map(guild.get_member, db))))
        if members:
            await self.bot.say(members)
        else:
            await self.bot.say('No members are banned in this server.')

    @commands.command(no_pm=True, pass_context=True)
    @checks.admin_or_permissions(manage_server=True)
    async def unplonk(self, ctx, *, member: discord.Member):
        """Unbans a user from using the bot.

        To use this command you must have the Manage Server permission
        or have a Bot Admin role.
        """

        plonks = self.config.get('plonks', {})
        guild_id = ctx.message.server.id
        db = plonks.get(guild_id, [])

        try:
            db.remove(member.id)
        except ValueError:
            await self.bot.say('%s is not banned from using the bot in this server.' % member)
        else:
            plonks[guild_id] = db
            await self.config.put('plonks', plonks)
            await self.bot.say('%s has been unbanned from using the bot in this server.' % member)

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.admin_or_permissions(ban_members=True)
    async def mentionspam(self, ctx, count: int=None):
        """Enables auto-banning accounts that spam mentions.

        If a message contains `count` or more mentions then the
        bot will automatically attempt to auto-ban the member.
        The `count` must be greater than 3. If the `count` is 0
        then this is disabled.

        This only applies for user mentions. Everyone or Role
        mentions are not included.

        To use this command you must have the Ban Members permission
        or have a Bot Admin role.
        """

        counts = self.config.get('mentions', {})
        settings = counts.get(ctx.message.server.id)
        if count is None:
            if settings is None:
                return await self.bot.say('This server has not set up mention spam banning.')

            ignores = ', '.join('<#%s>' % e for e in settings.get('ignore', []))
            ignores = ignores if ignores else 'None'
            count = settings['count']
            return await self.bot.say('- Threshold: %s mentions\n- Ignored Channels: %s' % (count, ignores))

        if count == 0:
            counts.pop(ctx.message.server.id, None)
            await self.config.put('mentions', counts)
            await self.bot.say('Auto-banning members has been disabled.')
            return

        if count <= 3:
            await self.bot.say('\N{NO ENTRY SIGN} Auto-ban threshold must be greater than three.')
            return

        if settings is None:
            # new entry
            settings = {
                'ignore': []
            }

        settings.update(count=count)
        counts[ctx.message.server.id] = settings
        await self.config.put('mentions', counts)
        await self.bot.say('Now auto-banning members that mention more than %s users.' % count)

    @mentionspam.command(name='ignore', pass_context=True, no_pm=True, aliases=['bypass'])
    async def mentionspam_ignore(self, ctx, *channels: discord.Channel):
        """Specifies what channels ignore mentionspam auto-bans.

        If a channel is given then that channel will no longer be protected
        by auto-banning from spammers.
        """
        counts = self.config.get('mentions', {})
        settings = counts.get(ctx.message.server.id)
        if settings is None:
            return await self.bot.say('\N{WARNING SIGN} This server has not configured mentionspam.')

        if len(channels) == 0:
            return await self.bot.say('Missing channels to ignore.')

        ignores = settings.get('ignore', [])
        ignores.extend(c.id for c in channels)
        settings['ignore'] = list(set(ignores)) # make it unique
        await self.config.put('mentions', counts)
        await self.bot.say('Mentions are now ignored on %s' % ', '.join('<#%s>' % c.id for c in channels))

    @mentionspam.command(name='protect', pass_context=True, no_pm=True, aliases=['unignore'])
    async def mentionspam_protect(self, ctx, *channels: discord.Channel):
        """Specifies what channels to take off the ignore list."""

        counts = self.config.get('mentions', {})
        settings = counts.get(ctx.message.server.id)
        if settings is None:
            return await self.bot.say('\N{WARNING SIGN} This server has not configured mentionspam.')

        if len(channels) == 0:
            return await self.bot.say('Missing channels to protect.')

        ignores = settings.get('ignore', [])
        unique = set(channels)
        for c in unique:
            try:
                ignores.remove(c.id)
            except ValueError:
                pass

        await self.config.put('mentions', counts)
        await self.bot.say('Updated mentionspam ignore list.')

    @commands.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_roles=True)
    async def colour(self, ctx, colour : discord.Colour, *, role : discord.Role):
        """Changes the colour of a role.

        The colour must be a hexadecimal value, e.g. FF2AEF. Don't prefix it
        with a pound (#) as it won't work. Colour names are also not supported.

        To use this command you must have the Manage Roles permission or
        have the Bot Admin role. The bot must also have Manage Roles permissions.

        This command cannot be used in a private message.
        """
        try:
            await self.bot.edit_role(ctx.message.server, role, colour=colour)
        except discord.HTTPException:
            await self.bot.say('The bot must have Manage Roles permissions to use this and its role must be higher.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.group(pass_context=True, no_pm=True, aliases=['purge'])
    @checks.admin_or_permissions(manage_messages=True)
    async def remove(self, ctx):
        """Removes messages that meet a criteria.

        In order to use this command, you must have Manage Messages permissions
        or have the Bot Admin role. Note that the bot needs Manage Messages as
        well. These commands cannot be used in a private message.

        When the command is done doing its work, you will get a private message
        detailing which users got removed and how many messages got removed.
        """

        if ctx.invoked_subcommand is None:
            await self.bot.say('Invalid criteria passed "{0.subcommand_passed}"'.format(ctx))

    async def do_removal(self, message, limit, predicate):
        try:
            deleted = await self.bot.purge_from(message.channel, limit=limit, before=message, check=predicate)
        except discord.Forbidden as e:
            return await self.bot.send_message(message.channel, 'I do not have permissions to delete messages.')
        except discord.HTTPException as e:
            return await self.bot.send_message(message.channel, 'Error: {} (try a smaller search?)'.format(e))

        spammers = Counter(m.author.display_name for m in deleted)
        messages = ['%s %s removed.' % (len(deleted), 'message was' if len(deleted) == 1 else 'messages were')]
        if len(deleted):
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(map(lambda t: '**{0[0]}**: {0[1]}'.format(t), spammers))

        await self.bot.say('\n'.join(messages), delete_after=10)

    @remove.command(pass_context=True)
    async def embeds(self, ctx, search=100):
        """Removes messages that have embeds in them."""
        await self.do_removal(ctx.message, search, lambda e: len(e.embeds))

    @remove.command(pass_context=True)
    async def files(self, ctx, search=100):
        """Removes messages that have attachments in them."""
        await self.do_removal(ctx.message, search, lambda e: len(e.attachments))

    @remove.command(pass_context=True)
    async def images(self, ctx, search=100):
        """Removes messages that have embeds or attachments."""
        await self.do_removal(ctx.message, search, lambda e: len(e.embeds) or len(e.attachments))

    @remove.command(name='all', pass_context=True)
    async def _remove_all(self, ctx, search=100):
        """Removes all messages."""
        await self.do_removal(ctx.message, search, lambda e: True)

    @remove.command(pass_context=True)
    async def user(self, ctx, member : discord.Member, search=100):
        """Removes all messages by the member."""
        await self.do_removal(ctx.message, search, lambda e: e.author == member)

    @remove.command(pass_context=True)
    async def contains(self, ctx, *, substr : str):
        """Removes all messages containing a substring.

        The substring must be at least 3 characters long.
        """
        if len(substr) < 3:
            await self.bot.say('The substring length must be at least 3 characters.')
            return

        await self.do_removal(ctx.message, 100, lambda e: substr in e.content)

    @remove.command(name='bot', pass_context=True)
    async def _bot(self, ctx, prefix, *, member: discord.Member):
        """Removes a bot user's messages and messages with their prefix.

        The member doesn't have to have the [Bot] tag to qualify for removal.
        """

        def predicate(m):
            return m.author == member or m.content.startswith(prefix)
        await self.do_removal(ctx.message, 100, predicate)

    @remove.command(pass_context=True)
    async def custom(self, ctx, *, args: str):
        """A more advanced prune command.

        Allows you to specify more complex prune commands with multiple
        conditions and search criteria. The criteria are passed in the
        syntax of `--criteria value`. Most criteria support multiple
        values to indicate 'any' match. A flag does not have a value.
        If the value has spaces it must be quoted.

        The messages are only deleted if all criteria are met unless
        the `--or` flag is passed.

        Criteria:
          user      A mention or name of the user to remove.
          contains  A substring to search for in the message.
          starts    A substring to search if the message starts with.
          ends      A substring to search if the message ends with.
          bot       A flag indicating if it's a bot user.
          embeds    A flag indicating if the message has embeds.
          files     A flag indicating if the message has attachments.
          emoji     A flag indicating if the message has custom emoji.
          search    How many messages to search. Default 100. Max 2000.
          or        A flag indicating to use logical OR for all criteria.
          not       A flag indicating to use logical NOT for all criteria.
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
        parser.add_argument('--search', type=int, default=100)

        try:
            args = parser.parse_args(shlex.split(args))
        except Exception as e:
            await self.bot.say(str(e))
            return

        predicates = []
        if args.bot:
            predicates.append(args.bot)

        if args.embeds:
            predicates.append(args.embeds)

        if args.files:
            predicates.append(args.files)

        if args.emoji:
            custom_emoji = re.compile(r'<:(\w+):(\d+)>')
            predicates.append(lambda m: custom_emoji.search(m.content))

        if args.user:
            users = []
            for u in args.user:
                try:
                    converter = commands.MemberConverter(ctx, u)
                    users.append(converter.convert())
                except Exception as e:
                    await self.bot.say(str(e))
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
        await self.do_removal(ctx.message, args.search, predicate)

def setup(bot):
    bot.add_cog(Mod(bot))
