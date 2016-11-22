import discord
from discord.ext import commands
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
        self.regex = re.compile(r'<@\!?([0-9]+)>')

    def member_entry(self, tup):
        index = tup[0]
        member = tup[1]
        return '{0}: {1} from {1.server.name}'.format(index, member)

    def has_potential_discriminator(self):
        return len(self.argument) > 5 and self.argument[-5] == '#'

    def get_server_members(self, server):
        if self.has_potential_discriminator():
            discrim = self.argument[-4:]
            direct = discord.utils.get(server.members, name=self.argument[:-5], discriminator=discrim)
            if direct is not None:
                return { direct }

        return { m for m in server.members if m.display_name == self.argument }

    async def get(self, ctx):
        """Given an invocation context, gets a user."""
        server = ctx.message.server
        bot = ctx.bot

        # check if the argument is a mention
        m = self.regex.match(self.argument)
        if m:
            user_id = m.group(1)
            if server:
                return server.get_member(user_id)

            # get the first member found in all servers with the user ID.
            gen = filter(None, map(lambda s: s.get_member(user_id), bot.servers))
            return next(gen, None)

        # it isn't, so search by name
        if server:
            results = self.get_server_members(server)
        else:
            results = set(filter(None, map(lambda s: s.get_member_named(self.argument), bot.servers)))

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
    __slots__ = ('sub', 'name', 'special')

    cache = {}

    def __init__(self, **kwargs):
        for attr in self.__slots__:
            try:
                value = kwargs[attr]
            except KeyError:
                value = None
            finally:
                setattr(self, attr, value)

    @classmethod
    def from_cache(cls, *, name, **kwargs):
        try:
            return cls.cache[name]
        except KeyError:
            cls.cache[name] = weapon = cls(name=name, **kwargs)
            return weapon

    def __str__(self):
        return self.name

class ProfileInfo:
    __slots__ = ('nnid', 'squad', 'weapon', 'rank')

    def __init__(self, **kwargs):
        self.nnid = kwargs.get('nnid')
        self.rank = kwargs.get('rank')
        self.squad = kwargs.get('squad')

        if 'weapon' in kwargs:
            weapon = kwargs['weapon']
            if weapon is not None:
                self.weapon = Weapon.from_cache(**weapon)
            else:
                self.weapon = None
        else:
            self.weapon = None

    def embed(self):
        ret = discord.Embed(title='Profile')
        squad = self.squad if self.squad else 'None'
        nnid = self.nnid if self.nnid else 'None'
        rank = self.rank if self.rank else 'None'

        ret.add_field(name='NNID', value=nnid)
        ret.add_field(name='Rank', value=rank)
        ret.add_field(name='Squad', value=squad)

        if self.weapon is not None:
            wep = self.weapon
            ret.add_field(name='Main Weapon', value=wep.name)
            ret.add_field(name='Sub Weapon', value=wep.sub)
            ret.add_field(name='Special', value=wep.special)

        return ret

class ProfileEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ProfileInfo):
            payload = {
                attr: getattr(obj, attr)
                for attr in ProfileInfo.__slots__
            }
            payload['__profile__'] = True
            return payload
        if isinstance(obj, Weapon):
            return { attr: getattr(obj, attr) for attr in Weapon.__slots__ }
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
            e = profile.embed()
            avatar = member.avatar_url if member.avatar else member.default_avatar_url
            e.set_author(name=str(member), icon_url=avatar)
            await self.bot.say(embed=e)

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
        nid = NNID.strip('"')
        if len(nid) > 16:
            await self.bot.say('An NNID has a maximum of 16 characters.')
            return

        await self.edit_field('nnid', ctx, nid)

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
        squad = squad.strip('"')
        if len(squad) > 100:
            await self.bot.say('Your squad is way too long. Keep it less than 100 characters.')
            return

        if squad.startswith('http'):
            squad = '<' + squad + '>'

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
        query = weapon.lower().strip('"')
        if len(query) < 4:
            await self.bot.say('The weapon name to query must be at least 4 characters long.')
            return

        result = [weapon for weapon in weapons if query in weapon['name'].lower()]
        if len(result) == 0:
            await self.bot.say('No weapon found that matches "{}"'.format(weapon))
            return
        elif len(result) == 1:
            await self.edit_field('weapon', ctx, Weapon.from_cache(**result[0]))
            return True

        def weapon_entry(tup):
            index = tup[0]
            wep = tup[1]['name']
            return '{0}: {1}'.format(index, wep)

        try:
            match = await formats.too_many_matches(self.bot, ctx.message, result, weapon_entry)
        except commands.CommandError as e:
            await self.bot.say(e)
        else:
            await self.edit_field('weapon', ctx, Weapon.from_cache(**match))
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
        entries.append(('Players with Weapon.from_caches', sum(weapons.values())))
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
        author = message.author
        sentinel = ctx.prefix + 'cancel'

        fmt = 'Hello {0.mention}. Let\'s walk you through making a profile!\n' \
              '**You can cancel this process whenever you want by typing {1.prefix}cancel.**\n' \
              'Now, what is your NNID?'

        await self.bot.say(fmt.format(author, ctx))
        check = lambda m: 16 >= len(m.content) >= 4 and m.content.count('\n') == 0
        nnid = await self.bot.wait_for_message(author=author, channel=message.channel, timeout=60.0, check=check)

        if nnid is None:
            await self.bot.say('You took too long {0.mention}. Goodbye.'.format(author))
            return

        if nnid.content == sentinel:
            await self.bot.say('Profile making cancelled. Goodbye.')
            return

        await ctx.invoke(self.nnid, NNID=nnid.content)
        await self.bot.say('Now tell me, what is your Splatoon rank? Please don\'t put the number.')
        check = lambda m: m.content.upper() in self.valid_ranks or m.content == sentinel
        rank = await self.bot.wait_for_message(author=author, channel=message.channel, check=check, timeout=60.0)

        if rank is None:
            await self.bot.say('Alright.. you took too long to give me a proper rank. Goodbye.')
            return

        if rank.content == sentinel:
            await self.bot.say('Profile making cancelled. Goodbye.')
            return

        await self.edit_field('rank', ctx, rank.content.upper())
        await self.bot.say('What weapon do you main?')
        for i in range(3):
            weapon = await self.bot.wait_for_message(author=author, channel=message.channel)

            if weapon.content == sentinel:
                await self.bot.say('Profile making cancelled. Goodbye.')
                return

            success = await ctx.invoke(self.weapon, weapon=weapon.content)
            if success:
                await self.bot.say('Alright! Your profile is all ready now.')
                break
            else:
                await self.bot.say('Oops. You have {} tries remaining.'.format(2 - i))
        else:
            # if we're here then we didn't successfully set up a weapon.
            tmp = 'Sorry we couldn\'t set up your profile. You should try using {0.prefix}profile weapon'
            await self.bot.say(tmp.format(ctx))

    @profile.command()
    async def search(self, *, query : str):
        """Searches profiles via either NNID or Squad.

        The query must be at least 3 characters long.

        First a search is passed through the NNID database and then we pass
        through the Squad database. Results are returned matching whichever
        criteria is met.
        """
        lowered = query.lower()
        if len(lowered) < 3:
            await self.bot.say('Query must be at least 3 characters long.')
            return

        profiles = self.config.all().items()
        members = set()
        def predicate(t):
            p = t[1]
            if p.squad is not None:
                if lowered in p.squad.lower():
                    return True

            if p.nnid is not None:
                if lowered in p.nnid.lower():
                    return True

            return False

        for user_id, profile in filter(predicate, profiles):
            for server in self.bot.servers:
                member = server.get_member(user_id)
                if member is not None:
                    members.add(member.name)

        fmt = 'Found the {} members matching the search:\n{}'
        await self.bot.say(fmt.format(len(members), ', '.join(members)))

def setup(bot):
    bot.add_cog(Profile(bot))
