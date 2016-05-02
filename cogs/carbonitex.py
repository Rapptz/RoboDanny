import aiohttp
import logging

log = logging.getLogger()

CARBONITEX_API_BOTDATA = 'https://www.carbonitex.net/discord/data/botdata.php'

class Carbonitex:
    """Cog for updating carbonitex.net bot information."""
    def __init__(self, bot):
        self.bot = bot

    async def _update_carbon(self):
        payload = {
            'key': self.bot.carbon_key,
            'servercount': len(self.bot.servers)
        }

        async with aiohttp.post(CARBONITEX_API_BOTDATA, data=payload) as resp:
            log.info('Carbon statistics returned {0.status} for {1}'.format(resp, payload))

    async def on_server_join(self, server):
        await self._update_carbon()

    async def on_server_leave(self, server):
        await self._update_carbon()

    async def on_ready(self):
        await self._update_carbon()

def setup(bot):
    bot.add_cog(Carbonitex(bot))
