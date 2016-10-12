from discord.ext import commands
from datetime import datetime
import discord
from .utils import checks
import aiohttp
from urllib.parse import parse_qs
from lxml import etree

def date(argument):
    formats = (
        '%Y/%m/%d',
        '%Y-%m-%d',
    )

    for fmt in formats:
        try:
            return datetime.strptime(argument, fmt)
        except ValueError:
            continue

    raise commands.BadArgument('Cannot convert to date. Expected YYYY/MM/DD or YYYY-MM-DD.')

class Buttons:
    """Buttons that make you feel."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(hidden=True)
    async def feelgood(self):
        """press"""
        await self.bot.say('*pressed*')

    @commands.command(hidden=True)
    async def feelbad(self):
        """depress"""
        await self.bot.say('*depressed*')

    @commands.command()
    async def love(self):
        """What is love?"""
        await self.bot.say('http://i.imgur.com/JthwtGA.png')

    @commands.command(hidden=True)
    async def bored(self):
        """boredom looms"""
        await self.bot.say('http://i.imgur.com/BuTKSzf.png')

    @commands.command(pass_context=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def nostalgia(self, ctx, date: date, *, channel: discord.Channel = None):
        """Pins an old message from a specific date.

        If a channel is not given, then pins from the channel the
        command was ran on.

        The format of the date must be either YYYY-MM-DD or YYYY/MM/DD.
        """

        if channel is None:
            channel = ctx.message.channel

        async for m in self.bot.logs_from(channel, after=date, limit=1):
            try:
                await self.bot.pin_message(m)
            except:
                await self.bot.say('\N{THUMBS DOWN SIGN} Could not pin message.')
            else:
                await self.bot.say('\N{THUMBS UP SIGN} Successfully pinned message.')

    @nostalgia.error
    async def nostalgia_error(self, error, ctx):
        if type(error) is commands.BadArgument:
            await self.bot.say(error)

    async def get_google_entries(self, query):
        params = {
            'q': query,
            'safe': 'on'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64)'
        }

        # list of URLs
        entries = []

        async with aiohttp.get('https://www.google.com/search', params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError('Google somehow failed to respond.')

            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            """
            Tree looks like this.. sort of..

            <div class="g">
                ...
                <h3>
                    <a href="/url?q=<url>" ...>title</a>
                </h3>
                ...
                <span class="st">
                    <span class="f">date here</span>
                    summary here, can contain <em>tag</em>
                </span>
            </div>
            """

            search_nodes = root.findall(".//div[@class='g']")
            for node in search_nodes:
                url_node = node.find('.//h3/a')
                if url_node is None:
                    continue

                url = url_node.attrib['href']
                if not url.startswith('/url?'):
                    continue

                url = parse_qs(url[5:])['q'][0] # get the URL from ?q query string

                # if I ever cared about the description, this is how
                entries.append(url)

                # short = node.find(".//span[@class='st']")
                # if short is None:
                #     entries.append((url, ''))
                # else:
                #     text = ''.join(short.itertext())
                #     entries.append((url, text.replace('...', '')))

        return entries

    @commands.command(aliases=['google'])
    async def g(self, *, query):
        """Searches google and gives you top result."""
        try:
            entries = await self.get_google_entries(query)
        except RuntimeError as e:
            await self.bot.say(str(e))
        else:
            next_two = entries[1:3]
            if next_two:
                formatted = '\n'.join(map(lambda x: '<%s>' % x, next_two))
                msg = '{}\n\n**See also:**\n{}'.format(entries[0], formatted)
            else:
                msg = entries[0]

            await self.bot.say(msg)

def setup(bot):
    bot.add_cog(Buttons(bot))
