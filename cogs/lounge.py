from discord.ext import commands
from .utils import checks
import aiohttp
import asyncio
import json
import discord
from lxml import etree

LOUNGE_GUILD_ID = 145079846832308224
META_CHANNEL_ID = 335119551702499328

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
        self.source = code.rstrip('`').replace('```', '')

    def get_command_from_language(self, language):
        cmds = {
            'cpp': 'g++ -std=c++1z -O2 -Wall -Wextra -pedantic -pthread main.cpp -lstdc++fs && ./a.out',
            'c': 'mv main.cpp main.c && gcc -std=c11 -O2 -Wall -Wextra -pedantic main.c && ./a.out',
            'py': 'python3 main.cpp',
            'python': 'python3 main.cpp',
            'haskell': 'runhaskell main.cpp'
        }

        cpp = cmds['cpp']
        for alias in ('cc', 'h', 'c++', 'h++', 'hpp'):
            cmds[alias] = cpp
        try:
            return cmds[language]
        except KeyError as e:
            if language:
                fmt = f'Unknown language to compile for: {language}'
            else:
                fmt = 'Could not find a language to compile with.'
            raise commands.BadArgument(fmt) from e

class ChannelSnapshot:
    __slots__ = ('name', 'bucket', 'position', 'id')

    def __init__(self, channel):
        self.name = channel.name
        self.bucket = channel._sorting_bucket
        self.position = channel.position
        self.id = channel.id

    def __lt__(self, other):
        return isinstance(other, ChannelSnapshot) and (self.bucket, self.position, self.id) < (other.bucket, other.position, other.id)

    def __eq__(self, other):
        return isinstance(other, ChannelSnapshot) and self.id == other.id

    def __str__(self):
        return f'<#{self.id}>'

class Lounge(commands.Cog, name='Lounge<C++>'):
    """Commands made for Lounge<C++>.

    Don't abuse these.
    """

    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        # (position, category_id): [(name, bucket, position, id)]
        self._channel_snapshot = {}

    def make_snapshot(self, before=None):
        before = before or discord.Object(id=0)
        ret = {}
        guild = self.bot.get_guild(LOUNGE_GUILD_ID)
        for category, channels in guild.by_category():
            key = (-1, -1) if category is None else (category.position, category.id)
            snap = ret[key] = []
            for channel in channels:
                if channel.id == before.id:
                    channel = before

                snap.append(ChannelSnapshot(channel))

        return ret

    @staticmethod
    def logically_sorted_snapshot(snapshot):
        as_list = sorted(snapshot.items(), key=lambda t: t[0])
        return {
            category: sorted(channels)
            for ((_, category), channels) in as_list
        }


    async def display_snapshot(self):
        guild = self.bot.get_guild(LOUNGE_GUILD_ID)
        current = self.logically_sorted_snapshot(self.make_snapshot())
        before = self.logically_sorted_snapshot(self._channel_snapshot)

        embed = discord.Embed(title='Channel Position Change')

        for category_id, current_channels in current.items():
            older_channels = before.get(category_id)
            if older_channels is None:
                embed.description = 'Uh... weird position change happened here. No idea.'
                continue

            if older_channels != current_channels:
                category = guild.get_channel(category_id)
                before_str = '\n'.join(str(x) for x in older_channels)
                after_str = '\n'.join(str(x) for x in current_channels)
                embed.add_field(name='Before', value=f'**{category}**\n{before_str}', inline=True)
                embed.add_field(name='After', value=f'**{category}**\n{after_str}', inline=True)
                embed.add_field(name='\u200b', value='\u200b', inline=True)

        channel = guild.get_channel(META_CHANNEL_ID)
        if channel is None:
            return

        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if self._lock.locked():
            return

        if after.guild.id != LOUNGE_GUILD_ID or after.position == before.position:
            return

        async with self._lock:
            self._channel_snapshot = self.make_snapshot(before)
            await asyncio.sleep(10)
            await self.display_snapshot()

    @commands.command()
    @checks.is_lounge_cpp()
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

        The python support is now 3.5.2.

        Please don't spam this for Stacked's sake.
        """
        payload = {
            'cmd': code.command,
            'src': code.source
        }

        data = json.dumps(payload)

        async with ctx.typing():
            async with ctx.session.post('http://coliru.stacked-crooked.com/compile', data=data) as resp:
                if resp.status != 200:
                    await ctx.send('Coliru did not respond in time.')
                    return

                output = await resp.text(encoding='utf-8')

                if len(output) < 1992:
                    await ctx.send(f'```\n{output}\n```')
                    return

                # output is too big so post it in gist
                async with ctx.session.post('http://coliru.stacked-crooked.com/share', data=data) as r:
                    if r.status != 200:
                        await ctx.send('Could not create coliru shared link')
                    else:
                        shared_id = await r.text()
                        await ctx.send(f'Output too big. Coliru link: http://coliru.stacked-crooked.com/a/{shared_id}')

    @coliru.error
    async def coliru_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(CodeBlock.missing_error)

    @commands.command()
    async def cpp(self, ctx, *, query: str):
        """Search something on cppreference"""

        url = 'http://en.cppreference.com/w/cpp/index.php'
        params = {
            'title': 'Special:Search',
            'search': query
        }

        async with ctx.session.get(url, params=params) as resp:
            if resp.status != 200:
                return await ctx.send(f'An error occurred (status code: {resp.status}). Retry later.')

            if resp.url.path != '/w/cpp/index.php':
                return await ctx.send(f'<{resp.url}>')

            e = discord.Embed()
            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            nodes = root.findall(".//div[@class='mw-search-result-heading']/a")

            description = []
            special_pages = []
            for node in nodes:
                href = node.attrib['href']
                if not href.startswith('/w/cpp'):
                    continue

                if href.startswith(('/w/cpp/language', '/w/cpp/concept')):
                    # special page
                    special_pages.append(f'[{node.text}](http://en.cppreference.com{href})')
                else:
                    description.append(f'[`{node.text}`](http://en.cppreference.com{href})')

            if len(special_pages) > 0:
                e.add_field(name='Language Results', value='\n'.join(special_pages), inline=False)
                if len(description):
                    e.add_field(name='Library Results', value='\n'.join(description[:10]), inline=False)
            else:
                if not len(description):
                    return await ctx.send('No results found.')

                e.title = 'Search Results'
                e.description = '\n'.join(description[:15])

            e.add_field(name='See More', value=f'[`{discord.utils.escape_markdown(query)}` results]({resp.url})')
            await ctx.send(embed=e)

def setup(bot):
    bot.add_cog(Lounge(bot))
