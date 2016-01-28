from discord.ext import commands
import discord.utils
from .utils import config, formats
import json, re
from collections import Counter

class DefaultProfileType:
    def __str__(self):
        return 'me'

MyOwnProfile = DefaultProfileType()

class MemberParser:
    """
    Lazily fetches an argument and then when asked attempts to get the data.
    """

    def __init__(self, argument):
        self.argument = argument.strip()
        self.regex = re.compile(r'<@([0-9]+)>')

    def member_entry(self, tup):
        index = tup[0]
        member = tup[1]
        return '{0}. {1.name}#{1.discriminator} from {1.server.name}'.format(index, member)

    async def get(self, ctx):
        """Given an invocation context, gets a user."""
        server = ctx.message.server
        bot = ctx.bot
        members = server.members if server is not None else bot.get_all_members()

        # check if the argument is a mention
        m = self.regex.match(self.argument)
        if m:
            user_id = m.group(1)
            return discord.utils.get(members, id=user_id)

        # it isn't, so search by name
        results = {m for m in members if m.name == self.argument}
        results = list(results)
        if len(results) == 0:
            # we have no matches... so we must return None
            return None


        if len(results) == 1:
            # we have an exact match.
            return results[0]

        # no exact match
        msg = ctx.message
        member = await formats.too_many_matches(bot, msg, results, self.member_entry)
        return member


class Weapon:
    def __init__(self, **kwargs):
        self.__dict__ = kwargs

class ProfileInfo:
    def __init__(self, **kwargs):
        self.nnid = kwargs.get('nnid')
        self.rank = kwargs.get('rank')
        self.squad = kwargs.get('squad')

        if 'weapon' in kwargs:
            weapon = kwargs['weapon']
            if weapon is not None:
                self.weapon = Weapon(**weapon)
            else:
                self.weapon = None
        else:
            self.weapon = None

    def __str__(self):
        output = []
        output.append('NNID: {0.nnid}'.format(self))
        output.append('Rank: {0.rank}'.format(self))
        output.append('Squad: {0.squad}'.format(self))
        if self.weapon is not None:
            output.append('Weapon: {0.name} ({0.sub} with {0.special})'.format(self.weapon))
        else:
            output.append('Weapon: None')
        return '\n'.join(output)


class ProfileEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ProfileInfo):
            payload = obj.__dict__.copy()
            payload['__profile__'] = True
            return payload
        if isinstance(obj, Weapon):
            return obj.__dict__
        return json.JSONEncoder.default(self, obj)

def profile_decoder(obj):
    if '__profile__' in obj:
        return ProfileInfo(**obj)
    return obj

class Profile:
    """Profile related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('profiles.json', encoder=ProfileEncoder, object_hook=profile_decoder)

    async def get_profile(self, ctx, parser):
        try:
            if parser is MyOwnProfile:
                member = ctx.message.author
            else:
                member = await parser.get(ctx)
        except commands.CommandError as e:
            await self.bot.say(e)
            return

        if member is None:
            await self.bot.say('Member not found. Note that this is case sensitive. You can use a mention instead.')
            return

        profile = self.config.get(member.id)
        if profile is None:
            if parser is not MyOwnProfile:
                await self.bot.say('This member did not set up a profile.')
            else:
                await self.bot.say('You did not set up a profile. One has been created for you.')
                await self.config.put(member.id, ProfileInfo())
        else:
            fmt = 'Profile for **{0.name}**:\n{1}'
            await self.bot.say(fmt.format(member, profile))


    @commands.group(pass_context=True, invoke_without_command=True)
    async def profile(self, ctx, *, member : MemberParser = MyOwnProfile):
        """Manages your profile.

        If you don't pass in a subcommand, it will do a lookup based on
        the member passed in. Similar to the profile get subcommand. If
        no member is passed in, you will get your own profile.

        All commands will create a profile for you with no fields set.
        """
        await self.get_profile(ctx, member)

    @profile.command(pass_context=True)
    async def get(self, ctx, *, member : MemberParser = MyOwnProfile):
        """Retrieves either your profile or someone else's profile.

        You can retrieve a member's profile either by mentioning them
        or by passing in the case-sensitive name of the user. If too
        many members are found then the bot will ask you which one
        you want.
        """
        await self.get_profile(ctx, member)

    async def edit_field(self, attr, ctx, data):
        user_id = ctx.message.author.id
        profile = self.config.get(user_id, ProfileInfo())
        setattr(profile, attr, data)
        await self.config.put(user_id, profile)
        await self.bot.say('Field successfully edited.')

    @profile.command(pass_context=True)
    async def nnid(self, ctx, *, NNID : str):
        """Sets the NNID portion of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        await self.edit_field('nnid', ctx, NNID)

    @profile.command(pass_context=True)
    async def rank(self, ctx, rank : str):
        """Sets the Splatoon rank part of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        rank = rank.upper()
        valid = { 'C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A', 'A+', 'S', 'S+' }
        if rank not in valid:
            await self.bot.say('That is not a valid Splatoon rank.')
        else:
            await self.edit_field('rank', ctx, rank)

    @profile.command(pass_context=True)
    async def squad(self, ctx, *, squad : str):
        """Sets the Splatoon squad part of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        await self.edit_field('squad', ctx, squad)

    @profile.command(pass_context=True)
    async def weapon(self, ctx, *, weapon : str):
        """Sets the Splatoon weapon part of your profile.

        If you don't have a profile set up then it'll create one for you.
        The weapon must be a valid weapon that is in the Splatoon database.
        If too many matches are found you'll be asked which weapon you meant.
        """

        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await self.bot.say('The Splatoon related commands are turned off.')
            return

        weapons = splatoon.config.get('weapons', [])
        query = weapon.lower()
        if len(query) < 4:
            await self.bot.say('The weapon name to query must be at least 4 characters long.')
            return

        result = [weapon for weapon in weapons if query in weapon['name'].lower()]
        if len(result) == 0:
            await self.bot.say('No weapon found that matches "{}"'.format(weapon))
            return
        elif len(result) == 1:
            await self.edit_field('weapon', ctx, Weapon(**result[0]))
            return

        def weapon_entry(tup):
            index = tup[0]
            wep = tup[1]['name']
            return '#{0}: {1}'.format(index, wep)

        try:
            match = await formats.too_many_matches(self.bot, ctx.message, result, weapon_entry)
        except commands.CommandError as e:
            await self.bot.say(e)
        else:
            await self.edit_field('weapon', ctx, Weapon(**match))

    @profile.command()
    async def stats(self):
        """Retrieves some statistics on the profile database."""

        profiles = self.config.all().values()
        ranks = Counter(profile.rank for profile in profiles if profile.rank is not None)
        total_ranked = sum(ranks.values())
        entries = [
            ('Total Profiles', len(self.config))
        ]

        entries.append(('Ranked Players', total_ranked))
        for rank, value in ranks.items():
            entries.append((rank, format(value / total_ranked, '.2%')))

        weapons = Counter(profile.weapon.name for profile in profiles if profile.weapon is not None)
        entries.append(('Players with Weapons', sum(weapons.values())))
        top_cut = weapons.most_common(3)
        for weapon, count in top_cut:
            entries.append((weapon, count))

        await formats.entry_to_code(self.bot, entries)


def setup(bot):
    bot.add_cog(Profile(bot))
