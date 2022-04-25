from __future__ import annotations
from typing import TYPE_CHECKING, Any
from typing_extensions import Annotated

import discord
from discord.ext import commands
import random as rng
from collections import Counter
from typing import Optional
from .utils.formats import plural

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import Context
    from .splatoon import Splatoon
    from .tags import Tags


class RNG(commands.Cog):
    """Utilities that provide pseudo-RNG."""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{GAME DIE}')

    @commands.group()
    async def random(self, ctx: Context):
        """Displays a random thing you request."""
        if ctx.invoked_subcommand is None:
            await ctx.send(f'Incorrect random subcommand passed. Try {ctx.prefix}help random')

    @random.command()
    async def weapon(self, ctx: Context, count: int = 1):
        """Displays a random Splatoon 2 weapon.

        The count parameter is how many to generate. It cannot be
        negative. If it's negative or zero then only one weapon will be
        selected. The maximum number of random weapons generated is 8.
        """

        splatoon: Optional[Splatoon] = self.bot.get_cog('Splatoon')  # type: ignore
        if splatoon is None:
            return await ctx.send('Splatoon commands currently disabled.')

        count = min(max(count, 1), 8)
        weapons = splatoon.splat2_data.get('weapons', [])
        if weapons:
            if count == 1:
                weapon = rng.choice(weapons)
                await ctx.send(f'{weapon["name"]} with {weapon["sub"]} and {weapon["special"]} special.')
            else:
                sample = rng.sample(weapons, count)
                await ctx.send('\n'.join(w['name'] for w in sample))

    @random.command()
    async def private(self, ctx: Context):
        """Displays an all private Splatoon 2 match.

        The map and mode is randomised along with both team's weapons.
        """
        splatoon: Optional[Splatoon] = self.bot.get_cog('Splatoon')  # type: ignore
        if splatoon is None:
            return await ctx.send('Splatoon commands currently disabled.')

        maps: list[str] = splatoon.splat2_data.get('maps', [])

        stage = rng.choice(maps) if maps else 'Random Stage'
        modes = ['Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control']
        mode = rng.choice(modes)
        result = [f'Playing {mode} on {stage}', '', '**Team Alpha**']

        weapons = rng.sample(splatoon.splat2_data.get('weapons', []), 8)
        for i in range(8):
            if i == 4:
                result.append('')
                result.append('**Team Bravo**')

            result.append(f'Player {i + 1}: {weapons[i]["name"]}')

        await ctx.send('\n'.join(result))

    @random.command()
    async def tag(self, ctx: Context):
        """Displays a random tag.

        A tag showing up in this does not get its usage count increased.
        """
        tags: Optional[Tags] = self.bot.get_cog('Tags')  # type: ignore
        if tags is None:
            return await ctx.send('Tag commands currently disabled.')

        tag = await tags.get_random_tag(ctx.guild)
        if tag is None:
            return await ctx.send('This server has no tags.')

        await ctx.send(f'Random tag found: {tag["name"]}\n{tag["content"]}')

    @random.command(name='map')
    async def _map(self, ctx: Context):
        """Displays a random Splatoon 2 map."""
        splatoon: Optional[Splatoon] = self.bot.get_cog('Splatoon')  # type: ignore
        if splatoon is None:
            await ctx.send('Splatoon commands currently disabled.')
            return

        maps = splatoon.splat2_data.get('maps', [])
        if maps:
            await ctx.send(rng.choice(maps))

        del splatoon

    @random.command()
    async def mode(self, ctx: Context):
        """Displays a random Splatoon mode."""
        mode = rng.choice(['Turf War', 'Splat Zones', 'Clam Blitz', 'Rainmaker', 'Tower Control'])
        await ctx.send(mode)

    @random.command()
    async def game(self, ctx: Context):
        """Displays a random map/mode combination (no Turf War)"""
        splatoon: Optional[Splatoon] = self.bot.get_cog('Splatoon')  # type: ignore
        if splatoon is None:
            await ctx.send('Splatoon commands currently disabled.')
            return

        maps = splatoon.splat2_data.get('maps', [])
        if maps:
            mode = rng.choice(['Splat Zones', 'Tower Control', 'Rainmaker'])
            stage = rng.choice(maps)
            await ctx.send(f'{mode} on {stage}')

        del splatoon

    @random.command()
    async def number(self, ctx: Context, minimum: int = 0, maximum: int = 100):
        """Displays a random number within an optional range.

        The minimum must be smaller than the maximum and the maximum number
        accepted is 1000.
        """

        maximum = min(maximum, 1000)
        if minimum >= maximum:
            await ctx.send('Maximum is smaller than minimum.')
            return

        await ctx.send(str(rng.randint(minimum, maximum)))

    @random.command()
    async def lenny(self, ctx: Context):
        """Displays a random lenny face."""
        lenny = rng.choice(
            [
                "( ͡° ͜ʖ ͡°)",
                "( ͠° ͟ʖ ͡°)",
                "ᕦ( ͡° ͜ʖ ͡°)ᕤ",
                "( ͡~ ͜ʖ ͡°)",
                "( ͡o ͜ʖ ͡o)",
                "͡(° ͜ʖ ͡ -)",
                "( ͡͡ ° ͜ ʖ ͡ °)﻿",
                "(ง ͠° ͟ل͜ ͡°)ง",
                "ヽ༼ຈل͜ຈ༽ﾉ",
            ]
        )
        await ctx.send(lenny)

    @commands.command()
    async def choose(self, ctx: Context, *choices: Annotated[str, commands.clean_content]):
        """Chooses between multiple choices.

        To denote multiple choices, you should use double quotes.
        """
        if len(choices) < 2:
            return await ctx.send('Not enough choices to pick from.')

        await ctx.send(rng.choice(choices))

    @commands.command()
    async def choosebestof(self, ctx: Context, times: Optional[int], *choices: Annotated[str, commands.clean_content]):
        """Chooses between multiple choices N times.

        To denote multiple choices, you should use double quotes.

        You can only choose up to 10001 times and only the top 10 results are shown.
        """
        if len(choices) < 2:
            return await ctx.send('Not enough choices to pick from.')

        if times is None:
            times = (len(choices) ** 2) + 1

        times = min(10001, max(1, times))
        results = Counter(rng.choice(choices) for i in range(times))
        builder = []
        if len(results) > 10:
            builder.append('Only showing top 10 results...')
        for index, (elem, count) in enumerate(results.most_common(10), start=1):
            builder.append(f'{index}. {elem} ({plural(count):time}, {count/times:.2%})')

        await ctx.send('\n'.join(builder))


async def setup(bot: RoboDanny):
    await bot.add_cog(RNG(bot))
