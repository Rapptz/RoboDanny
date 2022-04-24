from __future__ import annotations
from typing import TYPE_CHECKING

from discord.ext import commands
import discord
import json
import logging

if TYPE_CHECKING:
    from bot import RoboDanny

log = logging.getLogger(__name__)

DISCORD_BOTS_API = 'https://discord.bots.gg/api/v1'


class DiscordBots(commands.Cog):
    """Cog for updating bots.discord.pw bot information."""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot

    async def update(self) -> None:
        guild_count = len(self.bot.guilds)

        payload = json.dumps(
            {
                'guildCount': guild_count,
                'shardCount': len(self.bot.shards),
            }
        )

        headers = {
            'authorization': self.bot.bots_key,
            'content-type': 'application/json',
        }

        url = f'{DISCORD_BOTS_API}/bots/{self.bot.user.id}/stats'
        async with self.bot.session.post(url, data=payload, headers=headers) as resp:
            log.info(f'DBots statistics returned {resp.status} for {payload}')

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.update()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.update()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.update()


async def setup(bot: RoboDanny):
    await bot.add_cog(DiscordBots(bot))
