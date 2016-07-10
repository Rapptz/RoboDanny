from discord.ext import commands
from datetime import datetime
import discord
from .utils import checks

def date(argument):
    formats = (
        '%Y/%m/%d',
        '%Y-%m-%d',
    )

    for fmt in formats:
        try:
            return datetime.strptime(argument, fmt)
        except ValueError:
            continue

    raise commands.BadArgument('Cannot convert to date. Expected YYYY/MM/DD or YYYY-MM-DD.')

class Buttons:
    """Buttons that make you feel."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(hidden=True)
    async def feelgood(self):
        """press"""
        await self.bot.say('*pressed*')

    @commands.command(hidden=True)
    async def feelbad(self):
        """depress"""
        await self.bot.say('*depressed*')

    @commands.command()
    async def love(self):
        """What is love?"""
        await self.bot.say('http://i.imgur.com/JthwtGA.png')

    @commands.command(hidden=True)
    async def bored(self):
        """boredom looms"""
        await self.bot.say('http://i.imgur.com/BuTKSzf.png')

    @commands.command(pass_context=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def nostalgia(self, ctx, date: date, *, channel: discord.Channel = None):
        """Pins an old message from a specific date.

        If a channel is not given, then pins from the channel the
        command was ran on.

        The format of the date must be either YYYY-MM-DD or YYYY/MM/DD.
        """

        if channel is None:
            channel = ctx.message.channel

        async for m in self.bot.logs_from(channel, after=date, limit=1):
            try:
                await self.bot.pin_message(m)
            except:
                await self.bot.say('\N{THUMBS DOWN SIGN} Could not pin message.')
            else:
                await self.bot.say('\N{THUMBS UP SIGN} Successfully pinned message.')

    @nostalgia.error
    async def nostalgia_error(self, error, ctx):
        if type(error) is commands.BadArgument:
            await self.bot.say(error)

def setup(bot):
    bot.add_cog(Buttons(bot))
