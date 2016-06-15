from discord.ext import commands
import discord
from cogs.utils import checks
import datetime, re
import json, asyncio
import copy
import logging
import sys
from collections import Counter

description = """
Hello! I am a bot written by Danny to provide some nice utilities.
"""

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
]

discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.CRITICAL)
log = logging.getLogger()
log.setLevel(logging.INFO)
handler = logging.FileHandler(filename='rdanny.log', encoding='utf-8', mode='w')
log.addHandler(handler)

help_attrs = dict(hidden=True)
bot = commands.Bot(command_prefix=['?', '!', '\u2757'], description=description, pm_help=None, help_attrs=help_attrs)

@bot.event
async def on_command_error(error, ctx):
    if isinstance(error, commands.NoPrivateMessage):
        await bot.send_message(ctx.message.author, 'This command cannot be used in private messages.')
    elif isinstance(error, commands.DisabledCommand):
        await bot.send_message(ctx.message.author, 'Sorry. This command is disabled and cannot be used.')
    elif isinstance(error, commands.CommandInvokeError):
        print('In {0.command.qualified_name}: {1}'.format(ctx, error), file=sys.stderr)

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
async def on_command(command, ctx):
    bot.commands_used[command.name] += 1
    message = ctx.message
    destination = None
    if message.channel.is_private:
        destination = 'Private Message'
    else:
        destination = '#{0.channel.name} ({0.server.name})'.format(message)

    log.info('{0.timestamp}: {0.author.name} in {1}: {0.content}'.format(message, destination))

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.author.bot:
        return

    mod = bot.get_cog('Mod')

    if mod is not None and not checks.is_owner_check(message):
        # check if the user is bot banned
        if message.author.id in mod.config.get('plonks', []):
            return

        # check if the channel is ignored
        # but first, resolve their permissions

        perms = message.channel.permissions_for(message.author)
        bypass_ignore = perms.manage_roles

        # if we don't have manage roles then we should
        # check if it's the owner of the bot or they have Bot Admin role.

        if not bypass_ignore:
            if not message.channel.is_private:
                bypass_ignore = discord.utils.get(message.author.roles, name='Bot Admin') is not None

        # now we can finally realise if we can actually bypass the ignore.

        if not bypass_ignore:
            if message.channel.id in mod.config.get('ignored', []):
                return

    await bot.process_commands(message)

@bot.command(pass_context=True, hidden=True)
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
    if any('debug' in arg.lower() for arg in sys.argv):
        bot.command_prefix = '$'

    credentials = load_credentials()
    bot.client_id = credentials['client_id']
    bot.commands_used = Counter()
    bot.carbon_key = credentials['carbon_key']
    for extension in initial_extensions:
        try:
            bot.load_extension(extension)
        except Exception as e:
            print('Failed to load extension {}\n{}: {}'.format(extension, type(e).__name__, e))

    bot.run(credentials['token'])
    handlers = log.handlers[:]
    for hdlr in handlers:
        hdlr.close()
        log.removeHandler(hdlr)
