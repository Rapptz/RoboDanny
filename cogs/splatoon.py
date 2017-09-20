from discord.ext import commands
from .utils import config, checks, maps, fuzzy, time, formats
from .utils.formats import Plural
from .utils.paginator import FieldPages, Pages

from urllib.parse import quote as urlquote
from email.utils import parsedate_to_datetime
from collections import namedtuple, defaultdict

import itertools
import datetime
import random
import asyncio
import discord
import logging
import aiohttp
import pathlib
import yarl
import json
import re

log = logging.getLogger(__name__)

GameEntry = namedtuple('GameEntry', ('stage', 'mode'))

def is_valid_entry(result, entry):
    # no dupes
    if entry in result:
        return False

    # make sure the map isn't played in the last 2 games
    last_two_games = result[-2:]
    for prev in last_two_games:
        if prev.stage == entry.stage:
            return False

    return True

def get_random_scrims(modes, maps, count):
    result = []
    current_mode_index = 0
    for index in range(count):
        # only try up to 25 times instead of infinitely
        for i in range(25):
            entry = GameEntry(stage=random.choice(maps), mode=modes[current_mode_index])
            if is_valid_entry(result, entry):
                result.append(entry)
                current_mode_index += 1
                if current_mode_index >= len(modes):
                    current_mode_index = 0
                break

    return result

def is_salmon_moderator():
    def predicate(ctx):
        return ctx.channel.id == 359442516820623361
    return commands.check(predicate)

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

SALMON_RUN_MAPS = {
    'Spawning Grounds': 'https://i.imgur.com/6W5xsyr.jpg',
    'Marooner\'s Bay': 'https://i.imgur.com/gD0VEyP.jpg',
    'Lost Outpost': 'https://i.imgur.com/GuyhMG1.jpg',
}

# There's going to be some code duplication here because it's more
# straightforward than trying to be clever, I guess.
# I hope at one day to fix this and make it not-so-ugly.
# Hopefully by completely dropping Splatoon 1 support in
# the future.

class BrandResults:
    __slots__ = ('ability_name', 'info', 'buffs', 'nerfs')

    def __init__(self, brand=None, ability_name=None):
        self.info = brand
        self.ability_name = ability_name
        self.buffs = []
        self.nerfs = []

    def is_brand(self):
        return self.ability_name is None

class Rotation:
    def __init__(self, data):
        self.mode = data['rule']['name']
        self.stage_a = data['stage_a']['name']
        self.stage_b = data['stage_b']['name']

        self.start_time = datetime.datetime.utcfromtimestamp(data['start_time'])
        self.end_time = datetime.datetime.utcfromtimestamp(data['end_time'])

    @property
    def current(self):
        now = datetime.datetime.utcnow()
        return self.start_time <= now <= self.end_time

    def get_generic_value(self):
        return f'{self.stage_a} and {self.stage_b}'

class Gear:
    __slots__ = ('kind', 'brand', 'name', 'stars', 'main', 'frequent_skill', 'price', 'image')

    def __init__(self, data):
        self.kind = data['kind']
        brand = data['brand']
        self.brand = brand['name']
        self.name = data['name']
        self.stars = data['rarity'] + 1
        self.image = data['image']

        try:
            self.frequent_skill = brand['frequent_skill']['name']
        except KeyError:
            self.frequent_skill = None

    @classmethod
    def from_json(cls, data):
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

class SalmonRun:
    def __init__(self, start_time, end_time, id):
        self.start_time = start_time
        self.end_time = end_time
        self.id = id
        self.stage = None
        self.deleted = False
        self.weapons = []

    @property
    def image(self):
        return SALMON_RUN_MAPS.get(self.stage)

    @classmethod
    def from_dict(cls, data):
        fromutc = datetime.datetime.fromtimestamp

        self = cls(fromutc(data['start_time']), fromutc(data['end_time']), data['id'])
        self.stage = data['stage']
        self.weapons = data['weapons']
        self.deleted = data['deleted']
        return self

    def to_dict(self):
        return {
            'start_time': self.start_time.timestamp(),
            'end_time': self.end_time.timestamp(),
            'id': self.id,
            'weapons': self.weapons,
            'stage': self.stage,
            'deleted': self.deleted,
            '__salmon__': True,
        }

    def reset(self):
        self.stage = None
        self.weapons = []

class SalmonRunPages(Pages):
    def __init__(self, ctx, entries):
        super().__init__(ctx, entries=entries, per_page=1)
        self.reaction_emojis = [
            ('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.first_page),
            ('\N{BLACK LEFT-POINTING TRIANGLE}', self.previous_page),
            ('\N{BLACK RIGHT-POINTING TRIANGLE}', self.next_page),
            ('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.last_page),
            ('\N{BLACK SQUARE FOR STOP}', self.stop_pages),
        ]

        self.splat2_data = ctx.cog.splat2_data

    async def show_page(self, page, *, first=False):
        self.current_page = page
        salmon = self.entries[page - 1]
        self.embed = e = discord.Embed(colour=0xFF7500, title='Salmon Run')

        if salmon.image:
            e.set_image(url=salmon.image)

        now = datetime.datetime.utcnow()
        if now <= salmon.start_time:
            e.set_footer(text='Starts').timestamp = salmon.start_time
            e.description = f'Starts in {time.human_timedelta(salmon.start_time)}'
        elif now <= salmon.end_time:
            e.set_footer(text='Ends').timestamp = salmon.end_time
            e.description = f'Ends in {time.human_timedelta(salmon.end_time)}'

        e.add_field(name='Weapons', value='\n'.join(salmon.weapons) or 'Unknown')
        e.add_field(name='Map', value=salmon.stage or 'Unknown')

        if self.maximum_pages > 1:
            e.title = f'Salmon Run {page} out of {self.maximum_pages}'

        if not self.paginating:
            return await self.channel.send(embed=self.embed)

        if not first:
            await self.message.edit(embed=self.embed)
            return

        self.message = await self.channel.send(embed=self.embed)
        for (reaction, _) in self.reaction_emojis:
            if self.maximum_pages == 2 and reaction in ('\u23ed', '\u23ee'):
                # no |<< or >>| buttons if we only have two pages
                # we can't forbid it if someone ends up using it but remove
                # it from the default set
                continue

            await self.message.add_reaction(reaction)

class Splatfest:
    def __init__(self, data):
        names = data['names']
        self.alpha = names['alpha_short'] # Pearl
        self.bravo = names['bravo_short'] # Marina
        self.alpha_long = names['alpha_long']
        self.bravo_long = names['bravo_long']

        times = data['times']
        fromutc = datetime.datetime.utcfromtimestamp
        self.start = fromutc(times['start'])
        self.end = fromutc(times['end'])
        self.result = fromutc(times['result'])
        self.announce = fromutc(times['announce'])

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
        now = datetime.datetime.utcnow()

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
        self.end_time = datetime.datetime.utcfromtimestamp(data['end_time'])

class GearPages(Pages):
    def __init__(self, ctx, entries):
        super().__init__(ctx, entries=entries, per_page=1)
        # remove help reaction
        self.reaction_emojis.pop()
        self.splat2_data = ctx.cog.splat2_data

    async def show_page(self, page, *, first=False):
        self.current_page = page
        merch = self.entries[page - 1]
        original_gear = None

        if isinstance(merch, Merchandise):
            price, gear, skill = merch.price, merch.gear, merch.skill
            description = f'{time.human_timedelta(merch.end_time)} left to buy'
            data = self.splat2_data.get(gear.kind, [])
            for elem in data:
                if elem.name == gear.name:
                    original_gear = elem
                    break
        elif isinstance(merch, Gear):
            price, gear, skill = merch.price, merch, merch.main
            description = discord.Embed.Empty

        self.embed = e = discord.Embed(colour=0x19D719, title=gear.name, description=description)

        if gear.image:
            e.set_thumbnail(url=f'https://app.splatoon2.nintendo.net{gear.image}')
        else:
            e.set_thumbnail(url='https://cdn.discordapp.com/emojis/338815018101506049.png')

        e.add_field(name='Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {price}')

        UNKNOWN = RESOURCE_TO_EMOJI['Unknown']

        main_slot = RESOURCE_TO_EMOJI.get(skill, UNKNOWN)
        remaining = UNKNOWN * gear.stars
        e.add_field(name='Slots', value=f'{main_slot} | {remaining}')

        if isinstance(merch, Merchandise):
            if original_gear is not None:
                original = RESOURCE_TO_EMOJI.get(original_gear.main, UNKNOWN)
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
            brands = self.splat2_data.get('brands', [])
            for brand in brands:
                if brand['name'] == gear.brand:
                    common = brand['buffed']
                    break
            else:
                common = 'Not found...'
        e.add_field(name='Common Gear Ability', value=common)

        if self.maximum_pages > 1:
            e.set_footer(text=f'Gear {page}/{self.maximum_pages}')

        if not self.paginating:
            return await self.channel.send(embed=self.embed)

        if not first:
            await self.message.edit(embed=self.embed)
            return

        self.message = await self.channel.send(embed=self.embed)
        for (reaction, _) in self.reaction_emojis:
            if self.maximum_pages == 2 and reaction in ('\u23ed', '\u23ee'):
                # no |<< or >>| buttons if we only have two pages
                # we can't forbid it if someone ends up using it but remove
                # it from the default set
                continue

            await self.message.add_reaction(reaction)

# JSON stuff

class Splatoon2Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Gear):
            payload = {
                attr: getattr(obj, attr)
                for attr in Gear.__slots__
            }
            payload['__gear__'] = True
            return payload
        if isinstance(obj, SalmonRun):
            return obj.to_dict()
        return super().default(obj)

def splatoon2_decoder(obj):
    if '__gear__' in obj:
        return Gear.from_json(obj)
    if '__salmon__' in obj:
        return SalmonRun.from_dict(obj)
    return obj

def mode_key(argument):
    lower = argument.lower().strip('"')
    if lower.startswith('rank'):
        return 'Ranked Battle'
    elif lower.startswith('turf') or lower.startswith('regular'):
        return 'Regular Battle'
    elif lower == 'league':
        return 'League Battle'
    else:
        raise commands.BadArgument('Unknown schedule type, try: "ranked", "regular", or "league"')

_iso_regex = re.compile(r'([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})[T\s]([0-9]{1,2})\:([0-9]{1,2})')

def iso8601(argument, *, _re=_iso_regex):
    # YYYY-MM-DDTHH:MM
    m = _re.match(argument)
    if m is None:
        raise commands.BadArgument(f'Bad time provided ({argument})')

    return datetime.datetime(*map(int, m.groups()))

class BrandOrAbility(commands.Converter):
    def __init__(self, splatoon2=True):
        self.splatoon2 = splatoon2

    async def convert(self, ctx, argument):
        query = argument.lower()
        if len(query) < 4:
            raise commands.BadArgument('The query must be at least 5 characters long.')

        data = ctx.cog.splat1_data if not self.splatoon2 else ctx.cog.splat2_data
        brands = data.get('brands', [])

        result = None

        # check for exact match
        for brand in brands:
            if brand['name'].lower() == query:
                return BrandResults(brand=brand)

        # check for fuzzy match
        for brand in brands:
            name = brand['name']

            # basic case, direct brand name match
            if fuzzy.partial_ratio(query, name.lower()) >= 80:
                return BrandResults(brand=brand)

        # now check if it matches an ability instead
        for brand in brands:
            buffed = brand['buffed']
            nerfed = brand['nerfed']

            # if it's not matched up there, it's definitely not matched here
            if not nerfed or not buffed:
                continue

            if fuzzy.partial_ratio(query, buffed.lower()) >= 60:
                result = BrandResults(ability_name=buffed)
                break

            if fuzzy.partial_ratio(query, nerfed.lower()) >= 60:
                result = BrandResults(ability_name=nerfed)
                break

        if result is None:
            raise commands.BadArgument('Could not find anything.')

        # check the brands that buff or nerf the ability we're looking for:
        for brand in brands:
            buffed = brand['buffed']
            nerfed = brand['nerfed']
            if not nerfed or not buffed:
                continue

            if buffed == result.ability_name:
                result.buffs.append(brand['name'])
            elif nerfed == result.ability_name:
                result.nerfs.append(brand['name'])

        return result

class GearQuery(commands.Converter):
    async def convert(self, ctx, argument):
        import shlex

        # parse our pseudo CLI
        args = shlex.split(argument.lower(), posix=False)
        iterator = iter(args)

        # check if flags is one of --brand, --ability, or --frequent
        info = {
            'query': None,
            '--brand': None,
            '--ability': None,
            '--frequent': None,
            '--type': None
        }

        current = 'query'
        temp = []
        for argument in args:
            if argument[0] == '-':
                if argument not in ('--brand', '--ability', '--frequent', '--type'):
                    msg = f'Invalid flag passed, received {argument} expected --brand, --ability, --frequent, or --type'
                    raise commands.BadArgument(msg)

                info[current] = ' '.join(temp)
                temp = []
                current = argument
                continue

            temp.append(argument)

        if temp:
            info[current] = ' '.join(temp)

        query = info['query']
        if len(query) < 4:
            raise commands.BadArgument('The query must be at least 5 characters long.')

        data = ctx.cog.splat2_data
        importance = []

        frequent_lookup = {
            x['buffed'].lower(): x['name']
            for x in data['brands']
            if x['buffed']
        }

        # search by name, main ability or brand
        # sort by importance
        scorer = fuzzy.partial_ratio
        brand = info['--brand']
        ability = info['--ability']
        frequent = info['--frequent']
        if frequent:
            m = fuzzy.extract_one(frequent, frequent_lookup, scorer=scorer, score_cutoff=70)
            if m is None:
                raise commands.BadArgument('Could not figure out the frequent ability requested.')
            _, _, frequent = m

        kind = info['--type']
        if kind in ('hat', 'hats'):
            kind = 'head'

        if kind in ('shirt', 'shirts'):
            kind = 'clothes'

        if kind == 'shoe':
            kind = 'shoes'

        if kind is None:
            iterator = itertools.chain(data['head'], data['shoes'], data['clothes'])
        else:
            iterator = data[kind]

        for gear in iterator:
            important = max(scorer(query, gear.name), scorer(query, gear.main), scorer(query, gear.brand))
            if important >= 70:
                # apply filters:
                if frequent and frequent != gear.brand:
                    continue

                if brand and scorer(brand, gear.brand) < 70:
                    continue

                if ability and scorer(ability, gear.main) < 70:
                    continue

                importance.append((gear, important))


        importance.sort(key=lambda t: t[1], reverse=True)
        if len(importance) == 0:
            raise commands.BadArgument('Could not find anything.')

        top, score = importance[0]
        if score == 100:
            return [top]

        return [g for g, _ in importance]

class Splatoon:
    """Splatoon related commands."""

    BASE_URL = yarl.URL('https://app.splatoon2.nintendo.net')

    def __init__(self, bot):
        self.bot = bot
        self.splat1_data = config.Config('splatoon.json', loop=bot.loop)
        self.splat2_data = config.Config('splatoon2.json', loop=bot.loop,
                                         object_hook=splatoon2_decoder, encoder=Splatoon2Encoder)
        self.map_data = []
        self.map_updater = bot.loop.create_task(self.update_maps())

        self._splatnet2 = bot.loop.create_task(self.splatnet2())
        self._authenticator = bot.loop.create_task(self.splatnet2_authenticator())
        self._is_authenticated = asyncio.Event(loop=bot.loop)

        # mode: List[Rotation]
        self.sp2_map_data = {}
        self.sp2_shop = []
        self.sp2_festival = None

    def __unload(self):
        self.map_updater.cancel()
        self._splatnet2.cancel()
        self._authenticator.cancel()

    async def __error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            return await ctx.send(error)

    @property
    def salmon_run(self):
        schedule = self.splat2_data.get('salmon_run', [])
        now = datetime.datetime.utcnow()
        return [s for s in schedule if now < s.end_time and not s.deleted]

    def get_salmon_run_by_id(self, salmon_run_id):
        schedule = self.splat2_data.get('salmon_run', [])

        # chances are we're gonna look for a newer entry
        # as a result let's reverse it since the newer IDs are at
        # the bottom of the list
        for element in reversed(schedule):
            if element.id == salmon_run_id:
                return element
        return None

    async def update_splatnet_cookie(self):
        username = self.splat1_data.get('username')
        password = self.splat1_data.get('password')
        await maps.get_new_splatnet_cookie(self.bot.session, username, password)

    async def update_maps(self):
        try:
            await self.update_splatnet_cookie()
            while not self.bot.is_closed():
                await self.update_schedule()
                await asyncio.sleep(120) # task runs every 2 minutes
        except asyncio.CancelledError:
            pass

    async def update_schedule(self):
        try:
            schedule = await maps.get_splatnet_schedule(self.bot.session)
        except:
            # if we get an exception, keep the old data
            # make sure to remove the old data that already ended
            self.map_data = [data for data in self.map_data if not data.is_over]
        else:
            self.map_data = []
            for entry in schedule:
                if entry.is_over:
                    continue
                self.map_data.append(entry)

    async def splatnet2_authenticator(self):
        try:
            session_token = self.splat2_data.get('session_token')
            while not self.bot.is_closed():
                iksm = self.splat2_data.get('iksm_session')
                if iksm is not None:
                    self.bot.session.cookie_jar.update_cookies({ 'iksm_session': iksm }, response_url=self.BASE_URL)
                    self._is_authenticated.set()

                expires = datetime.datetime.utcfromtimestamp(self.splat2_data.get('iksm_expires', 0.0))
                now = datetime.datetime.utcnow()

                if now < expires:
                    # our session is still valid, so let's wait a while before authenticating
                    delta = (expires - now).total_seconds()
                    await asyncio.sleep(delta)

                # at this point our session is invalid so let's re-authenticate
                self._is_authenticated.clear()
                session = self.bot.session

                url = 'https://accounts.nintendo.com/connect/1.0.0/api/token'
                data = {
                    'client_id': '71b963c1b7b6d119',
                    'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer-session-token',
                    'session_token': session_token
                }
                headers = {
                    'Content-Type': 'application/json; charset=utf-8',
                    'X-Platform': 'Android',
                    'X-ProductVersion': '1.1.0',
                    'User-Agent': 'com.nintendo.znca/1.1.0 (Android/4.4.2)'
                }

                # first, authenticate into the accounts.nintendo.com for our Bearer token and ID token.
                async with session.post(url, headers=headers, data=json.dumps(data)) as resp:
                    if resp.status != 200:
                        extra = f'SplatNet 2 authentication error: {resp.status} for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    # we don't care when the token expires but we need our ID token
                    js = await resp.json()
                    id_token = js['id_token']

                # authenticate to the nintendo.net API
                data = {
                    "parameter": {
                        "language": "en-US",
                        'naCountry': 'US',
                        "naBirthday": "1989-04-06",
                        "naIdToken": id_token
                    }
                }

                url = 'https://api-lp1.znc.srv.nintendo.net/v1/Account/Login'
                headers['Authorization'] = 'Bearer'

                async with session.post(url, headers=headers, data=json.dumps(data)) as resp:
                    if resp.status != 200:
                        extra = f'SplatNet 2 authentication error: {resp.status} for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    js = await resp.json()
                    if js['status'] != 0:
                        extra = f'Firebase issue for {url}:\n```js\n{json.dumps(js, indent=4)}\n```'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    token = js['result'].get('webApiServerCredential', {}).get('accessToken')
                    if token is None:
                        extra = f'SplatNet 2 authentication error: No accessToken for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                # get the web service token
                data = {
                    "parameter": {
                        "id": 5741031244955648 # SplatNet 2 ID
                    }
                }
                url = 'https://api-lp1.znc.srv.nintendo.net/v1/Game/GetWebServiceToken'
                headers['Authorization'] = f'Bearer {token}'
                async with session.post(url, headers=headers, data=json.dumps(data)) as resp:
                    if resp.status != 200:
                        extra = f'SplatNet 2 authentication error: {resp.status} for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    js = await resp.json()
                    if js['status'] != 0:
                        extra = f'Firebase issue for {url}:\n```js\n{json.dumps(js, indent=4)}\n```'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    access_token = js['result'].get('accessToken')
                    if access_token is None:
                        extra = f'SplatNet 2 authentication error: No accessToken for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                # Now we can **finally** access SplatNet 2
                headers.pop('Authorization')
                headers['x-gamewebtoken'] = access_token
                headers['x-isappanalyticsoptedin'] = 'false'
                headers['X-Requested-With'] = 'com.nintendo.znca'
                async with session.get(self.BASE_URL, params={'lang': 'en-US'}, headers=headers) as resp:
                    if resp.status != 200:
                        extra = f'SplatNet 2 authentication error: {resp.status} for {url}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    # finally our shit
                    # SimpleCookie API is cancer
                    m = re.search(r'iksm_session=(?P<session>.+?);.+?expires=(?P<expires>.+?);', str(resp.cookies))
                    if m is None:
                        extra = f'Regex fucked up. See {resp.cookie}'
                        await self.bot.get_cog('Stats').log_error(extra=extra)
                        await asyncio.sleep(300.0) # try again in 5 minutes
                        continue

                    iksm = m.group('session')
                    try:
                        expires = parsedate_to_datetime(m.group('expires'))
                    except:
                        expires = now.timestamp() + 300.0
                    else:
                        expires = expires.timestamp()

                    await self.splat2_data.put('iksm_session', iksm)
                    await self.splat2_data.put('iksm_expires', expires)

                log.info('Authenticated to SplatNet 2. Session: %s Expires: %s', iksm, expires)
                self._is_authenticated.set()
        except asyncio.CancelledError:
            pass
        except (OSError, discord.ConnectionClosed):
            self._authenticator.cancel()
            self._authenticator = self.bot.loop.create_task(self.splatnet2_authenticator())

    async def parse_splatnet2_schedule(self):
        try:
            self.sp2_map_data = {}
            async with self.bot.session.get(self.BASE_URL / 'api/schedules') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet schedule responded with {resp.status}.')
                    return 300.0 # try again in 5 minutes

                data = await resp.json()
                gachi = data.get('gachi', [])
                self.sp2_map_data['Ranked Battle'] = [
                    Rotation(d) for d in gachi
                ]
                regular = data.get('regular', [])
                self.sp2_map_data['Regular Battle'] = [
                    Rotation(d) for d in regular
                ]

                league = data.get('league', [])
                self.sp2_map_data['League Battle'] = [
                    Rotation(d) for d in league
                ]

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
                    return 300.0 # try again in 5 minutes

                now = datetime.datetime.utcnow()
                return 300.0 if now > new else (new - now).total_seconds()
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet schedule Error')
            return 300.0

    async def parse_splatnet2_onlineshop(self):
        try:
            self.sp2_shop = []
            async with self.bot.session.get(self.BASE_URL / 'api/onlineshop/merchandises') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Shop responded with {resp.status}.')
                    return 300.0 # try again in 5 minutes

                data = await resp.json()
                merch = data.get('merchandises')
                if not merch:
                    return 300.0

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
                                gear.image = value.gear.image
                                break

                await self.splat2_data.save()
                self.sp2_shop.sort(key=lambda m: m.end_time)
                now = datetime.datetime.utcnow()
                try:
                    end = self.sp2_shop[0]
                    return 300.0 if now > end else (end - now).total_seconds()
                except:
                    return 300.0
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Shop Error')
            return 300.0

    def scrape_data_from_player(self, player, bulk):
        for kind in ('shoes', 'head', 'clothes'):
            try:
                gear = Gear(player[kind])
            except KeyError:
                continue
            else:
                bulk[kind][gear.name] = gear.image

    async def scrape_splatnet_stats_and_images(self):
        try:
            bulk_lookup = defaultdict(dict)
            async with self.bot.session.get(self.BASE_URL / 'api/results') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Stats responded with {resp.status}.')
                    return 300.0 # try again in 5 minutes

                data = await resp.json()
                results = data['results']
                base_path = pathlib.Path('splatoon2_stats')

                newest = base_path / f'{results[0]["battle_number"]}.json'

                # we already scraped, so try again in an hour
                if newest.exists():
                    log.info('No Splatoon 2 result data to scrape, retrying in an hour.')
                    return 3600.0

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
                            await self.bot.get_cog('Stats').log_error(extra=extra)
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

                    await asyncio.sleep(1) # one request a second so we don't abuse

                log.info('Scraped Splatoon 2 results from %s games.', added)

                # done with bulk lookups so actually change and save now:
                for kind, data in bulk_lookup.items():
                    old = self.splat2_data.get(kind, [])
                    for gear in old:
                        try:
                            image = data[gear.name]
                        except KeyError:
                            continue
                        else:
                            gear.image = image

                await self.splat2_data.save()
                return 3600.0 # redo in an hour
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Stat Error')
            return 300.0

    async def parse_splatnet2_splatfest(self):
        try:
            self.sp2_festival = None
            async with self.bot.session.get(self.BASE_URL / 'api/festivals/active') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Splatfest Error')
                    return 300.0

                js = await resp.json()
                festivals = js['festivals']
                if len(festivals) == 0:
                    return 3600.0

                current = festivals[0]
                self.sp2_festival = Splatfest(current)
                return 3600.0
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Splatfest Error')
            return 300.0

    async def splatnet2(self):
        try:
            while not self.bot.is_closed():
                seconds = []
                await self._is_authenticated.wait()
                seconds.append(await self.parse_splatnet2_schedule())
                seconds.append(await self.parse_splatnet2_onlineshop())
                seconds.append(await self.scrape_splatnet_stats_and_images())
                seconds.append(await self.parse_splatnet2_splatfest())
                await asyncio.sleep(min(seconds))
        except asyncio.CancelledError:
            pass
        except (OSError, discord.ConnectionClosed):
            self._splatnet2.cancel()
            self._splatnet2 = self.bot.loop.create_task(self.splatnet2())

    def get_weapons_named(self, name, *, splatoon2=True):
        data = self.splat2_data if splatoon2 else self.splat1_data
        data = data.get('weapons', [])
        name = name.lower()

        choices = {w['name'].lower(): w for w in data}
        results = fuzzy.extract_or_exact(name, choices, scorer=fuzzy.token_sort_ratio, score_cutoff=60)
        return [v for k, _, v in results]

    @commands.group(aliases=['sp1', 'splatoon1'])
    async def splat1(self, ctx):
        """Commands for Splatoon 1, rather than Splatoon 2."""
        if ctx.invoked_subcommand is None:
            return await ctx.send("That doesn't seem like a valid Splatoon command.")

    @splat1.command(name='maps', aliases=['rotation'])
    async def splat1_maps(self, ctx):
        """Shows the current maps in the Splatoon schedule."""
        try:
            await ctx.send(self.map_data[0])
        except IndexError:
            await ctx.send('No map data found. Try again later.')

    @splat1.command(name='schedule')
    async def splat1_schedule(self, ctx):
        """Shows the current Splatoon schedule."""
        if self.map_data:
            await ctx.send('\n'.join(str(x) for x in self.map_data))
        else:
            await ctx.send('No map data found. Try again later.')

    def weapon_to_string(self, weapon):
        return f'**{weapon["name"]}**\nSub: {weapon["sub"]}, Special: {weapon["special"]}'

    @splat1.command(name='weapon')
    async def splat1_weapon(self, ctx, *, query: str):
        """Displays Splatoon weapon info from a query.

        The query must be at least 3 characters long, otherwise it'll tell you it failed.
        """
        query = query.strip().lower()
        weapons = self.splat1_data.get('weapons', [])
        if len(query) < 3:
            return await ctx.send('The query must be at least 3 characters long.')

        def predicate(weapon):
            lowered = [weapon.lower() for weapon in weapon.values()]
            return any(query in wep for wep in lowered)

        results = list(filter(predicate, weapons))
        if not results:
            return await ctx.send('No results found.')

        output = [f'Found {Plural(weapon=len(results))}:']
        output.extend(self.weapon_to_string(weapon) for weapon in results)

        if len(results) > 10:
            await ctx.author.send('\n'.join(output))
        else:
            await ctx.send('\n'.join(output))

    async def generate_scrims(self, ctx, maps, games, mode):
        modes = ['Rainmaker', 'Splat Zones', 'Tower Control']

        if mode is not None:
            mode = mode.lower()

            # shortcuts that can't be detected by fuzzy matching:
            shortcuts = {
                'rm': 'Rainmaker',
                'sz': 'Splat Zones',
                'tc': 'Tower Control',
                'tw': 'Turf War'
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
            game_count = max(min(games, len(maps) * 3), 3)
            random.shuffle(modes)
            scrims = get_random_scrims(modes, maps, game_count)
            result = [f'Game {game}: {scrim.mode} on {scrim.stage}' for game, scrim in enumerate(scrims, 1)]

        await ctx.send('\n'.join(result))

    async def _do_brand(self, ctx, brand):
        e = discord.Embed(colour=0x19D719)
        if brand.is_brand():
            e.add_field(name='Name', value=brand.info['name'])
            e.add_field(name='Common', value=brand.info['buffed'])
            e.add_field(name='Uncommon', value=brand.info['nerfed'])
            return await ctx.send(embed=e)

        e.description = f'The following brands deal with {brand.ability_name}.'
        e.add_field(name='Common', value='\n'.join(brand.buffs))
        e.add_field(name='Uncommon', value='\n'.join(brand.nerfs))
        await ctx.send(embed=e)

    @splat1.command(name='scrim')
    async def splat1_scrim(self, ctx, games=5, *, mode: str = None):
        """Generates Splatoon scrim map and mode combinations.

        The mode combinations do not have Turf War.

        The mode is rotated unless you pick a mode to play, in which all map
        combinations will use that mode instead.
        """

        maps = self.splat1_data.get('maps', [])
        await self.generate_scrims(ctx, maps, games, mode)

    @splat1.command(name='brand', invoke_without_command=True)
    async def splat1_brand(self, ctx, *, query: BrandOrAbility(splatoon2=False)):
        """Shows Splatoon brand info based on either the name or the ability given.

        If the query is an ability then it attempts to find out what brands
        influence that ability, otherwise it just looks for the brand being given.

        The query must be at least 4 characters long.
        """
        await self._do_brand(ctx, query)

    @commands.command(hidden=True)
    async def marie(self, ctx):
        """A nice little easter egg."""
        await ctx.send('http://i.stack.imgur.com/0OT9X.png')

    @commands.command()
    async def splatwiki(self, ctx, *, title: str):
        """Returns a Inkipedia page."""
        url = f'http://splatoonwiki.org/wiki/Special:Search/{urlquote(title)}'

        async with ctx.session.get(url) as resp:
            if 'Special:Search' in resp.url.path:
                await ctx.send(f'Could not find your page. Try a search:\n{resp.url.human_repr()}')
            elif resp.status == 200:
                await ctx.send(resp.url)
            elif resp.status == 502:
                await ctx.send('It seems that Inkipedia is taking too long to respond. Try again later.')
            else:
                await ctx.send(f'An error has occurred of status code {resp.status} happened.')

    async def generic_splatoon2_schedule(self, ctx):
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

            e.add_field(name=f'{key}: {rotation.mode}',
                        value=f'{rotation.stage_a} and {rotation.stage_b}',
                        inline=False)

        if end_time is not None:
            e.title = f'For {time.human_timedelta(end_time)}...'

        await ctx.send(embed=e)

    async def paginated_splatoon2_schedule(self, ctx, mode):
        try:
            data = self.sp2_map_data[mode]
        except KeyError:
            return await ctx.send('Sorry, no map data found...')

        entries = [
            (f'Now: {r.mode}' if r.current else f'In {time.human_timedelta(r.start_time)}: {r.mode}',
            f'{r.stage_a} and {r.stage_b}')
            for r in data
        ]

        try:
            p = FieldPages(ctx, entries=entries, per_page=4)
            p.embed.colour = 0xF02D7D
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

    @commands.command(aliases=['maps'])
    async def schedule(self, ctx, *, type: mode_key = None):
        """Shows the current Splatoon 2 schedule."""
        if type is None:
            await self.generic_splatoon2_schedule(ctx)
        else:
            await self.paginated_splatoon2_schedule(ctx, type)

    @commands.command()
    async def nextmaps(self, ctx):
        """Shows the next Splatoon 2 maps."""
        e = discord.Embed(colour=0x19D719)

        start_time = None
        for key, value in self.sp2_map_data.items():
            try:
                rotation = value[1]
            except IndexError:
                e.add_field(name=key, value='Nothing found...', inline=False)
                continue
            else:
                start_time = rotation.start_time

            e.add_field(name=f'{key}: {rotation.mode}',
                        value=f'{rotation.stage_a} and {rotation.stage_b}',
                        inline=False)

        if start_time is not None:
            e.title = f'In {time.human_timedelta(start_time)}'

        await ctx.send(embed=e)

    @commands.group(invoke_without_command=True)
    async def salmonrun(self, ctx):
        """Shows the Salmon Run schedule, if any."""
        salmon = self.salmon_run
        if not salmon:
            return await ctx.send('No Salmon Run schedule reported.')

        try:
            pages = SalmonRunPages(ctx, salmon)
            await pages.paginate()
        except Exception as e:
            await ctx.send(e)

    @salmonrun.command(name='new')
    @is_salmon_moderator()
    async def salmonrun_new(self, ctx, start_time: iso8601, end_time: iso8601):
        """Creates a new Salmon Run entry.

        Times must be provided in ISO-8601, e.g. YYYY-MM-DDTHH:MM
        or 2017-09-20T18:00 and must be in UTC.

        If you can't figure out how to provide times in UTC,
        google your local time to UTC.
        """

        current = self.splat2_data.get('salmon_run', [])

        if start_time > end_time:
            return await ctx.send('The end time must be after the start time...')

        # verify this is a new entry
        for schedule in current:
            if schedule.deleted:
                continue

            if schedule.start_time == start_time or schedule.end_time == end_time:
                msg =  'Schedule conflicts with pre-existing entry.\n' \
                      f'ID: {schedule.id}, Start: {schedule.start_time}, End: {schedule.end_time}'
                return await ctx.send(msg)

        # this should be fine now
        entry = SalmonRun(start_time, end_time, len(current) + 1)
        current.append(entry)
        await self.splat2_data.put('salmon_run', current)
        await ctx.send(f'Successfully created Salmon Run ID {entry.id} that starts {start_time} and ends {end_time}')

    @salmonrun.command(name='delete')
    @commands.is_owner()
    async def salmonrun_delete(self, ctx, id: int):
        """Deletes a Salmon Run entry."""

        entry = self.get_salmon_run_by_id(id)
        if entry is None:
            return await ctx.send('Could not find an entry with this ID.')

        entry.deleted = True
        await self.splat2_data.save()
        await ctx.send(f'Successfully deleted entry {entry.id}.')

    @salmonrun.command(name='list')
    @is_salmon_moderator()
    async def salmonrun_list(self, ctx):
        """Shows the salmon run moderation panel."""

        e = discord.Embed(title='Salmon Run Moderation', colour=0XFF9E4D)

        for entry in self.salmon_run:
            value = f'Time: {entry.start_time} ~ {entry.end_time}'
            if entry.weapons:
                value = f'{value}\nWeapons: {", ".join(entry.weapons)}'
            if entry.stage:
                value = f'{value}\nStage: {entry.stage}'
            e.add_field(name=f'Salmon Run ID: {entry.id}', value=value, inline=False)

        await ctx.send(embed=e)

    @salmonrun.command(name='map')
    @is_salmon_moderator()
    async def salmonrun_map(self, ctx, id: int, *, map):
        """Edits the map of a Salmon Run entry."""

        entry = self.get_salmon_run_by_id(id)
        if entry is None:
            return await ctx.send('Could not find an entry with this ID.')

        maps = list(SALMON_RUN_MAPS)
        found = fuzzy.find(map.strip('"'), maps)

        if found is None:
            return await ctx.send(f'Bad map. Must be one of {formats.human_join(list(SALMON_RUN_MAPS))}')

        entry.stage = found
        await self.splat2_data.save()
        await ctx.send(f'Successfully set the map for {entry.id} to {found}.')

    @salmonrun.command(name='weapon', aliases=['weapons'])
    @is_salmon_moderator()
    async def salmonrun_weapon(self, ctx, id: int, first, second, third, fourth):
        """Edits the weapons of a Salmon Run entry."""

        entry = self.get_salmon_run_by_id(id)
        if entry is None:
            return await ctx.send('Could not find an entry with this ID.')

        valid_weapons = [
            w['name']
            for w in self.splat2_data.get('weapons', [])
        ]

        # add Grizzco specific weapons
        valid_weapons.append('Mystery Weapon')

        weapons = []
        for to_find in (first, second, third, fourth):
            found = fuzzy.find(to_find, valid_weapons)
            if found is None:
                return await ctx.send(f'Could not find weapon {to_find}.')
            weapons.append(found)

        entry.weapons = weapons
        await self.splat2_data.save()
        await ctx.send(f'Successfully set weapons to {formats.human_join(weapons, final="and")}')

    @salmonrun.command(name='reset')
    @commands.is_owner()
    async def salmonrun_reset(self, ctx, id: int):
        """Removes the maps and weapons from a Salmon Run entry."""

        entry = self.get_salmon_run_by_id(id)
        if entry is None:
            return await ctx.send('Could not find an entry with this ID.')

        entry.reset()
        await self.splat2_data.save()
        await ctx.send(f'Successfully reset {entry.id}.')

    @commands.command(aliases=['splatnetshop'])
    async def splatshop(self, ctx):
        """Shows the currently running SplatNet 2 merchandise."""
        if not self.sp2_shop:
            return await ctx.send('Nothing currently being sold...')

        try:
            p = GearPages(ctx, self.sp2_shop)
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

    @commands.command()
    async def splatfest(self, ctx):
        """Shows information about the currently running NA Splatfest, if any."""
        if self.sp2_festival is None:
            return await ctx.send('No Splatfest has been announced.')

        await ctx.send(embed=self.sp2_festival.embed())

    @commands.command()
    async def weapon(self, ctx, *, query: str):
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
        e.title = f'Found {Plural(weapon=len(results))}'

        subs = '\n'.join(w['sub'] for w in results)
        names = '\n'.join(w['name'] for w in results)
        special = '\n'.join(w['special'] for w in results)

        e.add_field(name='Name', value=names)
        e.add_field(name='Sub', value=subs)
        e.add_field(name='Special', value=special)
        await ctx.send(embed=e)

    @commands.command()
    async def gear(self, ctx, *, query: GearQuery):
        """Searches for Splatoon 2 gear that matches your query.

        The query can be a main ability, a brand, or a name.

        For advanced queries to reduce results you can pass some filters:

        `--brand` with the brand name.
        `--ability` with the main ability.
        `--frequent` with the buffed main ability probability
        `--type` with the type of clothing (head, hat, shoes, or clothes)

        For example, a query like `ink resist --brand splash mob` will give all
        gear with Ink Resistance Up and Splash Mob as the brand.

        **Note**: you must pass a query before passing a filter.
        """

        try:
            p = GearPages(ctx, query)
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

    @commands.command()
    async def scrim(self, ctx, games=5, *, mode: str = None):
        """Generates Splatoon 2 scrim map and mode combinations.

        The mode combinations do not have Turf War.

        The mode is rotated unless you pick a mode to play, in which all map
        combinations will use that mode instead.
        """
        maps = self.splat2_data.get('maps', [])
        await self.generate_scrims(ctx, maps, games, mode)

    @commands.group(invoke_without_command=True)
    async def brand(self, ctx, *, query: BrandOrAbility):
        """Shows Splatoon 2 brand info

        This is based on either the name or the ability given.

        If the query is an ability then it attempts to find out what brands
        influence that ability, otherwise it just looks for the brand being given.

        The query must be at least 4 characters long.
        """
        await self._do_brand(ctx, query)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def new_weapon(self, ctx, name, sub, special):
        """Add a new Splatoon 2 weapon."""
        weapons = self.splat2_data.get('weapons', [])
        entry = {
            'name': name,
            'sub': sub,
            'special': special
        }
        weapons.append(entry)
        await self.splat2_data.put('weapons', weapons)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command(hidden=True)
    @commands.is_owner()
    async def new_map(self, ctx, *, name):
        """Add a new Splatoon 2 map."""
        entry = self.splat2_data.get('maps', [])
        entry.append(name)
        await self.splat2_data.put('maps', entry)
        await ctx.send('\N{OK HAND SIGN}')

def setup(bot):
    bot.add_cog(Splatoon(bot))
