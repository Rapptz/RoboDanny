class Plural:
    def __init__(self, **attr):
        iterator = attr.items()
        self.name, self.value = next(iter(iterator))

    def __str__(self):
        v = self.value
        if v > 1:
            return f'{v} {self.name}s' % (v, self.name)
        return f'{v} {self.name}'

def human_timedelta(dt):
    now = datetime.datetime.utcnow()
    delta = now - dt
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    years, days = divmod(days, 365)

    if years:
        if days:
            return f'{Plural(year=years)} and {Plural(day=days)} ago'
        return f'{Plural(year=years)} ago'

    if days:
        if hours:
            return f'{Plural(day=days)} and {Plural(hour=hours)} ago'
        return f'{Plural(day=days)} ago'

    if hours:
        if minutes:
            return f'{Plural(hour=hours)} and {Plural(minute=minutes)} ago'
        return f'{Plural(hour=hours)} ago'

    if minutes:
        if seconds:
            return f'{Plural(minute=minutes)} and {Plural(second=seconds)} ago'
        return f'{Plural(minute=minutes)} ago'
    return f'{Plural(second=seconds)} ago'


class TabularData:
    def __init__(self):
        self._widths = []
        self._columns = []
        self._rows = []

    def set_columns(self, columns):
        self._columns = columns
        self._widths = [len(c) + 2 for c in columns]

    def add_row(self, row):
        rows = [str(r) for r in row]
        self._rows.append(rows)
        for index, element in enumerate(rows):
            width = len(element) + 2
            if width > self._widths[index]:
                self._widths[index] = width

    def add_rows(self, rows):
        for row in rows:
            self.add_row(row)

    def render(self):
        """Renders a table in rST format.

        Example:

        +-------+-----+
        | Name  | Age |
        +-------+-----+
        | Alice | 24  |
        |  Bob  | 19  |
        +-------+-----+
        """

        sep = '+'.join('-' * w for w in self._widths)
        sep = f'+{sep}+'

        to_draw = [sep]

        def get_entry(d):
            elem = '|'.join(f'{e:^{self._widths[i]}}' for i, e in enumerate(d))
            return f'|{elem}|'

        to_draw.append(get_entry(self._columns))
        to_draw.append(sep)

        for row in self._rows:
            to_draw.append(get_entry(row))

        to_draw.append(sep)
        return '\n'.join(to_draw)
