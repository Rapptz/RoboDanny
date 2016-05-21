from discord.ext import commands
from .utils import checks, config
import asyncio
import discord
import datetime
from collections import Counter

DISCORD_API_ID = '81384788765712384'
USER_BOTS_ROLE = '178558252869484544'

def is_discord_api():
    return checks.is_in_servers(DISCORD_API_ID)

class API:
    """Discord API exclusive things."""

    def __init__(self, bot):
        self.bot = bot
        # config format:
        # <users>: Counter
        # <last_use>: datetime.datetime timestamp
        self.config = config.Config('rtfm.json')

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
        }

        base_url = 'http://discordpy.rtfd.io/en/latest/api.html'

        if obj is None:
            await self.bot.say(base_url)
            return

        portions = [x.lower() for x in obj.split('.')]
        if portions[0] == 'discord':
            portions = portions[1:]

        if len(portions) == 0:
            # we only said 'discord'... uh... ok.
            await self.bot.say(base_url)
            return

        base = transformations.get(portions[0])
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
        top_five = counter.most_common(5)
        if top_five:
            output.append('**Top {} users**:'.format(len(top_five)))

            for rank, (user, uses) in enumerate(top_five, 1):
                member = server.get_member(user)
                output.append('{}\u20e3 {}: {}'.format(rank, member, uses))

        await self.bot.say('\n'.join(output))

def setup(bot):
    bot.add_cog(API(bot))
