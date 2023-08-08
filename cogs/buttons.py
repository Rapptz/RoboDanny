from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, MutableMapping, NamedTuple, Optional, Set, TypedDict
from typing_extensions import Self, Annotated
from discord.ext import commands, menus
from discord import app_commands
from lxml import html
import discord
from .utils.paginator import RoboPages
import random
import logging
from lru import LRU
import yarl
import io
import re

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .utils.context import GuildContext, Context
    from .utils.paginator import RoboPages
    from bot import RoboDanny
    from aiohttp import ClientSession


def can_use_spoiler():
    def predicate(ctx: GuildContext) -> bool:
        if ctx.guild is None:
            raise commands.BadArgument('Cannot be used in private messages.')

        my_permissions = ctx.channel.permissions_for(ctx.guild.me)
        if not (my_permissions.read_message_history and my_permissions.manage_messages and my_permissions.add_reactions):
            raise commands.BadArgument(
                'Need Read Message History, Add Reactions and Manage Messages '
                'to permission to use this. Sorry if I spoiled you.'
            )
        return True

    return commands.check(predicate)


SPOILER_EMOJI_ID = 430469957042831371
DICTIONARY_EMBED_COLOUR = discord.Colour(0x5F9EB3)


def html_to_markdown(node: Any, *, include_spans: bool = False, base_url: Optional[yarl.URL] = None) -> str:
    text = []
    italics_marker = '_'

    for child in node:
        if child.tag == 'i':
            text.append(f'{italics_marker}{child.text.strip()}{italics_marker}')
            italics_marker = '_' if italics_marker == '*' else '*'
        elif child.tag == 'b':
            if text and text[-1].endswith('*'):
                text.append('\u200b')

            text.append(f'**{child.text.strip()}**')
        elif child.tag == 'a':
            # No markup for links
            if base_url is None:
                text.append(child.text)
            else:
                url = base_url.join(yarl.URL(child.attrib['href']))
                text.append(f'[{child.text}]({url})')
        elif include_spans and child.tag == 'span':
            text.append(child.text)

        if child.tail:
            text.append(child.tail)

    return ''.join(text).strip()

def inner_trim(s: str, *, _regex=re.compile(r'\s+')) -> str:
    return _regex.sub(' ', s.strip())


class FreeDictionaryDefinition(NamedTuple):
    definition: str
    example: Optional[str]
    children: list[FreeDictionaryDefinition]

    @classmethod
    def from_node(cls, node: Any) -> Self:
        # Note that in here we're inside either a ds-list or a ds-single node
        # The first child is basically always a superfluous bolded number
        number = node.find('b')
        definition: str = node.text or ''
        if number is not None:
            tail = number.tail
            node.remove(number)
            if tail:
                definition = tail

        definition += html_to_markdown(node, include_spans=False)
        definition = inner_trim(definition)

        example: Optional[str] = None
        example_nodes = node.xpath("./span[@class='illustration']")
        if example_nodes:
            example = example_nodes[0].text_content()

        children: list[FreeDictionaryDefinition] = [cls.from_node(child) for child in node.xpath("./div[@class='sds-list']")]
        return cls(definition, example, children)

    def to_json(self) -> dict[str, Any]:
        return {
            'definition': self.definition,
            'example': self.example,
            'children': [child.to_json() for child in self.children],
        }

    def to_markdown(self, *, indent: int = 2) -> str:
        content = self.definition
        if self.example:
            content = f'{content} [*{self.example}*]'
        if not content:
            content = '\u200b'
        if self.children:
            inner = '\n'.join(f'{" " * indent }- {child.to_markdown(indent=indent + 2)}' for child in self.children)
            return f'{content}\n{inner}'
        return content


class FreeDictionaryMeaning:
    part_of_speech: str
    definitions: list[FreeDictionaryDefinition]

    __slots__ = ('part_of_speech', 'definitions')

    def __init__(self, definitions: Any, part_of_speech: str) -> None:
        self.part_of_speech = part_of_speech
        self.definitions = [FreeDictionaryDefinition.from_node(definition) for definition in definitions]

    def to_json(self) -> dict[str, Any]:
        return {'part_of_speech': self.part_of_speech, 'definitions': [defn.to_json() for defn in self.definitions]}

    @property
    def markdown(self) -> str:
        inner = '\n'.join(f'{i}. {defn.to_markdown()}' for i, defn in enumerate(self.definitions, start=1))
        return f'{self.part_of_speech}\n{inner}'


class FreeDictionaryPhrasalVerb(NamedTuple):
    word: str
    meaning: FreeDictionaryMeaning

    def to_embed(self) -> discord.Embed:
        return discord.Embed(title=self.word, colour=DICTIONARY_EMBED_COLOUR, description=self.meaning.markdown)


class FreeDictionaryWord:
    raw_word: str
    word: str
    pronunciation_url: Optional[str]
    pronunciation: Optional[str]
    meanings: list[FreeDictionaryMeaning]
    phrasal_verbs: list[FreeDictionaryPhrasalVerb]
    etymology: Optional[str]

    def __init__(self, raw_word: str, word: str, node: Any, base_url: yarl.URL) -> None:
        self.raw_word = raw_word
        self.word = word
        self.meanings = []
        self.phrasal_verbs = []
        self.get_pronunciation(node)
        self.get_meanings(node)
        self.get_etymology(node, base_url)

    def get_pronunciation(self, node) -> None:
        self.pronunciation_url = None
        self.pronunciation = None
        snd = node.xpath("span[@class='snd' and @data-snd]")
        if not snd:
            return None

        snd = snd[0]
        pron = node.xpath("span[@class='pron']")
        if pron:
            self.pronunciation = pron[0].text_content() + (pron[0].tail or '')
            self.pronunciation = self.pronunciation.strip()

        data_src = node.attrib.get('data-src')
        if data_src is not None:
            mp3 = snd.attrib.get('data-snd')
            self.pronunciation_url = f'https://img.tfd.com/{data_src}/{mp3}.mp3'

    def get_meanings(self, node) -> None:
        conjugations: Optional[str] = None

        data_src = node.attrib.get('data-src')

        child_nodes = []
        if data_src == 'hm':
            child_nodes = node.xpath("./div[@class='pseg']")
        elif data_src == 'hc_dict':
            child_nodes = node.xpath('./div[not(@class)]')
        elif data_src == 'rHouse':
            child_nodes = node

        for div in child_nodes:
            definitions = div.xpath("div[@class='ds-list' or @class='ds-single']")
            if not definitions:
                # Probably a conjugation
                # If it isn't a conjugation then it probably just has a single definition
                bolded = div.find('b')
                if bolded is not None:
                    children = iter(div)
                    next(children)  # skip the italic `v.` bit
                    conjugations = html_to_markdown(children, include_spans=True)
                    continue

            pos_node = div.find('i')
            if pos_node is None:
                continue

            pos = html_to_markdown(div)
            if conjugations is not None:
                if conjugations.startswith(','):
                    pos = f'{pos}{conjugations}'
                else:
                    pos = f'{pos} {conjugations}'

            meaning = FreeDictionaryMeaning(definitions, pos)
            self.meanings.append(meaning)

        for div in node.find_class('pvseg'):
            # phrasal verbs are simple
            # <b><i>{word}</i></b>
            # ... definitions
            word = div.find('b/i')
            if word is None:
                continue

            word = word.text_content().strip()
            meaning = FreeDictionaryMeaning(div, 'phrasal verb')
            self.phrasal_verbs.append(FreeDictionaryPhrasalVerb(word, meaning))

    def get_etymology(self, node: Any, base_url: yarl.URL) -> None:
        etyseg = node.xpath("./div[@class='etyseg']")
        if not etyseg:
            self.etymology = None
            return

        etyseg = etyseg[0]
        self.etymology = etyseg.text + html_to_markdown(etyseg, include_spans=True, base_url=base_url)

        if self.etymology.startswith('[') and self.etymology.endswith(']'):
            self.etymology = self.etymology[1:-1]

    def to_json(self) -> dict[str, Any]:
        return {
            'raw_word': self.raw_word,
            'word': self.word,
            'pronunciation_url': self.pronunciation_url,
            'pronunciation': self.pronunciation,
            'meanings': [meaning.to_json() for meaning in self.meanings],
            'phrasal_verbs': [
                {
                    'word': verb.word,
                    'meaning': verb.meaning.to_json(),
                }
                for verb in self.phrasal_verbs
            ],
            'etymology': self.etymology,
        }


async def parse_free_dictionary_for_word(session: ClientSession, *, word: str) -> Optional[FreeDictionaryWord]:
    url = yarl.URL('https://www.thefreedictionary.com') / word

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/114.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
        'TE': 'trailers',
    }

    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None

        text = await resp.text()
        document = html.document_fromstring(text)

        try:
            definitions = document.get_element_by_id('Definition')
        except KeyError:
            return None

        h1 = document.find('h1')
        raw_word = h1.text if h1 is not None else word

        section = definitions.xpath("section[@data-src='hm' or @data-src='hc_dict' or @data-src='rHouse']")
        if not section:
            return None

        node = section[0]
        h2: Optional[Any] = node.find('h2')
        if h2 is None:
            return None

        try:
            return FreeDictionaryWord(raw_word, h2.text, node, resp.url)
        except RuntimeError:
            log.exception('Error happened while parsing free dictionary')
            return None


async def free_dictionary_autocomplete_query(session: ClientSession, *, query: str) -> list[str]:
    url = yarl.URL('https://www.thefreedictionary.com/_/search/suggest.ashx')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/114.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
        'TE': 'trailers',
    }

    async with session.get(url, params={'query': query}, headers=headers) as resp:
        if resp.status != 200:
            return []

        js = await resp.json()
        if len(js) == 2:
            return js[1]
        return []


class FreeDictionaryWordMeaningPageSource(menus.ListPageSource):
    entries: list[FreeDictionaryMeaning]

    def __init__(self, word: FreeDictionaryWord):
        super().__init__(entries=word.meanings, per_page=1)
        self.word: FreeDictionaryWord = word

    async def format_page(self, menu: RoboPages, entry: FreeDictionaryMeaning) -> discord.Embed:
        maximum = self.get_max_pages()
        heading = f'{self.word.raw_word}: {menu.current_page + 1} out of {maximum}' if maximum >= 2 else self.word.raw_word
        if self.word.pronunciation:
            title = f'{self.word.word} {self.word.pronunciation}'
        else:
            title = self.word.word

        embed = discord.Embed(title=title, colour=DICTIONARY_EMBED_COLOUR)
        embed.set_author(name=heading)
        embed.description = entry.markdown

        if self.word.etymology:
            embed.add_field(name='Etymology', value=self.word.etymology, inline=False)

        return embed


class UrbanDictionaryPageSource(menus.ListPageSource):
    BRACKETED = re.compile(r'(\[(.+?)\])')

    def __init__(self, data: list[dict[str, Any]]):
        super().__init__(entries=data, per_page=1)

    def cleanup_definition(self, definition: str, *, regex=BRACKETED) -> str:
        def repl(m):
            word = m.group(2)
            return f'[{word}](http://{word.replace(" ", "-")}.urbanup.com)'

        ret = regex.sub(repl, definition)
        if len(ret) >= 2048:
            return ret[0:2000] + ' [...]'
        return ret

    async def format_page(self, menu: RoboPages, entry: dict[str, Any]):
        maximum = self.get_max_pages()
        title = f'{entry["word"]}: {menu.current_page + 1} out of {maximum}' if maximum else entry['word']
        embed = discord.Embed(title=title, colour=0xE86222, url=entry['permalink'])
        embed.set_footer(text=f'by {entry["author"]}')
        embed.description = self.cleanup_definition(entry['definition'])

        try:
            up, down = entry['thumbs_up'], entry['thumbs_down']
        except KeyError:
            pass
        else:
            embed.add_field(name='Votes', value=f'\N{THUMBS UP SIGN} {up} \N{THUMBS DOWN SIGN} {down}', inline=False)

        try:
            date = discord.utils.parse_time(entry['written_on'][0:-1])
        except (ValueError, KeyError):
            pass
        else:
            embed.timestamp = date

        return embed


class ConvertibleUnit(NamedTuple):
    # (value) -> (converted, unit)
    formula: Callable[[float], tuple[float, str]]
    capture: str


UNIT_CONVERSIONS: dict[str, ConvertibleUnit] = {
    'km': ConvertibleUnit(lambda v: (v * 0.621371, 'mi'), r'km|(?:kilometer|kilometre)s?'),
    'm': ConvertibleUnit(lambda v: (v * 3.28084, 'ft'), r'm|(?:meter|metre)s?'),
    'ft': ConvertibleUnit(lambda v: (v * 0.3048, 'm'), r'ft|feet|foot'),
    'cm': ConvertibleUnit(lambda v: (v * 0.393701, 'in'), r'cm|(?:centimeter|centimetre)s?'),
    'in': ConvertibleUnit(lambda v: (v * 2.54, 'cm'), r'in|inch(?:es)?'),
    'mi': ConvertibleUnit(lambda v: (v * 1.60934, 'km'), r'mi|miles?'),
    'kg': ConvertibleUnit(lambda v: (v * 2.20462, 'lb'), r'kg|kilograms?'),
    'lb': ConvertibleUnit(lambda v: (v * 0.453592, 'kg'), r'(?:lb|pound)s?'),
    'L': ConvertibleUnit(lambda v: (v * 0.264172, 'gal'), r'l|(?:liter|litre)s?'),
    'gal': ConvertibleUnit(lambda v: (v * 3.78541, 'L'), r'gal|gallons?'),
    'C': ConvertibleUnit(lambda v: (v * 1.8 + 32, 'F'), r'c|°c|celsius'),
    'F': ConvertibleUnit(lambda v: ((v - 32) / 1.8, 'C'), r'f|°f|fahrenheit'),
}

UNIT_CONVERSION_REGEX_COMPONENT = '|'.join(f'(?P<{name}>{unit.capture})' for name, unit in UNIT_CONVERSIONS.items())
UNIT_CONVERSION_REGEX = re.compile(
    rf'(?P<value>\-?[0-9]+(?:[,.][0-9]+)?)\s*(?:{UNIT_CONVERSION_REGEX_COMPONENT})\b', re.IGNORECASE
)


class Unit(NamedTuple):
    value: float
    unit: str

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        match = UNIT_CONVERSION_REGEX.match(argument)
        if match is None:
            raise commands.BadArgument('Could not find a unit')

        value = float(match.group('value'))
        unit = match.lastgroup
        if unit is None:
            raise commands.BadArgument('Could not find a unit')

        return cls(value, unit)

    def converted(self) -> Self:
        return Unit(*UNIT_CONVERSIONS[self.unit].formula(self.value))

    @property
    def display_unit(self) -> str:
        # Work around the fact that ° can't be used in group names
        if self.unit in ('F', 'C'):
            return f'°{self.unit}'
        return f' {self.unit}'


class UnitCollector(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> set[Unit]:
        units = set()
        for match in UNIT_CONVERSION_REGEX.finditer(argument):
            value = float(match.group('value'))
            unit = match.lastgroup
            if unit is None:
                raise commands.BadArgument('Could not find a unit')

            units.add(Unit(value, unit))

        if not units:
            raise commands.BadArgument('Could not find a unit')

        return units


class RedditMediaURL:
    def __init__(self, url: yarl.URL):
        self.url: yarl.URL = url
        self.filename: str = url.parts[1] + '.mp4'

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        try:
            url = yarl.URL(argument)
        except Exception as e:
            raise commands.BadArgument('Not a valid URL.')

        headers = {
            'User-Agent': 'Discord:RoboDanny:v4.0 (by /u/Rapptz)',
        }
        await ctx.typing()
        if url.host == 'v.redd.it':
            # have to do a request to fetch the 'main' URL.
            async with ctx.session.get(url, headers=headers) as resp:
                url = resp.url

        is_valid_path = url.host and url.host.endswith('.reddit.com')
        if not is_valid_path:
            raise commands.BadArgument('Not a reddit URL.')

        # Now we go the long way
        async with ctx.session.get(url / '.json', headers=headers) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f'Reddit API failed with {resp.status}.')

            data = await resp.json()
            try:
                submission = data[0]['data']['children'][0]['data']
            except (KeyError, TypeError, IndexError):
                raise commands.BadArgument('Could not fetch submission.')

            try:
                media = submission['media']['reddit_video']
            except (KeyError, TypeError):
                try:
                    # maybe it's a cross post
                    crosspost = submission['crosspost_parent_list'][0]
                    media = crosspost['media']['reddit_video']
                except (KeyError, TypeError, IndexError):
                    raise commands.BadArgument('Could not fetch media information.')

            try:
                fallback_url = yarl.URL(media['fallback_url'])
            except KeyError:
                raise commands.BadArgument('Could not fetch fall back URL.')

            return cls(fallback_url)


class SpoilerCacheData(TypedDict):
    author_id: int
    channel_id: int
    title: str
    text: Optional[str]
    attachments: list[discord.Attachment]


class SpoilerCache:
    __slots__ = ('author_id', 'channel_id', 'title', 'text', 'attachments')

    def __init__(self, data: SpoilerCacheData):
        self.author_id: int = data['author_id']
        self.channel_id: int = data['channel_id']
        self.title: str = data['title']
        self.text: Optional[str] = data['text']
        self.attachments: list[discord.Attachment] = data['attachments']

    def has_single_image(self) -> bool:
        return bool(self.attachments) and self.attachments[0].filename.lower().endswith(('.gif', '.png', '.jpg', '.jpeg'))

    def to_embed(self, bot: RoboDanny) -> discord.Embed:
        embed = discord.Embed(title=f'{self.title} Spoiler', colour=0x01AEEE)
        if self.text:
            embed.description = self.text

        if self.has_single_image():
            if self.text is None:
                embed.title = f'{self.title} Spoiler Image'
            embed.set_image(url=self.attachments[0].url)
            attachments = self.attachments[1:]
        else:
            attachments = self.attachments

        if attachments:
            value = '\n'.join(f'[{a.filename}]({a.url})' for a in attachments)
            embed.add_field(name='Attachments', value=value, inline=False)

        user = bot.get_user(self.author_id)
        if user:
            embed.set_author(name=str(user), icon_url=user.display_avatar.url)

        return embed

    def to_spoiler_embed(self, ctx: Context, storage_message: discord.abc.Snowflake) -> discord.Embed:
        description = 'This spoiler has been hidden. Press the button to reveal it!'
        embed = discord.Embed(title=f'{self.title} Spoiler', description=description)
        if self.has_single_image() and self.text is None:
            embed.title = f'{self.title} Spoiler Image'

        embed.set_footer(text=storage_message.id)
        embed.colour = 0x01AEEE
        embed.set_author(name=ctx.author, icon_url=ctx.author.display_avatar.url)
        return embed


class SpoilerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(1, 10.0), commands.BucketType.user)

    def _bucket_key(self, tup: tuple[int, int]) -> tuple[int, int]:
        return tup

    def is_rate_limited(self, message_id: int, user_id: int) -> bool:
        # This is a lie but it should just work as-is
        bucket = self.get_bucket((message_id, user_id))  # type: ignore
        return bucket is not None and bucket.update_rate_limit() is not None


class FeedbackModal(discord.ui.Modal, title='Submit Feedback'):
    summary = discord.ui.TextInput(label='Summary', placeholder='A brief explanation of what you want')
    details = discord.ui.TextInput(label='Details', style=discord.TextStyle.long, required=False)

    def __init__(self, cog: Buttons) -> None:
        super().__init__()
        self.cog: Buttons = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = self.cog.feedback_channel
        if channel is None:
            await interaction.response.send_message('Could not submit your feedback, sorry about this', ephemeral=True)
            return

        embed = self.cog.get_feedback_embed(interaction, summary=str(self.summary), details=self.details.value)
        await channel.send(embed=embed)
        await interaction.response.send_message('Successfully submitted feedback', ephemeral=True)


class SpoilerView(discord.ui.View):
    def __init__(self, cog: Buttons) -> None:
        super().__init__(timeout=None)
        self.cog: Buttons = cog

    @discord.ui.button(
        label='Reveal Spoiler',
        style=discord.ButtonStyle.grey,
        emoji=discord.PartialEmoji(name='spoiler', id=430469957042831371),
        custom_id='cogs:buttons:reveal_spoiler',
    )
    async def reveal_spoiler(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.message is not None
        assert interaction.channel_id is not None

        cache = await self.cog.get_spoiler_cache(interaction.channel_id, interaction.message.id)
        if cache is not None:
            embed = cache.to_embed(self.cog.bot)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Jump to Spoiler', url=interaction.message.jump_url))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message('Could not find this message in storage', ephemeral=True)


class Buttons(commands.Cog):
    """Buttons that make you feel."""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self._spoiler_cache: MutableMapping[int, SpoilerCache] = LRU(128)
        self._spoiler_cooldown = SpoilerCooldown()
        self._spoiler_view = SpoilerView(self)
        bot.add_view(self._spoiler_view)

    def cog_unload(self) -> None:
        self._spoiler_view.stop()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{RADIO BUTTON}')

    @property
    def feedback_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(182325885867786241)
        if guild is None:
            return None

        return guild.get_channel(263814407191134218)  # type: ignore

    @property
    def storage_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(182325885867786241)
        if guild is None:
            return None

        return guild.get_channel(430229522340773899)  # type: ignore

    @commands.command(hidden=True)
    async def feelgood(self, ctx: Context):
        """press"""
        await ctx.send('*pressed*')

    @commands.command(hidden=True)
    async def feelbad(self, ctx: Context):
        """depress"""
        await ctx.send('*depressed*')

    @commands.command()
    async def love(self, ctx: Context):
        """What is love?"""
        responses = [
            'https://www.youtube.com/watch?v=HEXWRTEbj1I',
            'https://www.youtube.com/watch?v=i0p1bmr0EmE',
            'an intense feeling of deep affection',
            'something we don\'t have',
        ]

        response = random.choice(responses)
        await ctx.send(response)

    @commands.command(hidden=True)
    async def bored(self, ctx: Context):
        """boredom looms"""
        await ctx.send('http://i.imgur.com/BuTKSzf.png')

    def get_feedback_embed(
        self,
        obj: Context | discord.Interaction,
        *,
        summary: str,
        details: Optional[str] = None,
    ) -> discord.Embed:
        e = discord.Embed(title='Feedback', colour=0x738BD7)

        if details is not None:
            e.description = details
            e.title = summary[:256]
        else:
            e.description = summary

        if obj.guild is not None:
            e.add_field(name='Server', value=f'{obj.guild.name} (ID: {obj.guild.id})', inline=False)

        if obj.channel is not None:
            e.add_field(name='Channel', value=f'{obj.channel} (ID: {obj.channel.id})', inline=False)

        if isinstance(obj, discord.Interaction):
            e.timestamp = obj.created_at
            user = obj.user
        else:
            e.timestamp = obj.message.created_at
            user = obj.author

        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_footer(text=f'Author ID: {user.id}')
        return e

    @commands.command()
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.user)
    async def feedback(self, ctx: Context, *, content: str):
        """Gives feedback about the bot.

        This is a quick way to request features or bug fixes
        without being in the bot's server.

        The bot will communicate with you via PM about the status
        of your request if possible.

        You can only request feedback once a minute.
        """

        channel = self.feedback_channel
        if channel is None:
            return

        e = self.get_feedback_embed(ctx, summary=content)
        await channel.send(embed=e)
        await ctx.send(f'{ctx.tick(True)} Successfully sent feedback')

    @app_commands.command(name='feedback')
    async def feedback_slash(self, interaction: discord.Interaction):
        """Give feedback about the bot directly to the owner."""

        await interaction.response.send_modal(FeedbackModal(self))

    @commands.command()
    @commands.is_owner()
    async def pm(self, ctx: Context, user_id: int, *, content: str):
        user = self.bot.get_user(user_id) or (await self.bot.fetch_user(user_id))

        fmt = (
            content + '\n\n*This is a DM sent because you had previously requested feedback or I found a bug'
            ' in a command you used, I do not monitor this DM. Responses to this DM are not mirrored anywhere.*'
        )
        try:
            await user.send(fmt)
        except:
            await ctx.send(f'Could not PM user by ID {user_id}.')
        else:
            await ctx.send('PM successfully sent.')

    async def redirect_post(self, ctx: Context, title, text):
        storage = self.storage_channel
        if storage is None:
            raise RuntimeError('Spoiler storage was not found')

        supported_attachments = ('.png', '.jpg', '.jpeg', '.webm', '.gif', '.mp4', '.txt')
        if not all(attach.filename.lower().endswith(supported_attachments) for attach in ctx.message.attachments):
            raise RuntimeError(f'Unsupported file in attachments. Only {", ".join(supported_attachments)} supported.')

        files = []
        total_bytes = 0
        eight_mib = 8 * 1024 * 1024
        for attach in ctx.message.attachments:
            async with ctx.session.get(attach.url) as resp:
                if resp.status != 200:
                    continue

                content_length = int(resp.headers['Content-Length'])

                # file too big, skip it
                if (total_bytes + content_length) > eight_mib:
                    continue

                total_bytes += content_length
                fp = io.BytesIO(await resp.read())
                files.append(discord.File(fp, filename=attach.filename))

            if total_bytes >= eight_mib:
                break

        # on mobile, messages that are deleted immediately sometimes persist client side
        await asyncio.sleep(0.2)
        await ctx.message.delete()
        data = discord.Embed(title=title)
        if text:
            data.description = text

        data.set_author(name=ctx.author.id)
        data.set_footer(text=ctx.channel.id)

        try:
            message = await storage.send(embed=data, files=files)
        except discord.HTTPException as e:
            raise RuntimeError(f'Sorry. Could not store message due to {e.__class__.__name__}: {e}.') from e

        to_dict: SpoilerCacheData = {
            'author_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'attachments': message.attachments,
            'title': title,
            'text': text,
        }

        cache = SpoilerCache(to_dict)
        return message, cache

    async def get_spoiler_cache(self, channel_id: int, message_id: int) -> Optional[SpoilerCache]:
        try:
            return self._spoiler_cache[message_id]
        except KeyError:
            pass

        storage = self.storage_channel
        if storage is None:
            return None

        # slow path requires 2 lookups
        # first is looking up the message_id of the original post
        # to get the embed footer information which points to the storage message ID
        # the second is getting the storage message ID and extracting the information from it
        channel: Optional[discord.abc.Messageable] = self.bot.get_channel(channel_id)  # type: ignore
        if not channel:
            return None

        try:
            original_message = await channel.fetch_message(message_id)
            storage_message_id = int(original_message.embeds[0].footer.text)  # type: ignore  # Guarded by exception
            message = await storage.fetch_message(storage_message_id)
        except:
            # this message is probably not the proper format or the storage died
            return None

        data = message.embeds[0]
        to_dict: SpoilerCacheData = {
            'author_id': int(data.author.name),  # type: ignore
            'channel_id': int(data.footer.text),  # type: ignore
            'attachments': message.attachments,
            'title': data.title,
            'text': None if not data.description else data.description,
        }
        cache = SpoilerCache(to_dict)
        self._spoiler_cache[message_id] = cache
        return cache

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.id != SPOILER_EMOJI_ID:
            return

        if self._spoiler_cooldown.is_rate_limited(payload.message_id, payload.user_id):
            return

        user = self.bot.get_user(payload.user_id) or (await self.bot.fetch_user(payload.user_id))
        if not user or user.bot:
            return

        cache = await self.get_spoiler_cache(payload.channel_id, payload.message_id)

        if cache is not None:
            embed = cache.to_embed(self.bot)
            await user.send(embed=embed)

    @commands.command()
    @can_use_spoiler()
    async def spoiler(self, ctx: Context, title: str, *, text: Optional[str] = None):
        """Marks your post a spoiler with a title.

        Once your post is marked as a spoiler it will be
        automatically deleted and the bot will send a message
        to those who opt-in to view the spoiler.

        The only media types supported are png, gif, jpeg, mp4,
        and webm.

        Only 8MiB of total media can be uploaded at once.
        Sorry, Discord limitation.

        To opt-in to a post's spoiler you must press the button.
        """

        if len(title) > 100:
            return await ctx.send('Sorry. Title has to be shorter than 100 characters.')

        try:
            storage_message, cache = await self.redirect_post(ctx, title, text)
        except Exception as e:
            return await ctx.send(str(e))

        spoiler_message = await ctx.send(embed=cache.to_spoiler_embed(ctx, storage_message), view=self._spoiler_view)
        self._spoiler_cache[spoiler_message.id] = cache

    @commands.command(usage='<url>')
    @commands.cooldown(1, 5.0, commands.BucketType.member)
    async def vreddit(self, ctx: Context, *, reddit: RedditMediaURL):
        """Downloads a v.redd.it submission.

        Regular reddit URLs or v.redd.it URLs are supported.
        """

        filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
        async with ctx.session.get(reddit.url) as resp:
            if resp.status != 200:
                return await ctx.send('Could not download video.')

            if int(resp.headers['Content-Length']) >= filesize:
                return await ctx.send('Video is too big to be uploaded.')

            data = await resp.read()
            await ctx.send(file=discord.File(io.BytesIO(data), filename=reddit.filename))

    @vreddit.error
    async def on_vreddit_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))

    @commands.command(name='urban')
    async def _urban(self, ctx: Context, *, word: str):
        """Searches urban dictionary."""

        url = 'http://api.urbandictionary.com/v0/define'
        async with ctx.session.get(url, params={'term': word}) as resp:
            if resp.status != 200:
                return await ctx.send(f'An error occurred: {resp.status} {resp.reason}')

            js = await resp.json()
            data = js.get('list', [])
            if not data:
                return await ctx.send('No results found, sorry.')

        pages = RoboPages(UrbanDictionaryPageSource(data), ctx=ctx)
        await pages.start()

    @commands.hybrid_command(name='define')
    @app_commands.describe(word='The word to look up')
    async def _define(self, ctx: Context, *, word: str):
        """Looks up an English word in the dictionary."""

        result = await parse_free_dictionary_for_word(ctx.session, word=word)
        if result is None:
            return await ctx.send('Could not find that word.', ephemeral=True)

        # Check if it's a phrasal verb somehow
        phrase = discord.utils.find(lambda v: v.word.lower() == word.lower(), result.phrasal_verbs)
        if phrase is not None:
            embed = phrase.to_embed()
            return await ctx.send(embed=embed)

        if not result.meanings:
            return await ctx.send('Could not find any definitions for that word.', ephemeral=True)

        # Paginate over the various meanings of the word
        pages = RoboPages(FreeDictionaryWordMeaningPageSource(result), ctx=ctx, compact=True)
        await pages.start()

    @_define.autocomplete('word')
    async def _define_word_autocomplete(
        self, interaction: discord.Interaction, query: str
    ) -> list[app_commands.Choice[str]]:
        if not query:
            return []

        result = await free_dictionary_autocomplete_query(self.bot.session, query=query)
        return [app_commands.Choice(name=word, value=word) for word in result][:25]

    @commands.command(name='convert')
    async def _convert(self, ctx: Context, *, values: Annotated[Set[Unit], UnitCollector] = None):
        """Converts between various units.

        Supported unit conversions:

        - km <-> mi
        - m <-> ft
        - cm <-> in
        - kg <-> lb
        - L <-> gal
        - °C <-> °F
        """

        if values is None:
            reply = ctx.replied_message
            if reply is None:
                return await ctx.send('You need to provide some values to convert or reply to a message with values.')

            values = await UnitCollector().convert(ctx, reply.content)

        pairs: list[tuple[str, str]] = []
        for value in values:
            original = f'{value.value:g}{value.display_unit}'
            converted = value.converted()
            pairs.append((original, f'{converted.value:g}{converted.display_unit}'))

        # Pad for width since this is monospace
        width = max(len(original) for original, _ in pairs)
        fmt = '\n'.join(f'{original:<{width}} -> {converted}' for original, converted in pairs)
        await ctx.send(f'```\n{fmt}\n```')

    @_convert.error
    async def on_convert_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))


async def setup(bot: RoboDanny):
    await bot.add_cog(Buttons(bot))
