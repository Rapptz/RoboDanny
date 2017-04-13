class Plural:
    def __init__(self, **attr):
        iterator = attr.items()
        self.name, self.value = next(iter(iterator))

    def __str__(self):
        v = self.value
        if v > 1:
            return '%s %ss' % (v, self.name)
        return '%s %s' % (v, self.name)

def human_timedelta(dt):
    now = datetime.datetime.utcnow()
    delta = now - dt
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    years, days = divmod(days, 365)

    if years:
        if days:
            return '%s and %s ago' % (Plural(year=years), Plural(day=days))
        return '%s ago' % Plural(year=years)

    if days:
        if hours:
            return '%s and %s ago' % (Plural(day=days), Plural(hour=hours))
        return '%s ago' % Plural(day=days)

    if hours:
        if minutes:
            return '%s and %s ago' % (Plural(hour=hours), Plural(minute=minutes))
        return '%s ago' % Plural(hour=hours)

    if minutes:
        if seconds:
            return '%s and %s ago' % (Plural(minute=minutes), Plural(second=seconds))
        return '%s ago' % Plural(minute=minutes)
    return '%s ago' % Plural(second=seconds)

def to_checkmark(opt):
    return '<:vpGreenTick:257437292820561920>' if opt else '<:vpRedTick:257437215615877129>'

class Checkmark:
    def __init__(self, opt, label):
        self.opt = opt
        self.label = label

    def __str__(self):
        return '%s: %s' % (to_checkmark(self.opt), self.label)
