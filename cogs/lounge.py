from discord.ext import commands
from .utils import checks
import aiohttp
import json

class CodeBlock:
    def __init__(self, argument):
        block, code = argument.split('\n', 1)
        if not block.startswith('```') and not code.endswith('```'):
            raise commands.BadArgument('Could not find a code block.')

        language = block[3:]
        self.command = self.get_command_from_language(language)
        self.source = code.rstrip('`')

    def get_command_from_language(self, language):
        commands = {
            'cpp': 'g++ -std=c++14 -O2 -Wall -Wextra -pedantic -pthread main.cpp && ./a.out',
            'c': 'mv main.cpp main.c && gcc -std=c11 -O2 -Wall -Wextra -pedantic main.c && ./a.out',
            'py': 'python main.cpp', # coliru has no python3
            'python': 'python main.cpp',
            'haskell': 'runhaskell main.cpp'
        }

        try:
            return commands[language.lower()]
        except KeyError as e:
            raise commands.BadArgument('Unknown language to compile for: {}'.format(e)) from e

class Lounge:
    """Commands for Lounge<C++> only.

    Don't abuse these.
    """

    def __init__(self, bot):
        self.bot = bot

    # allow it in Lounge<C++> and Discord API
    @commands.command(pass_context=True)
    @checks.is_in_servers('81384788765712384', '145079846832308224')
    async def coliru(self, ctx, *, code : CodeBlock):
        """Compiles code via Coliru.

        You have to pass in a codeblock with the language syntax
        either set to one of these:

        - cpp
        - python
        - py
        - haskell

        Anything else isn't supported. The C++ compiler uses g++ -std=c++14.

        Please don't spam this for Stacked's sake.
        """
        payload = {
            'cmd': code.command,
            'src': code.source
        }

        data = json.dumps(payload)

        async with aiohttp.post('http://coliru.stacked-crooked.com/compile', data=data) as resp:
            if resp.status != 200:
                await self.bot.say('Coliru did not respond in time.')
                return
            output = await resp.text()

            if len(output) < 1992:
                fmt = '```\n{}\n```'.format(output)
                await self.bot.say(fmt)
                return

            # output is too big so post it in gist
            async with aiohttp.post('http://coliru.stacked-crooked.com/share', data=data) as resp:
                if resp.status != 200:
                    await self.bot.say('Could not create coliru shared link')
                else:
                    shared_id = await resp.text()
                    await self.bot.say('Output too big. Coliru link: http://coliru.stacked-crooked.com/a/' + shared_id)

    @coliru.error
    async def coliru_error(self, error, ctx):
        if isinstance(error, commands.BadArgument):
            await self.bot.say(error)

def setup(bot):
    bot.add_cog(Lounge(bot))
