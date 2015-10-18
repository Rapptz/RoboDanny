import discord
import commands
import datetime

bot = discord.Client()

@bot.event
def on_ready():
    print('Logged in as:')
    print('Username: ' + bot.user.name)
    print('ID: ' + bot.user.id)
    print('------')
    bot.uptime = datetime.datetime.utcnow()

@bot.event
def on_message(message):
    commands.dispatch_messages(bot, message)

if __name__ == '__main__':
    commands.load_config()
    bot.login(commands.config['username'], commands.config['password'])
    bot.run()
