from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Optional, TypedDict
from typing_extensions import Self
from discord.ext import commands, menus
from discord import app_commands
from lxml import html
from itertools import groupby
import discord
from .utils.paginator import RoboPages
import logging
import yarl
import re

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .utils.context import GuildContext, Context
    from .utils.paginator import RoboPages
    from bot import RoboDanny
    from aiohttp import ClientSession, ClientResponse


class IchiError(RuntimeError):
    pass


class HTTPError(IchiError):
    def __init__(self, resp: ClientResponse, *args):
        self.resp = resp
        super().__init__(*args)


class Cell:
    """Represents an ichi.moe cell.

    There can be many cells in ichi.moe. They comprise a morphological fragment
    that is deconjugated. Since it deals with a rather large fragment, compound
    words are listed as multiple words.

    Attributes
    -----------
    url: Optional[str]
        The URL for the word's page.
    word: Optional[Word]
        The main word that defines this cell.
    romaji: Optional[str]
        The romaji of the word
    other_words: List[Word]
        Other words that are part of this cell.
    """

    url: Optional[str]
    romaji: Optional[str]
    word: Optional[Word]
    other_words: list[Word]

    def __init__(self, node: Any):
        self._parse(node)

    def _parse(self, node: Any):
        url_node = node.find("./div[@class='gloss-rtext']/a[@class='info-link']")
        if url_node is not None:
            self.url = 'https://ichi.moe' + url_node.get('href')
            self.romaji = url_node.text_content().strip()
        else:
            self.romaji = None
            self.url = None

        words = definition_list_to_words(node.find("./div/dl[@class='alternatives']"))
        if len(words) >= 1:
            self.word = words[0]
            self.other_words = words[1:]
        else:
            self.word = None
            self.other_words = []

    @property
    def words(self) -> tuple[Word, ...]:
        if self.word:
            return (self.word, *self.other_words)
        return tuple()


def convert_to_word(current_word, node):
    compound = node.find("./span[@class='compound-desc']/span[@class='compound-desc-word']")
    if compound is not None:
        return CompoundWord(current_word, node, compound)

    counter = node.find("span[@class='counter-value']")
    if counter is not None:
        return Counter(current_word, node, counter)

    # I have yet to find a word with more than one conjugation element.
    # I'd like to be proven wrong
    conjugation = node.find("div[@class='conjugations']/div[@class='conjugation']")
    if conjugation is not None:
        return ConjugatedWord(current_word, node, conjugation)

    return Word(current_word, node)


def definition_list_to_words(dl: Any) -> list[Word]:
    if dl is None:
        return []

    # dt must come before dd
    # the dt element has the word
    # the dd element has the metadata
    current_word = None
    words = []
    for child in dl:
        if child.tag == 'dt':
            current_word = child.text
            continue

        if child.tag == 'dd':
            words.append(convert_to_word(current_word, child))
    return words


class Definition:
    """Represents a definition to a word."""

    pos: list[str]
    meaning: str
    note: Optional[str]

    def __init__(self, node):
        self.pos = pos = node.find("span[@class='pos-desc']")
        if pos is not None:
            self.pos = pos.text[1:-1].split(',')
        else:
            self.pos = []

        self.meaning = meaning = node.find("span[@class='gloss-desc']")
        if meaning is not None:
            self.meaning = meaning.text
        else:
            self.meaning = ''

        self.note = note = node.find('span[@title]')
        if note is not None:
            self.note = note.get('title')

    def to_dict(self):
        return {
            'pos': self.pos,
            'meaning': self.meaning,
            'note': self.note,
        }

    def __format__(self, format_spec):
        value = self.meaning if self.note is None else f'{self.meaning} [{self.note}]'
        if format_spec == 's':
            return value
        else:
            return f'[{",".join(self.pos)}] {value}'


class Word:
    """Represents a word in ichi.moe."""

    READING_RE = re.compile(r'(?:\d+\.\s*)?(\w+)\s*(?:【(.+)】)?')

    def __init__(self, word: str, node: Any):
        self.raw_word: str = word
        defns = node.xpath("ol[@class='gloss-definitions']/li")
        self.definitions = [Definition(n) for n in defns]
        match = self.READING_RE.match(word)
        if match is None:
            raise IchiError(f'{word} does not match regex.')

        self.word: str = match.group(1)
        self.reading: str = match.group(2)
        self.description: Optional[str] = None

        desc = node.find("span[@class='suffix-desc']")
        if desc is not None:
            self.description = desc.text.strip()[1:-1]


class Counter(Word):
    """A special case of a word that represents a counter."""

    def __init__(self, word: str, word_node: Any, counter_node: Any):
        super().__init__(word, word_node)
        value = re.search(r'\d+', counter_node.text)
        if value is not None:
            self.value = int(value.group(0))
        else:
            self.value = 0


class ConjugationProperty:
    """Represents a conjugation property of a conjugation.

    Basically the type of conjugation and the part of speech.
    """

    __slots__ = ('pos', 'type', 'negative', 'formal')

    def __init__(self, node: Any):
        self.pos: Optional[str] = None
        self.type: Optional[str] = None
        self.negative = False
        self.formal = False

        for child in node:
            if child.tag != 'span':
                continue

            class_ = child.get('class')
            if class_ == 'pos-desc':
                self.pos = child.text_content()[1:-1]
            elif class_ == 'conj-type':
                self.type = child.text
            elif class_ == 'conj-formal':
                self.formal = True
            elif class_ == 'conj-negative':
                self.negative = True

    @property
    def full_type(self):
        words = []
        if self.type:
            words.append(self.type)
        if self.negative:
            words.append('Negative')
        if self.formal:
            words.append('Formal')
        return ' '.join(words)


class Conjugation:
    """Represents a word conjugation."""

    property: Optional[ConjugationProperty]
    gloss: Optional[Word]
    via: Optional[Conjugation]

    def __init__(self, conjugation: Any):
        prop = conjugation.find("div[@class='conj-prop']")
        if prop is not None:
            self.property = ConjugationProperty(prop)
        else:
            self.property = None

        gloss = conjugation.find("div[@class='conj-gloss']/dl")
        if gloss is not None:
            nodes = list(gloss)
            self.gloss = convert_to_word(nodes[0].text, nodes[1])
        else:
            self.gloss = None

        via = conjugation.find("div[@class='conj-via']")
        if via is not None:
            self.via = Conjugation(via)
        else:
            self.via = None


class ConjugatedWord(Word, Conjugation):
    """A special case of a word that is conjugated."""

    def __init__(self, word, word_node, conjugation):
        Word.__init__(self, word, word_node)
        Conjugation.__init__(self, conjugation)


class CompoundWord(Word):
    """A word that is made up of many different words."""

    def __init__(self, word: str, node: Any, compound: Any):
        super().__init__(word, node)
        self.fragments: str = compound.text.strip()
        self.compounds = definition_list_to_words(node.find("dl[@class='compounds']"))


async def get_cells(session: ClientSession, query: str) -> list[Cell]:
    params = {
        'q': query,
        'r': 'htr',
    }
    headers = {
        'DNT': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64; rv:69.0) Gecko/20100101 Firefox/69.0',
    }
    url = 'https://ichi.moe/cl/qr'

    async with session.get(url, params=params, headers=headers) as resp:
        if resp.status != 200:
            raise HTTPError(resp, f'{url!r} failed with {resp.status}')

        root = html.fromstring(await resp.read())
        glosses = root.xpath(".//div[@class='gloss-all']/div[@class='row gloss-row']/ul/li/div[@class='gloss']")
        return [Cell(node) for node in glosses]


JMDICT_TAGS = {
    "MA": "martial arts term",
    "X": "rude or X-rated term",
    "abbr": "abbreviation",
    "adj-i": "い-adjective",
    "adj-ix": "いい-adjective",
    "adj-na": "な-adjective",
    "adj-no": "の-adjective",
    "adj-pn": "pre-noun adjective",
    "adj-t": "たる-adjective",
    "adj-f": "pre-noun adjective",
    "adv": "adverb",
    "adv-to": "と-adverb",
    "arch": "archaism",
    "ateji": "ateji (phonetic) reading",
    "aux": "auxiliary",
    "aux-v": "auxiliary verb",
    "aux-adj": "auxiliary adjective",
    "Buddh": "Buddhist term",
    "chem": "chemistry term",
    "chn": "children's language",
    "col": "colloquialism",
    "comp": "computer terminology",
    "conj": "conjunction",
    "cop-da": "copula",
    "ctr": "counter",
    "derog": "derogatory",
    "eK": "exclusively kanji",
    "ek": "exclusively kana",
    "exp": "expression",
    "fam": "familiar language",
    "fem": "female language",
    "food": "food term",
    "geom": "geometry term",
    "hon": "keigo",
    "hum": "kenjougo",
    "iK": "irregular kanji",
    "id": "idiom",
    "ik": "irregular kana",
    "int": "interjection",
    "io": "irregular okurigana usage",
    "iv": "irregular verb",
    "ling": "linguistics terminology",
    "m-sl": "manga slang",
    "male": "male language",
    "male-sl": "male slang",
    "math": "mathematics",
    "mil": "military",
    "n": "noun",
    "n-adv": "adverbial noun",
    "n-suf": "suffix noun",
    "n-pref": "prefix noun",
    "n-t": "temporal noun",
    "num": "numeric",
    "oK": "out-dated kanji",
    "obs": "obsolete term",
    "obsc": "obscure term",
    "ok": "out-dated kana",
    "oik": "irregular kana form",
    "on-mim": "onomatopoeia",
    "pn": "pronoun",
    "poet": "poetical term",
    "pol": "teineigo",
    "pref": "prefix",
    "proverb": "proverb",
    "prt": "particle",
    "physics": "physics terminology",
    "quote": "quotation",
    "rare": "rare",
    "sens": "sensitive",
    "sl": "slang",
    "suf": "suffix",
    "uK": "usually in kanji alone",
    "uk": "usually in kana alone",
    "unc": "unclassified",
    "yoji": "yojijukugo (four-character idiom)",
    "v1": "る-verb",
    "v1-s": "る-verb",
    "v5aru": "う-verb - -aru special class",
    "v5b": "う-verb",
    "v5g": "う-verb",
    "v5k": "う-verb",
    "v5k-s": "う-verb - 行く・ゆく special class",
    "v5m": "う-verb",
    "v5n": "う-verb",
    "v5r": "う-verb",
    "v5r-i": "irregular う-verb",
    "v5s": "う-verb",
    "v5t": "う-verb",
    "v5u": "う-verb",
    "v5u-s": "special う-verb",
    "v5uru": "う-verb - Uru old class verb (old form of Eru)",
    "vz": "ずる-verb",
    "vi": "intransitive verb",
    "vk": "くる-verb",
    "vn": "irregular ぬ-verb",
    "vr": "irregular る-verb, plain form ends with -り",
    "vs": "する-verb",
    "vs-c": "su verb - precursor to the modern suru",
    "vs-s": "special する-verb",
    "vs-i": "irregular する-verb",
    "kyb": "Kyoto-ben",
    "osb": "Osaka-ben",
    "ksb": "Kansai-ben",
    "ktb": "Kantou-ben",
    "tsb": "Tosa-ben",
    "thb": "Touhoku-ben",
    "tsug": "Tsugaru-ben",
    "kyu": "Kyuushuu-ben",
    "rkb": "Ryuukyuu-ben",
    "nab": "Nagano-ben",
    "hob": "Hokkaido-ben",
    "vt": "transitive verb",
    "vulg": "vulgar",
    "n-pr": "proper noun",
    "v-unspec": "unspecified verb",
    "archit": "architecture term",
    "astron": "astronomy term",
    "baseb": "baseball term",
    "biol": "biology term",
    "bot": "botany term",
    "bus": "business term",
    "econ": "economics term",
    "engr": "engineering term",
    "finc": "finance term",
    "geol": "geology term",
    "law": "law term",
    "mahj": "mahjong term",
    "med": "medical term",
    "music": "music term",
    "Shinto": "Shinto term",
    "shogi": "shogi term",
    "sports": "sports term",
    "sumo": "sumo term",
    "zool": "zoology term",
    "joc": "humorous term",
    "anat": "anatomical term",
}


class IchiPageSource(menus.ListPageSource):
    # <Raw> (Word)
    # [POS] <- grouped by this
    # 1. definition (note: ...)
    # 2. ...
    # -------
    # <Raw> (Counter)
    # Value: <value>
    # <Same as definition list>
    # -------
    # <Raw> (ConjugatedWord)
    # If Via is available then use Via instead
    # <Gloss.Raw> <Property.Type>
    # <Same as definition list except for Gloss>
    # -------
    # <Raw> (CompoundWord)
    # <Compound: ...>
    # N fields for every element up there ^

    def format_definition_list(self, builder: list[str], word: Word):
        jmdict_tags = JMDICT_TAGS
        for pos, definitions in self.group_by_definition_list(word):
            extended_pos = ', '.join(jmdict_tags[p] for p in pos if p in jmdict_tags)
            if extended_pos:
                builder.append(f'**{extended_pos}**')
            if len(definitions) == 1:
                builder.append(f'{definitions[0]:s}')
            else:
                for index, defn in enumerate(definitions, start=1):
                    builder.append(f'{index}. {defn:s}')

    def group_by_definition_list(self, word: Word):
        defns = sorted(word.definitions, key=lambda w: w.pos)
        grouped: list[tuple[list[str], list[Definition]]] = []
        for key, groups in groupby(defns, key=lambda w: w.pos):
            grouped.append((key, list(groups)))
        return sorted(grouped, key=lambda t: len(t[1]), reverse=True)

    def format_word_title(self, word: str, romaji: Optional[str] = None):
        # if the word is ending with an "】", then we don't need to add a space.
        # Due to this character having extra padding, this would look far apart.
        if word.endswith('】'):
            return word.strip() + (f"({romaji})" if romaji else '')

        return word.strip() + (f"  ({romaji})" if romaji else '')

    def format_word(self, embed: discord.Embed, word: Word, romaji: Optional[str] = None):
        builder = [word.description] if word.description else []
        self.format_definition_list(builder, word)

        embed.add_field(
            name=self.format_word_title(word.raw_word, romaji),
            value='\n'.join(builder) or '...',
            inline=False
        )

    def format_counter(self, embed: discord.Embed, word: Counter, romaji: Optional[str] = None):
        value = f'Value: {word.value}'
        builder = [word.description, value] if word.description else [value]
        self.format_definition_list(builder, word)

        embed.add_field(
            name=self.format_word_title(word.raw_word, romaji),
            value='\n'.join(builder) or '...', inline=False
        )

    def format_conjugated_word(self, embed: discord.Embed, word: ConjugatedWord, romaji: Optional[str] = None):
        conj = word.via if word.via else word
        builder = [word.description] if word.description else []
        if word.definitions:
            self.format_definition_list(builder, word)

        if conj.gloss:
            if conj.property:
                builder.append(f'{conj.property.full_type} for {conj.gloss.raw_word}')
            self.format_definition_list(builder, conj.gloss)

        embed.add_field(
            name=self.format_word_title(word.raw_word, romaji),
            value='\n'.join(builder) or '...',
            inline=False
        )

    def format_compound_word(self, embed: discord.Embed, word: CompoundWord):
        embed.description = f'Compound: {word.fragments}'
        for compound in word.compounds:
            self.format_dispatch(embed, compound)

    def format_dispatch(self, embed: discord.Embed, word: Optional[Word], romaji: Optional[str] = None):
        if isinstance(word, ConjugatedWord):
            self.format_conjugated_word(embed, word, romaji)
        elif isinstance(word, CompoundWord):
            self.format_compound_word(embed, word)
        elif isinstance(word, Counter):
            self.format_counter(embed, word, romaji)
        elif word is not None:
            self.format_word(embed, word, romaji)

    def format_page(self, menu, cell: Cell):
        e = discord.Embed(title=menu.query_string, colour=0xDB00A5)

        romajis = (cell.romaji or "").split("/")
        self.format_dispatch(
            e,
            cell.word,
            romajis.pop(0) if romajis else None
        )

        for (word, romaji) in zip(cell.other_words, romajis):
            self.format_dispatch(e, word, romaji)

        e.set_footer(text=f'Page {menu.current_page + 1}/{self.get_max_pages()}')
        return e


class IchiAnalyzerPages(RoboPages):
    def __init__(self, ctx: Context, query: str, cells: list[Cell]):
        super().__init__(IchiPageSource(cells, per_page=1), ctx=ctx, compact=True)
        self.query = query
        self.query_string = '\N{KATAKANA MIDDLE DOT}'.join(x.word.word for x in cells if x.word is not None)


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


class Dictionary(commands.Cog):
    """Commands to look up words in various dictionaries."""

    def __init__(self, bot: RoboDanny) -> None:
        self.bot: RoboDanny = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{BOOKS}')

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

    @commands.hybrid_command()
    @app_commands.describe(query='The Japanese sentence to analyze')
    async def jisho(self, ctx: Context, *, query: str):
        """Analyzes a Japanese sentence into fragments"""

        if len(query) >= 256:
            return await ctx.send(f'Too long: {len(query)}/255')

        try:
            cells = await get_cells(self.bot.session, query)
        except HTTPError as e:
            return await ctx.send(str(e))

        if not cells:
            return await ctx.send('Nothing to process?')

        pages = IchiAnalyzerPages(ctx, query, cells)
        await pages.start()


async def setup(bot: RoboDanny) -> None:
    await bot.add_cog(Dictionary(bot))
