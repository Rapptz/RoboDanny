from discord.ext import commands
from .utils import config, checks

import discord
import re

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

def setup(bot):
    bot.add_cog(Emoji(bot))
