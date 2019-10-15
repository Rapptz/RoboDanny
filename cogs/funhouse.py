from discord.ext import commands
from .utils import checks
import discord

GUILD_ID = 81883016288276480
VOICE_ROOM_ID = 633466718035116052
GENERAL_VOICE_ID = 81883016309248000

class Funhouse(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.guild.id != GUILD_ID:
            return

        voice_room = member.guild.get_channel(VOICE_ROOM_ID)
        if before.channel is None and after.channel is not None and after.channel.id == GENERAL_VOICE_ID:
            # joined a channel
            await voice_room.set_permissions(member, read_messages=True)
        elif after.channel is None and before.channel is not None and before.channel.id == GENERAL_VOICE_ID:
            # left the channel
            await voice_room.set_permissions(member, read_messages=False)

    @commands.command(hidden=True)
    async def cat(self, ctx):
        """Gives you a random cat."""
        async with ctx.session.get('https://aws.random.cat/meow') as resp:
            if resp.status != 200:
                return await ctx.send('No cat found :(')
            js = await resp.json()
            await ctx.send(embed=discord.Embed(title='Random Cat').set_image(url=js['file']))

def setup(bot):
    bot.add_cog(Funhouse(bot))
