from discord.ext import commands
from .utils import checks
import aiohttp
import json

class CodeBlock:
    missing_error = 'Missing code block. Please use the following markdown\n\\`\\`\\`language\ncode here\n\\`\\`\\`'
    def __init__(self, argument):
        try:
            block, code = argument.split('\n', 1)
        except ValueError:
            raise commands.BadArgument(self.missing_error)

        if not block.startswith('```') and not code.endswith('```'):
            raise commands.BadArgument(self.missing_error)

        language = block[3:]
        self.command = self.get_command_from_language(language.lower())
        self.source = code.rstrip('`')

    def get_command_from_language(self, language):
        cmds = {
            'cpp': 'g++ -std=c++14 -O2 -Wall -Wextra -pedantic -pthread main.cpp && ./a.out',
            'c': 'mv main.cpp main.c && gcc -std=c11 -O2 -Wall -Wextra -pedantic main.c && ./a.out',
            'py': 'python main.cpp', # coliru has no python3
            'python': 'python main.cpp',
            'haskell': 'runhaskell main.cpp'
        }

        cpp = cmds['cpp']
        for alias in ('cc', 'h', 'c++', 'h++', 'hpp'):
            cmds[alias] = cpp
        try:
            return cmds[language]
        except KeyError as e:
            if language:
                fmt = 'Unknown language to compile for: {}'.format(language)
            else:
                fmt = 'Could not find a language to compile with.'
            raise commands.BadArgument(fmt) from e

class Lounge:
    """Commands made for Lounge<C++>.

    Don't abuse these.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.command(pass_context=True)
    async def coliru(self, ctx, *, code: CodeBlock):
        """Compiles code via Coliru.

        You have to pass in a code block with the language syntax
        either set to one of these:

        - cpp
        - c
        - python
        - py
        - haskell

        Anything else isn't supported. The C++ compiler uses g++ -std=c++14.

        The python support is only python2.7 (unfortunately).

        Please don't spam this for Stacked's sake.
        """
        payload = {
            'cmd': code.command,
            'src': code.source
        }

        data = json.dumps(payload)

        await self.bot.type()
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
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say(CodeBlock.missing_error)

def setup(bot):
    bot.add_cog(Lounge(bot))
