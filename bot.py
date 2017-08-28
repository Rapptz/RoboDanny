from discord.ext import commands
import discord
from cogs.utils import checks, context, db
from cogs.utils.config import Config
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
    'cogs.splatoon',
    'cogs.rng',
    'cogs.mod',
    'cogs.profile',
    'cogs.tags',
    'cogs.lounge',
    'cogs.carbonitex',
    'cogs.api',
    'cogs.stars',
    'cogs.admin',
    'cogs.buttons',
    'cogs.reminder',
    'cogs.stats',
    'cogs.emoji',
    'cogs.config',
    'cogs.tournament',
)

def _prefix_callable(bot, msg):
    user_id = bot.user.id
    base = [f'<@!{user_id}> ', f'<@{user_id}> ']
    if msg.guild is None:
        base.append('!')
        base.append('?')
    else:
        base.extend(bot.prefixes.get(msg.guild.id, ['?', '!']))
    return base

class RoboDanny(commands.AutoShardedBot):
    def __init__(self):
        super().__init__(command_prefix=_prefix_callable, description=description,
                         pm_help=None, help_attrs=dict(hidden=True))

        self.client_id = config.client_id
        self.carbon_key = config.carbon_key
        self.bots_key = config.bots_key
        self.challonge_api_key = config.challonge_api_key
        self.session = aiohttp.ClientSession(loop=self.loop)

        self.add_command(self.do)

        # guild_id: list
        self.prefixes = Config('prefixes.json')

        for extension in initial_extensions:
            try:
                self.load_extension(extension)
            except Exception as e:
                print(f'Failed to load extension {extension}.', file=sys.stderr)
                traceback.print_exc()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send('This command cannot be used in private messages.')
        elif isinstance(error, commands.DisabledCommand):
            await ctx.author.send('Sorry. This command is disabled and cannot be used.')
        elif isinstance(error, commands.CommandInvokeError):
            print(f'In {ctx.command.qualified_name}:', file=sys.stderr)
            traceback.print_tb(error.original.__traceback__)
            print(f'{error.original.__class__.__name__}: {error.original}', file=sys.stderr)

    def get_guild_prefixes(self, guild, *, local_inject=_prefix_callable):
        proxy_msg = discord.Object(id=None)
        proxy_msg.guild = guild
        return local_inject(self, proxy_msg)

    def get_raw_guild_prefixes(self, guild_id):
        return self.prefixes.get(guild_id, ['?', '!'])

    async def set_guild_prefixes(self, guild, prefixes):
        if len(prefixes) == 0:
            await self.prefixes.put(guild.id, [])
        elif len(prefixes) > 10:
            raise RuntimeError('Cannot have more than 10 custom prefixes.')
        else:
            await self.prefixes.put(guild.id, sorted(set(prefixes), reverse=True))

    async def on_ready(self):
        if not hasattr(self, 'uptime'):
            self.uptime = datetime.datetime.utcnow()

        print(f'Ready: {self.user} (ID: {self.user.id})')

    async def on_resumed(self):
        print('resumed...')

    async def process_commands(self, message):
        ctx = await self.get_context(message, cls=context.Context)

        if ctx.command is None:
            return

        async with ctx.acquire():
            await self.invoke(ctx)

    async def on_message(self, message):
        if message.author.bot:
            return
        await self.process_commands(message)

    async def close(self):
        await super().close()
        await self.session.close()

    def run(self):
        super().run(config.token, reconnect=True)

    @property
    def config(self):
        return __import__('config')

    @commands.command(hidden=True)
    @commands.is_owner()
    async def do(self, ctx, times: int, *, command):
        """Repeats a command a specified number of times."""
        msg = copy.copy(ctx.message)
        msg.content = command

        new_ctx = await self.get_context(msg, cls=context.Context)
        new_ctx.db = ctx.db

        for i in range(times):
            await new_ctx.reinvoke()
