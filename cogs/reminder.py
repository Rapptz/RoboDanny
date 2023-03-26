from __future__ import annotations
import random
from typing import TYPE_CHECKING, Any, Optional, Sequence, NamedTuple
from typing_extensions import Annotated

from .utils import time, formats, cache, fuzzy
from discord.ext import commands
from discord import app_commands
from lxml import etree

# TODO: replace with ZoneInfo when upgrading to 3.9
import dateutil.tz
from dateutil.zoneinfo import get_zonefile_instance
import discord
import asyncio
import asyncpg
import datetime
import textwrap

if TYPE_CHECKING:
    from typing_extensions import Self
    from .utils.context import Context
    from bot import RoboDanny


class MaybeAcquire:
    def __init__(self, connection: Optional[asyncpg.Connection], *, pool: asyncpg.Pool) -> None:
        self.connection: Optional[asyncpg.Connection] = connection
        self.pool: asyncpg.Pool = pool
        self._cleanup: bool = False

    async def __aenter__(self) -> asyncpg.Connection:
        if self.connection is None:
            self._cleanup = True
            self._connection = c = await self.pool.acquire()
            return c
        return self.connection

    async def __aexit__(self, *args) -> None:
        if self._cleanup:
            await self.pool.release(self._connection)


class SnoozeModal(discord.ui.Modal, title='Snooze'):
    duration = discord.ui.TextInput(label='Duration', placeholder='10 minutes', default='10 minutes', min_length=2)

    def __init__(self, parent: ReminderView, cog: Reminder, timer: Timer) -> None:
        super().__init__()
        self.parent: ReminderView = parent
        self.timer: Timer = timer
        self.cog: Reminder = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            when = time.FutureTime(str(self.duration)).dt
        except Exception:
            await interaction.response.send_message(
                'Duration could not be parsed, sorry. Try something like "5 minutes" or "1 hour"', ephemeral=True
            )
            return

        self.parent.snooze.disabled = True
        await interaction.response.edit_message(view=self.parent)

        refreshed = await self.cog.create_timer(
            when, self.timer.event, *self.timer.args, **self.timer.kwargs, created=interaction.created_at
        )
        author_id, _, message = self.timer.args
        delta = time.human_timedelta(when, source=refreshed.created_at)
        await interaction.followup.send(
            f"Alright <@{author_id}>, I've snoozed your reminder for {delta}: {message}", ephemeral=True
        )


class SnoozeButton(discord.ui.Button['ReminderView']):
    def __init__(self, cog: Reminder, timer: Timer) -> None:
        super().__init__(label='Snooze', style=discord.ButtonStyle.blurple)
        self.timer: Timer = timer
        self.cog: Reminder = cog

    async def callback(self, interaction: discord.Interaction) -> Any:
        assert self.view is not None
        await interaction.response.send_modal(SnoozeModal(self.view, self.cog, self.timer))


class ReminderView(discord.ui.View):
    message: discord.Message

    def __init__(self, *, url: str, timer: Timer, cog: Reminder, author_id: int) -> None:
        super().__init__(timeout=300)
        self.author_id: int = author_id
        self.snooze = SnoozeButton(cog, timer)
        self.add_item(discord.ui.Button(url=url, label='Go to original message'))
        self.add_item(self.snooze)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message('This snooze button is not for you, sorry!', ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        self.snooze.disabled = True
        await self.message.edit(view=self)


class TimeZone(NamedTuple):
    label: str
    key: str

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        assert isinstance(ctx.cog, Reminder)

        # Prioritise aliases because they handle short codes slightly better
        if argument in ctx.cog._timezone_aliases:
            return cls(key=argument, label=ctx.cog._timezone_aliases[argument])

        if argument in ctx.cog.valid_timezones:
            return cls(key=argument, label=argument)

        timezones = ctx.cog.find_timezones(argument)

        try:
            return await ctx.disambiguate(timezones, lambda t: t[0], ephemeral=True)
        except ValueError:
            raise commands.BadArgument(f'Could not find timezone for {argument!r}')

    def to_choice(self) -> app_commands.Choice[str]:
        return app_commands.Choice(name=self.label, value=self.key)


class Timer:
    __slots__ = ('args', 'kwargs', 'event', 'id', 'created_at', 'expires', 'timezone')

    def __init__(self, *, record: asyncpg.Record):
        self.id: int = record['id']

        extra = record['extra']
        self.args: Sequence[Any] = extra.get('args', [])
        self.kwargs: dict[str, Any] = extra.get('kwargs', {})
        self.event: str = record['event']
        self.created_at: datetime.datetime = record['created']
        self.expires: datetime.datetime = record['expires']
        self.timezone: str = record['timezone']

    @classmethod
    def temporary(
        cls,
        *,
        expires: datetime.datetime,
        created: datetime.datetime,
        event: str,
        args: Sequence[Any],
        kwargs: dict[str, Any],
        timezone: str,
    ) -> Self:
        pseudo = {
            'id': None,
            'extra': {'args': args, 'kwargs': kwargs},
            'event': event,
            'created': created,
            'expires': expires,
            'timezone': timezone,
        }
        return cls(record=pseudo)

    def __eq__(self, other: object) -> bool:
        try:
            return self.id == other.id  # type: ignore
        except AttributeError:
            return False

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def human_delta(self) -> str:
        return time.format_relative(self.created_at)

    @property
    def author_id(self) -> Optional[int]:
        if self.args:
            return int(self.args[0])
        return None

    def __repr__(self) -> str:
        return f'<Timer created={self.created_at} expires={self.expires} event={self.event}>'


class CLDRDataEntry(NamedTuple):
    description: str
    aliases: list[str]
    deprecated: bool
    preferred: Optional[str]


class Reminder(commands.Cog):
    """Reminders to do something."""

    # CLDR identifiers for most common timezones for the default autocomplete drop down
    # n.b. limited to 25 choices
    DEFAULT_POPULAR_TIMEZONE_IDS = (
        # America
        'usnyc',  # America/New_York
        'uslax',  # America/Los_Angeles
        'uschi',  # America/Chicago
        'usden',  # America/Denver
        # India
        'inccu',  # Asia/Kolkata
        # Europe
        'trist',  # Europe/Istanbul
        'rumow',  # Europe/Moscow
        'gblon',  # Europe/London
        'frpar',  # Europe/Paris
        'esmad',  # Europe/Madrid
        'deber',  # Europe/Berlin
        'grath',  # Europe/Athens
        'uaiev',  # Europe/Kyev
        'itrom',  # Europe/Rome
        'nlams',  # Europe/Amsterdam
        'plwaw',  # Europe/Warsaw
        # Canada
        'cator',  # America/Toronto
        # Australia
        'aubne',  # Australia/Brisbane
        'ausyd',  # Australia/Sydney
        # Brazil
        'brsao',  # America/Sao_Paulo
        # Japan
        'jptyo',  # Asia/Tokyo
        # China
        'cnsha',  # Asia/Shanghai
    )

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self._have_data = asyncio.Event()
        self._current_timer: Optional[Timer] = None
        self._task = bot.loop.create_task(self.dispatch_timers())
        self.valid_timezones: set[str] = set(get_zonefile_instance().zones)
        # User-friendly timezone names, some manual and most from the CLDR database.
        self._timezone_aliases: dict[str, str] = {
            'Eastern Time': 'America/New_York',
            'Central Time': 'America/Chicago',
            'Mountain Time': 'America/Denver',
            'Pacific Time': 'America/Los_Angeles',
            # (Unfortunately) special case American timezone abbreviations
            'EST': 'America/New_York',
            'CST': 'America/Chicago',
            'MST': 'America/Denver',
            'PST': 'America/Los_Angeles',
            'EDT': 'America/New_York',
            'CDT': 'America/Chicago',
            'MDT': 'America/Denver',
            'PDT': 'America/Los_Angeles',
        }
        self._default_timezones: list[app_commands.Choice[str]] = []

    async def cog_load(self) -> None:
        await self.parse_bcp47_timezones()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{ALARM CLOCK}')

    def cog_unload(self) -> None:
        self._task.cancel()

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))
        if isinstance(error, commands.TooManyArguments):
            await ctx.send(f'You called the {ctx.command.name} command with too many arguments.')

    async def parse_bcp47_timezones(self) -> None:
        async with self.bot.session.get(
            'https://raw.githubusercontent.com/unicode-org/cldr/main/common/bcp47/timezone.xml'
        ) as resp:
            if resp.status != 200:
                return

            parser = etree.XMLParser(ns_clean=True, recover=True, encoding='utf-8')
            tree = etree.fromstring(await resp.read(), parser=parser)

            # Build a temporary dictionary to resolve "preferred" mappings
            entries: dict[str, CLDRDataEntry] = {
                node.attrib['name']: CLDRDataEntry(
                    description=node.attrib['description'],
                    aliases=node.get('alias', 'Etc/Unknown').split(' '),
                    deprecated=node.get('deprecated', 'false') == 'true',
                    preferred=node.get('preferred'),
                )
                for node in tree.iter('type')
                # Filter the Etc/ entries (except UTC)
                if not node.attrib['name'].startswith(('utcw', 'utce', 'unk'))
                and not node.attrib['description'].startswith('POSIX')
            }

            for entry in entries.values():
                # These use the first entry in the alias list as the "canonical" name to use when mapping the
                # timezone to the IANA database.
                # The CLDR database is not particularly correct when it comes to these, but neither is the IANA database.
                # It turns out the notion of a "canonical" name is a bit of a mess. This works fine for users where
                # this is only used for display purposes, but it's not ideal.
                if entry.preferred is not None:
                    preferred = entries.get(entry.preferred)
                    if preferred is not None:
                        self._timezone_aliases[entry.description] = preferred.aliases[0]
                else:
                    self._timezone_aliases[entry.description] = entry.aliases[0]

            for key in self.DEFAULT_POPULAR_TIMEZONE_IDS:
                entry = entries.get(key)
                if entry is not None:
                    self._default_timezones.append(app_commands.Choice(name=entry.description, value=entry.aliases[0]))

    @cache.cache()
    async def get_timezone(self, user_id: int, /) -> Optional[str]:
        query = "SELECT timezone from user_settings WHERE id = $1;"
        record = await self.bot.pool.fetchrow(query, user_id)
        return record['timezone'] if record else None

    async def get_tzinfo(self, user_id: int, /) -> datetime.tzinfo:
        tz = await self.get_timezone(user_id)
        if tz is None:
            return datetime.timezone.utc
        return dateutil.tz.gettz(tz) or datetime.timezone.utc

    def find_timezones(self, query: str) -> list[TimeZone]:
        # A bit hacky, but if '/' is in the query then it's looking for a raw identifier
        # otherwise it's looking for a CLDR alias
        if '/' in query:
            return [TimeZone(key=a, label=a) for a in fuzzy.finder(query, self.valid_timezones)]

        keys = fuzzy.finder(query, self._timezone_aliases.keys())
        return [TimeZone(label=k, key=self._timezone_aliases[k]) for k in keys]

    async def get_active_timer(self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7) -> Optional[Timer]:
        query = """
            SELECT * FROM reminders
            WHERE (expires AT TIME ZONE 'UTC' AT TIME ZONE timezone) < (CURRENT_TIMESTAMP + $1::interval)
            ORDER BY expires
            LIMIT 1;
        """
        con = connection or self.bot.pool

        record = await con.fetchrow(query, datetime.timedelta(days=days))
        return Timer(record=record) if record else None

    async def wait_for_active_timers(self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7) -> Timer:
        async with MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            timer = await self.get_active_timer(connection=con, days=days)
            if timer is not None:
                self._have_data.set()
                return timer

            self._have_data.clear()
            self._current_timer = None
            await self._have_data.wait()

            # At this point we always have data
            return await self.get_active_timer(connection=con, days=days)  # type: ignore

    async def call_timer(self, timer: Timer) -> None:
        # delete the timer
        query = "DELETE FROM reminders WHERE id=$1;"
        await self.bot.pool.execute(query, timer.id)

        # dispatch the event
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self) -> None:
        try:
            while not self.bot.is_closed():
                # can only asyncio.sleep for up to ~48 days reliably
                # so we're gonna cap it off at 40 days
                # see: http://bugs.python.org/issue20493
                timer = self._current_timer = await self.wait_for_active_timers(days=40)
                now = datetime.datetime.utcnow()

                if timer.expires >= now:
                    to_sleep = (timer.expires - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.call_timer(timer)
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    async def short_timer_optimisation(self, seconds: float, timer: Timer) -> None:
        await asyncio.sleep(seconds)
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def get_timer(self, event: str, /, **kwargs: Any) -> Optional[Timer]:
        r"""Gets a timer from the database.

        Note you cannot find a database by its expiry or creation time.

        Parameters
        -----------
        event: str
            The name of the event to search for.
        \*\*kwargs
            Keyword arguments to search for in the database.

        Returns
        --------
        Optional[:class:`Timer`]
            The timer if found, otherwise None.
        """

        filtered_clause = [f"extra #>> ARRAY['kwargs', '{key}'] = ${i}" for (i, key) in enumerate(kwargs.keys(), start=2)]
        query = f"SELECT * FROM reminders WHERE event = $1 AND {' AND '.join(filtered_clause)} LIMIT 1"
        record = await self.bot.pool.fetchrow(query, event, *kwargs.values())
        return Timer(record=record) if record else None

    async def delete_timer(self, event: str, /, **kwargs: Any) -> None:
        r"""Deletes a timer from the database.

        Note you cannot find a database by its expiry or creation time.

        Parameters
        -----------
        event: str
            The name of the event to search for.
        \*\*kwargs
            Keyword arguments to search for in the database.
        """

        filtered_clause = [f"extra #>> ARRAY['kwargs', '{key}'] = ${i}" for (i, key) in enumerate(kwargs.keys(), start=2)]
        query = f"DELETE FROM reminders WHERE event = $1 AND {' AND '.join(filtered_clause)} RETURNING id"
        record: Any = await self.bot.pool.fetchrow(query, event, *kwargs.values())

        # if the current timer is being deleted
        if record is not None and self._current_timer and self._current_timer.id == record['id']:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    async def create_timer(self, when: datetime.datetime, event: str, /, *args: Any, **kwargs: Any) -> Timer:
        r"""Creates a timer.

        Parameters
        -----------
        when: datetime.datetime
            When the timer should fire.
        event: str
            The name of the event to trigger.
            Will transform to 'on_{event}_timer_complete'.
        \*args
            Arguments to pass to the event
        \*\*kwargs
            Keyword arguments to pass to the event
        connection: asyncpg.Connection
            Special keyword-only argument to use a specific connection
            for the DB request.
        created: datetime.datetime
            Special keyword-only argument to use as the creation time.
            Should make the timedeltas a bit more consistent.
        timezone: str
            Special keyword-only argument to use as the timezone for the
            expiry time. This automatically adjusts the expiry time to be
            in the future, should it be in the past.

        Note
        ------
        Arguments and keyword arguments must be JSON serialisable.

        Returns
        --------
        :class:`Timer`
        """

        pool = self.bot.pool

        try:
            now = kwargs.pop('created')
        except KeyError:
            now = discord.utils.utcnow()

        timezone_name = kwargs.pop('timezone', 'UTC')
        # Remove timezone information since the database does not deal with it
        when = when.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)

        timer = Timer.temporary(event=event, args=args, kwargs=kwargs, expires=when, created=now, timezone=timezone_name)
        delta = (when - now).total_seconds()
        if delta <= 60:
            # a shortcut for small timers
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        query = """INSERT INTO reminders (event, extra, expires, created, timezone)
                   VALUES ($1, $2::jsonb, $3, $4, $5)
                   RETURNING id;
                """

        row = await pool.fetchrow(query, event, {'args': args, 'kwargs': kwargs}, when, now, timezone_name)
        timer.id = row[0]

        # only set the data check if it can be waited on
        if delta <= (86400 * 40):  # 40 days
            self._have_data.set()

        # check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    @commands.hybrid_group(aliases=['timer', 'remind', 'remindme'], usage='<when>')
    async def reminder(
        self,
        ctx: Context,
        *,
        when: Annotated[time.FriendlyTimeResult, time.UserFriendlyTime(commands.clean_content, default='…')],
    ):
        """Reminds you of something after a certain amount of time.

        The input can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:

        - "next thursday at 3pm do something funny"
        - "do the dishes tomorrow"
        - "in 3 days do the thing"
        - "2d unmute someone"

        Times are in UTC unless a timezone is specified
        using the "timezone set" command.
        """

        zone = await self.get_timezone(ctx.author.id)
        timer = await self.create_timer(
            when.dt,
            'reminder',
            ctx.author.id,
            ctx.channel.id,
            when.arg,
            created=ctx.message.created_at,
            message_id=ctx.message.id,
            timezone=zone or 'UTC',
        )
        delta = time.human_timedelta(when.dt, source=timer.created_at)
        msg = f"Alright {ctx.author.mention}, in {delta}: {when.arg}"
        # 10% chance of advertising timezone support (temporarily...?)
        if zone is None and random.randint(0, 10) == 5:
            msg = f'{msg}\n\n\N{ELECTRIC LIGHT BULB} Did you know you can set your timezone with "{ctx.prefix}timezone set"?'

        await ctx.send(msg)

    @reminder.app_command.command(name='set')
    @app_commands.describe(when='When to be reminded of something.', text='What to be reminded of')
    async def reminder_set(
        self,
        interaction: discord.Interaction,
        when: app_commands.Transform[datetime.datetime, time.TimeTransformer],
        text: str = '…',
    ):
        """Sets a reminder to remind you of something at a specific time."""

        zone = await self.get_timezone(interaction.user.id)
        timer = await self.create_timer(
            when,
            'reminder',
            interaction.user.id,
            interaction.channel_id,
            text,
            created=interaction.created_at,
            message_id=None,
            timezone=zone or 'UTC',
        )
        delta = time.human_timedelta(when, source=timer.created_at)
        await interaction.response.send_message(f"Alright {interaction.user.mention}, in {delta}: {text}")

    @reminder_set.error
    async def reminder_set_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, time.BadTimeTransform):
            await interaction.response.send_message(str(error), ephemeral=True)

    @reminder.command(name='list', ignore_extra=False)
    async def reminder_list(self, ctx: Context):
        """Shows the 10 latest currently running reminders."""
        query = """SELECT id, expires, extra #>> '{args,2}'
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires
                   LIMIT 10;
                """

        records = await ctx.db.fetch(query, str(ctx.author.id))

        if len(records) == 0:
            return await ctx.send('No currently running reminders.')

        e = discord.Embed(colour=discord.Colour.blurple(), title='Reminders')

        if len(records) == 10:
            e.set_footer(text='Only showing up to 10 reminders.')
        else:
            e.set_footer(text=f'{len(records)} reminder{"s" if len(records) > 1 else ""}')

        for _id, expires, message in records:
            shorten = textwrap.shorten(message, width=512)
            e.add_field(name=f'{_id}: {time.format_relative(expires)}', value=shorten, inline=False)

        await ctx.send(embed=e)

    @reminder.command(name='delete', aliases=['remove', 'cancel'], ignore_extra=False)
    async def reminder_delete(self, ctx: Context, *, id: int):
        """Deletes a reminder by its ID.

        To get a reminder ID, use the reminder list command.

        You must own the reminder to delete it, obviously.
        """

        query = """DELETE FROM reminders
                   WHERE id=$1
                   AND event = 'reminder'
                   AND extra #>> '{args,0}' = $2;
                """

        status = await ctx.db.execute(query, id, str(ctx.author.id))
        if status == 'DELETE 0':
            return await ctx.send('Could not delete any reminders with that ID.')

        # if the current timer is being deleted
        if self._current_timer and self._current_timer.id == id:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        await ctx.send('Successfully deleted reminder.', ephemeral=True)

    @reminder.command(name='clear', ignore_extra=False)
    async def reminder_clear(self, ctx: Context):
        """Clears all reminders you have set."""

        # For UX purposes this has to be two queries.

        query = """SELECT COUNT(*)
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1;
                """

        author_id = str(ctx.author.id)
        total: asyncpg.Record = await ctx.db.fetchrow(query, author_id)
        total = total[0]
        if total == 0:
            return await ctx.send('You do not have any reminders to delete.')

        confirm = await ctx.prompt(f'Are you sure you want to delete {formats.plural(total):reminder}?')
        if not confirm:
            return await ctx.send('Aborting', ephemeral=True)

        query = """DELETE FROM reminders WHERE event = 'reminder' AND extra #>> '{args,0}' = $1;"""
        await ctx.db.execute(query, author_id)

        # Check if the current timer is the one being cleared and cancel it if so
        if self._current_timer and self._current_timer.author_id == ctx.author.id:
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        await ctx.send(f'Successfully deleted {formats.plural(total):reminder}.', ephemeral=True)

    @commands.hybrid_group()
    async def timezone(self, ctx: Context):
        """Commands related to managing or retrieving timezone info."""
        await ctx.send_help(ctx.command)

    @timezone.command(name='set')
    @app_commands.describe(tz='The timezone to change to.')
    async def timezone_set(self, ctx: Context, *, tz: TimeZone):
        """Sets your timezone.

        This is used to convert times to your local timezone when
        using the reminder command and other miscellaneous commands
        such as tempblock, tempmute, etc.
        """

        await ctx.db.execute(
            """INSERT INTO user_settings (id, timezone)
               VALUES ($1, $2)
               ON CONFLICT (id) DO UPDATE SET timezone = $2;
            """,
            ctx.author.id,
            tz.key,
        )

        self.get_timezone.invalidate(self, ctx.author.id)
        await ctx.send(f'Your timezone has been set to {tz.label} (IANA ID: {tz.key}).', ephemeral=True, delete_after=10)

    @timezone_set.autocomplete('tz')
    async def timezone_set_autocomplete(
        self, interaction: discord.Interaction, argument: str
    ) -> list[app_commands.Choice[str]]:
        if not argument:
            return self._default_timezones
        matches = self.find_timezones(argument)
        return [tz.to_choice() for tz in matches[:25]]

    @timezone.command(name='get')
    @app_commands.describe(user='The member to get the timezone of. Defaults to yourself.')
    async def timezone_get(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows the timezone of a user."""
        self_query = user.id == ctx.author.id
        tz = await self.get_timezone(user.id)
        if tz is None:
            return await ctx.send(f'{user} has not set their timezone.')

        time = discord.utils.utcnow().astimezone(dateutil.tz.gettz(tz)).strftime('%Y-%m-%d %I:%M %p')
        if self_query:
            msg = await ctx.send(f'Your timezone is {tz!r}. The current time is {time}.')
            await asyncio.sleep(5)
            await msg.edit(content=f'Your current time is {time}.')
        else:
            await ctx.send(f'The current time for {user} is {time}.')

    @timezone.command(name='clear')
    async def timezone_clear(self, ctx: Context):
        """Clears your timezone."""
        await ctx.db.execute("UPDATE user_settings SET timezone = NULL WHERE id=$1", ctx.author.id)
        self.get_timezone.invalidate(self, ctx.author.id)
        await ctx.send('Your timezone has been cleared.', ephemeral=True)

    @commands.Cog.listener()
    async def on_reminder_timer_complete(self, timer: Timer):
        author_id, channel_id, message = timer.args

        try:
            channel = self.bot.get_channel(channel_id) or (await self.bot.fetch_channel(channel_id))
        except discord.HTTPException:
            return

        guild_id = channel.guild.id if isinstance(channel, (discord.TextChannel, discord.Thread)) else '@me'
        message_id = timer.kwargs.get('message_id')
        msg = f'<@{author_id}>, {timer.human_delta}: {message}'
        view = discord.utils.MISSING

        if message_id:
            url = f'https://discord.com/channels/{guild_id}/{channel.id}/{message_id}'
            view = ReminderView(url=url, timer=timer, cog=self, author_id=author_id)

        try:
            msg = await channel.send(msg, view=view)  # type: ignore
        except discord.HTTPException:
            return
        else:
            if view is not discord.utils.MISSING:
                view.message = msg


async def setup(bot):
    await bot.add_cog(Reminder(bot))
