from discord.ext import commands
import discord
import re

DISCORD_PY_GUILD_ID = 336642139381301249
DISCORD_PY_BOTS_ROLE = 381980817125015563
DISCORD_PY_REWRITE_ROLE = 381981861041143808

class DPYExclusive:
    def __init__(self, bot):
        self.bot = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')

    async def on_member_join(self, member):
        if member.guild.id != DISCORD_PY_GUILD_ID:
            return

        if member.bot:
            await member.add_roles(discord.Object(id=DISCORD_PY_BOTS_ROLE))

    async def on_message(self, message):
        if not message.guild or message.guild.id != DISCORD_PY_GUILD_ID:
            return

        m = self.issue.search(message.content)
        if m is not None:
            url = 'https://github.com/Rapptz/discord.py/issues/'
            await message.channel.send(url + m.group('number'))

    @commands.command(hidden=True)
    @commands.check(lambda ctx: ctx.guild and ctx.guild.id == DISCORD_PY_GUILD_ID)
    async def rewrite(self, ctx):
        """Gives you the rewrite role.

        Necessary to get rewrite help and news.
        """

        if any(r.id == DISCORD_PY_REWRITE_ROLE for r in ctx.author.roles):
            return await ctx.message.add_reaction('\N{WARNING SIGN}')

        try:
            await ctx.author.add_roles(discord.Object(id=DISCORD_PY_REWRITE_ROLE))
        except:
            await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
        else:
            await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

def setup(bot):
    bot.add_cog(DPYExclusive(bot))
