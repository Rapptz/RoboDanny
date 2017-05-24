from discord.ext import commands
import discord
from cogs.utils import checks, context
import datetime, re
import json, asyncio
import copy
import logging
import traceback
import sys
from collections import Counter

import config
import asyncpg

description = """
Hello! I am a bot written by Danny to provide some nice utilities.
"""

try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logging.getLogger('discord').setLevel(logging.INFO)
logging.getLogger('discord.http').setLevel(logging.DEBUG)

log = logging.getLogger()
log.setLevel(logging.INFO)
handler = logging.FileHandler(filename='rdanny.log', encoding='utf-8', mode='w')
log.addHandler(handler)

class RoboDanny(commands.AutoShardedBot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or('?', '!'), description=description,
                         pm_help=None, help_attrs=dict(hidden=True))

        initial_extensions = (
            'cogs.meta',
            # 'cogs.splatoon',
            # 'cogs.rng',
            # 'cogs.mod',
            # 'cogs.profile',
            # 'cogs.tags',
            # 'cogs.lounge',
            # 'cogs.carbonitex',
            # 'cogs.mentions',
            # 'cogs.api',
            # 'cogs.stars',
            'cogs.admin',
            # 'cogs.buttons',
            # 'cogs.pokemon',
            # 'cogs.permissions',
            # 'cogs.stats',
            # 'cogs.emoji',
        )

        self.client_id = config.client_id
        self.carbon_key = config.carbon_key
        self.bots_key = config.bots_key

        self.add_command(self.do)
        self.add_check(self.only_me_for_rewrite)

        for extension in initial_extensions:
            try:
                self.load_extension(extension)
            except Exception as e:
                print('Failed to load extension %s' % extension, file=sys.stderr)
                traceback.print_exc()

    def __del__(self):
        handlers = log.handlers[:]
        for hdlr in handlers:
            hdlr.close()
            log.removeHandler(hdlr)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send('This command cannot be used in private messages.')
        elif isinstance(error, commands.DisabledCommand):
            await ctx.author.send('Sorry. This command is disabled and cannot be used.')
        elif isinstance(error, commands.CommandInvokeError):
            print('In {0.command.qualified_name}:'.format(ctx), file=sys.stderr)
            traceback.print_tb(error.original.__traceback__)
            print('{0.__class__.__name__}: {0}'.format(error.original), file=sys.stderr)

    async def on_ready(self):
        if not hasattr(self, 'uptime'):
            self.uptime = datetime.datetime.utcnow()

        if not hasattr(self, 'pool'):
            try:
                self.pool = await asyncpg.create_pool(config.postgres, command_timeout=60)
            except Exception as e:
                print('Could not set up PostgreSQL. Exiting.')
                log.exception('Could not set up PostgreSQL. Exiting.')
                await self.close()

        print('Ready: {0} (ID: {0.id})'.format(self.user))

    async def on_resumed(self):
        print('resumed...')

    async def on_message(self, message):
        if message.author.bot:
            return

        ctx = await self.get_context(message, cls=context.Context)
        await self.invoke(ctx)

    def only_me_for_rewrite(self, ctx):
        return ctx.author.id == 80088516616269824

    def run(self):
        super().run(config.token, reconnect=True)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def do(self, ctx, times: int, *, command):
        """Repeats a command a specified number of times."""
        msg = copy.copy(ctx.message)
        msg.content = command
        for i in range(times):
            await self.process_commands(msg)

if __name__ == '__main__':
    bot = RoboDanny()
    bot.run()
