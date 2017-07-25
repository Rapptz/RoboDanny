#!/bin/env python

# With credit to DanielKO

from lxml import etree
import datetime, re
import asyncio

NINTENDO_LOGIN_PAGE = "https://id.nintendo.net/oauth/authorize"
SPLATNET_CALLBACK_URL = "https://splatoon.nintendo.net/users/auth/nintendo/callback"
SPLATNET_CLIENT_ID = "12af3d0a3a1f441eb900411bb50a835a"
SPLATNET_SCHEDULE_URL = "https://splatoon.nintendo.net/schedule"

class Rotation(object):
    def __init__(self):
        self.start = None
        self.end = None
        self.turf_maps = []
        self.ranked_mode = None
        self.ranked_maps = []

    @property
    def is_over(self):
        return self.end < datetime.datetime.utcnow()

    def __str__(self):
        now = datetime.datetime.utcnow()
        prefix = ''
        if self.start > now:
            minutes_delta = int((self.start - now) / datetime.timedelta(minutes=1))
            hours = int(minutes_delta / 60)
            minutes = minutes_delta % 60
            prefix = '**In {0} hours and {1} minutes**:\n'.format(hours, minutes)
        else:
            prefix = '**Current Rotation**:\n'

        fmt = 'Turf War is {0[0]} and {0[1]}\n{1} is {2[0]} and {2[1]}'
        return prefix + fmt.format(self.turf_maps, self.ranked_mode, self.ranked_maps)

# based on https://github.com/Wiwiweb/SakuraiBot/blob/master/src/sakuraibot.py
async def get_new_splatnet_cookie(session, username, password):
    parameters = {'client_id': SPLATNET_CLIENT_ID,
                  'response_type': 'code',
                  'redirect_uri': SPLATNET_CALLBACK_URL,
                  'username': username,
                  'password': password}

    async with session.post(NINTENDO_LOGIN_PAGE, data=parameters) as response:
        cookie = response.history[-1].cookies.get('_wag_session')
        if cookie is None:
            raise Exception("Couldn't retrieve cookie")
        url = response.url

    # update our cookies for this session
    cookies = { '_wag_session': cookie }
    session.cookie_jar.update_cookies(cookies, response_url=url)

def parse_splatnet_time(timestr):
    # time is given as "MM/DD at H:MM [p|a].m. (PDT|PST)"
    # there is a case where it goes over the year, e.g. 12/31 at ... and then 1/1 at ...
    # this case is kind of weird though and is currently unexpected
    # it could even end up being e.g. 12/31/2015 ... and then 1/1/2016 ...
    # we'll never know

    regex = r'(?P<month>\d+)\/(?P<day>\d+)\s*at\s*(?P<hour>\d+)\:(?P<minutes>\d+)\s*(?P<p>a\.m\.|p\.m\.)\s*\((?P<tz>.+)\)'
    m = re.match(regex, timestr.strip())

    if m is None:
        raise RuntimeError('Apparently the timestamp "{}" does not match the regex.'.format(timestr))

    matches = m.groupdict()
    tz = matches['tz'].strip().upper()
    offset = None
    if tz == 'PDT':
        # EDT is UTC - 4, PDT is UTC - 7, so you need +7 to make it UTC
        offset = +7
    elif tz == 'PST':
        # EST is UTC - 5, PST is UTC - 8, so you need +8 to make it UTC
        offset = +8
    else:
        raise RuntimeError('Unknown timezone found: {}'.format(tz))

    pm = matches['p'].replace('.', '') # a.m. -> am

    current_time = datetime.datetime.utcnow()

    # Kind of hacky.
    fmt = "{2}/{0[month]}/{0[day]} {0[hour]}:{0[minutes]} {1}".format(matches, pm, current_time.year)
    splatoon_time = datetime.datetime.strptime(fmt, '%Y/%m/%d %I:%M %p') + datetime.timedelta(hours=offset)

    # check for new year
    if current_time.month == 12 and splatoon_time.month == 1:
        splatoon_time.replace(current_time.year + 1)

    return splatoon_time


async def get_splatnet_schedule(session):

    """
    This is repeated 3 times:

    <span class"stage-schedule"> ... </span> <--- figure out how to parse this
    <div class="stage-list">
        <div class="match-type">
            <span class="icon-regular-match"></span> <--- turf war
        </div>
        ... <span class="map-name"> ... </span>
        ... <span class="map-name"> ... </span>
    </div>
    <div class="stage-list">
        <div class="match-type">
            <span class="icon-earnest-match"></span> <--- ranked
        </div>
        ... <span class="rule-description"> ... </span> <--- Splat Zones, Rainmaker, Tower Control
        ... <span class="map-name"> ... </span>
        ... <span class="map-name"> ... </span>
    </div>
    """

    schedule = []
    async with session.get(SPLATNET_SCHEDULE_URL, data={'locale':"en"}) as response:
        text = await response.text()
        root = etree.fromstring(text, etree.HTMLParser())
        stage_schedule_nodes = root.xpath("//*[@class='stage-schedule']")
        stage_list_nodes = root.xpath("//*[@class='stage-list']")

        if len(stage_schedule_nodes)*2 != len(stage_list_nodes):
            raise RuntimeError("SplatNet changed, need to update the parsing!")

        for sched_node in stage_schedule_nodes:
            r = Rotation()

            start_time, end_time = sched_node.text.split("~")
            r.start = parse_splatnet_time(start_time)
            r.end = parse_splatnet_time(end_time)

            tw_list_node = stage_list_nodes.pop(0)
            r.turf_maps = tw_list_node.xpath(".//*[@class='map-name']/text()")

            ranked_list_node = stage_list_nodes.pop(0)
            r.ranked_maps = ranked_list_node.xpath(".//*[@class='map-name']/text()")
            r.ranked_mode = ranked_list_node.xpath(".//*[@class='rule-description']/text()")[0]

            schedule.append(r)

    return schedule
