from discord.ext import commands
import asyncio

class Context(commands.Context):
    async def entry_to_code(self, entries):
        width = max(map(lambda t: len(t[0]), entries))
        output = ['```']
        fmt = '{0:<{width}}: {1}'
        for name, entry in entries:
            output.append(fmt.format(name, entry, width=width))
        output.append('```')
        await self.send('\n'.join(output))

    async def indented_entry_to_code(self, entries):
        width = max(map(lambda t: len(t[0]), entries))
        output = ['```']
        fmt = '\u200b{0:>{width}}: {1}'
        for name, entry in entries:
            output.append(fmt.format(name, entry, width=width))
        output.append('```')
        await self.send('\n'.join(output))

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
                await self.send('Please give me a valid number. {} tries remaining...'.format(2 - i))

        raise ValueError('Too many tries. Goodbye.')
