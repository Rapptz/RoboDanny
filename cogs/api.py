from discord.ext import commands
from .utils import checks, config
import asyncio
import discord
import datetime
import re
from collections import Counter

DISCORD_API_ID = '81384788765712384'
USER_BOTS_ROLE = '178558252869484544'
CONTRIBUTORS_ROLE = '111173097888993280'

def is_discord_api():
    return checks.is_in_servers(DISCORD_API_ID)

def contributor_or_higher():
    def predicate(ctx):
        server = ctx.message.server
        if server is None:
            return False

        role = discord.utils.find(lambda r: r.id == CONTRIBUTORS_ROLE, server.roles)
        if role is None:
            return False

        return ctx.message.author.top_role.position >= role.position
    return commands.check(predicate)

class API:
    """Discord API exclusive things."""

    def __init__(self, bot):
        self.bot = bot
        # config format:
        # <users>: Counter
        # <last_use>: datetime.datetime timestamp
        self.config = config.Config('rtfm.json')

        # channel_id to dict with <name> to <role id> mapping
        self.feeds = config.Config('feeds.json')

        # regex for Pollr format
        self.pollr = re.compile(r'\*\*(?P<type>.+?)\*\*\s\|\sCase\s(?P<case>\d+)\n' \
                                r'\*\*User\*\*:\s(?P<user>.+?)\n' \
                                r'\*\*Reason\*\*:\s(?P<reason>.+?)\n' \
                                r'\*\*Responsible Moderator\*\*:(?P<mod>.+)')

    async def on_member_join(self, member):
        if member.server.id != DISCORD_API_ID:
            return

        if member.bot:
            role = discord.Object(id=USER_BOTS_ROLE)
            try:
                await self.bot.add_roles(member, role)
            except:
                await asyncio.sleep(10)
                await self.bot.add_roles(member, role)

    @commands.group(pass_context=True, aliases=['rtfd'], invoke_without_command=True)
    @is_discord_api()
    async def rtfm(self, ctx, *, obj : str = None):
        """Gives you a documentation link for a discord.py entity.

        Events, objects, and functions are all supported through a
        a cruddy fuzzy algorithm.
        """

        # update the stats
        invoker = ctx.message.author.id
        counter = self.config.get('users', {})
        if invoker not in counter:
            counter[invoker] = 1
        else:
            counter[invoker] += 1

        await self.config.put('users', counter)
        await self.config.put('last_use', datetime.datetime.utcnow().timestamp())

        transformations = {
            'client': discord.Client,
            'vc': discord.VoiceClient,
            'voiceclient': discord.VoiceClient,
            'voice_client': discord.VoiceClient,
            'voice': discord.VoiceClient,
            'message': discord.Message,
            'msg': discord.Message,
            'user': discord.User,
            'member': discord.Member,
            'game': discord.Game,
            'invite': discord.Invite,
            'role': discord.Role,
            'server': discord.Server,
            'color': discord.Colour,
            'colour': discord.Colour,
            'perm': discord.Permissions,
            'permissions': discord.Permissions,
            'perms': discord.Permissions,
            'channel': discord.Channel,
            'chan': discord.Channel,
            'obj': discord.Object,
            'object': discord.Object,
        }

        base_url = 'http://discordpy.rtfd.io/en/latest/api.html'

        if obj is None:
            await self.bot.say(base_url)
            return

        portions = obj.split('.')
        if portions[0] == 'discord':
            portions = portions[1:]

        if len(portions) == 0:
            # we only said 'discord'... uh... ok.
            await self.bot.say(base_url)
            return

        base = transformations.get(portions[0].lower())
        anchor = ''

        if base is not None:
            # check if it's a fuzzy match
            anchor = 'discord.' + base.__name__

            # get the attribute associated with it
            if len(portions) > 1:
                attribute = portions[1]
                if getattr(base, attribute, None):
                    anchor = anchor + '.' + attribute

        elif portions[0].startswith('on_'):
            # an event listener...
            anchor = 'discord.' + portions[0]
        else:
            # probably a direct attribute access.
            obj = discord
            for attr in portions:
                try:
                    obj = getattr(obj, attr)
                except AttributeError:
                    await self.bot.say('{0.__name__} has no attribute {1}'.format(obj, attr))
                    return
            anchor = 'discord.' + '.'.join(portions)

        await self.bot.say(base_url + '#' + anchor)

    @rtfm.command()
    @is_discord_api()
    async def stats(self):
        """Tells you stats about the ?rtfm command."""

        counter = Counter(self.config.get('users', {}))
        last_use = self.config.get('last_use', None)
        server = self.bot.get_server(DISCORD_API_ID)

        output = []
        if last_use:
            last_use = datetime.datetime.fromtimestamp(last_use)
            output.append('**Last RTFM**: {:%Y/%m/%d on %I:%M:%S %p UTC}'.format(last_use))
        else:
            output.append('**Last RTFM**: Never')

        total_uses = sum(counter.values())
        output.append('**Total uses**: ' + str(total_uses))


        # first we get the most used users
        top_ten = counter.most_common(10)
        if top_ten:
            output.append('**Top {} users**:'.format(len(top_ten)))

            for rank, (user, uses) in enumerate(top_ten, 1):
                member = server.get_member(user)
                if rank != 10:
                    output.append('{}\u20e3 {}: {}'.format(rank, member, uses))
                else:
                    output.append('\N{KEYCAP TEN} {}: {}'.format(member, uses))

        await self.bot.say('\n'.join(output))

    def library_name(self, channel):
        # language_<name>
        name = channel.name
        index = name.find('_')
        if index != -1:
            name = name[index + 1:]
        return name.replace('-', '.')

    @commands.group(name='feeds', pass_context=True, invoke_without_command=True)
    @is_discord_api()
    async def _feeds(self, ctx):
        """Shows the list of feeds that the channel has.

        A feed is something that users can opt-in to
        to receive news about a certain feed by running
        the `sub` command (and opt-out by doing the `unsub` command).
        You can publish to a feed by using the `publish` command.
        """
        channel = ctx.message.channel
        feeds = self.feeds.get(channel.id, {})
        if len(feeds) == 0:
            await self.bot.say('This channel has no feeds.')
            return

        fmt = 'Found {} feeds.\n{}'
        await self.bot.say(fmt.format(len(feeds), '\n'.join('- ' + r for r in feeds)))

    @_feeds.command(name='create', pass_context=True)
    @commands.has_permissions(manage_roles=True)
    @is_discord_api()
    async def feeds_create(self, ctx, *, name : str):
        """Creates a feed with the specified name.

        You need Manage Roles permissions to create a feed.
        """
        channel = ctx.message.channel
        server = channel.server
        feeds = self.feeds.get(channel.id, {})
        name = name.lower()
        if name in feeds:
            await self.bot.say('This feed already exists.')
            return

        # create the default role
        role_name = self.library_name(channel) + ' ' + name
        role = await self.bot.create_role(server, name=role_name, permissions=discord.Permissions.none())
        feeds[name] = role.id
        await self.feeds.put(channel.id, feeds)
        await self.bot.say('\u2705')

    @_feeds.command(name='delete', aliases=['remove'], pass_context=True)
    @commands.has_permissions(manage_roles=True)
    @is_discord_api()
    async def feeds_delete(self, ctx, *, feed : str):
        """Removes a feed from the channel.

        This will also delete the associated role so this
        action is irreversible.
        """
        channel = ctx.message.channel
        server = channel.server
        feeds = self.feeds.get(channel.id, {})
        feed = feed.lower()
        if feed not in feeds:
            await self.bot.say('This feed does not exist.')
            return

        role = feeds.pop(feed)
        try:
            await self.bot.delete_role(server, discord.Object(id=role))
        except discord.HTTPException:
            await self.bot.say('\U0001F52B')
        else:
            await self.feeds.put(channel.id, feeds)
            await self.bot.say('\U0001F6AE')

    async def do_subscription(self, ctx, feed, action):
        channel = ctx.message.channel
        member = ctx.message.author
        feeds = self.feeds.get(channel.id, {})
        feed = feed.lower()

        if feed not in feeds:
            await self.bot.say('This feed does not exist.')
            return

        role = feeds[feed]
        function = getattr(self.bot, action)
        try:
            await function(member, discord.Object(id=role))
        except discord.HTTPException:
            # muh rate limit
            await asyncio.sleep(10)
            await function(member, discord.Object(id=role))
        else:
            await self.bot.send_message(channel, '\u2705')

    @commands.command(pass_context=True)
    @is_discord_api()
    async def sub(self, ctx, *, feed : str):
        """Subscribes to the publication of a feed.

        This will allow you to receive updates from the channel
        owner. To unsubscribe, see the `unsub` command.
        """
        await self.do_subscription(ctx, feed, 'add_roles')

    @commands.command(pass_context=True)
    @is_discord_api()
    async def unsub(self, ctx, *, feed : str):
        """Unsubscribe to the publication of a feed.

        This will remove you from notifications of a feed you
        are no longer interested in. You can always sub back by
        using the `sub` command.
        """
        await self.do_subscription(ctx, feed, 'remove_roles')

    @commands.command(pass_context=True)
    @commands.has_permissions(manage_roles=True)
    @is_discord_api()
    async def publish(self, ctx, feed : str, *, content : str):
        """Publishes content to a feed.

        Everyone who is subscribed to the feed will be notified
        with the content. Use this to notify people of important
        events or changes.
        """
        channel = ctx.message.channel
        server = channel.server
        feeds = self.feeds.get(channel.id, {})
        feed = feed.lower()
        if feed not in feeds:
            await self.bot.say('This feed does not exist.')
            return

        role = discord.utils.get(server.roles, id=feeds[feed])
        if role is None:
            fmt = 'Uh.. a fatal error occurred here. The role associated with ' \
                  'this feed has been removed or not found. ' \
                  'Please recreate the feed.'
            await self.bot.say(fmt)
            return

        # delete the message we used to invoke it
        await self.bot.delete_message(ctx.message)

        # make the role mentionable
        await self.bot.edit_role(server, role, mentionable=True)

        # then send the message..
        msg = '{0.mention}: {1}'.format(role, content)[:2000]
        await self.bot.say(msg)

        # then make the role unmentionable
        await self.bot.edit_role(server, role, mentionable=False)

    @commands.command(pass_context=True)
    @is_discord_api()
    @contributor_or_higher()
    async def log(self, ctx, *, user: str):
        """Shows mod log entries for a user.

        Only searches the past 300 cases.
        """

        mod_log = ctx.message.server.get_channel('173201159761297408')
        entries = []
        async for m in self.bot.logs_from(mod_log, limit=300):
            entry = self.pollr.match(m.content)
            if entry is None:
                continue

            if user in entry.group('user'):
                entries.append(m.content)

        fmt = 'Found {} entries:\n{}'
        await self.bot.say(fmt.format(len(entries), '\n\n'.join(entries)))

def setup(bot):
    bot.add_cog(API(bot))
