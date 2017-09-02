from discord.ext import commands
from collections import namedtuple
from lxml import etree
from urllib.parse import urlparse

import itertools
import traceback
import datetime
import discord
import asyncio
import asyncpg
import enum
import re

from .utils import db, config, time

class Players(db.Table):
    id = db.PrimaryKeyColumn()
    discord_id = db.Column(db.Integer(big=True), unique=True, index=True)
    challonge = db.Column(db.String)
    switch = db.Column(db.String, unique=True)

class Teams(db.Table):
    id = db.PrimaryKeyColumn()
    active = db.Column(db.Boolean, default=True)
    challonge = db.Column(db.String, index=True)
    logo = db.Column(db.String)

class TeamMembers(db.Table, table_name='team_members'):
    id = db.PrimaryKeyColumn()
    team_id = db.Column(db.ForeignKey('teams', 'id'), index=True)
    player_id = db.Column(db.ForeignKey('players', 'id'), index=True)
    captain = db.Column(db.Boolean, default=False)

class ChallongeError(commands.CommandError):
    pass

class TournamentState(enum.IntEnum):
    invalid     = 0
    pending     = 1
    checking_in = 2
    checked_in  = 3
    underway    = 4
    complete    = 5

class PromptResult(enum.Enum):
    timeout   = 0
    error     = 1
    cannot_dm = 2
    cancel    = 3

# Temporary results that can be rolled back

class PromptTransaction:
    def __init__(self, team_page):
        self.team_page = team_page
        self.captain_fc = None
        self.captain_id = None
        self.captain_discord = None
        self.team_logo = None
        self.members = []
        self.existing_members = []

    def add_captain(self, discord_id, fc):
        self.captain_fc = fc
        self.captain_discord = discord_id

    def add_pre_existing_captain(self, player_id):
        self.captain_id = player_id

    def add_existing_member(self, player_id):
        self.existing_members.append(player_id)

    def add_member(self, discord_id, fc):
        self.members.append({ 'discord_id': discord_id, 'fc': fc })

    async def execute_sql(self, con):
        # this will be in a transaction that can be rolled back
        if self.captain_id is not None:
            query = """WITH team_insert AS (
                           INSERT INTO teams(challonge, logo)
                           VALUES ($1, $2)
                           RETURNING id
                        )
                        INSERT INTO team_members(team_id, player_id, captain)
                        SELECT id, $3, TRUE
                        FROM team_insert
                        RETURNING team_id;
                    """
            record = await con.fetchrow(query, self.team_page, self.team_logo, self.captain_id)
            team_id = record[0]
        else:
            query = """WITH player_insert AS (
                           INSERT INTO players(discord_id, switch)
                           VALUES ($1, $2)
                           RETURNING id
                       ),
                       team_insert AS (
                           INSERT INTO teams(challonge, logo)
                           VALUES ($3, $4)
                           RETURNING id
                       )
                       INSERT INTO team_members(team_id, player_id, captain)
                       SELECT x.team_id, y.player_id, TRUE
                       FROM team_insert AS x(team_id),
                            player_insert AS y(player_id)
                       RETURNING team_id;
                    """
            record = await con.fetchrow(query, self.captain_discord, self.captain_fc, self.team_page, self.team_logo)
            team_id = record[0]

        # insert pre-existing members:
        if self.existing_members:
            query = """INSERT INTO team_members(player_id, team_id)
                       SELECT x.player_id, $1
                       FROM UNNEST($2::int[]) AS x(player_id);
                    """
            await con.execute(query, team_id, self.existing_members)

        # insert new members
        if self.members:
            query = """WITH player_insert AS (
                           INSERT INTO players(discord_id, switch)
                           SELECT x.discord_id, x.fc
                           FROM jsonb_to_recordset($2::jsonb) AS x(discord_id bigint, fc text)
                           RETURNING id
                       )
                       INSERT INTO team_members(player_id, team_id)
                       SELECT x.id, $1
                       FROM player_insert x;
                    """
            await con.execute(query, team_id, self.members)

        return team_id

# Some validators for _prompt

_friend_code = re.compile(r'^(?:(?:SW)[- _]?)?(?P<one>[0-9]{4})[- _]?(?P<two>[0-9]{4})[- _]?(?P<three>[0-9]{4})$')

def fc_converter(arg, *, _fc=_friend_code):
    fc = arg.upper().strip('"')
    m = _fc.match(fc)
    if m is None:
        raise commands.BadArgument('Invalid Switch Friend Code given.')
    return '{one}-{two}-{three}'.format(**m.groupdict())

def valid_fc(message):
    try:
        return fc_converter(message.content)
    except:
        return False

def yes_no(message):
    l = message.content.lower()
    if l in ('y', 'yes'):
        return 1
    elif l in ('n', 'no'):
        return -1
    return False

def validate_url(url):
    o = urlparse(url, scheme='http')
    if o.scheme not in ('http', 'https'):
        return False
    url = o.netloc + o.path
    if not url:
        return False
    if not url.lower().endswith(('.png', '.jpeg', '.jpg', '.gif')):
        return False
    return o.geturl()

def valid_logo(message):
    if message.content.lower() == 'none':
        return None

    url = message.content
    if message.attachments:
        url = message.attachments[0].url
    return validate_url(url)

BOOYAH_GUILD_ID = 333799317385117699
TOURNEY_ORG_ROLE = 333812806887538688
PARTICIPANT_ROLE = 343137581564952587
NOT_CHECKED_IN_ROLE = 343137740889522177
ANNOUNCEMENT_CHANNEL = 342925685729263616
BOT_SPAM_CHANNEL = 343191203686252548
TOP_PARTICIPANT_ROLE = 353646321028169729

def is_to():
    def predicate(ctx):
        return ctx.guild and any(r.id == TOURNEY_ORG_ROLE for r in ctx.author.roles)
    return commands.check(predicate)

def in_booyah_guild():
    def predicate(ctx):
        return ctx.guild and ctx.guild.id == BOOYAH_GUILD_ID
    return commands.check(predicate)

ChallongeTeamInfo = namedtuple('ChallongeTeamInfo', 'name members')

# Members is an array of (Member, Switch-Code)
ParticipantInfo = namedtuple('ParticipantInfo', 'name logo members')

class Challonge:
    _validate = re.compile("""(?:https?\:\/\/)?(?:(?P<subdomain>[A-Za-z]+)\.)?challonge\.com\/    # Main URL
                              (?:(?:de|en|es|fr|hu|it|ja|ko|no|pl|pt|pt_BR|ru|sk|sv|tr|zh_CN)\/)? # Language selection
                              (?:teams\/)?(?P<slug>[^\/]+)                                        # Slug
                           """, re.VERBOSE)

    BASE_API = 'https://api.challonge.com/v1/tournaments'

    def __init__(self, bot, url, slug):
        self.session = bot.session
        self.api_key = bot.challonge_api_key
        self.url = url
        self.slug = slug

    @classmethod
    def from_url(cls, bot, url):
        if not url:
            return cls(bot, None, None)

        m = cls._validate.match(url)
        if m is None:
            raise ValueError('Invalid URL?')

        sub = m.group('subdomain')
        slug = m.group('slug')
        if sub:
            slug = f'{sub}-{slug}'

        return cls(bot, url, slug)

    @classmethod
    async def convert(cls, ctx, argument):
        try:
            return cls.from_url(ctx.bot, argument)
        except ValueError:
            raise ChallongeError('Not a valid challonge URL!') from None

    async def get_team_info(self, team_slug):
        url = f'http://challonge.com/teams/{team_slug}'

        # Challonge does not expose team info endpoint so we're gonna have
        # to end up using regular ol' web scraping.
        # So this code may break in the future if Challonge changes their DOM.
        # Luckily, it's straight forward currently.
        async with self.session.get(url) as resp:
            if resp.status != 200:
                raise ChallongeError(f'Challonge team page for {team_slug} responded with {resp.status}')

            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            # get team name

            team_name = root.find(".//div[@id='title']")
            if team_name is None:
                raise ChallongeError(f'Could not find team name. Contact Danny. URL: <{url}>')

            team_name = ''.join(team_name.itertext()).strip()

            # get team members
            members = root.findall(".//div[@class='team-member']/a")
            if members is None or len(members) == 0:
                raise ChallongeError(f'Could not find team members. Contact Danny. URL: <{url}>')

            members = [
                member.get('href').replace('/users/', '')
                for member in members
            ]

            return ChallongeTeamInfo(team_name, members)

    async def show(self, *, include_matches=False, include_participants=False):
        params = {
            'api_key': self.api_key,
            'include_matches': int(include_matches),
            'include_participants': int(include_participants),
        }
        url = f'{self.BASE_API}/{self.slug}.json'
        async with self.session.get(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('tournament', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def start(self, *, include_matches=True, include_participants=False):
        params = {
            'api_key': self.api_key,
            'include_matches': int(include_matches),
            'include_participants': int(include_participants)
        }

        url = f'{self.BASE_API}/{self.slug}/start.json'
        async with self.session.post(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('tournament', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def finalize(self, *, include_matches=False, include_participants=True):
        params = {
            'api_key': self.api_key,
            'include_matches': int(include_matches),
            'include_participants': int(include_participants)
        }

        url = f'{self.BASE_API}/{self.slug}/finalize.json'
        async with self.session.post(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('tournament', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def matches(self, *, state=None, participant_id=None):
        params = {
            'api_key': self.api_key
        }
        if state:
            params['state'] = state
        if participant_id is not None:
            params['participant_id'] = participant_id

        url = f'{self.BASE_API}/{self.slug}/matches.json'
        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')
            return await resp.json()

    async def score_match(self, match_id, winner_id, player1_score, player2_score):
        params = {
            'api_key': self.api_key,
            'match[winner_id]': winner_id,
            'match[scores_csv]': f'{player1_score}-{player2_score}'
        }

        url = f'{self.BASE_API}/{self.slug}/matches/{match_id}.json'
        async with self.session.put(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('match', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def add_participant(self, username, *, misc=None):
        params = {
            'api_key': self.api_key,
            'participant[challonge_username]': username
        }

        if misc is not None:
            params['participant[misc]'] = str(misc)

        url = f'{self.BASE_API}/{self.slug}/participants.json'
        async with self.session.post(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('participant', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def get_participant(self, participant_id, *, include_matches=False):
        params = {
            'api_key': self.api_key,
            'include_matches': int(include_matches)
        }
        url = f'{self.BASE_API}/{self.slug}/participants/{participant_id}.json'
        async with self.session.get(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return js.get('participant', {})
            if resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

    async def remove_participant(self, participant_id):
        url = f'{self.BASE_API}/{self.slug}/participants/{participant_id}.json'
        params = { 'api_key': self.api_key }
        async with self.session.delete(url, params=params) as resp:
            if resp.status != 200:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))

    async def participants(self):
        url = f'{self.BASE_API}/{self.slug}/participants.json'
        params = {
            'api_key': self.api_key
        }

        async with self.session.get(url, params=params) as resp:
            if resp.status == 200:
                js = await resp.json()
                return [x['participant'] for x in js if 'participant' in x]
            elif resp.status == 422:
                js = await resp.json()
                raise ChallongeError('\n'.join(x for x in js.get('errors', [])))
            else:
                raise ChallongeError(f'Challonge responded with {resp.status} for {url}.')

class Tournament:
    """Tournament specific tools."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('tournament.json')
        self._already_running_registration = set()

    async def __error(self, ctx, error):
        if isinstance(error, (ChallongeError, commands.BadArgument)):
            traceback.print_exc()
            await ctx.send(error)

    async def log(self, message, ctx=None, *, ping=False, error=False, **fields):
        if error is False:
            e = discord.Embed(colour=0x59b642, title=message)
        else:
            if error:
                e = discord.Embed(colour=0xb64259, title='Error')
            else:
                e = discord.Embed(colour=0xb69f42, title='Warning')

            exc = traceback.format_exc(chain=False, limit=10)
            if exc != 'NoneType: None\n':
                e.description = f'```py\n{exc}\n```'
            e.add_field(name='Reason', value=message, inline=False)

        if ctx is not None:
            e.add_field(name='Author', value=f'{ctx.author} (ID: {ctx.author.id})', inline=False)
            e.add_field(name='Command', value=ctx.message.content, inline=False)

        for name, value in fields.items():
            e.add_field(name=name, value=value)

        e.timestamp = datetime.datetime.utcnow()

        wh_id, wh_token = self.bot.config.tourney_webhook
        hook = discord.Webhook.partial(id=wh_id, token=wh_token, adapter=discord.AsyncWebhookAdapter(self.bot.session))
        await hook.send(embed=e, content='@here' if ping else None)

    @property
    def tournament_state(self):
        return TournamentState(self.config.get('state', 0))

    @property
    def challonge(self):
        return Challonge.from_url(self.bot, self.config.get('url', ''))

    @commands.group(invoke_without_command=True, aliases=['tournament'])
    @in_booyah_guild()
    async def tourney(self, ctx):
        """Shows you information about the currently tournament."""
        data = await self.challonge.show()
        e = discord.Embed(title=data['name'], url=self.challonge.url, colour=0xa83e4b)
        description = data['description']
        if description:
            e.description = description

        participants = None
        not_checked_in = None
        for role in ctx.guild.roles:
            if role.id == PARTICIPANT_ROLE:
                participants = role
            elif role.id == NOT_CHECKED_IN_ROLE:
                not_checked_in = role

        attendance = f'{data.get("participants_count", 0)} total attending'
        if participants:
            attendance = f'{attendance}\n{len(participants.members)} active'
        if not_checked_in:
            not_checked_in = len(not_checked_in.members)
            if not_checked_in:
                attendance = f'{attendance}\n{not_checked_in} not checked in'

        e.add_field(name='Attendance', value=attendance)

        current_round = self.config.get('round')
        if current_round is None:
            value = 'None'
            e.add_field(name='Current Round', value=None)
        else:
            round_ends = datetime.datetime.fromtimestamp(self.config.get('round_ends', 0.0))
            value = f'Round {current_round}\nEnds in {time.human_timedelta(round_ends)}'

        e.add_field(name='Current Round', value=value)

        state = self.tournament_state.name.replace('_', ' ').title()
        e.add_field(name='State', value=state)
        await ctx.send(embed=e)

    @tourney.command(name='open')
    @is_to()
    async def tourney_open(self, ctx, *, url: Challonge):
        """Opens a tournament for sign ups"""

        if self.tournament_state is not TournamentState.invalid:
            return await ctx.send(f'A tournament is already in progress. Try {ctx.prefix}tourney close')

        tourney = await url.show()
        if tourney.get('state') != 'pending':
            return await ctx.send('This tournament is not pending.')

        c = self.config.all()
        c['url'] = url.url
        c['state'] = TournamentState.pending.value
        await self.config.save()
        await ctx.send(f'Now accepting registrations until "{ctx.prefix}tourney checkin" is run.')
        return True

    @tourney.command(name='strict')
    @is_to()
    async def tourney_strict(self, ctx, *, url: Challonge):
        """Opens a tournament for strict sign-ups.

        When done with strict sign-ups, only those with the
        Top Participant role can register.
        """
        result = await ctx.invoke(self.tourney_open, url=url)
        if result is True:
            await self.config.put('strict', True)

    @tourney.command(name='checkin')
    @is_to()
    async def tourney_checkin(self, ctx):
        """Opens the tournament for checking in.

        Check-ins last 2 hours. Users will be reminded to check-in
        at 1 hour remaining, 30 minutes, 15 minutes, and 5 minutes remaining.

        Tournament must be in the pending state.
        """

        if self.tournament_state is not TournamentState.pending:
            return await ctx.send('This tournament is not pending.')

        # Create the check-in timers:
        reminder = self.bot.get_cog('Reminder')
        if reminder is None:
            return await ctx.send('Tell Danny the Reminder cog is off.')

        announcement = ctx.guild.get_channel(ANNOUNCEMENT_CHANNEL)
        if announcement is None:
            return await ctx.send('Missing the announcement channel to notify on.')

        not_checked_in = discord.utils.find(lambda r: r.id == NOT_CHECKED_IN_ROLE, ctx.guild.roles)
        if not_checked_in is None:
            return await ctx.send('Could not find the Not Checked In role.')

        base = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        durations = (
            (base, 0),
            # (base - datetime.timedelta(hours=1), 60),
            (base - datetime.timedelta(minutes=30), 30),
            (base - datetime.timedelta(minutes=15), 15),
            (base - datetime.timedelta(minutes=5), 5),
        )

        for when, remaining in durations:
            await reminder.create_timer(when, 'tournament_checkin', remaining, connection=ctx.db)

        await self.config.put('state', TournamentState.checking_in)
        await ctx.send(f'Check-ins are now being processed. When complete and ready, use {ctx.prefix}tourney start')

        await not_checked_in.edit(mentionable=True)
        msg = f"<@&{NOT_CHECKED_IN_ROLE}> check-ins are now being processed. " \
              f"To check-in please go to <#{BOT_SPAM_CHANNEL}> and use the `?checkin` command."
        await announcement.send(msg)
        await not_checked_in.edit(mentionable=False)

    async def on_tournament_checkin_timer_complete(self, timer):
        minutes_remaining, = timer.args

        guild = self.bot.get_guild(BOOYAH_GUILD_ID)
        if guild is None:
            # wtf
            return

        announcement = guild.get_channel(ANNOUNCEMENT_CHANNEL)
        if announcement is None:
            return

        role = discord.utils.find(lambda r: r.id == NOT_CHECKED_IN_ROLE, guild.roles)
        if role is None:
            return

        if len(role.members) == 0:
            # everyone surprisingly checked in?
            if self.tournament_state is TournamentState.checking_in:
                await self.config.put('state', TournamentState.checked_in)
            await self.log("No Check-Ins To Process", **{'Remaining Minutes': minutes_remaining})
            return

        if minutes_remaining != 0:
            # A reminder that they need to check-in
            msg = f'<@&{role.id}> Reminder: You have **{minutes_remaining} minutes** left to check-in.\n\n' \
                  f'To check-in please go to <#{BOT_SPAM_CHANNEL}> and use the `?checkin` command.'

            await role.edit(mentionable=True)
            await announcement.send(msg)
            await role.edit(mentionable=False)
            return

        # Check-in period is complete, so just terminate everyone who hasn't checked-in.
        has_not_checked_in = [m.id for m in role.members]

        # Remove the role from everyone who did not check in and notify them.

        msg = "Hello. You've been disqualified due to failure to check-in. " \
              "If you believe this is an error, please contact a TO."

        for member in role.members:
            try:
                await member.remove_roles(role)
                await member.send(msg)
            except:
                pass

        query = """SELECT DISTINCT team_members.team_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE players.discord_id = ANY($1::bigint[]);
                """

        disqualified_teams = await self.bot.pool.fetch(query, has_not_checked_in)
        disqualified_teams = { str(r[0]) for r in disqualified_teams }

        challonge = self.challonge
        tournament = await challonge.show(include_participants=True)
        participants = tournament['participants']

        removed_participants = []
        not_removed = 0

        for participant in participants:
            participant_id = participant['id']
            team_id = participant['misc']

            if team_id not in disqualified_teams:
                not_removed += 1
                continue

            try:
                await challonge.remove_participant(participant_id)
            except:
                pass
            else:
                remove_participants.append(participant_id)

        await self.config.put('state', TournamentState.checked_in)

        fields = {
            'Totals': f'{len(removed_participants)} removed from check ins\n{not_removed} checked in',
            'Removed Team IDs': '\n'.join(disqualified_teams),
            'Removed Participant IDs': '\n'.join(str(x) for x in removed_participants),
        }

        await self.log("Check-In Over", **fields)

        msg = "Check-ins are over! Please wait for a TO to start the tournament. " \
              "If you failed to check-in you have received a direct message saying so."

        await announcement.send(msg)

    async def prepare_participant_cache(self, *, connection=None):
        # participant_id: [ParticipantInfo]
        cache = {}
        # member_id: participant_id
        member_cache = {}

        participants = await self.challonge.participants()

        con = connection or self.bot.pool

        mapping = {}
        for participant in participants:
            if participant['final_rank'] is not None:
                continue

            misc = participant['misc']
            if misc is None or not misc.isdigit():
                continue

            mapping[int(misc)] = (participant['id'], participant['display_name'])

        query = """WITH team_info AS (
                       SELECT team_id,
                              array_agg(players.discord_id) AS "discord",
                              array_agg(players.switch) AS "switch"
                       FROM team_members
                       INNER JOIN players
                               ON players.id = team_members.player_id
                       WHERE team_id = ANY($1::bigint[])
                       GROUP BY team_id
                   )
                   SELECT t.team_id, teams.logo, t.discord, t.switch
                   FROM team_info t
                   INNER JOIN teams
                           ON teams.id = t.team_id;
                """

        records = await con.fetch(query, list(mapping))

        guild = self.bot.get_guild(BOOYAH_GUILD_ID)
        for team_id, team_logo, members, switch in records:
            participant_id, name = mapping[team_id]

            actual = []
            for index, member_id in enumerate(members):
                member_cache[member_id] = participant_id
                member = guild.get_member(member_id)
                if member is not None:
                    code = switch[index]
                    actual.append((member, code))

            cache[participant_id] = ParticipantInfo(name=name, logo=team_logo, members=actual)

        self._participants = cache
        self._member_participants = member_cache

    async def make_rooms_for_matches(self, ctx, matches, round_num, best_of):
        # channel_id mapped to:
        # match_id: match_id
        # player1_id: score
        # player2_id: score
        # confirmed: bool
        # identifier: str
        round_info = {}

        guild = self.bot.get_guild(BOOYAH_GUILD_ID)

        def embed_for_player(p, *, first=False):
            colour = 0xF02D7D if first else 0x19D719
            e = discord.Embed(title=p.name, colour=colour)
            if p.logo:
                e.set_thumbnail(url=p.logo)
            for member, switch in p.members:
                e.add_field(name=member, value=switch, inline=False)
            return e

        for match in matches:
            identifier = match['identifier'].lower()
            player1_id = match['player1_id']
            player2_id = match['player2_id']
            player_one = self._participants.get(player1_id)
            player_two = self._participants.get(player2_id)

            fields = {
                'Match ID': match['id'],
                'Player 1 ID': player1_id,
                'Player 2 ID': player2_id
            }

            if not (player_one and player_two):
                await self.log("Unable to find player information", error=True, **fields)
                continue

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True)
            }

            for member, _ in itertools.chain(player_one.members, player_two.members):
                overwrites[member] = discord.PermissionOverwrite(read_messages=True)

            try:
                channel = await guild.create_text_channel(f'group-{identifier}', overwrites=overwrites)
                round_info[str(channel.id)] = {
                    'match_id': match['id'],
                    'player1_id': player1_id,
                    'player2_id': player2_id,
                    str(player1_id): None,
                    str(player2_id): None,
                    'confirmed': False,
                    'identifier': match['identifier']
                }

                await channel.send(embed=embed_for_player(player_one, first=True))
                await channel.send(embed=embed_for_player(player_two))
                await asyncio.sleep(0.5)

                to_beat = (best_of // 2) + 1
                msg =  "@here Please use this channel to communicate!\n" \
                       "When your match is complete, **both teams must report their own scores**.\n" \
                      f"Reporting your score is done via the `?score` command. For example: `?score {to_beat}`\n" \
                       "**The ?score command can only be done in this channel.**"

                await channel.send(msg)
            except:
                await self.log("Failure when creating channel", error=True, **fields)


        base = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)

        conf = self.config.all()
        conf['round'] = round_num
        conf['best_of'] = best_of
        conf['state'] = TournamentState.underway
        conf['round_complete'] = False
        conf['total_matches'] = len(matches)
        conf['round_info'] = round_info
        conf['round_ends'] = base.timestamp()
        await self.config.save()

        times = (
            (base, 0),
            (base - datetime.timedelta(minutes=15), 15)
        )

        reminder = self.bot.get_cog('Reminder')
        for when, remaining in times:
            await reminder.create_timer(when, 'tournament_round', round_num, remaining, connection=ctx.db)

        fields = {
            'Total Matches': len(matches)
        }

        await self.log(f"Round {round_num} Started", **fields)

    async def clean_tournament_participants(self):
        guild = self.bot.get_guild(BOOYAH_GUILD_ID)
        role = discord.utils.find(lambda r: r.id == PARTICIPANT_ROLE, guild.roles)

        participants = await self.challonge.participants()
        to_remove = {p['id'] for p in participants if p['final_rank'] is not None}

        cleaned = 0
        failed = 0
        total = 0
        for member in role.members:
            total += 1
            try:
                p_id = self._member_participants[member.id]
            except KeyError:
                continue

            if p_id not in to_remove:
                continue

            try:
                await member.remove_roles(role)
            except:
                failed += 1
            else:
                cleaned += 1

        fields = {
            'Cleaned': cleaned,
            'Failed': failed,
            'Total Members': total
        }

        del self._participants
        await self.log("Participant Clean-up", **fields)
        await self.prepare_participant_cache()

    async def on_tournament_round_timer_complete(self, timer):
        round_num, remaining = timer.args

        guild = self.bot.get_guild(BOOYAH_GUILD_ID)

        if round_num != self.config.get('round'):
            fields = {
                'Expected': round_num,
                'Actual': self.config.get('round')
            }
            return await self.log(f'Round {round_num} Timer Outdated', **fields)

        total_matches = self.config.get('total_matches')
        matches_completed = sum(
            info.get('confirmed', False)
            for key, info in self.config.get('round_info', {}).items()
        )

        if total_matches == matches_completed:
            fields = {
                'Total Matches': total_matches
            }
            await self.clean_tournament_participants()
            return await self.log(f'Round {round_num} Already Complete', **fields)

        if self.config.get('round_complete', False):
            fields = {
                'Total Matches': total_matches,
                'Matches Completed': matches_completed,
            }
            await self.clean_tournament_participants()
            return await self.log(f'Round {round_num} Marked Complete Already', **fields)

        announcement = guild.get_channel(ANNOUNCEMENT_CHANNEL)
        role = discord.utils.find(lambda r: r.id == PARTICIPANT_ROLE, guild.roles)

        if not (announcement and role):
            fields = {
                'Round': round_num,
                'Channel': 'Found' if announcement else 'Not Found',
                'Role': 'Found' if role else 'Not Found',
            }
            return await self.log("Could not get role or channel for round announcement", **fields)

        if remaining != 0:
            # A reminder that the round is almost over
            await role.edit(mentionable=True)
            msg = f'<@&{role.id}>, round {round_num} will conclude in {remaining} minutes. ' \
                   'Please contact a TO if you have any issues.'
            await announcement.send(msg)
            await role.edit(mentionable=False)
            return

        # Round has concluded
        msg = f'<@&{role.id}>, round {round_num} has concluded! Please contact a TO if you require more time.'
        await role.edit(mentionable=True)
        await announcement.send(msg)
        await role.edit(mentionable=False)

        await self.config.put('round_complete', True)

        fields = {
            'Round': round_num,
            'Total Matches': total_matches,
            'Matches Completed': matches_completed
        }
        await self.log('Round Complete', **fields)

    async def start_tournament(self, ctx, best_of):
        tourney = await self.challonge.start()

        matches = [
            o['match']
            for o in tourney.get('matches')
            if o['match']['round'] == 1
        ]

        await self.make_rooms_for_matches(ctx, matches, round_num=1, best_of=best_of)

    async def continue_tournament(self, ctx, best_of, round_num):
        if not self.config.get('round_complete'):
            confirm = await ctx.prompt('The round is not complete yet, are you sure you want to continue?')
            if not confirm:
                return await ctx.send('Aborting.')

        matches = await self.challonge.matches()
        matches = [
            o['match']
            for o in matches
            if o['match']['round'] == round_num
        ]

        await self.clean_tournament_participants()
        await self.make_rooms_for_matches(ctx, matches, round_num=round_num, best_of=best_of)

    @tourney.command(name='start')
    @is_to()
    async def tourney_start(self, ctx, best_of=5):
        """Starts the tournament officially."""

        if self.tournament_state is TournamentState.checking_in:
            role = discord.utils.find(lambda r: r.id == NOT_CHECKED_IN_ROLE, ctx.guild.roles)
            if role is None:
                return await ctx.send('Uh could not find the Not Checked In role.')

            if len(role.members) == 0:
                # everyone checked in somehow
                await self.config.put('state', TournamentState.checked_in)

        if self.tournament_state not in (TournamentState.checked_in, TournamentState.underway):
            return await ctx.send('Tournament is not started or finished checking in.')

        if self.bot.get_cog('Reminder') is None:
            return await ctx.send('Reminder cog is disabled, tell Danny.')

        if not hasattr(self, '_participants'):
            await self.prepare_participant_cache()

        current_round = self.config.get('round', None)
        if current_round is None:
            # fresh tournament
            await self.start_tournament(ctx, best_of)
        else:
            await self.continue_tournament(ctx, best_of, current_round + 1)

    @tourney.command(name='confirm')
    @is_to()
    async def tourney_confirm(self, ctx, *, channel: discord.TextChannel):
        """Confirms a score and closes the channel."""

        if self.tournament_state is not TournamentState.underway:
            return await ctx.send('The tournament has not started yet.')

        if not hasattr(self, '_participants'):
            await self.prepare_participant_cache()

        round_info = self.config.get('round_info', {})
        info = round_info.get(str(channel.id))
        if info is None:
            return await ctx.send('Could not get round info for this channel.')

        player1_id = info['player1_id']
        player2_id = info['player2_id']
        first_team = self._participants.get(player1_id).name
        second_team = self._participants.get(player2_id).name

        first_score, second_score = info[str(player1_id)], info[str(player2_id)]

        round_num = self.config.get('round')
        msg = f'Are you sure you want to confirm the results of round {round_num} match {info["identifier"]}?\n' \
              f'{first_team} **{first_score}** - **{second_score}** {second_team}'

        confirm = await ctx.prompt(msg, delete_after=False, reacquire=False)
        if not confirm:
            return await ctx.send('Aborting')

        # actually confirm the score via challonge

        if first_score == second_score:
            winner_id = 'tie'
        elif first_score > second_score:
            winner_id = player1_id
        else:
            winner_id = player2_id

        await self.challonge.score_match(info['match_id'], winner_id, first_score, second_score)

        info['confirmed'] = True
        await self.config.put('round_info', round_info)
        fields = {
            'Team A': first_team,
            'Team B': second_team,
            'Match': f"{round_num}-{info['identifier']}: {info['match_id']}",
            'Team A Score': first_score,
            'Team B Score': second_score
        }
        await self.log("Score Confirmation", ctx, **fields)
        await channel.delete(reason='Score confirmation by TO.')
        await ctx.send('Confirmed.')

    @tourney.command(name='room')
    @is_to()
    async def tourney_room(self, ctx, *, channel: discord.TextChannel):
        """Opens a room for the TO."""

        if self.tournament_state is not TournamentState.underway:
            return await ctx.send('The tournament has not started yet.')

        round_info = self.config.get('round_info', {})
        if str(channel.id) not in round_info:
            return await ctx.send('This channel is not a group discussion channel.')

        await channel.set_permissions(ctx.author, read_messages=True)
        await ctx.send('Done.')

    @tourney.command(name='dq', aliases=['DQ', 'disqualify'])
    @is_to()
    async def tourney_dq(self, ctx, *, team: Challonge):
        """Disqualifies a team from the tournament.

        This removes their roles and stuff for you.
        """

        # get every member in the team

        query = """SELECT id FROM teams WHERE challonge=$1;"""
        team_id = await ctx.db.fetchrow(query, team.slug)
        if team_id is None:
            return await ctx.send('This team is not in the database.')

        # remove from challonge
        challonge = self.challonge
        team_id = team_id[0]
        participants = await challonge.participants()

        participant_id = next((p['id'] for p in participants if p['misc'] == str(team_id)), None)
        if participant_id is not None:
            await challonge.remove_participant(participant_id)

        members = await self.get_discord_users_from_team(ctx.db, team_id=team_id[0])

        for member in members:
            try:
                await member.remove_roles(discord.Object(id=PARTICIPANT_ROLE), discord.Object(id=NOT_CHECKED_IN_ROLE))
            except:
                pass

        fields = {
            'Members': '\n'.join(member.mention for member in members),
            'Participant ID': participant_id,
            'Team': f'Team ID: {team_id}\nURL: {team.url}',
        }

        await self.log('Disqualified', error=None, **fields)
        await ctx.send(f'Successfully disqualified <{team.url}>.')

    @tourney.command(name='top')
    @is_to()
    async def tourney_top(self, ctx, cut_off: int, *, url: Challonge):
        """Adds Top Participant roles based on the cut off for a URL."""

        tournament = await url.show(include_participants=True)
        if tournament.get('state') != 'complete':
            return await ctx.send('This tournament is incomplete.')

        team_ids = []
        for p in tournament['participants']:
            participant = p['participant']
            if participant['final_rank'] <= cut_off:
                team_ids.append(int(participant['misc']))

        query = """SELECT players.discord_id, team_members.team_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE team_members.team_id = ANY($1::int[]);
                """

        members = await ctx.db.fetch(query, team_ids)
        role = discord.Object(id=TOP_PARTICIPANT_ROLE)

        async with ctx.typing():
            good = 0
            for discord_id, team_id in members:
                member = ctx.guild.get_member(discord_id)
                try:
                    await member.add_roles(role, reason=f'Top {cut_off} Team ID: {team_id}')
                except:
                    pass
                else:
                    good += 1

        await ctx.send(f'Successfully applied {good} roles out of {len(members)} for top {cut_off} in <{url.url}>.')

    @tourney.command(name='close')
    @is_to()
    async def tourney_close(self, ctx):
        """Closes the currently running tournament."""

        if self.tournament_state is not TournamentState.underway:
            return await ctx.send('The tournament has not started yet.')

        # Finalize tournament
        tourney = await self.challonge.finalize()

        # Remove participant roles
        async with ctx.typing():
            await self.clean_tournament_participants()

            # Delete lingering match channels
            for channel_id in self.config.get('round_info', {}):
                channel = ctx.guild.get_channel(channel_id)
                if channel is not None:
                    await channel.delete(reason='Closing tournament.')

        # Clear state
        self.config._db = {}
        await self.config.save()
        await ctx.send('Tournament closed!')

    async def get_discord_users_from_team(self, connection, *, team_id):
        query = """SELECT players.discord_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE team_members.team_id = $1;
                """

        players = await connection.fetch(query, team_id)
        result = []
        guild = self.bot.get_guild(BOOYAH_GUILD_ID)
        for player in players:
            member = guild.get_member(player['discord_id'])
            if member is not None:
                result.append(member)
        return result

    async def register_pre_existing(self, ctx, team_id, team):
        # the captain has already registered their team before
        # so just fast track it and add it
        members = await self.get_discord_users_from_team(ctx.db, team_id=team_id)
        if not any(member.id == ctx.author.id for member in members):
            return await ctx.send(f'{ctx.author.mention}, You do not belong to this team so you cannot register with it.')

        challonge = self.challonge
        participant = await challonge.add_participant(team.name, misc=team_id)
        participant_id = participant['id']

        fields = {
            'Team ID': team_id,
            'Team Name': team.name,
            'Participant ID': participant_id,
            'Members': '\n'.join(member.mention for member in members)
        }

        msg = f"{ctx.author.mention}, you have been successfully invited to the tournament.\n" \
               "**Please follow these steps in order**\n" \
               "1. Go to <http://challonge.com/notifications>\n" \
               "2. Click on the newest \"You have been challonged\" invitation.\n" \
               "3. Follow the steps in the invitation.\n" \
              f"4. Reply to this message with: {participant_id}"

        await ctx.send(msg)
        await ctx.release()

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and str(participant_id) == m.content

        try:
            await self.bot.wait_for('message', check=check, timeout=300.0)
        except asyncio.TimeoutError:
            await ctx.send(f'{ctx.author.mention}, you did not verify your invite! Cancelling your registration.')
            await challonge.remove_participant(participant_id)
            await self.log("Cancelled Pre-Existing Registration", ctx, error=True, **fields)
            return

        participant = await challonge.get_participant(participant_id)

        if participant.get('invitation_pending', True):
            await ctx.send(f'{ctx.author.mention}, you did not accept your invite! Cancelling your registration.')
            await challonge.remove_participant(participant_id)
            await self.log("Failed Pre-Existing Registration", ctx, error=True, **fields)
            return

        await ctx.send(f'{ctx.author.mention}, alright you are good to go!')
        await self.log("Successful Pre-Existing Registration", ctx, **fields)
        for member in members:
            try:
                await member.add_roles(discord.Object(id=NOT_CHECKED_IN_ROLE), discord.Object(id=PARTICIPANT_ROLE))
            except discord.HTTPException:
                continue

    async def _prompt(self, dm, content, *, validator=None, max_tries=3, timeout=300.0, exit=True):
        validator = validator or (lambda x: True)

        def check(m):
            return m.channel.id == dm.id and m.author.id == dm.recipient.id

        try:
            await dm.send(content)
        except discord.HTTPException:
            return PromptResult.cannot_dm

        for i in range(max_tries):
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=timeout)
            except asyncio.TimeoutError:
                if exit:
                    await dm.send('Took too long. Exiting.')
                return PromptResult.timeout

            if msg.content == '?cancel':
                # special sentinel telling us to stop
                if exit:
                    await dm.send('Aborting.')
                return PromptResult.cancel

            is_valid = validator(msg)
            if is_valid is True:
                return msg
            elif is_valid is not False:
                # returning a different value
                return is_valid

            await dm.send(f"That doesn't seem right... {max_tries - i - 1} tries remaining.")

        if exit:
            await dm.send('Too many tries. Exiting.')
        return PromptResult.error

    async def new_registration(self, ctx, url, team):
        self._already_running_registration.add(ctx.author.id)
        dm = await ctx.author.create_dm()
        result = PromptTransaction(url.slug)

        msg = "Hello! I'm here to interactively set you up for the first-time registration of Booyah Battle.\n" \
             f"**If you want to cancel, you can cancel at any time by doing ?cancel**\n\n" \
              "Let's get us started with a question, **are you the captain of this team?** (say yes or no)"

        await ctx.release()
        reply = await self._prompt(dm, msg, validator=yes_no)
        if reply is PromptResult.cannot_dm:
            return await ctx.send(f'Hey {ctx.author.mention}, your DMs are disabled. Try again after you enable them.')

        if isinstance(reply, PromptResult):
            return

        if reply != 1:
            return await dm.send('Alright. Tell your captain to do this registration instead. Sorry!')

        # check if they're in the player database already
        await ctx.acquire()
        query = "SELECT id FROM players WHERE discord_id=$1;"
        record = await ctx.db.fetchrow(query, ctx.author.id)
        if record is None:
            await ctx.release()
            fc = await self._prompt(dm, "What is your switch friend code?", validator=valid_fc)
            if not isinstance(fc, str):
                return
            result.add_captain(ctx.author.id, fc)
        else:
            result.add_pre_existing_captain(record[0])

        logo_msg = "What is your team's logo? You can either do the following:\n" \
                   "- A URL pointing to the image, which must be a png/jpeg/jpg file.\n" \
                   "- An image uploaded directly to this channel.\n" \
                   "- Sending the message `None` to denote no logo."

        logo = await self._prompt(dm, logo_msg, validator=valid_logo)
        if logo is not None and not isinstance(logo, str):
            return

        result.team_logo = logo

        def valid_member(m, *, _find=discord.utils.find):
            name, _, discriminator = m.content.rpartition('#')
            value = _find(lambda u: u.name == name and u.discriminator == discriminator, self.bot.users)
            if value is not None:
                return value.id
            return False

        member_msg = "What is the member's name? You must use name#tag. e.g. Danny#0007"
        ask_member_msg = "Do you have any other team members in the server? (say yes or no)"
        members = {}
        for i in range(7):
            has_member = await self._prompt(dm, ask_member_msg, validator=yes_no, exit=False)
            if has_member is PromptResult.cancel:
                return

            if has_member != 1:
                break

            member = await self._prompt(dm, member_msg, validator=valid_member, timeout=120.0, exit=False)
            if member is PromptResult.cancel:
                return

            if member is PromptResult.timeout:
                await dm.send("Took too long... Let's move on.")
                break

            if member is PromptResult.error:
                break

            has_fc = await self._prompt(dm, "What is their switch friend code?", exit=False, validator=valid_fc, max_tries=2)
            if has_fc is PromptResult.cancel:
                return

            if isinstance(has_fc, PromptResult):
                continue

            members[member] = has_fc
            await dm.send('Successfully added member.')
            ask_member_msg = "Do you have any **additional** team members in the server? (say yes or no)"

        await ctx.acquire()

        # remove members that are in a team already
        query = """SELECT players.discord_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   WHERE teams.active
                   AND   players.discord_id = ANY($1::bigint[]);
                """

        records = await ctx.db.fetch(query, list(members))
        to_remove = {x[0] for x in records}

        members = [(a, b) for a, b in members.items() if a not in to_remove]

        # get pre-existing members
        query = """SELECT players.id
                   FROM players
                   WHERE players.discord_id = ANY($1::bigint[])
                """

        pre_existing = await ctx.db.fetch(query, [i for i, j in members])
        pre_existing = {x[0] for x in records}
        members = [(a, b) for a, b in members if a not in pre_existing]

        for player_id in pre_existing:
            result.add_existing_member(player_id)

        for discord_id, fc in members:
            result.add_member(discord_id, fc)

        transaction = ctx.db.transaction()
        await transaction.start()

        try:
            team_id = await result.execute_sql(ctx.db)
        except:
            await transaction.rollback()
            await self.log('Registration SQL Failure', ctx, error=True)
            return

        try:
            # send invite
            challonge = self.challonge
            participant = await challonge.add_participant(team.name, misc=team_id)

            # wait for accepting
            msg = f"You have been successfully invited to the tournament.\n" \
                   "**Please follow these steps in order**\n" \
                   "1. Go to <http://challonge.com/notifications>\n" \
                   "2. Click on the newest \"You have been challonged\" invitation.\n" \
                   "3. Follow the steps in the invitation.\n" \
                  f"4. Reply to this message with: {team_id}"

            def verify(m):
                return m.content == str(team_id)
            verified = await self._prompt(dm, msg, validator=verify, timeout=120.0)
        except:
            await transaction.rollback()
            await self.log("Registration failure", ctx, error=True)
            await dm.send('An error happened while trying to register.')
            return

        participant_id = participant.get('id')
        fields = {
            'Team ID': team_id,
            'Team Name': team.name,
            'Participant ID': participant_id,
        }

        if isinstance(verified, PromptResult):
            await transaction.rollback()
            await challonge.remove_participant(participant_id)
            await self.log('Took too long to accept invite', ctx, error=None, **fields)
            return

        try:
            participant = await challonge.get_participant(participant_id)
        except ChallongeError as e:
            await transaction.rollback()
            await self.log(f"Challonge error while registering: {e}", ctx, error=True, **fields)
            await dm.send(e)
        except:
            await transaction.rollback()
            await self.log("Unknown error while registering", ctx, error=True, **fields)
        else:
            if participant.get('invitation_pending', True):
                await transaction.rollback()
                await challonge.remove_participant(participant_id)
                await self.log("Did not accept invite", ctx, error=None, **fields)
                await dm.send('Invite not accepted! Aborting.')
            else:
                await transaction.commit()
                members = await self.get_discord_users_from_team(ctx.db, team_id=team_id)
                fields['Members'] = '\n'.join(member.mention for member in members)

                await self.log("Successful Registration", ctx, **fields)
                msg = "You've successfully registered! Next time, to be easier you can skip this entire process."
                await dm.send(msg)

                for member in members:
                    try:
                        await member.add_roles(discord.Object(id=NOT_CHECKED_IN_ROLE), discord.Object(id=PARTICIPANT_ROLE))
                    except discord.HTTPException:
                        continue

    @commands.command()
    @in_booyah_guild()
    async def register(self, ctx, *, url: Challonge):
        """Signs up for a running tournament."""

        if self.tournament_state is not TournamentState.pending:
            return await ctx.send('No tournament is up for sign ups right now.')

        if self.config.get('strict', False):
            if not any(role.id == TOP_PARTICIPANT_ROLE for role in ctx.author.roles):
                return await ctx.send('You do not have the Top Participant role.')

        try:
            team = await self.challonge.get_team_info(url.slug)
        except ChallongeError:
            return await ctx.send('This is not a valid challonge team page!')

        query = """SELECT id FROM teams WHERE challonge=$1;"""
        pre_existing = await ctx.db.fetchrow(query, url.slug)
        if pre_existing is not None:
            return await self.register_pre_existing(ctx, pre_existing['id'], team)

        if ctx.author.id in self._already_running_registration:
            return await ctx.send('You are already running a registration right now...')

        query = """SELECT team_id
                   FROM team_members
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE teams.active
                   AND   players.discord_id=$1;
                """

        team_id = await ctx.db.fetchrow(query, ctx.author.id)
        if team_id is not None:
            return await ctx.send('You are already part of another team.')

        try:
            await self.new_registration(ctx, url, team)
        finally:
            self._already_running_registration.discard(ctx.author.id)

    @commands.command()
    @in_booyah_guild()
    async def checkin(self, ctx):
        """Checks you in to the current running tournament."""

        if self.tournament_state is not TournamentState.checking_in:
            return await ctx.send('No tournament is up for check-ins right now.')

        query = """SELECT team_members.team_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE players.discord_id = $1;
                """

        record = await ctx.db.fetchrow(query, ctx.author.id)
        if record is None:
            return await ctx.send('You do not have a team signed up.')

        team_id = record[0]
        members = await self.get_discord_users_from_team(ctx.db, team_id=team_id)

        did_not_remove = []
        for member in members:
            try:
                await member.remove_roles(discord.Object(id=NOT_CHECKED_IN_ROLE))
            except:
                did_not_remove.append(member)

        fields = {
            'Checked-in Members': '\n'.join(member.mention for member in members),
        }

        if did_not_remove:
            fields['Failed Removals'] = '\n'.join(member.mention for member in members)
        else:
            fields['Failed Removals'] = 'None'

        await self.log("Check-In Processed", ctx, **fields)
        await ctx.message.add_reaction(ctx.tick(True).strip('<:>'))

    @commands.command()
    @in_booyah_guild()
    async def score(self, ctx, wins: int):
        """Submits your score to the tournament."""

        if self.tournament_state is not TournamentState.underway:
            return await ctx.send('A tournament is not currently running.')

        if not hasattr(self, '_participants'):
            await self.prepare_participant_cache()

        info = self.config.get('round_info', {})
        ours = info.get(str(ctx.channel.id))
        if ours is None:
            return await ctx.send('This channel is not a currently running group channel.')

        best_of = self.config.get('best_of')

        if wins > ((best_of // 2) + 1):
            return await ctx.send('That sort of score is impossible friend.')

        our_participant_id = self._member_participants.get(ctx.author.id)
        if our_participant_id is None:
            return await ctx.send('Apparently, you are not participating in this tournament.')

        if ours['confirmed']:
            return await ctx.send('This score has been confirmed by a TO and cannot be changed. Contact a TO.')

        their_participant_id = ours['player2_id'] if ours['player1_id'] == our_participant_id else ours['player1_id']
        our_score = ours[str(our_participant_id)]
        their_score = ours[str(their_participant_id)]
        round_num = self.config.get('round')

        fields = {
            'Reporting Team': self._participants[our_participant_id].name,
            'Room': f'{round_num}-{ours["identifier"]}: {ctx.channel.mention}',
            'Match ID': ours['match_id'],
            'Reporter Score': None,
            'Enemy Score': their_score,
            'Round': f'Round {round_num}: Best of {best_of}'
        }

        changed_score = False
        ping = False
        round_complete = self.config.get('round_complete', False)
        if our_score is None:
            fields['Reporter Score'] = wins
        else:
            if our_score == wins:
                return await ctx.send('You already submitted this exact score before bud.')

            fields['Reporter Score'] = f'{our_score} -> {wins}'
            changed_score = True

        if their_score is None:
            ours[str(our_participant_id)] = wins
            title = 'Changed score submission' if changed_score else 'Score Submission'
        else:
            if their_score + wins > best_of:
                await ctx.send('Your score conflicts with the enemy score.')
                reason = 'Score conflict'
                if round_complete:
                    reason = f'{reason} + done after round completion'
                await self.log(reason, ctx, error=True, **fields)
                return

            ours[str(our_participant_id)] = wins
            title = 'Changed complete score submission' if changed_score else 'Complete Score Submission'
            ping = True

        await ctx.send('Score reported.')
        if round_complete:
            fields['Info'] = title
            await self.log('Submission After Round Complete', ctx, ping=ping, error=None, **fields)
        else:
            await self.log(title, ctx, ping=ping, **fields)

        await self.config.put('round_info', info)

    @commands.group()
    @in_booyah_guild()
    async def team(self, ctx):
        """Manages your team."""
        pass

    @team.command(name='create')
    async def team_create(self, ctx, *, url: Challonge):
        """Creates a team."""

        # Check if they're an active player

        query = """SELECT id FROM players WHERE discord_id = $1;"""
        record = await ctx.db.fetchrow(query, ctx.author.id)
        if record is None:
            return await ctx.send(f'You have not registered as a player. Try {ctx.prefix}player ' \
                                   'switch SW-1234-5678-9012 to register yourself as a player.')

        player_id = record['id']

        # Check if they're in an active team

        query = """SELECT team_id, captain
                   FROM team_members
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   WHERE teams.active
                   AND team_members.player_id = $1
                """

        record = await ctx.db.fetchrow(query, player_id)
        if record is not None:
            return await ctx.send('You are already an active member of a team.')

        team_info = await url.get_team_info(url.slug)

        # Check if this team already exists
        query = """SELECT id FROM teams WHERE challonge=$1;"""
        exists = await ctx.db.fetchrow(query, url.slug)
        if exists:
            return await ctx.send('This team already exists.')

        # Actually insert
        query = """WITH to_insert AS (
                       INSERT INTO teams (challonge)
                       VALUES ($1)
                       RETURNING id
                   )
                   INSERT INTO team_members (team_id, player_id, captain)
                   SELECT to_insert.id, $2, TRUE
                   FROM to_insert;
                """

        await ctx.db.execute(query, url.slug, player_id)
        await ctx.send(f'Successfully created team {team_info.name}. See "{ctx.prefix}help team" for more commands.')

    async def get_owned_team_info(self, ctx):
        query = """SELECT team_id AS "id",
                          players.id AS "owner_id",
                          'https://challonge.com/teams/' || teams.challonge AS "challonge"
                   FROM team_members
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE team_members.captain
                   AND   players.discord_id = $1
                   AND   teams.active;
                """

        record = await ctx.db.fetchrow(query, ctx.author.id)
        return record

    @team.command(name='delete')
    async def team_delete(self, ctx):
        """Marks your current team as inactive."""
        team = await self.get_owned_team_info(ctx)

        # Get the owned team
        if team is None:
            return await ctx.send('You do not own any team.')

        query = """UPDATE teams SET active = FALSE WHERE id = $1;"""
        await ctx.db.execute(query, team['id'])
        await ctx.send('Team successfully marked as inactive.')

    @team.command(name='add')
    async def team_add(self, ctx, *, member: discord.Member):
        """Adds a member to your team."""

        team = await self.get_owned_team_info(ctx)
        if team is None:
            return await ctx.send('You do not own any team.')

        query = """SELECT id FROM players WHERE discord_id=$1;"""
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            await ctx.send('It appears this member has not registered before. ' \
                          f'Ask them to do so by inputting their switch code via "{ctx.prefix}player switch" command.')
            return

        player_id = record['id']

        query = """SELECT teams.challonge
                   FROM team_members
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   WHERE team_members.player_id = $1
                   AND   teams.active
                   AND   teams.id <> $2;
                """

        record = await ctx.db.fetchrow(query, player_id, team['id'])
        if record is not None:
            return await ctx.send(f'This member is already part of the <https://challonge.com/teams/{record[0]}> team.')

        # Verify the member wants to be added to the team
        msg = f'Hello {member.mention}, {ctx.author.mention} would like to add you to <{team["challonge"]}>. Do you agree?'
        verify = await ctx.prompt(msg, delete_after=False, author_id=member.id)
        if not verify:
            return await ctx.send('Aborting.')

        query = """INSERT INTO team_members (player_id, team_id) VALUES ($1, $2)"""
        await ctx.db.execute(query, player_id, team['id'])
        await ctx.send('Successfully added member.')

        # transparently try to add roles depending on the tournament state
        participants = await self.challonge.participants()
        team_id = str(team['id'])
        participant_id = next((p['id'] for p in participants if p['misc'] == team_id and p['final_rank'] is None), None)
        if participant_id is None:
            return

        await member.add_roles(discord.Object(id=PARTICIPANT_ROLE))
        if self.tournament_state is TournamentState.pending:
            await member.add_roles(discord.Object(id=NOT_CHECKED_IN_ROLE))

        if self.tournament_state is TournamentState.underway:
            # see if they have a room active and add them there
            if not hasattr(self, '_participants'):
                await self.prepare_participant_cache()

            try:
                info = self._participants[participant_id]
            except KeyError:
                pass
            else:
                # add to the cache
                self._member_participants[member.id] = participant_id

            # add to the channel
            for channel_id, obj in self.config.get('round_info', {}).items():
                if obj['player1_id'] == participant_id or obj['player2_id'] == participant_id:
                    channel = ctx.guild.get_channel(int(channel_id))
                    if channel:
                        await channel.set_permissions(member, read_messages=True)

    @team.command(name='remove')
    async def team_remove(self, ctx, *, member: discord.Member):
        """Removes a member from your team."""

        team = await self.get_owned_team_info(ctx)
        if team is None:
            return await ctx.send('You do not own any team.')

        query = """DELETE FROM team_members
                   USING players
                   WHERE team_id = $1
                   AND players.id = team_members.player_id
                   AND players.discord_id = $2
                   RETURNING players.id
                """

        deleted = await ctx.db.fetchrow(query, team['id'], member.id)
        if not deleted:
            return await ctx.send('This member could not be removed. They might not be in your team.')

        await ctx.send('Removed member successfully.')

        # transparently try to remove roles depending on the tournament state
        participants = await self.challonge.participants()
        team_id = str(team['id'])
        participant_id = next((p['id'] for p in participants if p['misc'] == team_id and p['final_rank'] is None), None)
        if participant_id is None:
            return

        await member.remove_roles(discord.Object(id=PARTICIPANT_ROLE))
        if self.tournament_state is TournamentState.pending:
            await member.remove_roles(discord.Object(id=NOT_CHECKED_IN_ROLE))

        # we'll leave them in the room for now
        return

    @team.command(name='logo')
    async def team_logo(self, ctx, *, url=None):
        """Sets the logo for your team.

        You can upload an image to Discord directly if you want to.
        """

        team = await self.get_owned_team_info(ctx)
        if team is None:
            return await ctx.send('You do not own any team.')

        if url is None:
            if not ctx.message.attachments:
                return await ctx.send('No logo provided.')
            url = message.attachments[0].url

        actual_url = validate_url(url)
        if not actual_url:
            return await ctx.send('Invalid URL provided.')

        query = "UPDATE teams SET logo = $1 WHERE id = $2;"
        await ctx.db.execute(query, actual_url, team['id'])
        await ctx.send('Successfully updated team logo.')

    @team.command(name='captain')
    async def team_captain(self, ctx, *, member: discord.Member):
        """Transfers ownership of a team to another member.

        They must belong on the team for ownership to transfer.
        """

        team = await self.get_owned_team_info(ctx)
        if team is None:
            return await ctx.send('You do not own any team.')

        query = """SELECT player_id
                   FROM team_members
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE players.discord_id = $1
                   AND team_members.team_id = $2;
                """

        record = await ctx.db.fetchrow(query, member.id, team['id'])
        if record is None:
            return await ctx.send('Member does not belong to team.')

        query = """UPDATE team_members
                   SET captain = NOT captain
                   WHERE team_id = $1
                   AND player_id IN ($2, $3);
                """

        await ctx.db.execute(query, team['id'], record[0], team['owner_id'])
        await ctx.send('Successfully transferred ownership.')

    @team.command(name='show')
    async def team_show(self, ctx, *, url: Challonge):
        """Shows a team's info."""

        query = "SELECT * FROM teams WHERE challonge=$1;"
        record = await ctx.db.fetchrow(query, url.slug)

        if record is None:
            return await ctx.send('No info for this team!')

        team_info = await self.challonge.get_team_info(url.slug)
        e = discord.Embed(title=team_info.name, url=url.url, colour=0x19D719)

        if record['logo']:
            e.set_thumbnail(url=record['logo'])

        query = """SELECT players.discord_id, players.switch
                   FROM team_members
                   INNER JOIN players ON players.id = team_members.player_id
                   WHERE team_id=$1;
                """

        players = await ctx.db.fetch(query, record['id'])
        e.add_field(name='Active?', value='Yes' if record['active'] else 'No')

        for member_id, switch in players:
            member = ctx.guild.get_member(member_id)
            if member:
                e.add_field(name=str(member), value=switch, inline=False)

        await ctx.send(embed=e)

    @commands.group(invoke_without_command=True)
    @in_booyah_guild()
    async def player(self, ctx, *, member: discord.Member):
        """Manages your player profile."""

        query = """SELECT * FROM players WHERE discord_id=$1;"""
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            return await ctx.send('No info for this player.')

        query = """SELECT teams.challonge, team_members.captain
                   FROM team_members
                   INNER JOIN teams
                           ON teams.id = team_members.team_id
                   INNER JOIN players
                           ON players.id = team_members.player_id
                   WHERE teams.active
                   AND players.discord_id=$1
                """

        info = await ctx.db.fetchrow(query, member.id)
        e = discord.Embed()
        e.set_author(name=str(member), icon_url=member.avatar_url)

        if record['challonge']:
            e.url = f'https://challonge.com/users/{record["challonge"]}'

        challonge_url = f'https://challonge.com/teams/{info["challonge"]}' if info else 'None'
        e.add_field(name='Switch', value=record['switch'])
        e.add_field(name='Active Team', value=challonge_url)
        e.add_field(name='Captain?', value='Yes' if info and info['captain'] else 'No')
        await ctx.send(embed=e)

    @player.command(name='switch')
    @in_booyah_guild()
    async def player_switch(self, ctx, *, fc: fc_converter):
        """Sets your Nintendo Switch code for your player profile."""

        query = """INSERT INTO players (discord_id, switch)
                   VALUES ($1, $2)
                   ON CONFLICT (discord_id) DO
                   UPDATE SET switch = $2;
                """

        try:
            await ctx.db.execute(query, ctx.author.id, fc)
        except asyncpg.UniqueViolationError:
            await ctx.send('Someone already has this set as their switch code.')
        else:
            await ctx.send('Updated switch code.')


def setup(bot):
    bot.add_cog(Tournament(bot))
