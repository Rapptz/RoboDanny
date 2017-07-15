from discord.ext import commands
import random as rng

class RNG:
    """Utilities that provide pseudo-RNG."""

    def __init__(self, bot):
        self.bot = bot

    @commands.group(pass_context=True)
    async def random(self, ctx):
        """Displays a random thing you request."""
        if ctx.invoked_subcommand is None:
            await ctx.send(f'Incorrect random subcommand passed. Try {ctx.prefix}help random')

    @random.command()
    async def weapon(self, ctx, count=1):
        """Displays a random Splatoon 2 weapon.

        The count parameter is how many to generate. It cannot be
        negative. If it's negative or zero then only one weapon will be
        selected. The maximum number of random weapons generated is 8.
        """

        splatoon = self.bot.get_cog('Splatoon')
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
    async def private(self, ctx):
        """Displays an all private Splatoon 2 match.

        The map and mode is randomised along with both team's weapons.
        """
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            return await ctx.send('Splatoon commands currently disabled.')

        maps = splatoon.splat2_data.get('maps', [])

        stage = rng.choice(maps) if maps else 'Random Stage'
        modes = [ 'Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control' ]
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
    async def tag(self, ctx):
        """Displays a random tag.

        A tag showing up in this does not get its usage count increased.
        """
        tags = self.bot.get_cog('Tags')
        if tags is None:
            return await ctx.send('Tag commands currently disabled.')

        tag = await tags.get_random_tag(ctx.guild, connection=ctx.db)
        if tag is None:
            return await ctx.send('This server has no tags.')

        await ctx.send(f'Random tag found: {tag["name"]}\n{tag["content"]}')

    @random.command(name='map')
    async def _map(self, ctx):
        """Displays a random Splatoon 2 map."""
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await ctx.send('Splatoon commands currently disabled.')
            return

        maps = splatoon.splat2_data.get('maps', [])
        if maps:
            await ctx.send(rng.choice(maps))

        del splatoon

    @random.command()
    async def mode(self, ctx):
        """Displays a random Splatoon mode."""
        mode = rng.choice(['Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control'])
        await ctx.send(mode)

    @random.command()
    async def game(self, ctx):
        """Displays a random map/mode combination (no Turf War)"""
        splatoon = self.bot.get_cog('Splatoon')
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
    async def number(self, ctx, minimum=0, maximum=100):
        """Displays a random number within an optional range.

        The minimum must be smaller than the maximum and the maximum number
        accepted is 1000.
        """

        maximum = min(maximum, 1000)
        if minimum >= maximum:
            await ctx.send('Maximum is smaller than minimum.')
            return

        await ctx.send(rng.randint(minimum, maximum))

    @random.command()
    async def lenny(self, ctx):
        """Displays a random lenny face."""
        lenny = rng.choice([
            "( ͡° ͜ʖ ͡°)", "( ͠° ͟ʖ ͡°)", "ᕦ( ͡° ͜ʖ ͡°)ᕤ", "( ͡~ ͜ʖ ͡°)",
            "( ͡o ͜ʖ ͡o)", "͡(° ͜ʖ ͡ -)", "( ͡͡ ° ͜ ʖ ͡ °)﻿", "(ง ͠° ͟ل͜ ͡°)ง",
            "ヽ༼ຈل͜ຈ༽ﾉ"
        ])
        await ctx.send(lenny)

    @commands.command()
    async def choose(self, ctx, *choices: commands.clean_content):
        """Chooses between multiple choices.

        To denote multiple choices, you should use double quotes.
        """
        if len(choices) < 2:
            return await ctx.send('Not enough choices to pick from.')

        await ctx.send(rng.choice(choices))

def setup(bot):
    bot.add_cog(RNG(bot))
