from discord.ext import commands
import discord
from cogs.utils import checks
import datetime, re
import json, asyncio
import copy
import logging
import traceback
import sys
from collections import Counter

description = """
Hello! I am a bot written by Danny to provide some nice utilities.
"""

try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

initial_extensions = [
    'cogs.meta',
    'cogs.splatoon',
    'cogs.rng',
    'cogs.mod',
    'cogs.profile',
    'cogs.tags',
    'cogs.lounge',
    'cogs.repl',
    'cogs.carbonitex',
    'cogs.mentions',
    'cogs.api',
    'cogs.stars',
    'cogs.admin',
    'cogs.buttons',
    'cogs.pokemon',
    'cogs.permissions',
    'cogs.stats',
    'cogs.emoji',
    'cogs.poll',
]

discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.CRITICAL)
log = logging.getLogger()
log.setLevel(logging.INFO)
handler = logging.FileHandler(filename='rdanny.log', encoding='utf-8', mode='w')
log.addHandler(handler)

help_attrs = dict(hidden=True)

prefix = ['?', '!', '\N{HEAVY EXCLAMATION MARK SYMBOL}']
bot = commands.Bot(command_prefix=prefix, description=description, pm_help=None, help_attrs=help_attrs)

@bot.event
async def on_command_error(error, ctx):
    if isinstance(error, commands.NoPrivateMessage):
        await bot.send_message(ctx.message.author, 'This command cannot be used in private messages.')
    elif isinstance(error, commands.DisabledCommand):
        await bot.send_message(ctx.message.author, 'Sorry. This command is disabled and cannot be used.')
    elif isinstance(error, commands.CommandInvokeError):
        print('In {0.command.qualified_name}:'.format(ctx), file=sys.stderr)
        traceback.print_tb(error.original.__traceback__)
        print('{0.__class__.__name__}: {0}'.format(error.original), file=sys.stderr)

@bot.event
async def on_ready():
    print('Logged in as:')
    print('Username: ' + bot.user.name)
    print('ID: ' + bot.user.id)
    print('------')
    if not hasattr(bot, 'uptime'):
        bot.uptime = datetime.datetime.utcnow()

@bot.event
async def on_resumed():
    print('resumed...')

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

@bot.command(pass_context=True, hidden=True)
@checks.is_owner()
async def do(ctx, times : int, *, command):
    """Repeats a command a specified number of times."""
    msg = copy.copy(ctx.message)
    msg.content = command
    for i in range(times):
        await bot.process_commands(msg)

@bot.command()
async def changelog():
    """Gives a URL to the current bot changelog."""
    await bot.say('https://discord.gg/0118rJdtd1rVJJfuI')

def load_credentials():
    with open('credentials.json') as f:
        return json.load(f)

if __name__ == '__main__':
    credentials = load_credentials()
    debug = any('debug' in arg.lower() for arg in sys.argv)
    if debug:
        bot.command_prefix = '$'
        token = credentials.get('debug_token', credentials['token'])
    else:
        token = credentials['token']

    bot.client_id = credentials['client_id']
    bot.carbon_key = credentials['carbon_key']
    bot.bots_key = credentials['bots_key']

    if debug:
        initial_extensions.remove('cogs.carbonitex')

    for extension in initial_extensions:
        try:
            bot.load_extension(extension)
        except Exception as e:
            print('Failed to load extension {}\n{}: {}'.format(extension, type(e).__name__, e))

    bot.run(token)
    handlers = log.handlers[:]
    for hdlr in handlers:
        hdlr.close()
        log.removeHandler(hdlr)
