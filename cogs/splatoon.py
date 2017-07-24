from discord.ext import commands
from .utils import config, checks, maps, fuzzy, time
from .utils.formats import Plural
from .utils.paginator import FieldPages, Pages

from urllib.parse import quote as urlquote
from email.utils import parsedate_to_datetime
from collections import namedtuple

import datetime
import random
import asyncio
import discord
import logging
import aiohttp
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
        while True:
            entry = GameEntry(stage=random.choice(maps), mode=modes[current_mode_index])
            if is_valid_entry(result, entry):
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
    'Cold Blooded': '<:coldblooded:338814717340418069>',
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
    'Ink Resistance': '<:inkresistance:338814717592076289>',
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
    __slots__ = ('kind', 'brand', 'name', 'rarity', 'frequent_skill', 'image')

    def __init__(self, data):
        self.kind = data['kind']
        brand = data['brand']
        self.brand = brand['name']
        self.name = data['name']
        self.rarity = data['rarity'] + 1
        self.image = data['image']

        try:
            self.frequent_skill = brand['frequent_skill']['name']
        except KeyError:
            self.frequent_skill = None

class SalmonRun:
    def __init__(self, data):
        schedule = data['schedule']
        self.start_time = datetime.datetime.utcfromtimestamp(schedule['start_time'])
        self.end_time = datetime.datetime.utcfromtimestamp(schedule['end_time'])
        self.gear = Gear(data['reward_gear']['gear'])

class Merchandise:
    def __init__(self, data):
        self.gear = Gear(data['gear'])
        self.skill = data['skill']['name']
        self.price = data['price']
        self.end_time = datetime.datetime.utcfromtimestamp(data['end_time'])

class Merchandises(Pages):
    def __init__(self, ctx, entries):
        super().__init__(ctx, entries=entries, per_page=1)
        # remove help reaction
        self.reaction_emojis.pop()

    async def show_page(self, page, *, first=False):
        self.current_page = page
        merch = self.entries[page - 1]

        self.embed = e = discord.Embed(colour=0x19D719, title=merch.gear.name)
        e.set_thumbnail(url=f'https://app.splatoon2.nintendo.net{merch.gear.image}')
        e.description = f'{time.human_timedelta(merch.end_time)} left to buy'

        e.add_field(name='Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {merch.price}')

        try:
            main_slot = RESOURCE_TO_EMOJI[merch.skill]
        except KeyError:
            main_slot = RESOURCE_TO_EMOJI['Unknown']

        remaining = RESOURCE_TO_EMOJI['Unknown'] * merch.gear.rarity

        e.add_field(name='Slots', value=f'{main_slot} | {remaining}')
        e.add_field(name='Brand', value=merch.gear.brand)

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

class Splatoon:
    """Splatoon related commands."""

    BASE_URL = yarl.URL('https://app.splatoon2.nintendo.net')

    def __init__(self, bot):
        self.bot = bot
        self.splat1_data = config.Config('splatoon.json', loop=bot.loop)
        self.splat2_data = config.Config('splatoon2.json', loop=bot.loop)
        self.map_data = []
        self.map_updater = bot.loop.create_task(self.update_maps())

        # temporary measure until fully reverse engineered
        # I did not find out that the session cookie actually expired until later
        # for now, these last 24 hours which is good enough
        session_cookie = self.splat2_data.get('session')
        bot.session.cookie_jar.update_cookies({ 'iksm_session': session_cookie }, response_url=self.BASE_URL)

        self._splatnet2 = bot.loop.create_task(self.splatnet2())
        self._authenticator = bot.loop.create_task(self.splatnet2_authenticator())
        self._is_authenticated = asyncio.Event(loop=bot.loop)

        # mode: List[Rotation]
        self.sp2_map_data = {}
        self.sp2_salmon_run = None
        self.sp2_shop = []

    def __unload(self):
        self.map_updater.cancel()
        self._splatnet2.cancel()
        self._authenticator.cancel()

    async def __error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            return await ctx.send(error)

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
                    'X-ProductVersion': '1.0.4',
                    'User-Agent': 'com.nintendo.znca/1.0.4 (Android/4.4.2)'
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
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet responded with {resp.status}.')
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
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Error')
            return 300.0

    async def parse_splatnet2_salmon_run(self):
        try:
            self.sp2_salmon_run = None
            async with self.bot.session.get(self.BASE_URL / 'api/timeline') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet responded with {resp.status}.')
                    return 300.0 # try again in 5 minutes

                data = await resp.json()
                salmon = data.get('coop')
                if salmon:
                    try:
                        if salmon['importance'] < 0:
                            return 3600.0

                        self.sp2_salmon_run = SalmonRun(salmon)
                        now = datetime.datetime.utcnow()
                        end = self.sp2_salmon_run.end_time
                        return 3600.0 if now > end else (end - now).total_seconds()
                    except KeyError:
                        return 3600.0
                return 3600.0
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Error')
            return 3600.0

    async def parse_splatnet2_onlineshop(self):
        try:
            self.sp2_shop = []
            async with self.bot.session.get(self.BASE_URL / 'api/onlineshop/merchandises') as resp:
                if resp.status != 200:
                    await self.bot.get_cog('Stats').log_error(extra=f'Splatnet responded with {resp.status}.')
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

                self.sp2_shop.sort(key=lambda m: m.end_time)
                now = datetime.datetime.utcnow()
                try:
                    end = self.sp2_shop[0]
                    return 300.0 if now > end else (end - now).total_seconds()
                except:
                    return 300.0
        except Exception as e:
            await self.bot.get_cog('Stats').log_error(extra=f'Splatnet Error')
            return 300.0

    async def splatnet2(self):
        try:
            while not self.bot.is_closed():
                seconds = []
                await self._is_authenticated.wait()
                seconds.append(await self.parse_splatnet2_schedule())
                seconds.append(await self.parse_splatnet2_salmon_run())
                seconds.append(await self.parse_splatnet2_onlineshop())
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
        game_count = max(min(games, len(maps)), 3)

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
            for index, stage in enumerate(random.sample(maps, game_count), 1):
                result.append(f'Game {index}: {stage}')
        else:
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

    @commands.command()
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

    @commands.command()
    async def salmonrun(self, ctx):
        """Shows the Salmon Run schedule, if any."""
        salmon = self.sp2_salmon_run
        if salmon is None:
            return await ctx.send('No Salmon Run schedule reported.')

        e = discord.Embed(title='Salmon Run', colour=0xFF7500)
        now = datetime.datetime.utcnow()
        if now < salmon.start_time:
            e.add_field(name='Starts In', value=time.human_timedelta(salmon.start_time), inline=False)
        elif now < salmon.end_time:
            e.add_field(name='Ends In', value=time.human_timedelta(salmon.end_time), inline=False)

        e.add_field(name='Reward Name', value=salmon.gear.name)
        e.add_field(name='Reward Brand', value=salmon.gear.brand)
        e.set_thumbnail(url=str(self.BASE_URL) + salmon.gear.image)
        await ctx.send(embed=e)

    @commands.command(aliases=['splatnetshop'])
    async def splatshop(self, ctx):
        """Shows the currently running SplatNet 2 merchandise."""
        if not self.sp2_shop:
            return await ctx.send('Nothing currently being sold...')

        try:
            p = Merchandises(ctx, self.sp2_shop)
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

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
