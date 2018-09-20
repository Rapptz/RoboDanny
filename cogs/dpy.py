from discord.ext import commands
import discord
import re

DISCORD_PY_GUILD_ID = 336642139381301249
DISCORD_PY_BOTS_ROLE = 381980817125015563
DISCORD_PY_REWRITE_ROLE = 381981861041143808
DISCORD_PY_JP_ROLE = 490286873311182850

class DPYExclusive:
    def __init__(self, bot):
        self.bot = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')
        self._invite_cache = {}
        self.bot.loop.create_task(self._prepare_invites())

    async def _prepare_invites(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(DISCORD_PY_GUILD_ID)
        invites = await guild.invites()
        self._invite_cache = {
            invite.code: invite.uses
            for invite in invites
        }

    def __local_check(self, ctx):
        return ctx.guild and ctx.guild.id == DISCORD_PY_GUILD_ID

    async def on_member_join(self, member):
        if member.guild.id != DISCORD_PY_GUILD_ID:
            return

        if member.bot:
            await member.add_roles(discord.Object(id=DISCORD_PY_BOTS_ROLE))
            return

        JP_INVITE_CODES = ('y9Bm8Yx', 'nXzj3dg')
        invites = await member.guild.invites()
        for invite in invites:
            if invite.code in JP_INVITE_CODES and invite.uses > self._invite_cache[invite.code]:
                await member.add_roles(discord.Object(id=DISCORD_PY_JP_ROLE))
            self._invite_cache[invite.code] = invite.uses

    async def on_message(self, message):
        if not message.guild or message.guild.id != DISCORD_PY_GUILD_ID:
            return

        m = self.issue.search(message.content)
        if m is not None:
            url = 'https://github.com/Rapptz/discord.py/issues/'
            await message.channel.send(url + m.group('number'))

    async def toggle_role(self, ctx, role_id):
        if any(r.id == role_id for r in ctx.author.roles):
            try:
                await ctx.author.remove_roles(discord.Object(id=role_id))
            except:
                await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
            else:
                await ctx.message.add_reaction('\N{HEAVY MINUS SIGN}')
            finally:
                return

        try:
            await ctx.author.add_roles(discord.Object(id=role_id))
        except:
            await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
        else:
            await ctx.message.add_reaction('\N{HEAVY PLUS SIGN}')

    @commands.command(hidden=True)
    async def rewrite(self, ctx):
        """Gives you the rewrite role.

        Necessary to get rewrite help and news.
        """

        await self.toggle_role(ctx, DISCORD_PY_REWRITE_ROLE)

    @commands.command(hidden=True, aliases=['日本語'])
    async def nihongo(self, ctx):
        """日本語チャットに参加したい場合はこの役職を付ける"""

        await self.toggle_role(ctx, DISCORD_PY_JP_ROLE)

def setup(bot):
    bot.add_cog(DPYExclusive(bot))
