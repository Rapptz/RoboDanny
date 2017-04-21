from discord.ext import commands
from .utils import config, checks

from collections import Counter

import discord
import datetime
import re
import io

BLOB_GUILD_ID = '272885620769161216'
EMOJI_REGEX = re.compile(r'<:.+?:([0-9]{15,21})>')

class BlobEmoji(commands.Converter):
    def convert(self):
        guild = self.ctx.bot.get_server(BLOB_GUILD_ID)
        emojis = {e.id: e for e in guild.emojis}

        m = EMOJI_REGEX.match(self.argument)
        if m is not None:
            emoji = emojis.get(m.group(1))
        elif self.argument.isdigit():
            emoji = emojis.get(self.argument)
        else:
            emoji = discord.utils.find(lambda e: e.name == self.argument, emojis.values())

        if emoji is None:
            raise commands.BadArgument('Not a valid blob emoji.')
        return emoji

def usage_per_day(dt, usages):
    tracking_started = datetime.datetime(2017, 3, 31)
    now = datetime.datetime.utcnow()
    if dt < tracking_started:
        base = tracking_started
    else:
        base = dt

    days = (now - base).total_seconds() / 86400 # 86400 seconds in a day
    if int(days) == 0:
        return usages
    return usages / days

class Emoji:
    """Custom emoji tracking statistics for Wolfiri"""

    def __init__(self, bot):
        self.bot = bot

        # guild_id: data
        # where data is
        # emoji_id: count
        self.config = config.Config('emoji_statistics.json')

    def __check(self, ctx):
        server = ctx.message.server
        if server is not None and server.id == BLOB_GUILD_ID:
            return ctx.prefix == '?'
        return True

    async def on_message(self, message):
        if message.server is None:
            return

        if message.author.bot:
            return # no bots.

        matches = EMOJI_REGEX.findall(message.content)
        if not matches:
            return

        db = self.config.get(message.server.id, {})
        for emoji_id in set(matches):
            try:
                count = db[emoji_id]
            except KeyError:
                db[emoji_id] = 1
            else:
                db[emoji_id] = count + 1

        await self.config.put(message.server.id, db)

    def get_all_blob_stats(self):
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis}
        total_usage = Counter()
        for data in self.config.all().values():
            total_usage.update(data)

        blob_usage = Counter({e: 0 for e in blob_ids})
        blob_usage.update(x for x in total_usage.elements() if x in blob_ids)

        e = discord.Embed(title='Blob Statistics', colour=0xf1c40f)

        common = blob_usage.most_common()
        total_count = sum(blob_usage.values())
        global_usage = sum(total_usage.values())
        fmt = '{0} ({1:.2%} of all emoji usage)'

        e.add_field(name='Total Usage', value=fmt.format(total_count, total_count / global_usage))

        def elem_to_string(key, count):
            elem = blob_ids.get(key)
            per_day = usage_per_day(elem.created_at, count)
            return '{0}: {1} times, {3:.2f}/day ({2:.2%})'.format(elem, count, count / total_count, per_day)

        top = [elem_to_string(key, count) for key, count in common[0:7]]
        bottom = [elem_to_string(key, count) for key, count in common[-7:]]
        e.add_field(name='Most Common', value='\n'.join(top), inline=False)
        e.add_field(name='Least Common', value='\n'.join(bottom), inline=False)
        return e

    def get_blob_stats_for(self, emoji):
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis}

        e = discord.Embed(colour=0xf1c40f, title='Statistics')
        total_usage = Counter()
        for data in self.config.all().values():
            total_usage.update(data)

        blob_usage = Counter({e: 0 for e in blob_ids})
        blob_usage.update(x for x in total_usage.elements() if x in blob_ids)
        usage = blob_usage.get(emoji.id)
        total = sum(blob_usage.values())

        rank = None
        for (index, (x, _)) in enumerate(blob_usage.most_common()):
            if x == emoji.id:
                rank = index + 1
                break

        e.add_field(name='Emoji', value=emoji)
        e.add_field(name='Usage', value='{0}, {2:.2f}/day ({1:.2%})'.format(usage, usage / total,
                                                                            usage_per_day(emoji.created_at, usage)))
        e.add_field(name='Rank', value=rank)
        return e

    @commands.group(hidden=True, invoke_without_command=True)
    async def blobstats(self, *, emoji: BlobEmoji = None):
        """Usage statistics of blobs."""
        if emoji is None:
            e = self.get_all_blob_stats()
        else:
            e = self.get_blob_stats_for(emoji)

        await self.bot.say(embed=e)

    @blobstats.error
    async def blobstats_error(self, error, ctx):
        if isinstance(error, commands.BadArgument):
            await self.bot.say(str(error))

    @blobstats.command(hidden=True, name='server')
    async def blobstats_server(self, *, server_id: str = None):
        """What server uses blobs the most?

        Useful for detecting abuse as well.
        """
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis}

        per_guild = Counter()

        # RIP
        for guild_id, data in self.config.all().items():
            for key in blob_ids:
                per_guild[guild_id] += data.get(key, 0)

        total_usage = sum(per_guild.values())

        e = discord.Embed(colour=0xf1c40f, title='Server Statistics')

        if server_id is not None:
            guild = self.bot.get_server(server_id)
            count = per_guild[server_id]
            e.title = guild.name if guild else 'Unknown Guild?'
            e.add_field(name='Usage', value='{0} ({1:.2%})'.format(count, count / total_usage))
            if guild and guild.me:
                e.add_field(name='Per Day', value=usage_per_day(guild.me.joined_at, count))
                e.set_footer(text='Joined on')
                e.timestamp = guild.me.joined_at

            return await self.bot.say(embed=e)

        top_ten = per_guild.most_common(10)

        def formatter(tup):
            guild = self.bot.get_server(tup[0])
            count = tup[1]
            percent = count / total_usage
            if guild is None:
                return '- <BAD:{0}>: {1} ({2:.2%})'.format(tup[0], count, percent)
            return '- {0}: {1}, {2:.2f}/day ({3:.2%})'.format(guild, count, usage_per_day(guild.me.joined_at, count), percent)

        e.description = '\n'.join(map(formatter, top_ten))
        await self.bot.say(embed=e)

    @commands.command(hidden=True)
    @checks.is_owner()
    async def clear_emoji_data(self, *, server_id):
        """Deletes a server's emoji data."""

        if self.config.get(server_id) is not None:
            await self.config.remove(server_id)
            await self.bot.say('Deleted.')
        else:
            await self.bot.say('No data found.')

    @commands.command(pass_context=True, aliases=['blobpost'], hidden=True)
    @checks.is_in_servers(BLOB_GUILD_ID)
    @checks.admin_or_permissions(administrator=True)
    async def blobsort(self, ctx):
        """Sorts the blob post."""
        emojis = sorted(ctx.message.server.emojis, key=lambda e: e.name)
        fp = io.BytesIO()
        pages = [emojis[i:i + 30] for i in range(0, len(emojis), 30)]

        for number, page in enumerate(pages, 1):
            fmt = 'Page %s\n' % number
            fp.write(fmt.encode('utf-8'))
            for emoji in page:
                fmt = ':{0.name}: = `:{0.name}:`\n'.format(emoji)
                fp.write(fmt.encode('utf-8'))

            fp.write(b'\n')

        fp.seek(0)
        await self.bot.upload(fp, filename='blob_posts.txt')

def setup(bot):
    bot.add_cog(Emoji(bot))
