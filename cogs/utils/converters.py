from __future__ import annotations
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from .context import Context


# This is because Discord is stupid with Slash Commands and doesn't actually have integer types.
# So to accept snowflake inputs you need a string and then convert it into an integer.
class Snowflake:
    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> int:
        try:
            return int(argument)
        except ValueError:
            param = ctx.current_parameter
            if param:
                raise commands.BadArgument(f'{param.name} argument expected a Discord ID not {argument!r}')
            raise commands.BadArgument(f'expected a Discord ID not {argument!r}')
