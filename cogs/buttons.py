from discord.ext import commands
from datetime import datetime
import discord
from .utils import checks
import aiohttp
from urllib.parse import urlparse, parse_qs
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

    def parse_google_card(self, node):
        if node is None:
            return None

        e = discord.Embed(colour=0x738bd7)

        # check if it's a calculator card:
        calculator = node.find(".//span[@id='cwos']")
        if calculator is not None:
            e.title = 'Calculator'
            e.description = calculator.text
            return e

        # check for unit conversion card
        # The 'main' div contains 2 div for the source and the target of the conversion.
        # Each contains an <input> and a <select> where we can find the value and the label of the used unit.
        unit = node.find(".//div[@class='vk_c _cy obcontainer card-section']")
        if unit is not None:
            try:
                source = unit.find(".//div[@id='_Aif']")
                source_value = source.find("./input").attrib['value']
                source_unit = source.find("./select/option[@selected='1']").text
                target = unit.find(".//div[@id='_Cif']")
                target_value = target.find("./input").attrib['value']
                target_unit = target.find(".//select/option[@selected='1']").text
            except:
                return None
            else:
                e.title = 'Unit Conversion'
                e.description = '{} {} = {} {}'.format(source_value, source_unit, target_value, target_unit)
                return e

        # check for currency conversion card
        # The 'main' div contains 2 div for the source and the target of the conversion.
        # The source div has a span with the value in its content, and the unit in the tail
        # The target div has 2 spans, respectively containing the value and the unit
        currency = node.find(".//div[@class='currency g vk_c obcontainer']")
        if currency is not None:
            try:
                source = ''.join(currency.find(".//div[@class='vk_sh vk_gy cursrc']").itertext()).strip()
                target = ''.join(currency.find(".//div[@class='vk_ans vk_bk curtgt']").itertext()).strip()
            except:
                return None
            else:
                e.title = 'Currency Conversion'
                e.description = '{} {}'.format(source , target)
                return e

        # check for release date card
        # The 'main' div has 2 sections, one for the 'title', one for the 'body'.
        # The 'title' is a serie of 3 spans, 1st and 3rd with another nexted span containing the info, the 2nd
        # just contains a forward slash.
        # The 'body' is separated in 2 divs, one with the date and extra info, the other with 15 more nested
        # divs finally containing a <a> from which we can extract the thumbnail url.
        release = node.find(".//div[@class='xpdopen']/div[@class='_OKe']")
        if release is not None:
            # TODO : Check for timeline cards which are matched here with queries like `python release date`

            # Extract the release card title
            try:
                title = ' '.join(release.find(".//div[@class='_tN _IWg mod']/div[@class='_f2g']").itertext()).strip()
            except:
                e.title = 'Date info'
            else:
                e.title = title

            card_body = release.find(".//div[@class='kp-header']/div[@class='_axe _T9h']")

            # Extract the date info
            try:
                description = '\n'.join(card_body.find("./div[@class='_cFb']//div[@class='_uX kno-fb-ctx']").itertext()).strip()
            except:
                return None
            else:
                e.description = description

            # Extract the thumbnail
            thumbnail = card_body.find("./div[@class='_bFb']//a[@class='bia uh_rl']")
            if thumbnail is not None:
                e.set_thumbnail(url=parse_qs(urlparse(thumbnail.attrib['href']).query)['imgurl'][0])

            return e

        # Check for translation card
        translation = node.find(".//div[@id='tw-ob']")
        if translation is not None:
            try:
                source_language = translation.find(".//select[@id='tw-sl']").attrib['data-dsln']
                target_language = translation.find(".//select[@id='tw-tl']/option[@selected='1']").text
                translation = translation.find(".//pre[@id='tw-target-text']/span").text
            except:
                return None
            else:
                e.title = 'Translation from {} to {}'.format(source_language, target_language)
                e.description = translation
                return e

        # check for definition card
        definition = node.find(".//div[@id='uid_0']//div[@class='lr_dct_ent vmod']")
        if definition is not None:
            try:
                e.title = definition.find("./div[@class='vk_ans']/span").text
                definition_info = definition.findall("./div[@class='vmod']/div")
                e.description = definition_info[0].getchildren()[0].getchildren()[0].text # yikes v2
                for category in definition_info[1:]:
                    lexical_category = category.find("./div[@class='lr_dct_sf_h']/i/span").text
                    definitions = category.findall("./ol/li/div[@class='vmod']//div[@class='_Jig']/div/span")
                    body = []
                    for index, definition in enumerate(definitions, 1):
                        body.append('{}. {}'.format(index, definition.text))
                    e.add_field(name=lexical_category, value='\n'.join(body), inline=False)
            except:
                return None
            else:
                return e

        # check for "time in" card
        time_in = node.find(".//div[@class='vk_c vk_gy vk_sh card-section _MZc']")
        if time_in is not None:
            try:
                time_place = time_in.find("./span").text
                the_time = time_in.find("./div[@class='vk_bk vk_ans']").text
                the_date = ''.join(time_in.find("./div[@class='vk_gy vk_sh']").itertext()).strip()
            except:
                return None
            else:
                e.title = time_place
                e.description = '{}\n{}'.format(the_time, the_date)
                return e

        # check for weather card
        weather = node.find(".//div[@id='wob_wc']")
        if weather is not None:
            try:
                location = weather.find("./div[@id='wob_loc']").text
                summary = weather.find(".//span[@id='wob_dc']").text
                image = 'https:' + weather.find(".//img[@id='wob_tci']").attrib['src']
                temp_degrees = weather.find(".//span[@id='wob_tm']").text
                temp_farenheit = weather.find(".//span[@id='wob_ttm']").text
                precipitations = weather.find(".//span[@id='wob_pp']").text
                humidity = weather.find(".//span[@id='wob_hm']").text
                wind_kmh = weather.find(".//span[@id='wob_ws']").text
                wind_mph = weather.find(".//span[@id='wob_tws']").text
            except:
                return None
            else:
                e.title = 'Weather in ' + location
                e.description = summary
                e.set_thumbnail(url=image)
                e.add_field(name='Temperature', value='{}°C - {}°F'.format(temp_degrees, temp_farenheit))
                e.add_field(name='Precipitations', value=precipitations)
                e.add_field(name='Humidity', value=humidity)
                e.add_field(name='Wind speed', value='{} - {}'.format(wind_kmh, wind_mph))
                return e

        # nothing matched
        return None

    async def get_google_entries(self, query):
        params = {
            'hl': 'en',
            'q': query,
            'safe': 'on'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36'
        }

        # list of URLs
        entries = []

        # the result of a google card, an embed
        card = None

        async with aiohttp.get('https://www.google.com/search', params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError('Google somehow failed to respond.')

            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            # with open('google.html', 'w', encoding='utf-8') as f:
            #     f.write(etree.tostring(root, pretty_print=True).decode('utf-8'))

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

            card_node = root.find(".//div[@id='res']")
            card = self.parse_google_card(card_node)

            search_nodes = root.findall(".//div[@class='g']")
            for node in search_nodes:
                url_node = node.find('.//h3/a')
                if url_node is None:
                    continue

                url = url_node.attrib['href']
                entries.append(url)

                # if I ever cared about the description, this is how
                # short = node.find(".//span[@class='st']")
                # if short is None:
                #     entries.append((url, ''))
                # else:
                #     entries.append((url, short.text.replace('...', '')))

        return card, entries

    @commands.command(aliases=['google'])
    async def g(self, *, query):
        """Searches google and gives you top result."""
        await self.bot.type()
        try:
            card, entries = await self.get_google_entries(query)
        except RuntimeError as e:
            await self.bot.say(str(e))
        else:
            if card:
                value = '\n'.join(entries[:3])
                if value:
                    card.add_field(name='Search Results', value=value, inline=False)
                return await self.bot.say(embed=card)

            if len(entries) == 0:
                return await self.bot.say('No results found... sorry.')

            next_two = entries[1:3]
            if next_two:
                formatted = '\n'.join(map(lambda x: '<%s>' % x, next_two))
                msg = '{}\n\n**See also:**\n{}'.format(entries[0], formatted)
            else:
                msg = entries[0]

            await self.bot.say(msg)

    @commands.command(pass_context=True)
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.user)
    async def feedback(self, ctx, *, content: str):
        """Gives feedback about the bot.

        This is a quick way to request features or bug fixes
        without being in the bot's server.

        The bot will communicate with you via PM about the status
        of your request if possible.

        You can only request feedback once a minute.
        """

        e = discord.Embed(title='Feedback', colour=0x738bd7)
        msg = ctx.message

        channel = self.bot.get_channel('263814407191134218')
        if channel is None:
            return

        e.set_author(name=str(msg.author), icon_url=msg.author.avatar_url or msg.author.default_avatar_url)
        e.description = content
        e.timestamp = msg.timestamp

        if msg.server is not None:
            e.add_field(name='Server', value='{0.name} (ID: {0.id})'.format(msg.server), inline=False)

        e.add_field(name='Channel', value='{0} (ID: {0.id})'.format(msg.channel), inline=False)
        e.set_footer(text='Author ID: ' + msg.author.id)

        await self.bot.send_message(channel, embed=e)
        await self.bot.send_message(msg.channel, 'Successfully sent feedback \u2705')

    @commands.command()
    @checks.is_owner()
    async def pm(self, user_id: str, *, content: str):
        user = await self.bot.get_user_info(user_id)

        try:
            await self.bot.send_message(user, content)
        except:
            await self.bot.say('Could not PM user by ID ' + user_id)
        else:
            await self.bot.say('PM successfully sent.')

def setup(bot):
    bot.add_cog(Buttons(bot))
