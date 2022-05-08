from __future__ import annotations
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from .context import GuildContext

# The permission system of the bot is based on a "just works" basis
# You have permissions and the bot has permissions. If you meet the permissions
# required to execute the command (and the bot does as well) then it goes through
# and you can execute the command.
# Certain permissions signify if the person is a moderator or an
# admin (Administrator). Having these signify certain bypasses.
# Of course, the owner will always be able to execute commands.


async def check_permissions(ctx: GuildContext, perms: dict[str, bool], *, check=all):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    resolved = ctx.channel.permissions_for(ctx.author)
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_permissions(*, check=all, **perms: bool):
    async def pred(ctx: GuildContext):
        return await check_permissions(ctx, perms, check=check)

    return commands.check(pred)


async def check_guild_permissions(ctx: GuildContext, perms: dict[str, bool], *, check=all):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    if ctx.guild is None:
        return False

    resolved = ctx.author.guild_permissions
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_guild_permissions(*, check=all, **perms: bool):
    async def pred(ctx: GuildContext):
        return await check_guild_permissions(ctx, perms, check=check)

    return commands.check(pred)


# These do not take channel overrides into account


def is_manager():
    async def pred(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, {'manage_guild': True})

    return commands.check(pred)


def is_mod():
    async def pred(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, {'ban_members': True, 'manage_messages': True})

    return commands.check(pred)


def is_admin():
    async def pred(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, {'administrator': True})

    return commands.check(pred)


def admin_or_permissions(**perms: bool):
    perms['administrator'] = True

    async def predicate(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, perms, check=any)

    return commands.check(predicate)


def is_in_guilds(*guild_ids: int):
    def predicate(ctx: GuildContext) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        return guild.id in guild_ids

    return commands.check(predicate)


def is_lounge_cpp():
    return is_in_guilds(145079846832308224)
