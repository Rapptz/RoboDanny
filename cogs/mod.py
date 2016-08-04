from discord.ext import commands
from .utils import config, checks
from collections import Counter
import re
import discord
import asyncio
import argparse, shlex

class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)

class Mod:
    """Moderation related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('mod.json', loop=bot.loop)

    def bot_user(self, message):
        return message.server.me if message.channel.is_private else self.bot.user

    def __check(self, ctx):
        msg = ctx.message
        if checks.is_owner_check(msg):
            return True

        # user is bot banned
        if msg.author.id in self.config.get('plonks', []):
            return False

        # check if the channel is ignored
        # but first, resolve their permissions

        perms = msg.channel.permissions_for(msg.author)
        bypass_ignore = perms.administrator

        # now we can finally realise if we can actually bypass the ignore.

        if not bypass_ignore and msg.channel.id in self.config.get('ignored', []):
            return False

        return True

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
        messages = ['{} message(s) were deleted.'.format(deleted)]
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
            await self.bot.say('The bot does not have permissions to kick members.')
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
            await self.bot.say('The bot does not have permissions to ban members.')
        except discord.HTTPException:
            await self.bot.say('Banning failed.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(ban_members=True)
    async def softban(self, *, member : discord.Member):
        """Soft bans a member from the server.

        A softban is basically banning the member from the server but
        then unbanning the member as well. This allows you to essentially
        kick the member while removing their messages.

        To use this command you must have Ban Members permissions or have
        the Bot Admin role. Note that the bot must have the permission as well.
        """

        try:
            await self.bot.ban(member)
            await self.bot.unban(member.server, member)
        except discord.Forbidden:
            await self.bot.say('The bot does not have permissions to ban members.')
        except discord.HTTPException:
            await self.bot.say('Banning failed.')
        else:
            await self.bot.say('\U0001f44c')

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def plonk(self, *, member : discord.Member):
        """Bans a user from using the bot.

        Note that this ban is **global**. So they are banned from
        all servers that they access the bot with. So use this with
        caution.

        There is no way to bypass a plonk regardless of role or permissions.
        The only person who cannot be plonked is the bot creator. So this
        must be used with caution.

        To use this command you must have the Manage Server permission
        or have a Bot Admin role.
        """

        plonks = self.config.get('plonks', [])
        if member.id in plonks:
            await self.bot.say('That user is already bot banned.')
            return

        plonks.append(member.id)
        await self.config.put('plonks', plonks)
        await self.bot.say('{0.name} has been banned from using the bot.'.format(member))

    @commands.command(no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def unplonk(self, *, member : discord.Member):
        """Unbans a user from using the bot.

        To use this command you must have the Manage Server permission
        or have a Bot Admin role.
        """

        plonks = self.config.get('plonks', [])

        try:
            plonks.remove(member.id)
        except ValueError:
            pass
        else:
            await self.config.put('plonks', plonks)
            await self.bot.say('{0.name} has been unbanned from using the bot.'.format(member))

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
        except discord.Forbidden:
            await self.bot.say('The bot must have Manage Roles permissions to use this.')
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
        deleted = await self.bot.purge_from(message.channel, limit=limit, before=message, check=predicate)
        spammers = Counter(m.author.display_name for m in deleted)
        messages = ['{} messages(s) were removed.'.format(len(deleted))]
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
