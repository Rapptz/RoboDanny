from discord.ext import commands

# The permission system of the bot is based on a "just works" basis
# You have permissions and the bot has permissions. If you meet the permissions
# required to execute the command (and the bot does as well) then it goes through
# and you can execute the command.
# Certain permissions signify if the person is a moderator (Manage Server) or an
# admin (Administrator). Having these signify certain bypasses.
# Of course, the owner will always be able to execute commands.

async def check_permissions(ctx, perms):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    resolved = ctx.channel.permissions_for(ctx.author)
    return all(getattr(resolved, name, None) == value for name, value in perms.items())

def mod_or_permissions(**perms):
    perms['manage_guild'] = True
    async def predicate(ctx):
        return await check_permissions(ctx, perms)
    return commands.check(predicate)

def admin_or_permissions(**perms):
    perms['administrator'] = True
    async def predicate(ctx):
        return await check_permissions(ctx, perms)
    return commands.check(predicate)

def is_in_guilds(*guild_ids):
    def predicate(ctx):
        guild = ctx.guild
        if guild is None:
            return False
        return guild.id in guild_ids
    return commands.check(predicate)

def is_lounge_cpp():
    return is_in_guilds(145079846832308224)
