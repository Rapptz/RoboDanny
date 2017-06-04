import datetime
import parsedatetime as pdt
from dateutil.relativedelta import relativedelta
from .formats import Plural
from discord.ext import commands
import re

class ShortTime:
    compiled = re.compile("""(?:(?P<years>[0-9])(?:years?|y))?             # e.g. 2y
                             (?:(?P<months>[0-9]{1,2})(?:months?|mo))?     # e.g. 2months
                             (?:(?P<weeks>[0-9]{1,4})(?:weeks?|w))?        # e.g. 10w
                             (?:(?P<days>[0-9]{1,5})(?:days?|d))?          # e.g. 14d
                             (?:(?P<hours>[0-9]{1,5})(?:hours?|h))?        # e.g. 12h
                             (?:(?P<minutes>[0-9]{1,5})(?:minutes?|m))?    # e.g. 10m
                             (?:(?P<seconds>[0-9]{1,5})(?:seconds?|s))?    # e.g. 15s
                          $""", re.VERBOSE)

    def __init__(self, argument):
        match = self.compiled.match(argument)
        if match is None or not match.group(0):
            raise commands.BadArgument('invalid time provided')

        data = { k: int(v) for k, v in match.groupdict(default=0).items() }
        now = datetime.datetime.utcnow()
        self.dt = now + relativedelta(**data)

class HumanTime:
    calendar = pdt.Calendar(version=pdt.VERSION_CONTEXT_STYLE)

    def __init__(self, argument):
        now = datetime.datetime.utcnow()
        dt, status = self.calendar.parseDT(argument, sourceTime=now)
        if not status.hasDateOrTime:
            raise commands.BadArgument('invalid time provided, try e.g. "tomorrow" or "3 days"')

        if not status.hasTime:
            # replace it with the current time
            dt = dt.replace(hour=now.hour, minute=now.minute, second=now.second, microsecond=now.microsecond)

        self.dt = dt
        self._past = dt < now

class Time(HumanTime):
    def __init__(self, argument):
        try:
            o = ShortTime(argument)
        except Exception as e:
            super().__init__(argument)
        else:
            self.dt = o.dt
            self._past = False

class FutureTime(Time):
    def __init__(self, argument):
        super().__init__(argument)

        if self._past:
            raise commands.BadArgument('this time is in the past')

def human_timedelta(dt):
    now = datetime.datetime.utcnow()
    if dt > now:
        delta = dt - now
        suffix = ''
    else:
        delta = now - dt
        suffix = ' ago'

    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    years, days = divmod(days, 365)

    if years:
        if days:
            return f'{Plural(year=years)} and {Plural(day=days)}{suffix}'
        return f'{Plural(year=years)}{suffix}'

    if days:
        if hours:
            return f'{Plural(day=days)} and {Plural(hour=hours)}{suffix}'
        return f'{Plural(day=days)}{suffix}'

    if hours:
        if minutes:
            return f'{Plural(hour=hours)} and {Plural(minute=minutes)}{suffix}'
        return f'{Plural(hour=hours)}{suffix}'

    if minutes:
        if seconds:
            return f'{Plural(minute=minutes)} and {Plural(second=seconds)}{suffix}'
        return f'{Plural(minute=minutes)}{suffix}'
    return f'{Plural(second=seconds)}{suffix}'
