from discord.ext import commands
from collections import Counter

from .utils import checks

import logging

log = logging.getLogger()

class Stats:
    """Bot usage statistics."""

    def __init__(self, bot):
        self.bot = bot
        self.commands_used = Counter()
        self.socket_stats = Counter()

    async def on_command(self, command, ctx):
        self.commands_used[ctx.command.qualified_name] += 1
        message = ctx.message
        destination = None
        if message.channel.is_private:
            destination = 'Private Message'
        else:
            destination = '#{0.channel.name} ({0.server.name})'.format(message)

        log.info('{0.timestamp}: {0.author.name} in {1}: {0.content}'.format(message, destination))

    async def on_socket_response(self, msg):
        self.socket_stats[msg.get('t')] += 1

    @commands.command(hidden=True)
    @checks.is_owner()
    async def commandstats(self):
        p = commands.Paginator()
        width = len(max(self.commands_used, key=len))
        total = sum(self.commands_used.values())

        fmt = '{0:<{width}}: {1}'
        p.add_line(fmt.format('Total', total, width=width))
        for key, count in self.commands_used.most_common():
            p.add_line(fmt.format(key, count, width=width))

        for page in p.pages:
            await self.bot.say(page)

    @commands.command(hidden=True)
    async def socketstats(self):
        delta = type(self.bot.uptime).utcnow() - self.bot.uptime
        minutes = delta.total_seconds() / 60
        total = sum(self.socket_stats.values())
        cpm = total / minutes

        fmt = '%s socket events observed (%.2f/minute):\n%s'
        await self.bot.say(fmt % (total, cpm, self.socket_stats))

def setup(bot):
    bot.add_cog(Stats(bot))
