import aiohttp
import json
import logging

log = logging.getLogger(__name__)

CARBONITEX_API_BOTDATA = 'https://www.carbonitex.net/discord/data/botdata.php'
DISCORD_BOTS_API       = 'https://bots.discord.pw/api'

class Carbonitex:
    """Cog for updating carbonitex.net and bots.discord.pw bot information."""
    def __init__(self, bot):
        self.bot = bot

    async def update(self):
        guild_count = len(self.bot.guilds)
        carbon_payload = {
            'key': self.bot.carbon_key,
            'servercount': guild_count
        }

        async with self.bot.session.post(CARBONITEX_API_BOTDATA, data=carbon_payload) as resp:
            log.info(f'Carbon statistics returned {resp.status} for {carbon_payload}')

        payload = json.dumps({
            'server_count': guild_count
        })

        headers = {
            'authorization': self.bot.bots_key,
            'content-type': 'application/json'
        }

        url = f'{DISCORD_BOTS_API}/bots/{self.bot.user.id}/stats'
        async with self.bot.session.post(url, data=payload, headers=headers) as resp:
            log.info(f'DBots statistics returned {resp.status} for {payload}')

    async def on_guild_join(self, guild):
        await self.update()

    async def on_guild_remove(self, guild):
        await self.update()

    async def on_ready(self):
        await self.update()

def setup(bot):
    bot.add_cog(Carbonitex(bot))
