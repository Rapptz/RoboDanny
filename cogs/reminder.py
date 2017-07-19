from .utils import checks, db, time
from discord.ext import commands
import discord
import asyncio
import asyncpg
import datetime

class Reminders(db.Table):
    id = db.PrimaryKeyColumn()

    expires = db.Column(db.Datetime, index=True)
    created = db.Column(db.Datetime, default="now() at time zone 'utc'")
    event = db.Column(db.String)
    extra = db.Column(db.JSON, default="'{}'::jsonb")

class Timer:
    __slots__ = ('args', 'kwargs', 'event', 'id', 'created_at', 'expires')

    def __init__(self, *, record):
        self.id = record['id']

        extra = record['extra']
        self.args = extra.get('args', [])
        self.kwargs = extra.get('kwargs', {})
        self.event = record['event']
        self.created_at = record['created']
        self.expires = record['expires']

    @classmethod
    def temporary(cls, *, expires, created, event, args, kwargs):
        pseudo = {
            'id': None,
            'extra': { 'args': args, 'kwargs': kwargs },
            'event': event,
            'created': created,
            'expires': expires
        }
        return cls(record=pseudo)

    def __eq__(self, other):
        try:
            return self.id == other.id
        except AttributeError:
            return False

    def __hash__(self):
        return hash(self.id)

    @property
    def human_delta(self):
        return time.human_timedelta(self.created_at)

    def __repr__(self):
        return f'<Timer created={self.created_at} expires={self.expires} event={self.event}>'

class Reminder:
    """Reminders to do something."""

    def __init__(self, bot):
        self.bot = bot
        self._have_data = asyncio.Event(loop=bot.loop)
        self._current_timer = None
        self._task = bot.loop.create_task(self.dispatch_timers())

    def __unload(self):
        self._task.cancel()

    async def __error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    async def get_active_timers(self, *, connection=None, days=7):
        query = "SELECT * FROM reminders WHERE expires < (CURRENT_DATE + $1::interval) ORDER BY expires;"
        con = connection or self.bot.pool

        records = await con.fetch(query, datetime.timedelta(days=days))
        return [Timer(record=a) for a in records]

    async def wait_for_active_timers(self, *, connection=None, days=7):
        async with db.MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            timers = await self.get_active_timers(connection=con, days=days)
            if len(timers):
                self._have_data.set()
                return timers

            self._have_data.clear()
            self._current_timer = None
            await self._have_data.wait()
            return await self.get_active_timers(connection=con, days=days)

    async def call_timer(self, timer):
        # delete the timer
        query = "DELETE FROM reminders WHERE id=$1;"
        await self.bot.pool.execute(query, timer.id)

        # dispatch the event
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self):
        try:
            while not self.bot.is_closed():
                # can only asyncio.sleep for up to ~48 days reliably
                # so we're gonna cap it off at 40 days
                # see: http://bugs.python.org/issue20493
                timers = await self.wait_for_active_timers(days=40)
                timer = self._current_timer = timers[0]
                now = datetime.datetime.utcnow()

                if timer.expires >= now:
                    to_sleep = (timer.expires - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.call_timer(timer)
        except asyncio.CancelledError:
            pass
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    async def short_timer_optimisation(self, seconds, timer):
        await asyncio.sleep(seconds)
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def create_timer(self, *args, **kwargs):
        """Creates a timer.

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

        Note
        ------
        Arguments and keyword arguments must be JSON serialisable.

        Returns
        --------
        :class:`Timer`
        """
        when, event, *args = args

        try:
            connection = kwargs.pop('connection')
        except KeyError:
            connection = self.bot.pool

        now = datetime.datetime.utcnow()
        timer = Timer.temporary(event=event, args=args, kwargs=kwargs, expires=when, created=now)
        delta = (when - now).total_seconds()
        if delta <= 60:
            # a shortcut for small timers
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        query = """INSERT INTO reminders (event, extra, expires)
                   VALUES ($1, $2::jsonb, $3)
                   RETURNING id;
                """

        row = await connection.fetchrow(query, event, { 'args': args, 'kwargs': kwargs }, when)
        timer.id = row[0]

        self._have_data.set()

        # check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    @commands.command(aliases=['timer', 'remind'], usage='<when>')
    async def reminder(self, ctx, *, when: time.UserFriendlyTime(commands.clean_content, default='something')):
        """Reminds you of something after a certain amount of time.

        The input can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:

        - "next thursday at 3pm do something funny"
        - "do the dishes tomorrow"
        - "in 3 days do the thing"
        - "2d unmute someone"

        Times are in UTC.
        """

        timer = await self.create_timer(when.dt, 'reminder', ctx.author.id, ctx.channel.id, when.arg, connection=ctx.db)
        delta = time.human_timedelta(when.dt, source=ctx.message.created_at)
        await ctx.send(f"Alright {ctx.author.mention}, I'll remind you about {when.arg} in {delta}.")

    async def on_reminder_timer_complete(self, timer):
        author_id, channel_id, message = timer.args

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            # peculiar
            return

        await channel.send(f'<@{author_id}>, {timer.human_delta} you asked to be reminded of {message}.')

def setup(bot):
    bot.add_cog(Reminder(bot))
