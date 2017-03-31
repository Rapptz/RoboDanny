from discord.ext import commands
from .utils import config

from collections import Counter

import discord
import re

BLOB_GUILD_ID = '272885620769161216'

class Emoji:
    """Custom emoji tracking statistics for Wolfiri"""

    def __init__(self, bot):
        self.bot = bot
        self.regex = re.compile(r'<:.+?:([0-9]{16,21})>')

        # guild_id: data
        # where data is
        # emoji_id: count
        self.config = config.Config('emoji_statistics.json')

    async def on_message(self, message):
        if message.server is None:
            return

        matches = self.regex.findall(message.content)
        if not matches:
            return

        db = self.config.get(message.server.id, {})
        for emoji_id in matches:
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
        global_usage = Counter()
        for data in self.config.all().values():
            global_usage.update(data)

        global_usage = Counter(x for x in global_usage.elements() if x in blob_ids)

        e = discord.Embed(title='Blob Statistics', colour=0xf1c40f)

        common = global_usage.most_common()
        top = ['%s: %s times' % (blob_ids.get(key), count) for key, count in common[0:7]]
        bottom = ['%s: %s times' % (blob_ids.get(key), count) for key, count in common[-7:]]
        e.add_field(name='Most Common', value='\n'.join(top), inline=False)
        e.add_field(name='Least Common', value='\n'.join(bottom), inline=False)
        return e

    def get_blob_stats_for(self, emoji):
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis}

        e = discord.Emoji(colour=0xf1c40f, title='Statistics for ' + emoji.name)
        valid = blob_ids.get(emoji.id)
        if valid is None:
            e.description = 'Not a valid blob.'
            return e

        global_usage = Counter()
        for data in self.config.all().values():
            global_usage.update(data)

        global_usage = Counter(x for x in global_usage.elements() if x in blob_ids)
        usage = global_usage.get(emoji.id)

        rank = None
        for (index, (e, _)) in enumerate(global_usage.most_common()):
            if e == emoji.id:
                rank = index + 1
                break

        e.add_field(name='Usage', value=usage)
        e.add_field(name='Rank', value=rank)
        return e

    @commands.command(hidden=True)
    async def blobstats(self, *, emoji: discord.Emoji = None):
        """Usage statistics of blobs."""
        if emoji is None:
            e = self.get_all_blob_stats()
        else:
            e = self.get_blob_stats_for(emoji)

        await self.bot.say(embed=e)

def setup(bot):
    bot.add_cog(Emoji(bot))
