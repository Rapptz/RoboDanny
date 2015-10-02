import discord
import commands

bot = discord.Client()

@bot.event
def on_ready():
    print('Logged in as:')
    print('Username: ' + bot.user.name)
    print('ID: ' + bot.user.id)
    print('------')

@bot.event
def on_message(message):
    commands.dispatch_messages(bot, message)

@bot.event
def on_member_join(member):
    if member.server.id == '86177841854566400':
        # check if this is /r/Splatoon
        channel = discord.utils.find(lambda c: c.id == '86177841854566400', member.server.channels)
        if channel is not None:
            bot.send_message(channel, 'Welcome {}, to the /r/Splatoon Discord.'.format(member.name))

if __name__ == '__main__':
    commands.load_config()
    bot.login(commands.config['username'], commands.config['password'])
    bot.run()
