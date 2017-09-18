from discord.ext import commands
from .utils import db
from .utils.formats import Plural
from collections import defaultdict

import discord
import re

class Profiles(db.Table):
    # this is the user_id
    id = db.Column(db.Integer(big=True), primary_key=True)
    nnid = db.Column(db.String)
    squad = db.Column(db.String)

    # merger from the ?fc stuff
    fc_3ds = db.Column(db.String)
    fc_switch = db.Column(db.String)

    # extra Splatoon data is stored here
    extra = db.Column(db.JSON, default="'{}'::jsonb", nullable=False)

class DisambiguateMember(commands.IDConverter):
    async def convert(self, ctx, argument):
        # check if it's a user ID or mention
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]+)>$', argument)

        if match is not None:
            # exact matches, like user ID + mention should search
            # for every member we can see rather than just this guild.
            user_id = int(match.group(1))
            result = ctx.bot.get_user(user_id)
            if result is None:
                raise commands.BadArgument("Could not find this member.")
            return result

        # check if we have a discriminator:
        if len(argument) > 5 and argument[-5] == '#':
            # note: the above is true for name#discrim as well
            name, _, discriminator = argument.rpartition('#')
            pred = lambda u: u.name == name and u.discriminator == discriminator
            result = discord.utils.find(pred, ctx.bot.users)
        else:
            # disambiguate I guess
            if ctx.guild is None:
                matches = [
                    user for user in ctx.bot.users
                    if user.name == argument
                ]
                entry = str
            else:
                matches = [
                    member for member in ctx.guild.members
                    if member.name == argument
                    or (member.nick and member.nick == argument)
                ]

                def to_str(m):
                    if m.nick:
                        return f'{m} (a.k.a {m.nick})'
                    else:
                        return str(m)

                entry = to_str

            try:
                result = await ctx.disambiguate(matches, entry)
            except Exception as e:
                raise commands.BadArgument(f'Could not find this member. {e}') from None

        if result is None:
            raise commands.BadArgument("Could not found this member. Note this is case sensitive.")
        return result

def valid_nnid(argument):
    arg = argument.strip('"')
    if len(arg) > 16:
        raise commands.BadArgument('An NNID has a maximum of 16 characters.')
    return arg

_rank = re.compile(r'^(?P<mode>\w+(?:\s*\w+)?)\s*(?P<rank>[AaBbCcSs][\+-]?)\s*(?P<number>[0-9]{0,2})$')

def valid_rank(argument, *, _rank=_rank):
    m = _rank.match(argument.strip('"'))
    if m is None:
        raise commands.BadArgument('Could not figure out mode or rank.')

    mode = m.group('mode')
    valid = {
        'zones': 'Splat Zones',
        'splat zones': 'Splat Zones',
        'sz': 'Splat Zones',
        'zone': 'Splat Zones',
        'splat': 'Splat Zones',
        'tower': 'Tower Control',
        'control': 'Tower Control',
        'tc': 'Tower Control',
        'tower control': 'Tower Control',
        'rain': 'Rainmaker',
        'rainmaker': 'Rainmaker',
        'rain maker': 'Rainmaker',
        'rm': 'Rainmaker'
    }

    try:
        mode = valid[mode.lower()]
    except KeyError:
        raise commands.BadArgument(f'Unknown Splatoon 2 mode: {mode}') from None

    rank = m.group('rank').upper()
    if rank == 'S-':
        rank = 'S'

    number = m.group('number')
    if number:
        number = int(number)

        if number and rank != 'S+':
            raise commands.BadArgument('Only S+ can input numbers.')
        if number > 50:
            raise commands.BadArgument('S+50 is the current cap.')

    return mode, { 'rank': rank, 'number': number }

def valid_squad(argument):
    arg = argument.strip('"')
    if len(arg) > 100:
        raise commands.BadArgument('Squad name way too long. Keep it less than 100 characters.')

    if arg.startswith('http'):
        arg = f'<{arg}>'
    return arg

_friend_code = re.compile(r'^(?:(?:SW|3DS)[- _]?)?(?P<one>[0-9]{4})[- _]?(?P<two>[0-9]{4})[- _]?(?P<three>[0-9]{4})$')

def valid_fc(argument, *, _fc=_friend_code):
    fc = argument.upper().strip('"')
    m = _fc.match(fc)
    if m is None:
        raise commands.BadArgument('Not a valid friend code!')

    return '{one}-{two}-{three}'.format(**m.groupdict())

class SplatoonWeapon(commands.Converter):
    async def convert(self, ctx, argument):
        cog = ctx.bot.get_cog('Splatoon')
        if cog is None:
            raise commands.BadArgument('Splatoon related commands seemingly disabled.')

        query = argument.strip('"')
        if len(query) < 4:
            raise commands.BadArgument('Weapon name to query must be over 4 characters long.')

        weapons = cog.get_weapons_named(query)

        try:
            weapon = await ctx.disambiguate(weapons, lambda w: w['name'])
        except ValueError as e:
            raise commands.BadArgument(str(e)) from None
        else:
            return weapon

class Profile:
    def __init__(self, bot):
        self.bot = bot

    async def __error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    @commands.group(invoke_without_command=True)
    async def profile(self, ctx, *, member: DisambiguateMember = None):
        """Manages your profile.

        If you don't pass in a subcommand, it will do a lookup based on
        the member passed in. If no member is passed in, you will
        get your own profile.

        All commands will create a profile for you.
        """

        member = member or ctx.author

        query = """SELECT * FROM profiles WHERE id=$1;"""
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            if member == ctx.author:
                await ctx.send('You did not set up a profile.' \
                              f' If you want to input a switch friend code, type {ctx.prefix}profile switch 1234-5678-9012' \
                              f' or check {ctx.prefix}help profile')
            else:
                await ctx.send('This member did not set up a profile.')
            return

        # 0xF02D7D - Splatoon 2 Pink
        # 0x19D719 - Splatoon 2 Green
        e = discord.Embed(colour=0x19D719)

        keys = {
            'fc_switch': 'Switch FC',
            'nnid': 'Wii U NNID',
            'fc_3ds': '3DS FC'
        }

        for key, value in keys.items():
            e.add_field(name=value, value=record[key] or 'N/A', inline=True)

        # consoles = [f'__{v}__: {record[k]}' for k, v in keys.items() if record[k] is not None]
        # e.add_field(name='Consoles', value='\n'.join(consoles) if consoles else 'None!', inline=False)
        e.set_author(name=member.display_name, icon_url=member.avatar_url_as(format='png'))

        extra = record['extra'] or {}
        rank = extra.get('sp2_rank', {})
        value = 'Unranked'
        if rank:
            value = '\n'.join(f'{mode}: {data["rank"]}{data["number"]}' for mode, data in rank.items())

        e.add_field(name='Splatoon 2 Ranks', value=value)

        weapon = extra.get('sp2_weapon')
        e.add_field(name='Splatoon 2 Weapon', value=weapon and weapon['name'])

        e.add_field(name='Squad', value=record['squad'] or 'N/A')
        await ctx.send(embed=e)

    async def edit_fields(self, ctx, **fields):
        keys = ', '.join(fields)
        values = ', '.join(f'${2 + i}' for i in range(len(fields)))

        query = f"""INSERT INTO profiles (id, {keys})
                    VALUES ($1, {values})
                    ON CONFLICT (id)
                    DO UPDATE
                    SET ({keys}) = ({values});
                 """

        await ctx.db.execute(query, ctx.author.id, *fields.values())

    @profile.command()
    async def nnid(self, ctx, *, NNID: valid_nnid):
        """Sets the NNID portion of your profile."""
        await self.edit_fields(ctx, nnid=NNID)
        await ctx.send('Updated NNID.')

    @profile.command()
    async def squad(self, ctx, *, squad: valid_squad):
        """Sets the Splatoon 2 squad part of your profile."""
        await self.edit_fields(ctx, squad=squad)
        await ctx.send('Updated squad.')

    @profile.command(name='3ds')
    async def profile_3ds(self, ctx, *, fc: valid_fc):
        """Sets the 3DS friend code of your profile."""
        await self.edit_fields(ctx, fc_3ds=fc)
        await ctx.send('Updated 3DS friend code.')

    @profile.command()
    async def switch(self, ctx, *, fc: valid_fc):
        """Sets the Switch friend code of your profile."""
        await self.edit_fields(ctx, fc_switch=fc)
        await ctx.send('Updated Switch friend code.')

    @profile.command()
    async def weapon(self, ctx, *, weapon: SplatoonWeapon):
        """Sets the Splatoon 2 weapon part of your profile.

        If you don't have a profile set up then it'll create one for you.
        The weapon must be a valid weapon that is in the Splatoon database.
        If too many matches are found you'll be asked which weapon you meant.
        """

        query = """INSERT INTO profiles (id, extra)
                   VALUES ($1, jsonb_build_object('sp2_weapon', $2::jsonb))
                   ON CONFLICT (id) DO UPDATE
                   SET extra = jsonb_set(profiles.extra, '{sp2_weapon}', $2::jsonb)
                """

        await ctx.db.execute(query, ctx.author.id, weapon)
        await ctx.send(f'Successfully set weapon to {weapon["name"]}.')

    @profile.command(usage='<mode> <rank>')
    async def rank(self, ctx, *, argument: valid_rank):
        """Sets the Splatoon 2 rank part of your profile.

        You set the rank on a per mode basis, such as

        - tc/tower control
        - rm/rainmaker
        - sz/splat zones/zones
        """

        query = """INSERT INTO profiles (id, extra)
                   VALUES ($1, $2::jsonb)
                   ON CONFLICT (id) DO UPDATE
                   SET extra =
                       CASE
                           WHEN profiles.extra ? 'sp2_rank'
                           THEN jsonb_set(profiles.extra, '{sp2_rank}', profiles.extra->'sp2_rank' || $2::jsonb)
                           ELSE jsonb_set(profiles.extra, '{sp2_rank}', $2::jsonb)
                       END
                """

        mode, data = argument
        await ctx.db.execute(query, ctx.author.id, {mode: data})
        await ctx.send(f'Successfully set {mode} rank to {data["rank"]}{data["number"]}.')

    @profile.command()
    async def delete(self, ctx, *, field=None):
        """Deletes a field from your profile.

        The valid fields that could be deleted are:

        - nnid
        - switch
        - 3ds
        - squad
        - weapon
        - rank
        - tower control rank
        - splat zones rank
        - rainmaker rank

        Omitting a field will delete your entire profile.
        """

        # simple case: delete entire profile
        if field is None:
            confirm = await ctx.prompt("Are you sure you want to delete your profile?")
            if confirm:
                query = "DELETE FROM profiles WHERE id=$1;"
                await ctx.db.execute(query, ctx.author.id)
                await ctx.send('Successfully deleted profile.')
            else:
                await ctx.send('Aborting profile deletion.')
            return

        field = field.lower()

        valid_fields = ( 'nnid', 'switch', '3ds', 'squad', 'weapon', 'rank',
                         'tower control rank', 'splat zones rank', 'rainmaker rank')

        if field not in valid_fields:
            return await ctx.send("I don't know what field you want me to delete here bub.")

        # a little intermediate case, basic field deletion:
        field_to_column = {
            'nnid': 'nnid',
            'switch': 'fc_switch',
            '3ds': 'fc_3ds',
            'squad': 'squad'
        }

        column = field_to_column.get(field)
        if column:
            query = f"UPDATE profiles SET {column} = NULL WHERE id=$1;"
            await ctx.db.execute(query, ctx.author.id)
            return await ctx.send(f'Successfully deleted {field} field.')

        # whole key deletion
        if field in ('weapon', 'rank'):
            key = 'sp2_rank' if field == 'rank' else 'sp2_weapon'
            query = "UPDATE profiles SET extra = extra - $1 WHERE id=$2;"
            await ctx.db.execute(query, key, ctx.author.id)
            return await ctx.send(f'Successfully deleted {field} field.')

        # a little more complicated
        mode = field.replace(' rank', '').title()
        query = "UPDATE profiles SET extra = extra #- $1::text[] WHERE id=$2;"
        key = ['sp2_rank', mode]
        await ctx.db.execute(query, key, ctx.author.id)
        await ctx.send(f'Successfully deleted {mode} ranking.')

    @profile.command()
    async def search(self, ctx, *, query):
        """Searches profiles via either friend code, NNID, or Squad.

        The query must be at least 3 characters long.

        Results are returned matching whichever criteria is met.
        """

        # check if it's a valid friend code and search the database for it:

        try:
            value = valid_fc(query.upper())
        except:
            # invalid so let's search for NNID/Squad.
            value = query
            query = """SELECT format('<@%s>', id) AS "User", squad AS "Squad", fc_switch AS "Switch", nnid AS "NNID"
                       FROM profiles
                       WHERE squad ILIKE '%' || $1 || '%'
                       OR nnid ILIKE '%' || $1 || '%'
                       LIMIT 15;
                    """
        else:
            query = """SELECT format('<@%s>', id) AS "User", squad AS "Squad", fc_switch AS "Switch", fc_3ds AS "3DS"
                       FROM profiles
                       WHERE fc_switch=$1 OR fc_3ds=$1
                       LIMIT 15;
                    """

        records = await ctx.db.fetch(query, value)

        if len(records) == 0:
            return await ctx.send('No results found...')

        e = discord.Embed(colour=0xF02D7D)

        data = defaultdict(list)
        for record in records:
            for key, value in record.items():
                data[key].append(value if value else 'N/A')

        for key, value in data.items():
            e.add_field(name=key, value='\n'.join(value))

        # a hack to allow multiple inline fields
        e.set_footer(text=str(Plural(record=len(records))) + '\u2003' * 60 + '\u200b')
        await ctx.send(embed=e)

    @profile.command()
    async def stats(self, ctx):
        """Retrieves some statistics on the profile database."""

        query = "SELECT COUNT(*) FROM profiles;"

        total = await ctx.db.fetchrow(query)
        total = total[0]

        # top weapons used
        query = """SELECT extra #> '{sp2_weapon,name}' AS "Weapon",
                          COUNT(*) AS "Total"
                   FROM profiles
                   WHERE extra #> '{sp2_weapon,name}' IS NOT NULL
                   GROUP BY extra #> '{sp2_weapon,name}'
                   ORDER BY "Total" DESC;
                """

        weapons = await ctx.db.fetch(query)
        total_weapons = sum(r['Total'] for r in weapons)

        e = discord.Embed(colour=0x19D719)
        e.title = f'Statistics for {Plural(profile=total)}'

        # top 3 weapons
        value = f'*{total_weapons} players with weapons*\n' + \
               '\n'.join(f'{r["Weapon"]} ({r["Total"]} players)' for r in weapons[:3])
        e.add_field(name='Top Splatoon 2 Weapons', value=value, inline=False)

        # get ranked data
        for mode in ('Splat Zones', 'Tower Control', 'Rainmaker'):
            query = f"""SELECT extra #> '{{sp2_rank,{mode},rank}}' AS "Rank",
                               COUNT(*) AS "Total"
                        FROM profiles
                        WHERE extra #> '{{sp2_rank,{mode},rank}}' IS NOT NULL
                        GROUP BY extra #> '{{sp2_rank,{mode},rank}}'
                        ORDER BY "Total" DESC
                     """

            records = await ctx.db.fetch(query)
            total = sum(r['Total'] for r in records)

            value = f'*{total} players*\n' + '\n'.join(f'{r["Rank"]}: {r["Total"]} ({r["Total"] / total:.2%})' for r in records)
            e.add_field(name=mode, value=value, inline=True)

        await ctx.send(embed=e)

def setup(bot):
    bot.add_cog(Profile(bot))
