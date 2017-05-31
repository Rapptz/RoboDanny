from discord.ext import commands
import discord
from cogs.utils import checks, context, db
import datetime, re
import json, asyncio
import copy
import logging
import traceback
import aiohttp
import sys
from collections import Counter

import config
import asyncpg

description = """
Hello! I am a bot written by Danny to provide some nice utilities.
"""

log = logging.getLogger(__name__)

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

class RoboDanny(commands.AutoShardedBot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or('?', '!'), description=description,
                         pm_help=None, help_attrs=dict(hidden=True))

        self.client_id = config.client_id
        self.carbon_key = config.carbon_key
        self.bots_key = config.bots_key
        self.session = aiohttp.ClientSession(loop=self.loop)

        self.add_command(self.do)
        self.add_check(self.only_me_for_rewrite)

        for extension in initial_extensions:
            try:
                self.load_extension(extension)
            except Exception as e:
                print('Failed to load extension %s' % extension, file=sys.stderr)
                traceback.print_exc()

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
