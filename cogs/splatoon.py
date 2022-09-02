from __future__ import annotations
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Optional, TypedDict
from typing_extensions import Annotated, NotRequired, Self

from discord.ext import commands, menus
from discord import app_commands
from .utils import config, fuzzy, time
from .utils.formats import plural
from .utils.paginator import RoboPages, FieldPageSource

from urllib.parse import quote as urlquote
from collections import defaultdict

import traceback
import datetime
import random
import asyncio
import discord
import logging
import pathlib
import yarl
import json

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import Context

    # Splatoon 2 Config schema

    class SplatoonConfigGear(TypedDict):
        kind: Optional[str]
        brand: str
        name: str
        stars: int
        main: Optional[str]
        frequent_skill: Optional[str]
        price: Optional[int]
        image: Optional[str]
        __gear__: Literal[True]

    class SplatoonConfigWeapon(TypedDict):
        name: str
        sub: str
        special: str
        special_cost: NotRequired[str]
        level: NotRequired[str]
        ink_saver_level: NotRequired[str]
        __weapon__: NotRequired[Literal[True]]

    class SplatoonConfigBrand(TypedDict):
        name: str
        buffed: Optional[str]
        nerfed: Optional[str]

    class SplatoonConfig(TypedDict):
        username: Optional[str]
        password: Optional[str]
        session_token: Optional[str]
        iksm_session: Optional[str]

        abilities: list[str]
        clothes: list[SplatoonConfigGear]
        shoes: list[SplatoonConfigGear]
        head: list[SplatoonConfigGear]
        weapons: list[SplatoonConfigWeapon]
        brands: list[SplatoonConfigBrand]
        maps: list[str]

    # SplatNet2 payloads (incomplete)

    RotationKey = Literal['gachi', 'clam_bitz', 'tower_control', 'rainmaker', 'regular']

    class RotationStagePayload(TypedDict):
        image: str
        id: str
        name: str

    class RotationGameModePayload(TypedDict):
        name: str
        key: RotationKey

    class RotationRulePayload(TypedDict):
        key: RotationKey
        multiline_name: str
        name: str

    class RotationPayload(TypedDict):
        id: int
        game_mode: RotationGameModePayload
        end_time: int
        start_time: int
        rule: RotationRulePayload
        stage_a: RotationStagePayload
        stage_b: RotationStagePayload

    class SchedulePayload(TypedDict):
        gachi: list[RotationPayload]
        regular: list[RotationPayload]
        league: list[RotationPayload]

    class SalmonRunStagePayload(TypedDict):
        image: str
        name: str

    class SalmonRunInnerWeaponPayload(TypedDict):
        thumbnail: str
        id: str
        image: str
        name: str

    class SalmonRunWeaponPayload(TypedDict):
        weapon: NotRequired[SalmonRunInnerWeaponPayload]
        id: str

    class SalmonRunDetailsPayload(TypedDict):
        stage: SalmonRunStagePayload
        start_time: int
        end_time: int
        weapons: list[SalmonRunWeaponPayload]

    class SalmonRunPayload(TypedDict):
        details: list[SalmonRunDetailsPayload]
        schedules: list[dict[str, int]]


log = logging.getLogger(__name__)


class GameEntry(NamedTuple):
    stage: str
    mode: str

    def is_valid(self, current: list[Self]) -> bool:
        # no dupes
        if self in current:
            return False

        # make sure the map isn't played in the last 2 games
        last_two_games = current[-2:]
        for prev in last_two_games:
            if prev.stage == self.stage:
                return False

        return True


class Unauthenticated(Exception):
    """Exception for when the iksm_session is expired"""


def get_random_scrims(modes: list[str], maps: list[str], count: int) -> list[GameEntry]:
    result: list[GameEntry] = []
    current_mode_index = 0
    for index in range(count):
        # only try up to 25 times instead of infinitely
        for i in range(25):
            entry = GameEntry(stage=random.choice(maps), mode=modes[current_mode_index])
            if entry.is_valid(result):
                result.append(entry)
                current_mode_index += 1
                if current_mode_index >= len(modes):
                    current_mode_index = 0
                break

    return result


RESOURCE_TO_EMOJI = {
    # Some of these names can be finnicky.
    'Ability Doubler': '<:abilitydoubler:338814715088338957>',
    'Bomb Defense Up': '<:bombdefup:338814715184807957>',
    'Sub Power Up': '<:subpowerup:338814716669329410>',
    'Swim Speed Up': '<:swimspeedup:338814717009068034>',
    'Cold-Blooded': '<:coldblooded:338814717340418069>',
    'Ink Saver (Main)': '<:inksavermain:338814717353263128>',
    'Ink Recovery Up': '<:inkrecoveryup:338814717411721216>',
    'Ink Saver (Sub)': '<:inksaversub:338814717470703616>',
    'Stealth Jump': '<:stealthjump:338814717483024395>',
    'Last-Ditch Effort': '<:lastditcheffort:338814717483155458>',
    'Special Saver': '<:specialsaver:338814717500063746>',
    'Tenacity': '<:tenacity:338814717525098499>',
    'Opening Gambit': '<:openinggambit:338814717529292802>',
    'Quick Respawn': '<:quickrespawn:338814717537812481>',
    'Respawn Punisher': '<:respawnpunisher:338814717562978304>',
    'Drop Roller': '<:droproller:338814717579755520>',
    'Ink Resistance Up': '<:inkresistance:338814717592076289>',
    'Comeback': '<:comeback:338814717663379456>',
    'Thermal Ink': '<:thermal_ink:338814717663641600>',
    'Ninja Squid': '<:ninjasquid:338814717743202304>',
    'Haunt': '<:haunt:338814717755654144>',
    'Object Shredder': '<:objectshredder:338814717780951050>',
    'Quick Super Jump': '<:quicksuperjump:338814717789208576>',
    'Special Charge Up': '<:specialchargeup:338814717793665024>',
    'Run Speed Up': '<:runspeedup:338814717823025152>',
    'Special Power Up': '<:specialpowerup:338815052234752000>',
    'Money': '<:money:338815193305972736>',
    'Unknown': '<:unknown:338815018101506049>',
}


class Rotation:
    def __init__(self, data: RotationPayload):
        self.mode: str = data['rule']['name']
        self.stage_a: str = data['stage_a']['name']
        self.stage_b: str = data['stage_b']['name']

        self.start_time: datetime.datetime = datetime.datetime.fromtimestamp(data['start_time'], tz=datetime.timezone.utc)
        self.end_time: datetime.datetime = datetime.datetime.fromtimestamp(data['end_time'], tz=datetime.timezone.utc)

    @property
    def current(self) -> bool:
        now = discord.utils.utcnow()
        return self.start_time <= now <= self.end_time

    def get_generic_value(self) -> str:
        return f'{self.stage_a} and {self.stage_b}'


class Gear:
    __slots__ = ('kind', 'brand', 'name', 'stars', 'main', 'frequent_skill', 'price', 'image')

    def __init__(self, data: dict[str, Any]):
        # This comes from SplatNet2 payload, I do not have the data anymore
        self.kind: Optional[str] = data['kind']
        brand = data['brand']
        self.brand: str = brand['name']
        self.name: str = data['name']
        self.stars: int = data['rarity'] + 1
        self.image: Optional[str] = data['image']
        self.main: Optional[str] = None
        self.price: Optional[int] = None
        self.frequent_skill: Optional[str]

        try:
            self.frequent_skill = brand['frequent_skill']['name']
        except KeyError:
            self.frequent_skill = None

    @classmethod
    def from_json(cls, data: SplatoonConfigGear) -> Self:
        """Load from our JSON file."""
        self = cls.__new__(cls)

        self.kind = data.get('kind')
        self.brand = data['brand']
        self.name = data['name']
        self.price = data['price']
        self.main = data['main']
        self.stars = data['stars']
        self.image = data.get('image')
        self.frequent_skill = data.get('frequent_skill')
        return self


class Weapon:
    __slots__ = ('name', 'sub', 'special', 'special_cost', 'level', 'ink_saver_level')

    def __init__(self, data: SplatoonConfigWeapon) -> None:
        self.name: str = data['name']
        self.sub: str = data['sub']
        self.special: str = data['special']
        self.special_cost: Optional[str] = data.get('special_cost')
        self.level: Optional[str] = data.get('level')
        self.ink_saver_level: Optional[str] = data.get('ink_saver_level')

    def to_dict(self) -> SplatoonConfigWeapon:
        payload: SplatoonConfigWeapon = {
            'name': self.name,
            'sub': self.sub,
            'special': self.special,
        }

        if self.special_cost is not None:
            payload['special_cost'] = self.special_cost
        if self.level is not None:
            payload['level'] = self.level
        if self.ink_saver_level is not None:
            payload['ink_saver_level'] = self.ink_saver_level

        return payload


class SalmonRun:
    def __init__(self, data: SalmonRunDetailsPayload):
        self.start_time: datetime.datetime = datetime.datetime.fromtimestamp(data['start_time'], tz=datetime.timezone.utc)
        self.end_time: datetime.datetime = datetime.datetime.fromtimestamp(data['end_time'], tz=datetime.timezone.utc)

        stage = data.get('stage', {})
        self.stage: str = stage.get('name')
        self._image: Optional[str] = stage.get('image')
        weapons = data.get('weapons')
        self.weapons: list[str] = []
        for weapon in weapons:
            actual_weapon_data = weapon.get('weapon')
            if actual_weapon_data is None:
                name = 'Rare-only Mystery' if weapon['id'] == '-2' else 'Mystery'
            else:
                name = actual_weapon_data.get('name', 'Mystery')
            self.weapons.append(name)

    @property
    def image(self) -> Optional[str]:
        return self._image and f'https://app.splatoon2.nintendo.net{self._image}'


class SalmonRunPageSource(menus.ListPageSource):
    def __init__(self, entries: list[SalmonRun]):
        super().__init__(entries=entries, per_page=1)

    async def format_page(self, menu: RoboPages, salmon: SalmonRun):
        e = discord.Embed(colour=0xFF7500, title='Salmon Run')

        if salmon.image:
            e.set_image(url=salmon.image)

        now = discord.utils.utcnow()
        if now <= salmon.start_time:
            e.set_footer(text='Starts').timestamp = salmon.start_time
            e.description = f'Starts in {time.human_timedelta(salmon.start_time)}'
        elif now <= salmon.end_time:
            e.set_footer(text='Ends').timestamp = salmon.end_time
            e.description = f'Ends in {time.human_timedelta(salmon.end_time)}'

        e.add_field(name='Weapons', value='\n'.join(salmon.weapons) or 'Unknown')
        e.add_field(name='Map', value=salmon.stage or 'Unknown')

        maximum = self.get_max_pages()
        if maximum > 1:
            e.title = f'Salmon Run {menu.current_page + 1} out of {maximum}'

        return e


class Splatfest:
    def __init__(self, data):
        names = data['names']
        self.alpha = names['alpha_short']  # Pearl
        self.bravo = names['bravo_short']  # Marina
        self.alpha_long = names['alpha_long']
        self.bravo_long = names['bravo_long']

        times = data['times']
        self.start = datetime.datetime.fromtimestamp(times['start'], tz=datetime.timezone.utc)
        self.end = datetime.datetime.fromtimestamp(times['end'], tz=datetime.timezone.utc)
        self.result = datetime.datetime.fromtimestamp(times['result'], tz=datetime.timezone.utc)
        self.announce = datetime.datetime.fromtimestamp(times['announce'], tz=datetime.timezone.utc)

        self.image = data['images']['panel']
        self.id = data['festival_id']

        colours = data['colors']

        def to_colour(d):
            return discord.Colour.from_rgb(int(d['r'] * 255), int(d['g'] * 255), int(d['b'] * 255))

        self.alpha_colour = to_colour(colours['alpha'])
        self.bravo_colour = to_colour(colours['bravo'])
        self.colour = to_colour(colours['middle'])

    def embed(self):
        e = discord.Embed(colour=self.colour, title=f'{self.alpha} vs {self.bravo}')
        now = discord.utils.utcnow()

        e.add_field(name='Pearl', value=self.alpha_long)
        e.add_field(name='Marina', value=self.bravo_long)

        if self.start > now:
            state = 'Starting'
            value = f'In {time.human_timedelta(self.start, source=now)}'
        elif self.end > now > self.start:
            state = 'Ending'
            value = f'In {time.human_timedelta(self.end, source=now)}'
        elif self.result > now:
            state = 'Waiting For Results'
            value = f'In {time.human_timedelta(self.result, source=now)}'
        else:
            state = 'Ended'
            value = time.human_timedelta(self.end, source=now)

        e.add_field(name=state, value=value, inline=False)
        e.set_image(url=f'https://app.splatoon2.nintendo.net{self.image}')
        return e


class Merchandise:
    def __init__(self, data):
        self.gear = Gear(data['gear'])
        self.skill = data['skill']['name']
        self.price = data['price']
        self.end_time = datetime.datetime.fromtimestamp(data['end_time'], tz=datetime.timezone.utc)


class MerchPageSource(menus.ListPageSource):
    def __init__(self, entries: list[Merchandise]):
        super().__init__(entries=entries, per_page=1)

    def format_page(self, menu: RoboPages, merch: Merchandise):
        original_gear = None
        data: SplatoonConfig = menu.ctx.cog.splat2_data  # type: ignore

        price, gear, skill = merch.price, merch.gear, merch.skill
        description = f'{time.human_timedelta(merch.end_time)} left to buy'
        gears: list[Gear] = data.get(gear.kind, [])  # type: ignore
        for elem in gears:
            if elem.name == gear.name:
                original_gear = elem
                break

        e = discord.Embed(colour=0x19D719, title=gear.name, description=description)

        if gear.image:
            e.set_thumbnail(url=f'https://app.splatoon2.nintendo.net{gear.image}')
        else:
            e.set_thumbnail(url='https://cdn.discordapp.com/emojis/338815018101506049.png')

        e.add_field(name='Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {price or "???"}')

        UNKNOWN = RESOURCE_TO_EMOJI['Unknown']

        main_slot = RESOURCE_TO_EMOJI.get(skill, UNKNOWN)
        remaining = UNKNOWN * gear.stars
        e.add_field(name='Slots', value=f'{main_slot} | {remaining}')

        if original_gear is not None:
            original = RESOURCE_TO_EMOJI.get(original_gear.main, UNKNOWN)  # type: ignore
            original_remaining = UNKNOWN * original_gear.stars
            original_price = original_gear.price
        else:
            original = UNKNOWN
            original_remaining = remaining
            original_price = '???'

        e.add_field(name='Original Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {original_price}')
        e.add_field(name='Original Slots', value=f'{original} | {original_remaining}')

        e.add_field(name='Brand', value=gear.brand)
        if gear.frequent_skill is not None:
            common = gear.frequent_skill
        else:
            brands = data.get('brands', [])
            for brand in brands:
                if brand['name'] == gear.brand:
                    common = brand['buffed']
                    break
            else:
                common = 'Not found...'

        e.add_field(name='Common Gear Ability', value=common)
        maximum = self.get_max_pages()
        if maximum > 1:
            e.set_footer(text=f'Gear {menu.current_page + 1}/{maximum}')

        return e


# JSON stuff


class SplatoonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Gear):
            payload = {attr: getattr(obj, attr) for attr in Gear.__slots__}
            payload['__gear__'] = True
            return payload
        if isinstance(obj, Weapon):
            payload = obj.to_dict()
            payload['__weapon__'] = True
            return payload
        return super().default(obj)


def splatoon_decoder(obj: Any) -> Any:
    if '__gear__' in obj:
        return Gear.from_json(obj)
    if '__weapon__' in obj:
        return Weapon(obj)
    return obj


def mode_key(argument: str) -> str:
    lower = argument.lower().strip('"')
    if lower.startswith('rank'):
        return 'Ranked Battle'
    elif lower.startswith('turf') or lower.startswith('regular'):
        return 'Regular Battle'
    elif lower == 'league':
        return 'League Battle'
    else:
        raise commands.BadArgument('Unknown schedule type, try: "ranked", "regular", or "league"')


class AddWeaponModal(discord.ui.Modal, title='Add New Weapon'):
    name = discord.ui.TextInput(label='Name', placeholder='The weapon name')
    sub = discord.ui.TextInput(label='Sub', placeholder='The sub weapon name')
    special = discord.ui.TextInput(label='Special', placeholder='The special weapon name')

    def __init__(self, cog: Splatoon):
        super().__init__()
        self.cog: Splatoon = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        weapons = self.cog.splat2_data.get('weapons', [])
        entry = {
            'name': self.name.value,
            'sub': self.sub.value,
            'special': self.special.value,
        }
        weapons.append(entry)
        await self.cog.splat2_data.put('weapons', weapons)
        await interaction.response.send_message(f'Successfully added new weapon {self.name}')


class SimpleTextModal(discord.ui.Modal):
    def __init__(self, *, title: str, label: str) -> None:
        super().__init__(title=title)
        self.input = discord.ui.TextInput(label=label)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.stop()


class AdminPanel(discord.ui.View):
    def __init__(self, cog: Splatoon):
        super().__init__()
        self.cog: Splatoon = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.cog.bot.is_owner(interaction.user):
            await interaction.response.send_message('This panel is not meant for you.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Add Weapon')
    async def add_weapon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddWeaponModal(self.cog))

    @discord.ui.button(label='Add Map')
    async def add_map(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SimpleTextModal(title='Add New Map', label='Name')
        await interaction.response.send_modal(modal)
        await modal.wait()

        name = modal.input.value
        entry = self.cog.splat2_data.get('maps', [])
        entry.append(name)
        await self.cog.splat2_data.put('maps', entry)

        await modal.interaction.response.send_message(f'Successfully added new map {name}')

    @discord.ui.button(label='Refresh iksm_session')
    async def refresh_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SimpleTextModal(title='Refresh Cookie', label='Cookie')
        await interaction.response.send_modal(modal)
        await modal.wait()

        cookie = modal.input.value
        await self.cog.splat2_data.put('iksm_session', cookie)
        await modal.interaction.response.send_message(f'Successfully refreshed cookie', ephemeral=True)

    @discord.ui.button(label='Exit', style=discord.ButtonStyle.red)
    async def exit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.delete_original_response()


class Splatoon(commands.GroupCog):
    """Splatoon related commands."""

    BASE_URL = yarl.URL('https://app.splatoon2.nintendo.net')

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self.splat2_data: config.Config[Any] = config.Config(
            'splatoon2.json', object_hook=splatoon_decoder, encoder=SplatoonEncoder
        )
        self.splat3_data: config.Config[Any] = config.Config(
            'splatoon3.json', object_hook=splatoon_decoder, encoder=SplatoonEncoder
        )
        self._splatnet2: asyncio.Task[None] = asyncio.create_task(self.splatnet2())
        self._last_request: datetime.datetime = discord.utils.utcnow()

        # mode: List[Rotation]
        self.sp2_map_data: dict[str, list[Rotation]] = {}
        self.sp2_shop: list[Merchandise] = []
        self.sp2_festival: Optional[Splatfest] = None
        self.sp2_salmonrun: list[SalmonRun] = []

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='SquidPink', id=230079634086166530)

    def cog_unload(self):
        self._splatnet2.cancel()

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            return await ctx.send(str(error))

    @property
    def salmon_run(self) -> list[SalmonRun]:
        now = discord.utils.utcnow()
        return [s for s in self.sp2_salmonrun if now < s.end_time]

    async def log_error(self, *, ctx: Optional[Context] = None, extra: Any = None):
        e = discord.Embed(title='Error', colour=0xDD5F53)
        e.description = f'```py\n{traceback.format_exc()}\n```'
        e.add_field(name='Extra', value=extra, inline=False)
        e.timestamp = discord.utils.utcnow()

        if ctx is not None:
            fmt = '{0} (ID: {0.id})'
            author = fmt.format(ctx.author)
            channel = fmt.format(ctx.channel)
            guild = 'None' if ctx.guild is None else fmt.format(ctx.guild)

            e.add_field(name='Author', value=author)
            e.add_field(name='Channel', value=channel)
            e.add_field(name='Guild', value=guild)

        await self.bot.stats_webhook.send(embed=e)

    async def parse_splatnet2_schedule(self) -> Optional[float]:
        self.sp2_map_data = {}
        async with self.bot.session.get(self.BASE_URL / 'api/schedules') as resp:
            if resp.status == 403:
                raise Unauthenticated()

            if resp.status != 200:
                await self.log_error(extra=f'Splatnet schedule responded with {resp.status}.')
                return

            data = await resp.json()
            gachi = data.get('gachi', [])
            self.sp2_map_data['Ranked Battle'] = [Rotation(d) for d in gachi]
            regular = data.get('regular', [])
            self.sp2_map_data['Regular Battle'] = [Rotation(d) for d in regular]

            league = data.get('league', [])
            self.sp2_map_data['League Battle'] = [Rotation(d) for d in league]

            newest = []
            for key, value in self.sp2_map_data.items():
                value.sort(key=lambda r: r.end_time)
                try:
                    newest.append(value[0].end_time)
                except IndexError:
                    pass

            try:
                new = max(newest)
            except ValueError:
                return

            now = discord.utils.utcnow()
            if now <= new:
                return (new - now).total_seconds()

    async def parse_splatnet2_onlineshop(self) -> Optional[float]:
        self.sp2_shop = []
        async with self.bot.session.get(self.BASE_URL / 'api/onlineshop/merchandises') as resp:
            if resp.status == 403:
                raise Unauthenticated()

            if resp.status != 200:
                await self.log_error(extra=f'Splatnet Shop responded with {resp.status}.')
                return

            data = await resp.json()
            merch = data.get('merchandises')
            if not merch:
                return

            dirty = False
            for elem in merch:
                try:
                    value = Merchandise(elem)
                except KeyError:
                    pass
                else:
                    self.sp2_shop.append(value)

                    # update our image cache
                    kind = self.splat2_data.get(value.gear.kind, [])
                    for gear in kind:
                        if gear.name == value.gear.name:
                            if not dirty:
                                dirty = gear.image != value.gear.image
                            gear.image = value.gear.image
                            break

            if dirty:
                await self.splat2_data.save()

            self.sp2_shop.sort(key=lambda m: m.end_time)
            now = discord.utils.utcnow()

            try:
                end = self.sp2_shop[0].end_time
                if now <= end:
                    return (end - now).total_seconds()
            except IndexError:
                pass

    def scrape_data_from_player(self, player: dict[str, Any], bulk: dict[str, Any]):
        for kind in ('shoes', 'head', 'clothes'):
            try:
                gear = Gear(player[kind])
            except KeyError:
                continue
            else:
                bulk[kind][gear.name] = gear

    async def scrape_splatnet_stats_and_images(self) -> Optional[float]:
        bulk_lookup = defaultdict(dict)
        async with self.bot.session.get(self.BASE_URL / 'api/results') as resp:
            if resp.status == 403:
                raise Unauthenticated()

            if resp.status != 200:
                await self.log_error(extra=f'Splatnet Stats responded with {resp.status}.')
                return

            data = await resp.json()
            results = data['results']
            base_path = pathlib.Path('splatoon2_stats')

            newest = base_path / f'{results[0]["battle_number"]}.json'

            # we already scraped, so try again in an hour
            if newest.exists():
                log.info('No Splatoon 2 result data to scrape, retrying in an hour.')
                return

            # I am sorry papa nintendo
            pre_existing_statistics = sorted([int(p.stem) for p in base_path.iterdir()])
            if pre_existing_statistics:
                largest = pre_existing_statistics[-1]
            else:
                largest = 0

            added = 0
            for result in results:
                try:
                    number = result['battle_number']
                except KeyError:
                    continue

                if int(number) <= largest:
                    continue

                # request the full information:
                async with self.bot.session.get(resp.url / number) as r:
                    if r.status != 200:
                        extra = f'Splatoon Stat {number} responded with {r.status}.'
                        await self.log_error(extra=extra)
                        continue

                    js = await r.json()

                    # save our statistics
                    path = base_path / f'{number}.json'
                    with path.open('w', encoding='utf-8') as fp:
                        json.dump(js, fp, indent=2)

                    added += 1

                    # add stuff to image cache
                    for enemy in js.get('other_team_members', []):
                        self.scrape_data_from_player(enemy.get('player', {}), bulk_lookup)

                    me = js.get('player_result', {}).get('player', {})
                    self.scrape_data_from_player(me, bulk_lookup)

                    for team in js.get('my_team_members', []):
                        self.scrape_data_from_player(team.get('player', {}), bulk_lookup)

                await asyncio.sleep(1)  # one request a second so we don't abuse

            log.info('Scraped Splatoon 2 results from %s games.', added)

            # done with bulk lookups so actually change and save now:
            for kind, data in bulk_lookup.items():
                old = self.splat2_data.get(kind, [])
                for gear in old:
                    try:
                        new_data = data.pop(gear.name)
                    except KeyError:
                        continue
                    else:
                        gear.image = new_data.image

                # add new data
                log.info('Scraped %s new pieces of %s gear.', len(data), kind)
                for value in data.values():
                    old.append(value)

            await self.splat2_data.save()

    async def parse_splatnet2_splatfest(self) -> Optional[float]:
        self.sp2_festival = None
        async with self.bot.session.get(self.BASE_URL / 'api/festivals/active') as resp:
            if resp.status == 403:
                raise Unauthenticated()

            if resp.status != 200:
                await self.log_error(extra=f'Splatnet Splatfest Error')
                return

            js = await resp.json()
            festivals = js['festivals']
            if len(festivals) == 0:
                return

            current = festivals[0]
            self.sp2_festival = Splatfest(current)

    async def parse_splatnet2_salmonrun(self):
        self.sp2_salmonrun = []
        async with self.bot.session.get(self.BASE_URL / 'api/coop_schedules') as resp:
            if resp.status != 200:
                await self.log_error(extra=f'Splatnet Salmon Run Error')
                return

            js = await resp.json()

            data = js['details']
            if len(data) == 0:
                return

            self.sp2_salmonrun = [SalmonRun(d) for d in data]
            self.sp2_salmonrun.sort(key=lambda m: m.end_time)
            now = discord.utils.utcnow()
            end = self.sp2_salmonrun[0].end_time
            return None if now > end else (end - now).total_seconds()

    async def splatnet2(self):
        try:
            cookie = self.splat2_data.get('iksm_session')
            if cookie is None:
                raise Unauthenticated()

            self.bot.session.cookie_jar.update_cookies({'iksm_session': cookie}, response_url=self.BASE_URL)
            while not self.bot.is_closed():
                seconds = []
                seconds.append((await self.parse_splatnet2_schedule()) or 3600.0)
                seconds.append((await self.parse_splatnet2_onlineshop()) or 3600.0)
                seconds.append((await self.parse_splatnet2_salmonrun()) or 3600.0)
                seconds.append((await self.scrape_splatnet_stats_and_images()) or 3600.0)
                seconds.append((await self.parse_splatnet2_splatfest()) or 3600.0)
                self._last_request = discord.utils.utcnow()
                await asyncio.sleep(min(seconds))
        except Unauthenticated:
            await self.log_error(extra=f'Unauthenticated for SplatNet')
            raise
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed):
            self._splatnet2.cancel()
            self._splatnet2 = self.bot.loop.create_task(self.splatnet2())
        except Exception:
            await self.log_error(extra='SplatNet 2 Error')

    def get_weapons_named(self, name: str) -> list[Weapon]:
        data: list[Weapon] = self.splat3_data.get('weapons', [])
        name = name.lower()

        choices = {w.name.lower(): w for w in data}
        results = fuzzy.extract_or_exact(name, choices, scorer=fuzzy.token_sort_ratio, score_cutoff=60)
        return [v for k, _, v in results]

    def query_weapons_named(self, name: str) -> list[Weapon]:
        data: list[Weapon] = self.splat3_data.get('weapons', [])
        results = fuzzy.finder(name, data, key=lambda w: w.name)
        return results

    async def generate_scrims(self, ctx: Context, maps: list[str], games: int, mode: Optional[str]):
        modes = ['Rainmaker', 'Splat Zones', 'Tower Control', 'Clam Blitz']

        if mode is not None:
            mode = mode.lower()

            # shortcuts that can't be detected by fuzzy matching:
            shortcuts = {
                'rm': 'Rainmaker',
                'sz': 'Splat Zones',
                'tc': 'Tower Control',
                'tw': 'Turf War',
                'cb': 'Clam Blitz',
            }

            real_mode = shortcuts.get(mode)
            if real_mode is None:
                real_mode = fuzzy.extract_one(mode, modes + ['Turf War'], scorer=fuzzy.partial_ratio, score_cutoff=50)
                if real_mode is not None:
                    real_mode = real_mode[0]
                else:
                    return await ctx.send('Could not figure out what mode you meant.')

            result = [f'The following games will be played in {real_mode}.']
            game_count = max(min(games, len(maps)), 3)
            for index, stage in enumerate(random.sample(maps, game_count), 1):
                result.append(f'Game {index}: {stage}')
        else:
            game_count = max(min(games, len(maps) * 4), 3)
            random.shuffle(modes)
            scrims = get_random_scrims(modes, maps, game_count)
            result = [f'Game {game}: {scrim.mode} on {scrim.stage}' for game, scrim in enumerate(scrims, 1)]

        await ctx.safe_send('\n'.join(result))

    @commands.command(hidden=True)
    async def marie(self, ctx: Context):
        """A nice little easter egg."""
        await ctx.send('http://i.stack.imgur.com/0OT9X.png')

    @commands.hybrid_command()
    @app_commands.describe(title='The title of the page to search for.')
    async def splatwiki(self, ctx: Context, *, title: str):
        """Returns a Inkipedia page."""
        url = f'http://splatoonwiki.org/wiki/Special:Search/{urlquote(title)}'

        async with ctx.session.get(url) as resp:
            if 'Special:Search' in resp.url.path:
                await ctx.send(f'Could not find your page. Try a search:\n{resp.url.human_repr()}')
            elif resp.status == 200:
                await ctx.send(str(resp.url))
            elif resp.status == 502:
                await ctx.send('It seems that Inkipedia is taking too long to respond. Try again later.')
            else:
                await ctx.send(f'An error has occurred of status code {resp.status} happened.')

    async def generic_splatoon2_schedule(self, ctx: Context):
        e = discord.Embed(colour=0x19D719)
        end_time = None

        for key, value in self.sp2_map_data.items():
            try:
                rotation = value[0]
            except IndexError:
                e.add_field(name=key, value='Nothing found...', inline=False)
                continue
            else:
                end_time = rotation.end_time

            e.add_field(name=f'{key}: {rotation.mode}', value=f'{rotation.stage_a} and {rotation.stage_b}', inline=False)

        if end_time is not None:
            e.title = f'For {time.human_timedelta(end_time)}...'

        await ctx.send(embed=e)

    async def paginated_splatoon2_schedule(self, ctx: Context, mode: str):
        try:
            data = self.sp2_map_data[mode]
        except KeyError:
            return await ctx.send('Sorry, no map data found...')

        entries = [
            (
                f'Now: {r.mode}' if r.current else f'In {time.human_timedelta(r.start_time)}: {r.mode}',
                f'{r.stage_a} and {r.stage_b}',
            )
            for r in data
        ]

        p = FieldPageSource(entries, per_page=4)
        p.embed.colour = 0xF02D7D
        menu = RoboPages(p, ctx=ctx, compact=True)
        await menu.start()

    @commands.hybrid_command(aliases=['maps'])
    @app_commands.choices(
        type=[
            app_commands.Choice(name='Ranked Battle', value='rank'),
            app_commands.Choice(name='Turf War', value='turf'),
            app_commands.Choice(name='League Battle', value='league'),
        ]
    )
    @app_commands.describe(type='The type of schedule to show')
    async def schedule(self, ctx, *, type: Annotated[Optional[str], mode_key] = None):
        """Shows the current Splatoon 2 schedule."""
        if type is None:
            await self.generic_splatoon2_schedule(ctx)
        else:
            await self.paginated_splatoon2_schedule(ctx, type)

    @commands.hybrid_command()
    async def nextmaps(self, ctx: Context):
        """Shows the next Splatoon 2 maps."""
        e = discord.Embed(colour=0x19D719, description='Nothing found...')

        start_time = None
        for key, value in self.sp2_map_data.items():
            try:
                rotation = value[1]
            except IndexError:
                e.add_field(name=key, value='Nothing found...', inline=False)
                continue
            else:
                start_time = rotation.start_time
                e.description = None

            e.add_field(name=f'{key}: {rotation.mode}', value=f'{rotation.stage_a} and {rotation.stage_b}', inline=False)

        if start_time is not None:
            e.title = f'In {time.human_timedelta(start_time)}'

        await ctx.send(embed=e)

    @commands.hybrid_command()
    async def salmonrun(self, ctx: Context):
        """Shows the Salmon Run schedule, if any."""
        salmon = self.salmon_run
        if not salmon:
            return await ctx.send('No Salmon Run schedule reported.')

        pages = RoboPages(SalmonRunPageSource(salmon), ctx=ctx, compact=True)
        await pages.start()

    @commands.hybrid_command(aliases=['splatnetshop'])
    async def splatshop(self, ctx: Context):
        """Shows the currently running SplatNet 2 merchandise."""
        if not self.sp2_shop:
            return await ctx.send('Nothing currently being sold...')

        pages = RoboPages(MerchPageSource(self.sp2_shop), ctx=ctx, compact=True)
        await pages.start()

    @commands.hybrid_command()
    async def splatfest(self, ctx: Context):
        """Shows information about the currently running NA Splatfest, if any."""
        if self.sp2_festival is None:
            return await ctx.send('No Splatfest has been announced.')

        await ctx.send(embed=self.sp2_festival.embed())

    @commands.hybrid_command()
    @app_commands.describe(query='The weapon name, sub, or special to search for')
    async def weapon(self, ctx: Context, *, query: str):
        """Displays Splatoon 2 weapon info from a query.

        The query must be at least 3 characters long, otherwise it'll tell you it failed.
        """
        query = query.strip().lower()
        weapons = self.splat2_data.get('weapons', [])
        if len(query) < 3:
            return await ctx.send('The query must be at least 3 characters long.')

        def predicate(weapon):
            lowered = [weapon.lower() for weapon in weapon.values()]
            return any(query in wep for wep in lowered)

        results = list(filter(predicate, weapons))
        if not results:
            return await ctx.send('No results found.')

        e = discord.Embed(colour=discord.Colour.blurple())
        e.title = f'Found {plural(len(results)):weapon}'

        subs = '\n'.join(w['sub'] for w in results)
        names = '\n'.join(w['name'] for w in results)
        special = '\n'.join(w['special'] for w in results)

        e.add_field(name='Name', value=names)
        e.add_field(name='Sub', value=subs)
        e.add_field(name='Special', value=special)
        await ctx.send(embed=e)

    @commands.hybrid_command()
    @app_commands.describe(games='The number of games to scrim for', mode='The mode to play in')
    @app_commands.choices(
        mode=[app_commands.Choice(name=r, value=r) for r in ('Rainmaker', 'Splat Zones', 'Tower Control', 'Clam Blitz')]
    )
    async def scrim(self, ctx: Context, games: int = 5, *, mode: Optional[str] = None):
        """Generates Splatoon 2 scrim map and mode combinations.

        The mode combinations do not have Turf War.

        The mode is rotated unless you pick a mode to play, in which all map
        combinations will use that mode instead.
        """
        maps = self.splat2_data.get('maps', [])
        await self.generate_scrims(ctx, maps, games, mode)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def splatoon_admin(self, ctx: Context):
        """Administration panel for Splatoon configuration"""

        e = discord.Embed(colour=0x19D719, title='Splatoon Cog Administration')
        splatnet = not self._splatnet2.done()
        unauthed = self._splatnet2.done() and isinstance(self._splatnet2.exception(), Unauthenticated)

        e.add_field(name='SplatNet 2 Running', value=ctx.tick(splatnet), inline=True)
        e.add_field(name='SplatNet 2 Authenticated', value=ctx.tick(not unauthed), inline=True)
        e.add_field(name='Last SplatNet 2 Request', value=time.format_dt(self._last_request, 'R'), inline=False)

        await ctx.send(embed=e, view=AdminPanel(self))


async def setup(bot: RoboDanny):
    await bot.add_cog(Splatoon(bot))
