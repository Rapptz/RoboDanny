async def entry_to_code(bot, entries):
    width = max(map(lambda t: len(t[0]), entries))
    output = ['```']
    fmt = '{0:<{width}}: {1}'
    for name, entry in entries:
        output.append(fmt.format(name, entry, width=width))
    output.append('```')
    await bot.say('\n'.join(output))

import datetime

async def indented_entry_to_code(bot, entries):
    width = max(map(lambda t: len(t[0]), entries))
    output = ['```']
    fmt = '\u200b{0:>{width}}: {1}'
    for name, entry in entries:
        output.append(fmt.format(name, entry, width=width))
    output.append('```')
    await bot.say('\n'.join(output))

async def too_many_matches(bot, msg, matches, entry):
    check = lambda m: m.content.isdigit()
    await bot.say('There are too many matches... Which one did you mean? **Only say the number**.')
    await bot.say('\n'.join(map(entry, enumerate(matches, 1))))

    # only give them 3 tries.
    for i in range(3):
        message = await bot.wait_for_message(author=msg.author, channel=msg.channel, check=check)
        index = int(message.content)
        try:
            return matches[index - 1]
        except:
            await bot.say('Please give me a valid number. {} tries remaining...'.format(2 - i))

    raise ValueError('Too many tries. Goodbye.')

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
