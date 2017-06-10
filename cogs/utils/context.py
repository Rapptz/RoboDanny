from discord.ext import commands
import asyncio

class Context(commands.Context):
    async def entry_to_code(self, entries):
        width = max(len(a) for a, b in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'{name:<{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    async def indented_entry_to_code(self, entries):
        width = max(len(a) for a, b in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'\u200b{name:>{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    def __repr__(self):
        # we need this for our cache key strategy
        return '<Context>'

    @property
    def session(self):
        return self.bot.session

    async def too_many_matches(self, matches, entry):
        await self.send('There are too many matches... Which one did you mean? **Only say the number**.')
        await self.send('\n'.join(map(entry, enumerate(matches, 1))))

        def check(m):
            return m.content.isdigit() and m.author.id == ctx.author.id and m.channel == ctx.channel.id

        # only give them 3 tries.
        for i in range(3):
            try:
                message = await self.wait_for('message', check=check, timeout=10.0)
            except asyncio.TimeoutError:
                raise ValueError('Took too long. Goodbye.')

            index = int(message.content)
            try:
                return matches[index - 1]
            except:
                await self.send(f'Please give me a valid number. {2 - i} tries remaining...')

        raise ValueError('Too many tries. Goodbye.')

    def tick(self, opt, label=None):
        emoji = '<:check:316583761540022272>' if opt else '<:xmark:316583761699536896>'
        if label is not None:
            return f'{emoji}: {label}'
        return emoji
