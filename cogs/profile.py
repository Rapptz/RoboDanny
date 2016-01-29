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

    def __str__(self):
        return self.name

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
        self.valid_ranks = { 'C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A', 'A+', 'S', 'S+' }

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
                await ctx.invoke(self.make)
        else:
            fmt = 'Profile for **{0.name}#{0.discriminator}**:\n{1}'
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
        await self.bot.say('Field {} set to {}.'.format(attr, data))

    @profile.command(pass_context=True)
    async def nnid(self, ctx, *, NNID : str):
        """Sets the NNID portion of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        await self.edit_field('nnid', ctx, NNID.strip('"'))

    @profile.command(pass_context=True)
    async def rank(self, ctx, rank : str):
        """Sets the Splatoon rank part of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        rank = rank.upper()
        if rank not in self.valid_ranks:
            await self.bot.say('That is not a valid Splatoon rank.')
        else:
            await self.edit_field('rank', ctx, rank)

    @profile.command(pass_context=True)
    async def squad(self, ctx, *, squad : str):
        """Sets the Splatoon squad part of your profile.

        If you don't have a profile set up then it'll create one for you.
        """
        await self.edit_field('squad', ctx, squad.strip('"'))

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
        query = weapon.lower().strip('"')
        if len(query) < 4:
            await self.bot.say('The weapon name to query must be at least 4 characters long.')
            return

        result = [weapon for weapon in weapons if query in weapon['name'].lower()]
        if len(result) == 0:
            await self.bot.say('No weapon found that matches "{}"'.format(weapon))
            return
        elif len(result) == 1:
            await self.edit_field('weapon', ctx, Weapon(**result[0]))
            return True

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
            return True

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
        rank_data = [(rank, value / total_ranked) for rank, value in ranks.items()]
        rank_data.sort(key=lambda t: t[1], reverse=True)        
        for rank, value in rank_data:
            entries.append((rank, format(value, '.2%')))

        weapons = Counter(profile.weapon.name for profile in profiles if profile.weapon is not None)
        entries.append(('Players with Weapons', sum(weapons.values())))
        top_cut = weapons.most_common(3)
        for weapon, count in top_cut:
            entries.append((weapon, count))

        await formats.entry_to_code(self.bot, entries)

    @profile.command(pass_context=True)
    async def delete(self, ctx, *fields : str):
        """Deletes certain fields from your profile.

        The valid fields that could be deleted are:

        - nnid
        - squad
        - weapon
        - rank

        Omitting any fields will delete your entire profile.
        """
        uid = ctx.message.author.id
        profile = self.config.get(uid)
        if profile is None:
            await self.bot.say('You don\'t have a profile set up.')
            return

        if len(fields) == 0:
            await self.config.remove(uid)
            await self.bot.say('Your profile has been deleted.')
            return

        for attr in map(str.lower, fields):
            if hasattr(profile, attr):
                setattr(profile, attr, None)

        await self.config.put(uid, profile)
        fmt = 'The fields {} have been deleted.'
        if len(fields) == 1:
            fmt = 'The field {} has been deleted'
        await self.bot.say(fmt.format(', '.join(fields)))


    @profile.command(pass_context=True)
    async def make(self, ctx):
        """Interactively set up a profile.

        This command will walk you through the steps required to create
        a profile. Note that it only goes through the basics of a profile,
        a squad, for example, is not asked for.
        """

        message = ctx.message
        await self.bot.say('Hello. Let\'s walk you through making a profile!\nWhat is your NNID?')
        nnid = await self.bot.wait_for_message(author=message.author, channel=message.channel)
        await ctx.invoke(self.nnid, NNID=nnid.content)
        await self.bot.say('Now tell me, what is your Splatoon rank? Please don\'t put the number.')
        check = lambda m: m.content.upper() in self.valid_ranks
        rank = await self.bot.wait_for_message(author=message.author, channel=message.channel, check=check, timeout=60.0)
        if rank is None:
            await self.bot.say('Alright.. you took too long to give me a proper rank. Goodbye.')
            return

        await self.edit_field('rank', ctx, rank.content.upper())
        await self.bot.say('What weapon do you main?')
        weapon = await self.bot.wait_for_message(author=message.author, channel=message.channel)
        success = await ctx.invoke(self.weapon, weapon=weapon.content)
        if success:
            await self.bot.say('Alright! Your profile is all ready now.')
        else:
            await self.bot.say('Make sure to use {0.prefix}profile weapon to change your weapon.'.format(ctx))

def setup(bot):
    bot.add_cog(Profile(bot))
