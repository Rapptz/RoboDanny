from .utils import config, checks, time
from discord.ext import commands
import discord
import asyncio
import datetime
import json

DISCORD_EPOCH = discord.utils.DISCORD_EPOCH

class Timer:
    __slots__ = ('args', 'event', 'id', 'created', '_expires')

    def __init__(self, *, id, args, event, created, **kwargs):
        self.id = id
        self.args = args
        self.event = event

        if isinstance(created, datetime.datetime):
            created = created.timestamp()

        self.created = created

    def __eq__(self, other):
        try:
            return self.id == other.id
        except AttributeError:
            return False

    def __hash__(self):
        return hash(self.id)

    @discord.utils.cached_slot_property('_expires')
    def expires(self):
        return discord.utils.snowflake_time(self.id)

    @property
    def created_at(self):
        return datetime.datetime.fromtimestamp(self.created)

    @property
    def human_delta(self):
        return time.human_timedelta(self.created_at)

    def to_json(self):
        return {
            'args': self.args,
            'event': self.event,
            'id': self.id,
            'created': self.created,
        }

    def __repr__(self):
        return f'<Timer created={self.created_at} expires={self.expires} event={self.event}>'

    @classmethod
    def from_json(cls, obj):
        try:
            obj['id']
        except KeyError:
            return obj
        return cls(**obj)

class Reminder:
    """Reminders to do something."""

    def __init__(self, bot):
        self.bot = bot
        # 'data' has the timers,
        # 'count' has the total number of timers since inception
        #  used to generate the timer IDs
        self.timers = config.Config('reminders.json', hook=Timer)
        self._have_data = asyncio.Event(loop=bot.loop)
        self._current_timer = None
        self._task = bot.loop.create_task(self.dispatch_timers())

    def __unload(self):
        self._task.cancel()

    async def __error(self, ctx, error):
        import traceback
        traceback.print_exc()
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    def get_active_timers(self, *, days=7):
        data = self.timers.get('data', [])
        threshold = datetime.datetime.utcnow() + datetime.timedelta(days=days)
        a = [o for o in data if o.expires < threshold]
        a.sort(key=lambda x: x.expires)
        return a

    async def wait_for_active_timers(self, *, days=7):
        timers = self.get_active_timers(days=days)
        if len(timers):
            self._have_data.set()
            return timers

        self._have_data.clear()
        self._current_timer = None
        await self._have_data.wait()
        return self.get_active_timers(days=days)

    async def call_timer(self, timer):
        # remove timer from the storage
        data = self.timers.get('data', [])
        try:
            data.remove(timer)
        except ValueError:
            # wtf?
            pass

        await self.timers.put('data', data)

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

    async def short_timer_optimisation(self, seconds, timer):
        await asyncio.sleep(seconds)
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def create_timer(self, when, event, *args):
        total = self.timers.get('count', 0)
        data = self.timers.get('data', [])

        now = datetime.datetime.utcnow()
        unix_seconds = (when - datetime.datetime(1970, 1, 1)).total_seconds()
        ms = int(unix_seconds * 1000 - DISCORD_EPOCH)
        timer_id = (ms << 22) + total

        timer = Timer(id=timer_id, event=event, args=args, created=now)

        delta = (when - now).total_seconds()
        if delta <= 60:
            # a shortcut for small timers
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        await self.timers.put('count', total + 1)
        data.append(timer)
        await self.timers.put('data', data)
        self._have_data.set()

        # check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    @commands.command(aliases=['timer'])
    async def reminder(self, ctx, when: time.FutureTime, *, message: commands.clean_content = 'something'):
        """Reminds you of something after a certain amount of time.

        The time can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:

        - "next thursday at 3pm"
        - "tomorrow"
        - "3 days"
        - "2d"

        Times are in UTC.
        """

        timer = await self.create_timer(when.dt, 'reminder', ctx.author.id, ctx.channel.id, message)
        delta = time.human_timedelta(when.dt)
        await ctx.send(f"Alright {ctx.author.mention}, I'll remind you about {message} in {delta}.")

    async def on_reminder_timer_complete(self, timer):
        author_id, channel_id, message = timer.args

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            # peculiar
            return

        await channel.send(f'<@{author_id}>, {timer.human_delta} you asked to be reminded of {message}.')

def setup(bot):
    bot.add_cog(Reminder(bot))
