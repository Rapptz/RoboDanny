from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, NamedTuple, Optional, TypedDict, Union
from typing_extensions import Annotated, NotRequired, Self

from discord.ext import commands, menus, tasks
from discord import app_commands
from .utils import config, fuzzy, time
from .utils.formats import plural
from .utils.paginator import RoboPages, FieldPageSource

from urllib.parse import quote as urlquote
from collections import defaultdict
import time as ctime
from lxml.html import fromstring as html_fromstring

import traceback
import datetime
import random
import asyncio
import discord
import logging
import pathlib
import base64
import mmh3
import yarl
import math
import json
import io

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import Context
    import aiohttp

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

    # SplatNet3 payloads (incomplete)

    class ImagePayload(TypedDict):
        url: str

    class StageStatsPayload(TypedDict):
        winRateAr: Optional[float]
        winRateLf: Optional[float]
        winRateGl: Optional[float]
        winRateCl: Optional[float]

    class StagePayload(TypedDict):
        id: str
        name: str
        coopStageId: NotRequired[int]
        vsStageId: NotRequired[int]
        image: NotRequired[ImagePayload]
        thumbnailImage: NotRequired[ImagePayload]
        originalImage: NotRequired[ImagePayload]
        stats: NotRequired[Optional[StageStatsPayload]]

    class WeaponPayload(TypedDict):
        name: str
        image: ImagePayload

    class BasicEntityPayload(TypedDict):
        """Used by a few things and they all more or less share this semantic in common"""

        name: str
        id: str
        image: NotRequired[ImagePayload]

    class VsRulePayload(TypedDict):
        name: Literal['Turf War', 'Tower Control', 'Rainmaker', 'Splat Zones', 'Clam Blitz']
        rule: Literal['TURF_WAR', 'LOFT', 'GOAL', 'AREA', 'CLAM']
        id: str

    class VsModePayload(TypedDict):
        mode: Literal['BANKARA', 'REGULAR']
        id: str

    class ColourPayload(TypedDict):
        r: int
        g: int
        b: int
        a: NotRequired[int]

    class BadgePayload(TypedDict):
        id: str
        image: ImagePayload

    class NameplateBackgroundPayload(TypedDict):
        id: str
        image: ImagePayload
        textColor: ColourPayload

    class NameplatePayload(TypedDict):
        badges: list[BadgePayload]
        background: NameplateBackgroundPayload

    class PlayerResultPayload(TypedDict):
        kill: int
        death: int
        assist: int
        special: int
        noroshiTry: Optional[int]  # Looks like the tricolour thing

    class PlayerPayload(TypedDict):
        __isPlayer: Literal['VsPlayer']
        byname: str
        isMyself: bool
        weapon: WeaponPayload
        species: Literal['INKLING', 'OCTOLING']
        name: str
        nameId: str
        nameplate: NameplatePayload
        id: str
        headGear: GearPayload
        clothingGear: GearPayload
        shoesGear: GearPayload
        paint: int
        result: NotRequired[PlayerResultPayload]

    class BaseMatchSettingPayload(TypedDict):
        vsStages: list[StagePayload]
        vsRule: VsRulePayload

    class RegularMatchSettingPayload(BaseMatchSettingPayload):
        __isVsSetting: Literal['RegularMatchSetting']
        __typename: Literal['RegularMatchSetting']

    class RankedMatchSettingPayload(BaseMatchSettingPayload):
        __isVsSetting: Literal['BankaraMatchSetting']
        __typename: Literal['BankaraMatchSetting']
        mode: Literal['OPEN', 'CHALLENGE']

    class XMatchSettingPayload(BaseMatchSettingPayload):
        __isVsSetting: Literal['XMatchSetting']
        __typename: Literal['XMatchSetting']

    class LeagueMatchSettingPayload(BaseMatchSettingPayload):
        __isVsSetting: Literal['LeagueMatchSetting']
        __typename: Literal['LeagueMatchSetting']

    class FestMatchSettingPayload(BaseMatchSettingPayload):
        __isVsSetting: Literal['FestMatchSetting']
        __typename: Literal['FestMatchSetting']

    MatchSettingPayload = Union[
        RegularMatchSettingPayload,
        RankedMatchSettingPayload,
        XMatchSettingPayload,
        LeagueMatchSettingPayload,
    ]

    class BaseScheduleRotationPayload(TypedDict):
        startTime: str
        endTime: str
        festMatchSetting: Optional[FestMatchSettingPayload]

    class RegularScheduleRotationPayload(BaseScheduleRotationPayload):
        regularMatchSetting: RegularMatchSettingPayload

    class RankedScheduleRotationPayload(BaseScheduleRotationPayload):
        bankaraMatchSettings: list[RankedMatchSettingPayload]

    class XScheduleRotationPayload(BaseScheduleRotationPayload):
        xMatchSetting: XMatchSettingPayload

    class LeagueScheduleRotationPayload(BaseScheduleRotationPayload):
        leagueMatchSetting: LeagueMatchSettingPayload

    ScheduleRotationPayload = Union[
        RegularScheduleRotationPayload,
        RankedScheduleRotationPayload,
        XScheduleRotationPayload,
        LeagueScheduleRotationPayload,
    ]

    class SalmonRunSettingPayload(TypedDict):
        __typename: Literal['CoopNormalSetting']
        coopStage: StagePayload
        weapons: list[WeaponPayload]

    class SalmonRunRotationPayload(BaseScheduleRotationPayload):
        setting: SalmonRunSettingPayload

    class GearPowerPayload(TypedDict):
        name: str
        image: ImagePayload
        desc: NotRequired[str]

    class BrandPayload(TypedDict):
        id: str
        name: str
        image: ImagePayload
        usualGearPower: NotRequired[GearPowerPayload]

    class GearPayload(TypedDict):
        __typename: Literal['HeadGear', 'ClothesGear', 'ShoesGear']
        __isGear: NotRequired[Literal['HeadGear', 'ClothesGear', 'ShoesGear']]
        name: str
        primaryGearPower: GearPowerPayload
        additionalGearPowers: list[GearPowerPayload]
        image: ImagePayload
        originalImage: NotRequired[ImagePayload]
        brand: BrandPayload

    class MerchandisePayload(TypedDict):
        id: str
        saleEndTime: str
        price: int
        gear: GearPayload

    class PickupBrandPayload(TypedDict):
        image: ImagePayload
        brand: BrandPayload
        saleEndTime: str
        brandGears: list[MerchandisePayload]
        nextBrand: BrandPayload

    class SplatNetShopPayload(TypedDict):
        pickupBrand: PickupBrandPayload
        limitedGears: list[MerchandisePayload]

    class TeamPayload(TypedDict):
        color: ColourPayload
        judgement: Literal['WIN', 'LOSE', 'DRAW']
        players: list[PlayerPayload]
        order: int

    class AwardPayload(TypedDict):
        name: str
        rank: Literal['GOLD', 'SILVER']

    # Very partially typed
    class VsHistoryDetailPayload(TypedDict):
        __typename: Literal['VsHistoryDetail']
        id: str
        vsRule: VsRulePayload
        vsMode: VsModePayload
        # player: PlayerPayload # This is technically partial so idc about it
        judgement: Literal['WIN', 'LOSE', 'DRAW']
        myTeam: TeamPayload
        vsStage: StagePayload
        otherTeams: list[TeamPayload]
        awards: list[AwardPayload]
        duration: int
        playedTime: str


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
    """Exception for when the iksm_session or session_token is expired"""


class SplatNetError(Exception):
    pass


class SplatNet3:
    """A wrapper for the SplatNet 3 API.

    Parameters
    -----------
    session_token: :class:`str`
        The token returned from ``/connect/1.0.0/api/session_token``.

        This can only be recovered from manually doing the OAuth2 flow and
        fetching the token manually. It acts as a refresh token in the OAuth2
        flow.
    session: :class:`aiohttp.ClientSession`
        The session to use for making requests.
    """

    APP_VERSION = '2.3.1'
    WEB_VIEW_VERSION = '2.0.0-8a061f6c'

    def __init__(self, session_token: str, *, session: aiohttp.ClientSession) -> None:
        self.session_token = session_token
        self.session: aiohttp.ClientSession = session
        self.bullet_token: Optional[str] = None
        self.access_token: Optional[str] = None
        self.expires_in: Optional[datetime.datetime] = None
        self.user_info: Optional[dict[str, Any]] = None
        self.fetched_app_version: Optional[str] = None
        self.fetched_web_view_version: Optional[str] = None

    async def get_app_version(self) -> str:
        if self.fetched_app_version is None:
            # Android app store doesn't make it easy to get the version from it, so use the iOS app store
            async with self.session.get('https://apps.apple.com/us/app/nintendo-switch-online/id1234806557') as resp:
                if resp.status != 200:
                    return self.APP_VERSION

                text = await resp.text()
                root = html_fromstring(text)
                nodes = root.find_class('whats-new__latest__version')
                if nodes:
                    self.fetched_app_version = nodes[0].text_content().replace('Version ', '')
                else:
                    return self.APP_VERSION

        return self.fetched_app_version

    async def get_web_view_version(self) -> str:
        if self.fetched_web_view_version is None:
            async with self.session.get(
                'https://raw.githubusercontent.com/nintendoapis/nintendo-app-versions/main/data/splatnet3-app.json'
            ) as resp:
                if resp.status != 200:
                    return self.WEB_VIEW_VERSION

                text = await resp.text()
                data = json.loads(text)
                version = data['version']
                revision = data['revision'][:8]
                self.fetched_web_view_version = f'{version}-{revision}'
        return self.fetched_web_view_version

    async def get_debug_version_display(self) -> str:
        version = await self.get_app_version()
        web_view_version = await self.get_web_view_version()
        return f'App Version: {version} (Web View: {web_view_version})'

    async def get_f_token(self, id_token: str, hash_method: int = 1, retries: int = 0) -> tuple[str, str, int]:
        """Get the ``f`` token and the timestamp for the request.

        Returns
        --------
        tuple[:class:`str`, :class:`str`, :class:`int`]
            The ``f`` token, the request ID, and the timestamp.
        """

        headers = {
            'User-Agent': 'RoboDanny/5.0.0',
            'Content-Type': 'application/json; charset=utf-8',
        }

        payload = {
            'token': id_token,
            'hash_method': hash_method,
        }

        async with self.session.post(
            'https://nxapi-znca-api.fancy.org.uk/api/znca/f', json=payload, headers=headers
        ) as resp:
            if resp.status >= 400:
                if retries >= 5:
                    raise SplatNetError(f'Failed to get f token (status code {resp.status})')
                else:
                    log.info('Failed to get f token (status code: %d), retrying...', resp.status)
                    await asyncio.sleep(5 * (retries + 1))
                    return await self.get_f_token(id_token, hash_method, retries=retries + 1)

            data = await resp.json()
            return data['f'], data['request_id'], data['timestamp']

    def is_expired(self) -> bool:
        return self.expires_in is None or self.expires_in < datetime.datetime.utcnow()

    async def refresh_expired_tokens(self, *, retries: int = 0) -> None:
        payload = {
            'client_id': '71b963c1b7b6d119',  # NSO app
            'session_token': self.session_token,
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer-session-token',
        }

        url = 'https://accounts.nintendo.com/connect/1.0.0/api/token'
        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/json',
            'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 7.1.2; SM-G965N Build/N2G48H)',
        }
        async with self.session.post(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                raise SplatNetError(f'Could not get OAuth2 token (status code {resp.status})')

            token_response = await resp.json()

        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'Accept-Language': 'en-US',
            'Authorization': f'Bearer {token_response["access_token"]}',
            'Content-Type': 'application/json',
            'User-Agent': 'NASDKAPI; Android',
        }

        if self.user_info is None:
            async with self.session.get('https://api.accounts.nintendo.com/2.0.0/users/me', headers=headers) as resp:
                if resp.status >= 400:
                    raise SplatNetError(f'Could not get user information (status code {resp.status})')

                self.user_info = user_info = await resp.json()
                log.info('Successfully got user info for account %s', user_info['nickname'])
        else:
            user_info = self.user_info

        url = 'https://api-lp1.znc.srv.nintendo.net/v3/Account/Login'
        app_version = await self.get_app_version()
        headers = {
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': f'com.nintendo.znca/{app_version}(Android/7.1.2)',
            'X-Platform': 'Android',
            'X-ProductVersion': app_version,
        }

        id_token = token_response['id_token']
        await asyncio.sleep(3)
        f, request_id, timestamp = await self.get_f_token(id_token)

        payload = {
            'parameter': {
                'naIdToken': id_token,
                'naCountry': user_info['country'],
                'naBirthday': user_info['birthday'],
                'language': user_info['language'],
                'f': f,
                'timestamp': timestamp,
                'requestId': request_id,
            },
        }

        async with self.session.post(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                raise SplatNetError(f'Could not get account login token (status code {resp.status})')

            data = await resp.json()
            if 'result' not in data:
                if data.get('status', None) == 9403 and retries < 5:
                    log.info('Got an invalid SplatNet 3 token error, retrying again in 30 seconds...')
                    await asyncio.sleep(30)
                    return await self.refresh_expired_tokens(retries=retries + 1)
                raise SplatNetError(f'Could not get account login token (data: {data!r})')

            new_access_token = data['result']['webApiServerCredential']['accessToken']

        # if self.registration_token is None:
        #     headers.pop('X-Platform', None)
        #     headers.pop('X-ProductVersion', None)
        #     headers['Authorization'] = f'Bearer {new_access_token}'

        #     url = 'https://api-lp1.znc.srv.nintendo.net/v1/Notification/RegisterDevice'
        #     async with self.session.post(url, headers=headers) as resp:
        #         if resp.status >= 400:
        #             raise SplatNetError(f'Could not get registration token (status code {resp.status})')

        #         data = await resp.json()
        #         self.registration_token = data['result']['registrationToken']

        url = 'https://api-lp1.znc.srv.nintendo.net/v2/Game/GetWebServiceToken'
        headers = {
            'Accept-Encoding': 'gzip',
            'Authorization': f'Bearer {new_access_token}',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': f'com.nintendo.znca/{app_version}(Android/7.1.2)',
            'X-Platform': 'Android',
            'X-ProductVersion': app_version,
        }

        await asyncio.sleep(3)
        f, request_id, timestamp = await self.get_f_token(new_access_token, hash_method=2)
        payload = {
            'parameter': {
                'id': '4834290508791808',
                'registrationToken': new_access_token,
                'f': f,
                'requestId': request_id,
                'timestamp': timestamp,
            },
        }

        async with self.session.post(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                raise SplatNetError(f'Could not get web service token (status code {resp.status})')

            data = await resp.json()
            if 'result' not in data:
                if data.get('status', None) == 9403 and retries < 5:
                    log.info('Got an invalid SplatNet 3 web service token error, retrying again in 30 seconds...')
                    await asyncio.sleep(30)
                    return await self.refresh_expired_tokens(retries=retries + 1)
                raise SplatNetError(f'Could not get web service token (status code {resp.status})')

            expires_in = data['result']['expiresIn']
            self.expires_in = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
            self.access_token = data['result']['accessToken']

        url = 'https://api.lp1.av5ja.srv.nintendo.net/api/bullet_tokens'
        web_view_version = await self.get_web_view_version()
        cookies = {'_gtoken': self.access_token}
        headers = {
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'en-US',
            'Content-Type': 'application/json',
            'Referer': 'https://api.lp1.av5ja.srv.nintendo.net/?lang=en-US&na_country=ES&na_lang=en-US',
            'Origin': 'https://api.lp1.av5ja.srv.nintendo.net',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 7.1.2; SM-G965N Build/QP1A.190711.020; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/92.0.4515.131 Mobile Safari/537.36',
            'X-NACountry': 'US',
            'X-Requested-With': 'com.nintendo.znca',
            'X-Web-View-Ver': web_view_version,
        }
        async with self.session.post(url, headers=headers, cookies=cookies) as resp:
            if resp.status >= 400:
                raise SplatNetError(f'Could not get bullet token (status code {resp.status})')

            data = await resp.json()
            self.bullet_token = data['bulletToken']

        log.info('Successfully authenticated to SplatNet 3 until %s', self.expires_in)

    async def cached_graphql_query(
        self, query_hash: str, *, version: int = 1, variables: dict[str, Any] | None = None
    ) -> Any:
        if self.is_expired():
            log.info('SplatNet 3 authentication token has expired, refreshing...')
            await self.refresh_expired_tokens()

        url = 'https://api.lp1.av5ja.srv.nintendo.net/api/graphql'
        cookies: dict[str, Any] = {'_gtoken': self.access_token}
        web_view_version = await self.get_web_view_version()
        headers = {
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'en-US',
            'Content-Type': 'application/json',
            'Origin': 'https://api.lp1.av5ja.srv.nintendo.net',
            'Authorization': f'Bearer {self.bullet_token}',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 7.1.2; SM-G965N Build/QP1A.190711.020; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/92.0.4515.131 Mobile Safari/537.36',
            'X-NACountry': 'US',
            'X-Requested-With': 'com.nintendo.znca',
            'X-Web-View-Ver': web_view_version,
        }

        payload = {
            'variables': variables or {},
            'extensions': {
                'persistedQuery': {
                    'version': version,
                    'sha256Hash': query_hash,
                }
            },
        }

        async with self.session.post(url, headers=headers, cookies=cookies, json=payload) as resp:
            if resp.status == 401:
                log.info('SplatNet 3 authentication token has expired, refreshing...')
                await self.refresh_expired_tokens()
                return await self.cached_graphql_query(query_hash, version=version, variables=variables)

            if resp.status >= 400:
                raise SplatNetError(f'Could not get graphql query (hash: {query_hash} status code {resp.status})')

            data = await resp.json()
            return data

    async def schedule(self) -> Optional[SplatNetSchedule]:
        # Called StageScheduleQuery internally
        data = await self.cached_graphql_query('730cd98e84f1030d3e9ac86b6f1aae13')
        data = data.get('data', {})
        if not data:
            return None
        return SplatNetSchedule(data)

    async def shop(self) -> Optional[SplatNetShopPayload]:
        # Called GesotownQuery internally
        data = await self.cached_graphql_query('a43dd44899a09013bcfd29b4b13314ff')
        data = data.get('data', {}).get('gesotown', {})
        if not data:
            return None
        return data

    async def latest_battles(self) -> dict[str, Any]:
        # Called LatestBattleHistoriesQuery internally
        # Too lazy to type this payload
        data = await self.cached_graphql_query('4f5f26e64bca394b45345a65a2f383bd')
        return data.get('data', {})

    async def battle_info_for(self, id: str) -> dict[str, Any]:
        # Called VsHistoryDetailQuery internally
        data = await self.cached_graphql_query('291295ad311b99a6288fc95a5c4cb2d2', variables={'vsResultId': id})
        return data.get('data', {})

    async def outfit_equipment(self) -> dict[str, Any]:
        # Called MyOutfitCommonDataEquipmentsQuery internally
        data = await self.cached_graphql_query('d29cd0c2b5e6bac90dd5b817914832f8')
        return data.get('data', {})


class InvalidBrandOrAbility(commands.CommandError, app_commands.AppCommandError):
    pass


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
    'Ability Doubler': '<:abilitydoubler:1018635151506415666>',
    'Comeback': '<:comeback:1018636386531811478>',
    'Drop Roller': '<:droproller:1018636387022536825>',
    'Haunt': '<:haunt:1018636388310208604>',
    'Ink Recovery Up': '<:inkrecoveryup:1018636389354590338>',
    'Ink Resistance Up': '<:inkresist:1018636390415745195>',
    'Ink Saver (Main)': '<:inksavermain:1018636391376236625>',
    'Ink Saver (Sub)': '<:inksaversub:1018636392412237945>',
    'Intensify Action': '<:intensifyaction:1018636392944898069>',
    'Last-Ditch Effort': '<:lastditcheffort:1018636394429694042>',
    'Ninja Squid': '<:ninjasquid:1018634355788238938>',
    'Object Shredder': '<:objectshredder:1018636395281141880>',
    'Opening Gambit': '<:openinggambit:1018642746413686875>',
    'Quick Respawn': '<:quickrespawn:1018636396140957808>',
    'Quick Super Jump': '<:quicksuperjump:1018636397038538873>',
    'Respawn Punisher': '<:respawnpunisher:1018636397923532891>',
    'Run Speed Up': '<:runspeedup:1018636398858879066>',
    'Special Charge Up': '<:specialchargeup:1018636399588679773>',
    'Special Power Up': '<:specialpowerup:1018636400582742016>',
    'Special Saver': '<:specialsaver:1018636401350279300>',
    'Stealth Jump': '<:stealthjump:1018636402193334297>',
    'Sub Power Up': '<:subpowerup:1018636403686523040>',
    'Sub Resistance Up': '<:subresistup:1018636404479230062>',
    'Swim Speed Up': '<:swimspeedup:1018636405401976902>',
    'Tenacity': '<:tenacity:1018636406412816464>',
    'Thermal Ink': '<:thermalink:1018636407322980432>',
    'Money': '<:cash:1018631026592985098>',
    'Unknown': '<:unknown:338815018101506049>',
}


def get_splatoon3_brands_and_abilities() -> tuple[list[str], list[str]]:
    with open('splatoon3.json', 'r', encoding='utf-8') as fp:
        data: SplatoonConfig = json.load(fp)
        return [d['name'] for d in data['brands']], data['abilities']


SPLATOON_3_BRANDS, SPLATOON_3_ABILITIES = get_splatoon3_brands_and_abilities()
SPLATOON_2_PINK = 0xF02D7D
SPLATOON_2_GREEN = 0x19D719
SPLATOON_3_YELLOW = 0xEAFF3D
SPLATOON_3_PURPLE = 0x603BFF


class SpecialAbilityInfo(TypedDict):
    type: Literal['Headgear', 'Clothing', 'Shoes']
    chunks: list[str]


SPLATOON_3_SPECIAL_ABILITIES: dict[str, SpecialAbilityInfo] = {
    'Opening Gambit': {'type': 'Headgear', 'chunks': ['Run Speed Up', 'Swim Speed Up', 'Ink Resistance Up']},
    'Last-Ditch Effort': {'type': 'Headgear', 'chunks': ['Ink Saver (Main)', 'Ink Saver (Sub)', 'Ink Recovery Up']},
    'Tenacity': {'type': 'Headgear', 'chunks': ['Special Charge Up', 'Special Saver', 'Special Power Up']},
    'Comeback': {'type': 'Headgear', 'chunks': ['Run Speed Up', 'Swim Speed Up', 'Special Charge Up']},
    'Ninja Squid': {'type': 'Clothing', 'chunks': ['Ink Recovery Up', 'Run Speed Up', 'Swim Speed Up']},
    'Haunt': {'type': 'Clothing', 'chunks': ['Quick Respawn', 'Sub Power Up', 'Ink Resistance Up']},
    'Thermal Ink': {'type': 'Clothing', 'chunks': ['Ink Saver (Main)', 'Ink Saver (Sub)', 'Intensify Action']},
    'Respawn Punisher': {'type': 'Clothing', 'chunks': ['Special Saver', 'Quick Respawn', 'Sub Resistance Up']},
    'Ability Doubler': {'type': 'Clothing', 'chunks': []},
    'Stealth Jump': {'type': 'Shoes', 'chunks': ['Quick Super Jump', 'Sub Resistance Up', 'Intensify Action']},
    'Object Shredder': {'type': 'Shoes', 'chunks': ['Ink Recovery Up', 'Special Power Up', 'Sub Power Up']},
    'Drop Roller': {'type': 'Shoes', 'chunks': ['Quick Super Jump', 'Ink Resistance Up', 'Intensify Action']},
}


def fromisoformat(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))


class SplatNetSchedule:
    __slots__ = (
        'regular',
        'ranked_series',
        'ranked_open',
        'league',
        'x_rank',
        'salmon_run',
        'splatfest',
    )

    VALID_NAMES = (
        'regular',
        'ranked_series',
        'ranked_open',
    )

    def __init__(self, data: dict[str, Any]) -> None:
        turf_wars: list[RegularScheduleRotationPayload] = data.get('regularSchedules', {}).get('nodes', [])
        self.regular: list[Rotation] = []
        for payload in turf_wars:
            start_time = fromisoformat(payload['startTime'])
            end_time = fromisoformat(payload['endTime'])
            inner = payload['regularMatchSetting']
            if inner is not None:
                stages = inner['vsStages']
                rule = inner['vsRule']['name']
                self.regular.append(Rotation(start_time=start_time, end_time=end_time, rule=rule, stages=stages))

        splatfest: list[BaseScheduleRotationPayload] = data.get('festSchedules', {}).get('nodes', [])
        self.splatfest: list[Rotation] = []
        for payload in splatfest:
            start_time = fromisoformat(payload['startTime'])
            end_time = fromisoformat(payload['endTime'])
            inner = payload.get('festMatchSetting')
            if inner is not None:
                stages = inner['vsStages']
                rule = inner['vsRule']['name']
                self.splatfest.append(Rotation(start_time=start_time, end_time=end_time, rule=rule, stages=stages))

        ranked: list[RankedScheduleRotationPayload] = data.get('bankaraSchedules', {}).get('nodes', [])
        self.ranked_series: list[Rotation] = []
        self.ranked_open: list[Rotation] = []
        for payload in ranked:
            start_time = fromisoformat(payload['startTime'])
            end_time = fromisoformat(payload['endTime'])
            inner = payload['bankaraMatchSettings'] or []
            for inner_setting in inner:
                stages = inner_setting['vsStages']
                rule = inner_setting['vsRule']['name']
                mode = inner_setting['mode']
                rotation = Rotation(start_time=start_time, end_time=end_time, rule=rule, stages=stages)
                if mode == 'OPEN':
                    self.ranked_open.append(rotation)
                elif mode == 'CHALLENGE':
                    self.ranked_series.append(rotation)

        league: list[LeagueScheduleRotationPayload] = data.get('leagueSchedules', {}).get('nodes', [])
        self.league: list[Rotation] = []
        for payload in league:
            start_time = fromisoformat(payload['startTime'])
            end_time = fromisoformat(payload['endTime'])
            inner = payload['leagueMatchSetting']
            if inner is not None:
                stages = inner['vsStages']
                rule = inner['vsRule']['name']
                self.league.append(Rotation(start_time=start_time, end_time=end_time, rule=rule, stages=stages))

        x_rank: list[XScheduleRotationPayload] = data.get('xSchedules', {}).get('nodes', [])
        self.x_rank: list[Rotation] = []
        for payload in x_rank:
            start_time = fromisoformat(payload['startTime'])
            end_time = fromisoformat(payload['endTime'])
            inner = payload['xMatchSetting']
            if inner is not None:
                stages = inner['vsStages']
                rule = inner['vsRule']['name']
                self.x_rank.append(Rotation(start_time=start_time, end_time=end_time, rule=rule, stages=stages))

        salmon_run: list[SalmonRunRotationPayload] = (
            data.get('coopGroupingSchedule', {}).get('regularSchedules', {}).get('nodes', [])
        )
        self.salmon_run: list[SalmonRun] = [SalmonRun(data) for data in salmon_run]

    def pairs(self) -> tuple[tuple[str, list[Rotation]], ...]:
        if self.splatfest:
            return (('Splatfest', self.splatfest),)

        return (
            ('Anarchy Battle (Series)', self.ranked_series),
            ('Anarchy Battle (Open)', self.ranked_open),
            # ('League', self.league),
            ('X Battle', self.x_rank),
            ('Regular Battle', self.regular),
        )

    @property
    def soonest_expiry(self) -> Optional[datetime.datetime]:
        expiry_times = []
        for sequence in (self.regular, self.ranked_series, self.ranked_open, self.league, self.x_rank, self.salmon_run):
            if sequence:
                expiry_times.append(sequence[0].end_time)

        try:
            return min(expiry_times)
        except ValueError:
            return None


class Rotation:
    def __init__(
        self, *, start_time: datetime.datetime, end_time: datetime.datetime, rule: str, stages: list[StagePayload]
    ) -> None:
        self.start_time: datetime.datetime = start_time
        self.end_time: datetime.datetime = end_time
        self.stage_a: str = stages[0]['name']
        self.stage_b: str = stages[1]['name']
        self.rule: str = rule

    @property
    def current(self) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc)
        return self.start_time <= now <= self.end_time

    def __repr__(self) -> str:
        return f'<Rotation start={self.start_time} end={self.end_time} rule={self.rule} stage_a={self.stage_a!r} stage_b={self.stage_b!r}>'


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
    def from_splatnet3(cls, data: GearPayload) -> Self:
        self = cls.__new__(cls)
        typename = data['__typename']
        if typename == 'HeadGear':
            self.kind = 'head'
        elif typename == 'ClothingGear':
            self.kind = 'clothes'
        elif typename == 'ShoesGear':
            self.kind = 'shoes'
        else:
            self.kind = None
        brand = data['brand']
        self.brand = brand['name']
        self.name = data['name']
        self.stars = len(data['additionalGearPowers'])
        if 'usualGearPower' in brand:
            self.frequent_skill = brand['usualGearPower']['name']
        else:
            self.frequent_skill = None
        self.main = data['primaryGearPower']['name']
        self.price = None
        self.image = data.get('image', {}).get('url')
        return self

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

    def to_embed(self, cog: Splatoon) -> discord.Embed:
        title = self.name.replace(' ', '_')
        e = discord.Embed(colour=cog.random_colour(), title=self.name, url=f'https://splatoonwiki.org/wiki/{title}')

        if self.image:
            e.set_thumbnail(url=self.image)
        else:
            e.set_thumbnail(url='https://cdn.discordapp.com/emojis/338815018101506049.png')

        e.add_field(name='Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {self.price or "???"}')

        UNKNOWN = RESOURCE_TO_EMOJI['Unknown']

        main_slot = RESOURCE_TO_EMOJI.get(self.main or 'Unknown', UNKNOWN)
        remaining = UNKNOWN * self.stars
        e.add_field(name='Slots', value=f'{main_slot} | {remaining}')
        e.add_field(name='Brand', value=self.brand)
        if self.frequent_skill is not None:
            common = self.frequent_skill
        else:
            brands: list[SplatoonConfigBrand] = cog.splat3_data.get('brands', [])
            brand = discord.utils.find(lambda b: self.brand == b['name'], brands)
            if brand is None:
                common = 'Not found...'
            else:
                common = brand['buffed']

        e.add_field(name='Common Gear Ability', value=common)
        return e

    def to_select_option(self, *, value: Any = discord.utils.MISSING) -> discord.SelectOption:
        return discord.SelectOption(
            label=self.name,
            value=value,
            description=f'{self.brand}, {plural(self.stars):star}, {self.price or "???"} cash',
        )


class GearPageSource(menus.ListPageSource):
    def __init__(self, entries: list[Gear], cog: Splatoon) -> None:
        super().__init__(entries=entries, per_page=1)
        self.cog: Splatoon = cog

    async def format_page(self, menu: RoboPages, page: Gear):
        return page.to_embed(self.cog)


class GearSelect(discord.ui.Select):
    def __init__(self, gear: list[Gear], cog: Splatoon) -> None:
        super().__init__(
            placeholder=f'Choose gear... ({len(gear)} found)',
            options=[g.to_select_option(value=str(i)) for i, g in enumerate(gear)],
        )
        self.gear: list[Gear] = gear
        self.cog: Splatoon = cog

    async def callback(self, interaction: discord.Interaction) -> Any:
        index = int(self.values[0])
        await interaction.response.edit_message(embed=self.gear[index].to_embed(self.cog))


class GearQuery(commands.FlagConverter):
    name: Optional[str] = commands.flag(description='The name of the gear')
    brand: Optional[str] = commands.flag(description='The brand of the ability')
    ability: Optional[str] = commands.flag(description='The main ability of the gear')
    frequent: Optional[str] = commands.flag(description='The buffed ability of the gear')
    type: Literal['hat', 'head', 'shoes', 'shirt', 'clothes', 'any'] = commands.flag(
        description='The type of gear to search for', default='any'
    )


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

    @property
    def choice_name(self) -> str:
        return f'{self.name} \N{EN DASH} {self.sub} \N{EN DASH} {self.special}'

    def to_select_option(self, *, value: Any = discord.utils.MISSING) -> discord.SelectOption:
        return discord.SelectOption(label=self.name, value=value, description=f'{self.sub} with {self.special}')

    @property
    def embed(self) -> discord.Embed:
        e = discord.Embed(title=self.name, colour=SPLATOON_3_YELLOW)
        e.add_field(name='Sub', value=self.sub)
        e.add_field(name='Special', value=self.special)
        if self.special_cost:
            e.add_field(name='Special Cost', value=f'{self.special_cost}p')
        if self.level:
            e.set_footer(text=f'Unlocked at level {self.level}')
        return e


class WeaponPageSource(menus.ListPageSource):
    def __init__(self, entries: list[Weapon]) -> None:
        super().__init__(entries=entries, per_page=1)

    async def format_page(self, menu: RoboPages, page: Weapon):
        return page.embed


class WeaponSelect(discord.ui.Select):
    def __init__(self, weapons: list[Weapon]) -> None:
        super().__init__(
            placeholder=f'Choose a weapon... ({len(weapons)} found)',
            options=[w.to_select_option(value=str(i)) for i, w in enumerate(weapons)],
        )
        self.weapons: list[Weapon] = weapons

    async def callback(self, interaction: discord.Interaction) -> Any:
        index = int(self.values[0])
        await interaction.response.edit_message(embed=self.weapons[index].embed)


class SalmonRun:
    def __init__(self, data: SalmonRunRotationPayload):
        self.start_time: datetime.datetime = fromisoformat(data['startTime'])
        self.end_time: datetime.datetime = fromisoformat(data['endTime'])

        setting = data['setting']
        stage = setting['coopStage']
        weapons = setting['weapons']
        self.stage: str = stage.get('name')
        self.weapons: list[str] = [weapon.get('name', 'Unknown') for weapon in weapons]
        self.image: Optional[str] = stage.get('image', {}).get('url')


class SalmonRunPageSource(menus.ListPageSource):
    def __init__(self, cog: Splatoon, entries: list[SalmonRun]):
        super().__init__(entries=entries, per_page=1)
        self.cog: Splatoon = cog

    async def format_page(self, menu: RoboPages, salmon: SalmonRun):
        e = discord.Embed(colour=0xFF7500, title='Salmon Run')

        image = self.cog.get_image_url_for(salmon.stage) or salmon.image
        if image:
            e.set_image(url=image)

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
    def __init__(self, data: MerchandisePayload):
        self.gear = Gear.from_splatnet3(data['gear'])
        self.price = data['price']
        self.end_time = fromisoformat(data['saleEndTime'])


class MerchPageSource(menus.ListPageSource):
    def __init__(self, cog: Splatoon, entries: list[Merchandise]):
        super().__init__(entries=entries, per_page=1)
        self.cog: Splatoon = cog

    def format_page(self, menu: RoboPages, merch: Merchandise):
        original_gear = None
        data: SplatoonConfig = menu.ctx.cog.splat3_data  # type: ignore

        gear = merch.gear
        description = f'{time.human_timedelta(merch.end_time)} left to buy'
        gears: list[Gear] = data.get(gear.kind, [])  # type: ignore
        for elem in gears:
            if elem.name == gear.name:
                original_gear = elem
                break

        e = discord.Embed(colour=SPLATOON_3_YELLOW, title=gear.name, description=description)

        image = self.cog.get_image_url_for(gear.name) or gear.image
        if image:
            e.set_thumbnail(url=image)
        else:
            e.set_thumbnail(url='https://cdn.discordapp.com/emojis/338815018101506049.png')

        e.add_field(name='Price', value=f'{RESOURCE_TO_EMOJI["Money"]} {merch.price or "???"}')

        UNKNOWN = RESOURCE_TO_EMOJI['Unknown']

        main_slot = RESOURCE_TO_EMOJI.get(gear.main or UNKNOWN, UNKNOWN)
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


class SplatNetPlayer:
    def __init__(self, payload: PlayerPayload) -> None:
        self.name: str = payload['name']
        self.discriminator: str = payload['nameId']
        self.byname: str = payload['byname']
        self.me: bool = payload.get('isMyself', False)
        weapon = payload['weapon']
        self.weapon: str = weapon['name']
        self.special: str = weapon.get('specialWeapon', {}).get('name', 'Unknown')
        self.paint: int = payload['paint']
        results = payload.get('result') or {}
        self.kills: Optional[int] = results.get('kill')
        self.deaths: Optional[int] = results.get('death')
        self.assists: Optional[int] = results.get('assist')
        self.specials: Optional[int] = results.get('special')

    @property
    def score(self) -> str:
        scores = []
        if self.paint is None:
            scores.append('0p')
        else:
            scores.append(f'{self.paint}p')

        if self.kills is None:
            scores.append('--')
        else:
            if self.assists:
                scores.append(f'x{self.kills}<{self.assists}>')
            else:
                scores.append(f'x{self.kills}')

        if self.deaths is None:
            scores.append('--')
        else:
            scores.append(f'x{self.deaths}')

        if self.specials is None:
            scores.append('--')
        else:
            scores.append(f'[x{self.specials}](https://dis.gd/ "{self.special}")')

        return ' '.join(scores)

    @property
    def display_name(self) -> str:
        name = f'({self.weapon}) {self.name}'
        if self.me:
            return f'**{name}**'
        return name


def payload_to_colour(payload: ColourPayload) -> discord.Colour:
    return discord.Colour.from_rgb(
        math.floor(payload['r'] * 255), math.floor(payload['g'] * 255), math.floor(payload['b'] * 255)
    )


class SplatNetTeam:
    def __init__(self, payload: TeamPayload) -> None:
        self.judgement: str = payload['judgement']
        self.colour: discord.Colour = payload_to_colour(payload['color'])
        result = payload.get('result') or {}
        self.score: int = result.get('score', 0)
        self.paint_ratio: Optional[float] = result.get('paintRatio')
        self.order: int = payload['order']
        self.players: list[SplatNetPlayer] = [SplatNetPlayer(p) for p in payload['players']]


class SplatNetBattleInfo:
    def __init__(self, payload: VsHistoryDetailPayload) -> None:
        self.mode: str = payload['vsRule']['name']
        self.judgement: str = payload['judgement']
        self.stage: str = payload['vsStage']['name']
        self.my_team: SplatNetTeam = SplatNetTeam(payload['myTeam'])
        self.other_teams: list[SplatNetTeam] = [SplatNetTeam(t) for t in payload['otherTeams']]
        self.timestamp: datetime.datetime = fromisoformat(payload['playedTime'])
        self.awards: list[AwardPayload] = payload.get('awards', [])
        bankara_match = payload.get('bankaraMatch')
        self.ranked_mode: Optional[str] = bankara_match and bankara_match.get('mode')

    def is_recent(self) -> bool:
        now = discord.utils.utcnow()
        return self.timestamp + datetime.timedelta(minutes=1) > now

    def to_embed(self) -> discord.Embed:
        e = discord.Embed(
            colour=self.my_team.colour,
            title=f'{self.mode}: {self.judgement}',
        )
        e.set_author(name=self.stage)
        teams = [self.my_team] + self.other_teams
        teams.sort(key=lambda t: t.order)
        for team in teams:
            names = '\n'.join(p.display_name for p in team.players)
            scores = '\n'.join(p.score for p in team.players)
            if team.paint_ratio is not None:
                title = f'{team.judgement} ({team.paint_ratio * 100:.2%})'
            else:
                title = team.judgement

            e.add_field(name=title, value=names, inline=True)
            e.add_field(name='\u200b', value='\u200b', inline=True)
            e.add_field(name='K/D/S', value=scores, inline=True)

        e.timestamp = self.timestamp
        if self.ranked_mode is not None:
            if self.ranked_mode == 'CHALLENGE':
                e.set_footer(text='Anarchy Battle (Series)')
            else:
                e.set_footer(text='Anarchy Battle (Open)')

        if self.awards:
            medals = '\n'.join(a['name'] if a['rank'] != 'GOLD' else f'\N{SPORTS MEDAL} {a["name"]}' for a in self.awards)
            e.add_field(name='Awards', value=medals)

        return e


class BrandOrAbility:
    def __init__(self, *, name: str, brand: bool) -> None:
        self.name: str = name
        self.brand: bool = brand

    @classmethod
    def get(cls, argument: str) -> Self:
        lowered = argument.lower()
        found = discord.utils.find(lambda b: b.lower() == lowered, SPLATOON_3_BRANDS)
        if found is not None:
            return cls(name=found, brand=True)
        found = discord.utils.find(lambda a: a.lower() == lowered, SPLATOON_3_ABILITIES)
        if found is None:
            raise InvalidBrandOrAbility()
        return cls(name=found, brand=False)

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        try:
            return cls.get(argument)
        except InvalidBrandOrAbility:
            # Try and disambiguate
            results = fuzzy.finder(argument, SPLATOON_3_BRANDS + SPLATOON_3_ABILITIES)
            if len(results) > 25:
                raise

            found = await ctx.disambiguate(results, lambda e: e)
            brand = found in SPLATOON_3_BRANDS
            return cls(name=found, brand=brand)

    @classmethod
    async def transform(cls, interaction: discord.Interaction, argument: str) -> Self:
        return cls.get(argument)


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
    if lower in SplatNetSchedule.VALID_NAMES:
        return lower

    if lower in ('rank', 'ranked', 'ranked series', 'series', 'rank series', 'anarchy series'):
        return 'ranked_series'
    elif lower in ('ranked open', 'rank open', 'anarchy open', 'anarchy', 'open'):
        return 'ranked_open'
    elif lower.startswith('series'):
        return 'ranked_series'
    elif lower.startswith('turf') or lower.startswith('regular'):
        return 'regular'
    elif lower.startswith(('fest', 'splatfest')):
        return 'splatfest'
    elif lower in ('x', 'x battle', 'x rank'):
        return 'x_rank'
    # elif lower == 'league':
    #     return 'league'
    else:
        raise commands.BadArgument(
            'Unknown schedule type, try: "ranked", "series", "turf", "regular", "splatfest", "x", or "league"'
        )


class AddWeaponModal(discord.ui.Modal, title='Add New Weapon'):
    name = discord.ui.TextInput(label='Name', placeholder='The weapon name')
    sub = discord.ui.TextInput(label='Sub', placeholder='The sub weapon name')
    special = discord.ui.TextInput(label='Special', placeholder='The special weapon name')

    def __init__(self, cog: Splatoon):
        super().__init__()
        self.cog: Splatoon = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        weapons = self.cog.splat3_data.get('weapons', [])
        entry: SplatoonConfigWeapon = {
            'name': str(self.name),
            'sub': str(self.sub),
            'special': str(self.special),
        }
        weapons.append(Weapon(entry))
        await self.cog.splat3_data.put('weapons', weapons)
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
        entry = self.cog.splat3_data.get('maps', [])
        entry.append(name)
        await self.cog.splat3_data.put('maps', entry)

        await modal.interaction.response.send_message(f'Successfully added new map {name}')

    @discord.ui.button(label='Refresh session token')
    async def refresh_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SimpleTextModal(title='Refresh Session', label='Session Token')
        modal.input.default = self.cog.splat3_data.get('session_token')
        await interaction.response.send_modal(modal)
        await modal.wait()

        await self.cog.refresh_splatnet_session(modal.input.value or None)
        await modal.interaction.response.send_message(f'Successfully refreshed session token', ephemeral=True)

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
        self._splatnet3: asyncio.Task[None] = asyncio.create_task(self.splatnet3())
        self._last_request: datetime.datetime = discord.utils.utcnow()

        # mode: List[Rotation]
        self.splatnet: SplatNet3 = discord.utils.MISSING
        self.sp3_map_data: Optional[SplatNetSchedule] = None
        self.sp3_shop: list[Merchandise] = []
        self._last_battle: Optional[VsHistoryDetailPayload] = None

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='SquidPink', id=230079634086166530)

    def cog_unload(self):
        self._splatnet3.cancel()

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            return await ctx.send(str(error))
        if isinstance(error, InvalidBrandOrAbility):
            return await ctx.send('Could not find a brand or ability with that name')

    @property
    def salmon_run(self) -> list[SalmonRun]:
        if self.sp3_map_data is None:
            return []
        now = discord.utils.utcnow()
        return [s for s in self.sp3_map_data.salmon_run if now < s.end_time]

    @property
    def last_battle(self) -> Optional[SplatNetBattleInfo]:
        if self._last_battle is None:
            return None
        return SplatNetBattleInfo(self._last_battle)

    def random_colour(self) -> int:
        return random.choice([SPLATOON_3_PURPLE, SPLATOON_3_YELLOW])

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

    def find_all_images(
        self, data: Mapping[str, Any], storage: list[tuple[str, str]] | None = None
    ) -> list[tuple[str, str]]:
        images = [] if storage is None else storage

        if 'name' in data:
            for key in ('image', 'originalImage'):
                if key in data and isinstance(data[key], dict) and 'url' in data[key]:
                    images.append((data['name'], data[key]['url']))
                    break

        for value in data.values():
            if isinstance(value, dict):
                self.find_all_images(value, images)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self.find_all_images(item, images)
        return images

    async def parse_splatnet3_schedule(self) -> Optional[float]:
        self.sp3_map_data = schedule = await self.splatnet.schedule()
        expiry = schedule and schedule.soonest_expiry
        log.info('Successfully retrieved SplatNet 3 schedule')
        if expiry:
            now = discord.utils.utcnow()
            return (expiry - now).total_seconds()
        return None

    async def parse_splatnet3_onlineshop(self) -> Optional[float]:
        self.sp3_shop = []
        payload = await self.splatnet.shop()
        if payload is None:
            log.info('No information retreived from SplatNet 3 online shop')
            return None

        log.info('Successfully retrieved SplatNet 3 online shop')
        for item in payload.get('pickupBrand', {}).get('brandGears', []):
            self.sp3_shop.append(Merchandise(item))
        for item in payload.get('limitedGears', []):
            self.sp3_shop.append(Merchandise(item))

        await self.bulk_upload_images(self.find_all_images(payload))

        self.sp3_shop.sort(key=lambda x: x.end_time)
        try:
            expiry = self.sp3_shop[0].end_time
        except IndexError:
            return None
        else:
            now = discord.utils.utcnow()
            return (expiry - now).total_seconds()

    async def scrape_splatnet3_stats_and_images(self) -> Optional[float]:
        data = await self.splatnet.latest_battles()
        if not data:
            return None

        groups = data.get('latestBattleHistories', {}).get('historyGroups', {}).get('nodes', [])
        if not groups:
            return

        base_path = pathlib.Path('splatoon3_stats')
        previous_battles = {p.stem for p in base_path.glob('*.json')}

        def parse_battle_id(x: str) -> str:
            # The battle ID seems to be a base64 encoded string
            # Format is something like...
            # VsHistoryDetail-u-ankjipxt57ytvacvlnmm:RECENT:<time>_<uuid>
            # We only really care about the part after RECENT:
            # I'm unsure if this format will change in the future however, so just in case do a fallback
            try:
                decoded = base64.b64decode(x)
            except:
                return x
            else:
                _, _, filename = decoded.decode('utf-8').partition(':RECENT:')
                return filename or x

        new_entries = 0
        images = []
        newest_raw_battle_id = None
        first_battle = True
        for group in groups:
            battles = group.get('historyDetails', {}).get('nodes', [])
            for battle in battles:
                raw_battle_id = battle.get('id')
                if raw_battle_id is None:
                    continue

                if newest_raw_battle_id is None:
                    newest_raw_battle_id = raw_battle_id

                battle_id = parse_battle_id(raw_battle_id)
                if battle_id in previous_battles:
                    continue

                # Sorry for the spam nintendo
                info = await self.splatnet.battle_info_for(raw_battle_id)
                if self._last_battle is None or first_battle:
                    self._last_battle = info.get('vsHistoryDetail')

                first_battle = False
                file = base_path / f'{battle_id}.json'
                with file.open('w', encoding='utf-8') as f:
                    json.dump(info, f, indent=2, ensure_ascii=False)

                previous_battles.add(battle_id)
                new_entries += 1

                images.extend(self.find_all_images(info))
                await asyncio.sleep(3)

        # Load the latest battle through cache if it's not found
        if self._last_battle is None and newest_raw_battle_id is not None:
            with open(base_path / f'{parse_battle_id(newest_raw_battle_id)}.json', 'r', encoding='utf-8') as f:
                self._last_battle = json.load(f).get('vsHistoryDetail')

        await self.bulk_upload_images(images)
        if new_entries:
            log.info('Scraped Splatoon 3 results from %s games.', new_entries)
        else:
            log.info('No Splatoon 3 result data to scrape, retrying in an hour.')

    async def get_or_save_image(self, key: str, url: str) -> Optional[str]:
        attachments = self.splat3_data.get('attachments', {})
        if key in attachments:
            return attachments[key]

        channel = self.bot.get_partial_messageable(1018160199749615696)
        async with self.bot.session.get(url) as resp:
            if resp.status != 200:
                await self.log_error(extra=f'Image [{key}]({url}) responded with {resp.status}.')
                return

            data = await resp.read()
            fp = io.BytesIO(data)
            msg = await channel.send(file=discord.File(fp, filename=f'{key}.png'))
            attachments[key] = url = msg.attachments[0].url
            await self.splat3_data.put('attachments', attachments)
            return url

    def get_image_url_for(self, key: str) -> Optional[str]:
        attachments = self.splat3_data.get('attachments', {})
        return attachments.get(key)

    async def bulk_upload_images(self, images: list[tuple[str, str]]) -> None:
        if not images:
            return

        channel = self.bot.get_partial_messageable(1018160199749615696)
        attachments = self.splat3_data.get('attachments', {})
        new_images = 0
        for chunk in discord.utils.as_chunks(images, 10):
            files: list[discord.File] = []
            for key, url in chunk:
                if key in attachments:
                    continue

                async with self.bot.session.get(url) as resp:
                    if resp.status != 200:
                        await self.log_error(extra=f'Image [{key}]({url}) responded with {resp.status}.')
                        continue

                    data = await resp.read()
                    fp = io.BytesIO(data)
                    files.append(discord.File(fp, filename=f'{key}.png'))

            if files:
                msg = await channel.send(files=files)
                new_images += len(files)
                for file, attachment in zip(files, msg.attachments):
                    attachments[file.filename[:-4]] = attachment.url

        if new_images:
            await self.splat3_data.put('attachments', attachments)
            log.info('Successfully scraped and uploaded %s images from SplatNet 3.', new_images)

    async def refresh_splatnet_session(self, session_token: Optional[str]) -> None:
        config = self.splat3_data.all()
        config['session_token'] = session_token
        await self.splat3_data.save()
        self._splatnet3.cancel()
        self._splatnet3 = self.bot.loop.create_task(self.splatnet3())

    async def splatnet3(self) -> None:
        try:
            session_token = self.splat3_data.get('session_token')
            if session_token is None:
                raise Unauthenticated()

            self.splatnet = SplatNet3(session_token, session=self.bot.session)
            await self.splatnet.refresh_expired_tokens()

            while not self.bot.is_closed():
                seconds = []
                seconds.append((await self.parse_splatnet3_schedule()) or 3600.0)
                seconds.append((await self.parse_splatnet3_onlineshop()) or 3600.0)
                seconds.append((await self.scrape_splatnet3_stats_and_images()) or 3600.0)
                self._last_request = discord.utils.utcnow()
                log.info('SplatNet 3 sleeping for %s seconds.', min(seconds))
                await asyncio.sleep(min(seconds))
        except Unauthenticated:
            await self.log_error(extra=f'Unauthenticated for SplatNet')
            raise
        except SplatNetError:
            await self.log_error(extra=f'Unauthenticated for SplatNet (internal error)')
            raise Unauthenticated()
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed):
            self._splatnet3.cancel()
            self._splatnet3 = self.bot.loop.create_task(self.splatnet3())
        except Exception:
            await self.log_error(extra='SplatNet 3 Error')

    def get_weapons_named(self, name: str) -> list[Weapon]:
        data: list[Weapon] = self.splat3_data.get('weapons', [])
        name = name.lower()

        choices = {w.name.lower(): w for w in data}
        results = fuzzy.extract_or_exact(name, choices, scorer=fuzzy.token_sort_ratio, score_cutoff=60)
        return [v for k, _, v in results]

    def query_weapons_autocomplete(self, name: str) -> list[Weapon]:
        data: list[Weapon] = self.splat3_data.get('weapons', [])
        results = fuzzy.finder(name, data, key=lambda w: w.choice_name)
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

    @commands.hybrid_command(aliases=['splatoonwiki', 'inkipedia'])
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

    async def generic_splatoon3_schedule(self, ctx: Context):
        if self.sp3_map_data is None:
            return await ctx.send('Sorry, no map data has been found yet.')

        end_time = None
        e = discord.Embed(colour=SPLATOON_3_PURPLE)

        for name, rotations in self.sp3_map_data.pairs():
            if not rotations:
                e.add_field(name=name, value='Nothing found...')
                continue

            rotation = rotations[0]
            end_time = rotation.end_time

            e.add_field(name=f'{name}: {rotation.rule}', value=f'{rotation.stage_a} and {rotation.stage_b}', inline=False)

        if end_time:
            e.title = f'Ends {discord.utils.format_dt(end_time, "R")}'

        await ctx.send(embed=e)

    async def paginated_splatoon3_schedule(self, ctx: Context, mode: str):
        rotations: list[Rotation] = getattr(self.sp3_map_data, mode, [])
        if not rotations:
            return await ctx.send('Sorry, no map data found...')

        mode_to_kind = {
            'regular': 'Regular Battle',
            'ranked_series': 'Anarchy Battle (Series)',
            'ranked_open': 'Anarchy Battle (Open)',
            'league': 'League Battle',
            'x_rank': 'X Battle',
        }

        kind = mode_to_kind.get(mode, 'Unknown')

        entries = [
            (
                f'Now: {r.rule}' if r.current else f'{discord.utils.format_dt(r.start_time, "R")}: {r.rule}',
                f'{r.stage_a} and {r.stage_b}',
            )
            for r in rotations
        ]

        p = FieldPageSource(entries, per_page=4)
        mode_to_colour = {
            'regular': 0xCFF622,
            'ranked_series': 0xF54910,
            'ranked_open': 0xF54910,
            'league': 0xF02D7D,
            'x_rank': 0x10DB9B,
        }
        p.embed.colour = mode_to_colour.get(mode, 0xCFF622)
        p.embed.title = kind
        menu = RoboPages(p, ctx=ctx, compact=True)
        await menu.start()

    @commands.hybrid_command(aliases=['maps'])
    @app_commands.choices(
        type=[
            app_commands.Choice(name='Anarchy Battle (Series)', value='ranked_series'),
            app_commands.Choice(name='Anarchy Battle (Open)', value='ranked_open'),
            app_commands.Choice(name='X Battle', value='x_rank'),
            app_commands.Choice(name='Turf War', value='regular'),
            app_commands.Choice(name='Splatfest', value='splatfest'),
        ]
    )
    @app_commands.describe(type='The type of schedule to show')
    async def schedule(self, ctx, *, type: Annotated[Optional[str], mode_key] = None):
        """Shows the current Splatoon 3 schedule."""
        if type is None:
            await self.generic_splatoon3_schedule(ctx)
        else:
            await self.paginated_splatoon3_schedule(ctx, type)

    @commands.hybrid_command()
    async def nextmaps(self, ctx: Context):
        """Shows the next Splatoon 3 maps."""
        e = discord.Embed(colour=SPLATOON_3_PURPLE, description='Nothing found...')
        if self.sp3_map_data is None:
            return await ctx.send(embed=e)

        start_time = None
        for key, value in self.sp3_map_data.pairs():
            try:
                rotation = value[1]
            except IndexError:
                e.add_field(name=key, value='Nothing found...', inline=False)
                continue
            else:
                start_time = rotation.start_time
                e.description = None

            e.add_field(name=f'{key}: {rotation.rule}', value=f'{rotation.stage_a} and {rotation.stage_b}', inline=False)

        if start_time is not None:
            e.title = f'Starts {discord.utils.format_dt(start_time, "R")}'

        await ctx.send(embed=e)

    @commands.hybrid_command()
    async def salmonrun(self, ctx: Context):
        """Shows the Salmon Run schedule, if any."""
        salmon = self.salmon_run
        if not salmon:
            return await ctx.send('No Salmon Run schedule reported.')

        pages = RoboPages(SalmonRunPageSource(self, salmon), ctx=ctx, compact=True)
        await pages.start()

    @commands.hybrid_command(aliases=['splatnetshop'])
    async def splatshop(self, ctx: Context):
        """Shows the currently running SplatNet 3 merchandise."""
        if not self.sp3_shop:
            return await ctx.send('Nothing currently being sold...')

        pages = RoboPages(MerchPageSource(self, self.sp3_shop), ctx=ctx, compact=True)
        await pages.start()

    @commands.hybrid_command()
    async def splatfest(self, ctx: Context):
        """Shows information about the currently running NA Splatfest, if any."""
        # TODO: Temporary until reverse engineer in the future
        await ctx.send('No Splatfest has been announced.')

    @commands.hybrid_command()
    @app_commands.describe(query='The weapon name, sub, or special to search for')
    async def weapon(self, ctx: Context, *, query: str):
        """Displays Splatoon 3 weapon info from a query.

        The query must be at least 3 characters long, otherwise it'll tell you it failed.
        """
        if len(query) < 3:
            return await ctx.send('The query must be at least 3 characters long.')

        weapons = self.query_weapons_autocomplete(query)
        if not weapons:
            await ctx.send('No weapons found matching this search query', ephemeral=True)
            return

        top_weapon = weapons[0]

        # Exact match
        if top_weapon.name.lower() == query.lower() or len(weapons) == 1:
            await ctx.send(embed=top_weapon.embed)
        elif len(weapons) <= 25:
            view = discord.ui.View()
            view.add_item(WeaponSelect(weapons))
            await ctx.send(embed=top_weapon.embed, view=view)
        else:
            pages = RoboPages(WeaponPageSource(weapons), ctx=ctx)
            await pages.start()

    @weapon.autocomplete('query')
    async def weapon_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        weapons = self.query_weapons_autocomplete(current)[:25]
        return [app_commands.Choice(name=weapon.choice_name, value=weapon.name) for weapon in weapons]

    def filter_gear_choices(
        self,
        query: Optional[str],
        *,
        brand: Optional[str] = None,
        ability: Optional[str] = None,
        frequent: Optional[str] = None,
        type: Optional[Literal['hat', 'head', 'shoes', 'clothes', 'shirt']] = None,
    ) -> list[Gear]:

        predicates: list[Callable[[Gear], bool]] = []
        if brand is not None:
            predicates.append(lambda g: g.brand == brand)
        if ability is not None:
            predicates.append(lambda g: g.main == ability)
        if frequent is not None:

            def frequent_predicate(gear: Gear) -> bool:
                if gear.frequent_skill is not None:
                    return gear.frequent_skill == frequent

                brands: list[SplatoonConfigBrand] = self.splat3_data['brands']
                buffed = discord.utils.find(lambda d: d['buffed'] == frequent, brands)
                return buffed is not None and gear.brand == buffed['name']

            predicates.append(frequent_predicate)

        gear: list[Gear]
        if type is not None:
            if type == 'hat':
                type = 'head'
            if type == 'shirt':
                type = 'clothes'
            gear = self.splat3_data.get(type, [])
        else:
            gear = self.splat3_data.get('head', []) + self.splat3_data.get('shoes', []) + self.splat3_data.get('clothes', [])

        first_pass = [g for g in gear if all(pred(g) for pred in predicates)]
        if not query:
            return first_pass
        return fuzzy.finder(query, first_pass, key=lambda g: g.name)

    @commands.hybrid_command()
    @app_commands.choices(
        brand=[app_commands.Choice(name=b, value=b) for b in SPLATOON_3_BRANDS],
        type=[
            app_commands.Choice(name='Head', value='head'),
            app_commands.Choice(name='Shoes', value='shoes'),
            app_commands.Choice(name='Clothes', value='clothes'),
        ],
    )
    async def gear(self, ctx: Context, *, query: GearQuery):
        """Searches for Splatoon 3 gear that matches your query.

        This command uses a syntax similar to Discord's search bar.

        The following flags are valid.

        `name:` include gear matching this name
        `brand:` include gear with this brand
        `ability:` include gear with this main ability
        `frequent:` with the buffed main ability probability
        `type:` with the type of clothing (head, hat, shoes, or clothes)

        For example, a query like `ability: ink resist brand: splash mob` will give all
        gear with Ink Resistance Up and Splash Mob as the brand.
        """

        results = self.filter_gear_choices(
            query.name,
            brand=fuzzy.find(query.brand, SPLATOON_3_BRANDS) if query.brand else None,
            ability=fuzzy.find(query.ability, SPLATOON_3_ABILITIES) if query.ability else None,
            frequent=fuzzy.find(query.frequent, SPLATOON_3_ABILITIES) if query.frequent else None,
            type=None if query.type == 'any' else query.type,
        )

        if not results:
            await ctx.send('No gear found... sorry')
        elif len(results) == 1:
            top = results[0]
            await ctx.send(embed=top.to_embed(self))
        elif len(results) <= 25:
            view = discord.ui.View()
            view.add_item(GearSelect(results, self))
            await ctx.send(view=view, embed=results[0].to_embed(self))
        else:
            pages = RoboPages(GearPageSource(results, self), ctx=ctx)
            await pages.start()

    @gear.autocomplete('name')
    async def gear_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        namespace = interaction.namespace
        matches = self.filter_gear_choices(
            current, brand=namespace.brand, ability=namespace.ability, frequent=namespace.frequent, type=namespace.type
        )[:25]
        return [app_commands.Choice(name=g.name, value=g.name) for g in matches]

    @gear.autocomplete('ability')
    @gear.autocomplete('frequent')
    async def gear_ability_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        abilities = fuzzy.finder(current, SPLATOON_3_ABILITIES)[:25]
        return [app_commands.Choice(name=a, value=a) for a in abilities]

    @gear.error
    async def gear_error(self, ctx: Context, error: Exception):
        if isinstance(error, commands.FlagError):
            msg = (
                "There were some problems with the flags you passed in. Please note that only the following flags work:\n"
                "`name`, `brand`, `ability`, `frequent`, and `type`\n\n"
                f"For example: {ctx.prefix}{ctx.invoked_with} brand: Splash Mob ability: Ink Resist"
            )
            await ctx.send(msg, ephemeral=True)

    @commands.hybrid_command()
    @app_commands.describe(query='The brand or ability to search for')
    async def brand(self, ctx, *, query: BrandOrAbility):
        """Shows Splatoon 3 brand info

        This is based on either the name or the ability given.
        If the query is an ability then it attempts to find out what brands
        influence that ability, otherwise it just looks for the brand being given.

        The query must be at least 4 characters long.
        """
        e = discord.Embed(colour=SPLATOON_3_PURPLE if query.brand else SPLATOON_3_YELLOW)
        brands: list[SplatoonConfigBrand] = self.splat3_data['brands']
        if query.brand:
            info = discord.utils.find(lambda b: b['name'] == query.name, brands)
            if info is None:
                return await ctx.send('Somehow could not find this brand')

            e.title = 'Brand Info'
            e.add_field(name='Name', value=info['name'])
            e.add_field(name='Common', value=info['buffed'])
            e.add_field(name='Uncommon', value=info['nerfed'])
            return await ctx.send(embed=e)

        e.title = 'Ability Info'
        buffs: list[str] = []
        nerfs: list[str] = []
        for brand in brands:
            if brand['buffed'] == query.name:
                buffs.append(brand['name'])
            elif brand['nerfed'] == query.name:
                nerfs.append(brand['name'])

        e.add_field(name='Common Brands', value='\n'.join(buffs) or 'Nothing')
        e.add_field(name='Uncommon Brands', value='\n'.join(nerfs) or 'Nothing')

        special = SPLATOON_3_SPECIAL_ABILITIES.get(query.name)
        if special is not None:
            e.add_field(name='Exclusive to', value=special['type'])
            e.add_field(name='Chunk conversion cost', value='\n'.join(special['chunks']) or 'Nothing')

        await ctx.send(embed=e)

    @brand.autocomplete('query')
    async def brand_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        total = SPLATOON_3_BRANDS + SPLATOON_3_ABILITIES
        filtered = fuzzy.finder(current, total)[:25]
        return [app_commands.Choice(name=a, value=a) for a in filtered]

    @commands.hybrid_command()
    @app_commands.describe(games='The number of games to scrim for', mode='The mode to play in')
    @app_commands.choices(
        mode=[app_commands.Choice(name=r, value=r) for r in ('Rainmaker', 'Splat Zones', 'Tower Control', 'Clam Blitz')]
    )
    async def scrim(self, ctx: Context, games: int = 5, *, mode: Optional[str] = None):
        """Generates Splatoon 3 scrim map and mode combinations.

        The mode combinations do not have Turf War.

        The mode is rotated unless you pick a mode to play, in which all map
        combinations will use that mode instead.
        """
        maps = self.splat3_data.get('maps', [])
        await self.generate_scrims(ctx, maps, games, mode)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def splatoon_admin(self, ctx: Context):
        """Administration panel for Splatoon configuration"""

        e = discord.Embed(colour=self.random_colour(), title='Splatoon Cog Administration')
        splatnet = not self._splatnet3.done()
        unauthed = self._splatnet3.done() and isinstance(self._splatnet3.exception(), Unauthenticated)

        e.add_field(name='SplatNet 3 Running', value=ctx.tick(splatnet), inline=True)
        e.add_field(name='SplatNet 3 Authenticated', value=ctx.tick(not unauthed), inline=True)
        e.add_field(name='SplatNet 3 Version', value=await self.splatnet.get_debug_version_display(), inline=False)
        e.add_field(name='Last SplatNet 3 Request', value=time.format_dt(self._last_request, 'R'), inline=False)

        await ctx.send(embed=e, view=AdminPanel(self))

    @commands.command(hidden=True, aliases=['lastbattle'])
    @commands.is_owner()
    async def lastgame(self, ctx: Context):
        """Shows the last game that was played"""

        async with ctx.typing():
            last_battle = self.last_battle
            if last_battle is None or not last_battle.is_recent():
                await self.scrape_splatnet3_stats_and_images()
                last_battle = self.last_battle

            if last_battle is None:
                return await ctx.send('No game has been played yet..?')

            await ctx.send(embed=last_battle.to_embed())

    @commands.command(hidden=True)
    @commands.is_owner()
    async def gearjson(self, ctx: Context):
        """Uploads a gear JSON file appropriate for Lean's site"""

        gear = await self.splatnet.outfit_equipment()
        history = await self.splatnet.latest_battles()

        groups = history.get('latestBattleHistories', {}).get('historyGroups', {}).get('nodes', [])
        if not groups:
            return await ctx.send('No battle history found')

        last_group = groups[0].get('historyDetails', {}).get('nodes', [])
        if not last_group:
            return await ctx.send('No battle history found')

        try:
            player_id = last_group[0]['player']['id']
        except (IndexError, KeyError):
            return await ctx.send('No battle history found')

        decoded = base64.b64decode(player_id).decode()
        value = decoded.split(':')[-1]
        h = mmh3.hash(value) & 0xFFFFFFFF
        key = base64.b64encode(bytes([k ^ (h & 0xFF) for k in value.encode()])).decode()

        data = json.dumps(
            {
                'key': key,
                'h': h,
                'timestamp': int(ctime.time()),
                'gear': {'data': gear},
            }
        ).encode()

        await ctx.send(file=discord.File(io.BytesIO(data), filename='gear.json'))


async def setup(bot: RoboDanny):
    await bot.add_cog(Splatoon(bot))
