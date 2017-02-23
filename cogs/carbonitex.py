import aiohttp
import json
import logging

log = logging.getLogger()

CARBONITEX_API_BOTDATA = 'https://www.carbonitex.net/discord/data/botdata.php'
DISCORD_BOTS_API       = 'https://bots.discord.pw/api'

class Carbonitex:
    """Cog for updating carbonitex.net and bots.discord.pw bot information."""
    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()

    def __unload(self):
        # pray it closes
        self.bot.loop.create_task(self.session.close())

    async def update(self):
        carbon_payload = {
            'key': self.bot.carbon_key,
            'servercount': len(self.bot.servers)
        }

        async with self.session.post(CARBONITEX_API_BOTDATA, data=carbon_payload) as resp:
            log.info('Carbon statistics returned {0.status} for {1}'.format(resp, carbon_payload))

        payload = json.dumps({
            'server_count': len(self.bot.servers)
        })

        headers = {
            'authorization': self.bot.bots_key,
            'content-type': 'application/json'
        }

        url = '{0}/bots/{1.user.id}/stats'.format(DISCORD_BOTS_API, self.bot)
        async with self.session.post(url, data=payload, headers=headers) as resp:
            log.info('DBots statistics returned {0.status} for {1}'.format(resp, payload))

    async def on_server_join(self, server):
        await self.update()

    async def on_server_remove(self, server):
        await self.update()

    async def on_ready(self):
        await self.update()

def setup(bot):
    bot.add_cog(Carbonitex(bot))
