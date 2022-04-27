from __future__ import annotations
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union
from typing_extensions import Annotated

from discord.ext import commands
from discord import app_commands
from .utils.formats import plural
from collections import defaultdict

import discord
import re

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import GuildContext, Context
    from cogs.splatoon import Splatoon


class DisambiguateMember(commands.IDConverter, app_commands.Transformer):
    async def convert(self, ctx: Context, argument: str) -> discord.abc.User:
        # check if it's a user ID or mention
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]+)>$', argument)

        if match is not None:
            # exact matches, like user ID + mention should search
            # for every member we can see rather than just this guild.
            user_id = int(match.group(1))
            result = ctx.bot.get_user(user_id)
            if result is None:
                try:
                    result = await ctx.bot.fetch_user(user_id)
                except discord.HTTPException:
                    raise commands.BadArgument("Could not find this member.") from None
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
                matches = [user for user in ctx.bot.users if user.name == argument]
                entry = str
            else:
                matches = [
                    member
                    for member in ctx.guild.members
                    if member.name == argument or (member.nick and member.nick == argument)
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

    @classmethod
    def type(cls) -> discord.AppCommandOptionType:
        return discord.AppCommandOptionType.user

    @classmethod
    async def transform(cls, interaction: discord.Interaction, value: discord.abc.User) -> discord.abc.User:
        return value


def valid_nnid(argument: str) -> str:
    arg = argument.strip('"')
    if len(arg) > 16:
        raise commands.BadArgument('An NNID has a maximum of 16 characters.')
    return arg


_rank = re.compile(r'^(?P<mode>\w+(?:\s*\w+)?)\s*(?P<rank>[AaBbCcSsXx][\+-]?)\s*(?P<number>[0-9]{0,4})$')


class SplatoonRank(app_commands.Transformer):
    mode: str
    rank: str
    number: str

    def __init__(self, argument: str, *, _rank=_rank):
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
            'rm': 'Rainmaker',
            'clam blitz': 'Clam Blitz',
            'clam': 'Clam Blitz',
            'blitz': 'Clam Blitz',
            'cb': 'Clam Blitz',
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
            value = int(number)
            if value and rank not in ('S+', 'X'):
                raise commands.BadArgument('Only S+ or X can input numbers.')
            if rank == 'S+' and value > 10:
                raise commands.BadArgument('S+10 is the current cap.')

        self.mode = mode
        self.rank = rank
        self.number = number

    def to_dict(self) -> dict[str, Any]:
        return {self.mode: {'rank': self.rank, 'number': self.number}}

    @classmethod
    async def transform(cls, interaction: discord.Interaction, value: str) -> Any:
        return cls(value)


def valid_squad(argument: str) -> str:
    arg = argument.strip('"')
    if len(arg) > 100:
        raise commands.BadArgument('Squad name way too long. Keep it less than 100 characters.')

    if arg.startswith('http'):
        arg = f'<{arg}>'
    return arg


_friend_code = re.compile(r'^(?:(?:SW|3DS)[- _]?)?(?P<one>[0-9]{4})[- _]?(?P<two>[0-9]{4})[- _]?(?P<three>[0-9]{4})$')


def valid_fc(argument: str, *, _fc=_friend_code) -> str:
    fc = argument.upper().strip('"')
    m = _fc.match(fc)
    if m is None:
        raise commands.BadArgument('Not a valid friend code!')

    return '{one}-{two}-{three}'.format(**m.groupdict())


class SplatoonWeapon(commands.Converter):
    async def convert(self, ctx: Context, argument: str):
        cog: Optional[Splatoon] = ctx.bot.get_cog('Splatoon')  # type: ignore
        if cog is None:
            raise commands.BadArgument('Splatoon related commands seemingly disabled.')

        query = argument.strip('"')
        if len(query) < 4:
            raise commands.BadArgument('Weapon name to query must be over 4 characters long.')

        weapons = cog.get_weapons_named(query)

        try:
            weapon = await ctx.disambiguate(weapons, lambda w: w['name'], ephemeral=True)
        except ValueError as e:
            raise commands.BadArgument(str(e)) from None
        else:
            return weapon


class Profile(commands.Cog):
    """Manage your Splatoon profile"""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{ADULT}')

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error), ephemeral=True)

    @commands.hybrid_group(invoke_without_command=True, fallback='get')
    @app_commands.describe(member='The member profile to get, if not given then it shows your profile')
    async def profile(
        self,
        ctx: Context,
        *,
        member: Annotated[Union[discord.Member, discord.User], DisambiguateMember] = None,
    ):
        """Retrieves a member's profile.

        All commands will create a profile for you.
        """

        member = member or ctx.author

        query = """SELECT * FROM profiles WHERE id=$1;"""
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            if member == ctx.author:
                await ctx.send(
                    'You did not set up a profile.'
                    f' If you want to input a switch friend code, type {ctx.prefix}profile switch 1234-5678-9012'
                    f' or check {ctx.prefix}help profile'
                )
            else:
                await ctx.send('This member did not set up a profile.')
            return

        # 0xF02D7D - Splatoon 2 Pink
        # 0x19D719 - Splatoon 2 Green
        e = discord.Embed(colour=0x19D719)

        keys = {
            'fc_switch': 'Switch FC',
            'nnid': 'Wii U NNID',
            'fc_3ds': '3DS FC',
        }

        for key, value in keys.items():
            e.add_field(name=value, value=record[key] or 'N/A', inline=True)

        # consoles = [f'__{v}__: {record[k]}' for k, v in keys.items() if record[k] is not None]
        # e.add_field(name='Consoles', value='\n'.join(consoles) if consoles else 'None!', inline=False)
        e.set_author(name=member.display_name, icon_url=member.display_avatar.with_format('png'))

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

    async def edit_fields(self, ctx: Context, **fields: str):
        keys = ', '.join(fields)
        values = ', '.join(f'${2 + i}' for i in range(len(fields)))

        query = f"""INSERT INTO profiles (id, {keys})
                    VALUES ($1, {values})
                    ON CONFLICT (id)
                    DO UPDATE
                    SET ({keys}) = ROW({values});
                 """

        await ctx.db.execute(query, ctx.author.id, *fields.values())

    @profile.command(usage='<NNID>')
    @app_commands.describe(nnid='Your NNID (Nintendo Network ID)')
    async def nnid(self, ctx: Context, *, nnid: Annotated[str, valid_nnid]):
        """Sets the NNID portion of your profile."""
        await self.edit_fields(ctx, nnid=nnid)
        await ctx.send('Updated NNID.')

    @profile.command()
    @app_commands.describe(squad='Your Splatoon squad')
    async def squad(self, ctx: Context, *, squad: Annotated[str, valid_squad]):
        """Sets the Splatoon 2 squad part of your profile."""
        await self.edit_fields(ctx, squad=squad)
        await ctx.send('Updated squad.')

    @profile.command(name='3ds')
    @app_commands.describe(fc='Your 3DS Friend Code')
    async def profile_3ds(self, ctx: Context, *, fc: Annotated[str, valid_fc]):
        """Sets the 3DS friend code of your profile."""
        await self.edit_fields(ctx, fc_3ds=fc)
        await ctx.send('Updated 3DS friend code.')

    @profile.command()
    @app_commands.describe(fc='Your Switch Friend Code')
    async def switch(self, ctx: Context, *, fc: Annotated[str, valid_fc]):
        """Sets the Switch friend code of your profile."""
        await self.edit_fields(ctx, fc_switch=fc)
        await ctx.send('Updated Switch friend code.')

    @profile.command()
    @app_commands.describe(weapon='Your Splatoon 2 main weapon')
    async def weapon(self, ctx: Context, *, weapon: Annotated[Dict[str, str], SplatoonWeapon]):
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

    @weapon.autocomplete('weapon')
    async def weapon_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cog: Optional[Splatoon] = self.bot.get_cog('Splatoon')  # type: ignore
        if cog is None:
            return []

        weapons = cog.query_weapons_named(current)[:25]
        return [app_commands.Choice(name=weapon['name'], value=weapon['name']) for weapon in weapons]

    @profile.command(usage='<mode> <rank>')
    @app_commands.describe(ranking='Your Splatoon 2 ranking')
    async def rank(self, ctx: Context, *, ranking: SplatoonRank):
        """Sets the Splatoon 2 rank part of your profile.

        You set the rank on a per mode basis, such as

        - tc/tower control
        - rm/rainmaker
        - sz/splat zones/zones
        - cb/clam/blitz/clam blitz
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

        await ctx.db.execute(query, ctx.author.id, ranking.to_dict())
        await ctx.send(f'Successfully set {ranking.mode} rank to {ranking.rank}{ranking.number}.')

    @profile.command()
    @app_commands.choices(
        field=[
            app_commands.Choice(value='all', name='Everything'),
            app_commands.Choice(value='nnid', name='NNID'),
            app_commands.Choice(value='switch', name='Switch Friend Code'),
            app_commands.Choice(value='3ds', name='3DS Friend Code'),
            app_commands.Choice(value='squad', name='Squad'),
            app_commands.Choice(value='weapon', name='Weapon'),
            app_commands.Choice(value='rank', name='All Rankings'),
            app_commands.Choice(value='tower control rank', name='Tower Control Rank'),
            app_commands.Choice(value='splat zones rank', name='Splat Zones Rank'),
            app_commands.Choice(value='rainmaker rank', name='Rainmaker Rank'),
            app_commands.Choice(value='clam blitz rank', name='Clam Blitz Rank'),
        ]
    )
    @app_commands.describe(field='The field to delete from your profile. If not given then your entire profile is deleted.')
    async def delete(
        self,
        ctx: Context,
        *,
        field: Literal[
            'all',
            'nnid',
            'switch',
            '3ds',
            'squad',
            'weapon',
            'rank',
            'tower control rank',
            'splat zones rank',
            'rainmaker rank',
            'clam blitz rank',
        ] = 'all',
    ):
        """Deletes a field from your profile.

        The valid fields that could be deleted are:

        - all
        - nnid
        - switch
        - 3ds
        - squad
        - weapon
        - rank
        - tower control rank
        - splat zones rank
        - rainmaker rank
        - clam blitz rank

        Omitting a field will delete your entire profile.
        """

        # simple case: delete entire profile
        if field == 'all':
            confirm = await ctx.prompt('Are you sure you want to delete your profile?')
            if confirm:
                query = "DELETE FROM profiles WHERE id=$1;"
                await ctx.db.execute(query, ctx.author.id)
                await ctx.send('Successfully deleted profile.')
            else:
                await ctx.send('Aborting profile deletion.')
            return

        # a little intermediate case, basic field deletion:
        field_to_column = {
            'nnid': 'nnid',
            'switch': 'fc_switch',
            '3ds': 'fc_3ds',
            'squad': 'squad',
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
    @app_commands.describe(query='The search query, must be at least 3 characters')
    async def search(self, ctx: Context, *, query: str):
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
        e.set_footer(text=format(plural(len(records)), 'record') + '\u2003' * 60 + '\u200b')
        await ctx.send(embed=e)

    @profile.command()
    async def stats(self, ctx: Context):
        """Retrieves some statistics on the profile database."""

        query = "SELECT COUNT(*) FROM profiles;"

        row: tuple[int] = await ctx.db.fetchrow(query)  # type: ignore
        total = row[0]

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
        e.title = f'Statistics for {plural(total):profile}'

        # top 3 weapons
        value = f'*{total_weapons} players with weapons*\n' + '\n'.join(
            f'{r["Weapon"]} ({r["Total"]} players)' for r in weapons[:3]
        )
        e.add_field(name='Top Splatoon 2 Weapons', value=value, inline=False)

        # get ranked data
        for index, mode in enumerate(('Splat Zones', 'Tower Control', 'Rainmaker', 'Clam Blitz')):
            query = f"""SELECT extra #> '{{sp2_rank,{mode},rank}}' AS "Rank",
                               COUNT(*) AS "Total"
                        FROM profiles
                        WHERE extra #> '{{sp2_rank,{mode},rank}}' IS NOT NULL
                        GROUP BY extra #> '{{sp2_rank,{mode},rank}}'
                        ORDER BY "Total" DESC
                     """

            records = await ctx.db.fetch(query)
            total = sum(r['Total'] for r in records)

            value = f'*{total} players*\n' + '\n'.join(
                f'{r["Rank"]}: {r["Total"]} ({r["Total"] / total:.2%})' for r in records
            )
            e.add_field(name=mode, value=value, inline=True)

            # add some empty padding so the embed doesn't look ugly
            if index % 2 == 1:
                e.add_field(name='\u200b', value='\u200b', inline=True)

        await ctx.send(embed=e)


async def setup(bot: RoboDanny):
    await bot.add_cog(Profile(bot))
