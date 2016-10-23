import discord
import re
from discord.ext import commands
from .utils import checks, config

class Pokemon:
    """Pokemon related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('pokemon.json')
        self.friend_code_regex = re.compile(r'^(?P<one>[0-9]{4})[- _]?(?P<two>[0-9]{4})[- _]?(?P<three>[0-9]{4})$')

    async def create_or_get_friend_code(self, message):
        fcs = self.config.get('friend_codes', {})
        author = message.author
        channel = message.channel
        try:
            code = fcs[author.id]
        except KeyError:
            # not found, so create it
            await self.bot.send_message(channel, 'I could not find your friend code in the database, what is it?\n' \
                                                 '**You can say "cancel" without quotes to stop me from asking.**')

            for i in range(5):
                # keep asking the author for their FC
                def check(m):
                    return m.author.id == author.id and \
                           m.channel.id == channel.id and \
                          (m.content[0].isdigit() or m.content[0] == 'c')

                reply = await self.bot.wait_for_message(check=check, timeout=300.0)
                if reply is None:
                    return await self.bot.send_message(channel, 'You took too long, see ya.')

                if reply.content == 'cancel':
                    return await self.bot.send_message(channel, 'Alright, adios.')

                match = self.friend_code_regex.match(reply.content)
                if match is None:
                    msg = "That doesn't look like a valid friend code. %s tries remaining." % (4 - i,)
                    await self.bot.send_message(channel, msg)
                    continue

                code = '{one}-{two}-{three}'.format(**match.groupdict())
                fcs[author.id] = code
                await self.config.put('friend_codes', fcs)
                return await self.bot.send_message(channel, 'Successfully set friend code to ' + code)
        else:
            await self.bot.send_message(channel, '3DS Friend Code for %s: %s' % (author.display_name, code))

    @commands.command(pass_context=True)
    async def fc(self, ctx, *, member: discord.Member = None):
        """Retrieve someone's friend code or set your own."""
        if member is None:
            return await self.create_or_get_friend_code(ctx.message)

        fcs = self.config.get('friend_codes', {})
        try:
            code = fcs[member.id]
        except KeyError:
            await self.bot.say('This person has not shared their 3DS friend code.')
        else:
            await self.bot.say('3DS Friend Code for %s: %s' % (member.display_name, code))

def setup(bot):
    bot.add_cog(Pokemon(bot))
