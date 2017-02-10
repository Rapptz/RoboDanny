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
            await self.bot.say('Incorrect random subcommand passed.')

    @random.command()
    async def weapon(self, count=1):
        """Displays a random Splatoon weapon.

        The count parameter is how many to generate. It cannot be
        negative. If it's negative or zero then only one weapon will be
        selected. The maximum number of random weapons generated is 8.
        """
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await self.bot.say('Splatoon cog is not loaded.')
            return

        count = count if count > 0 else 1
        count = count if count < 8 else 8

        weapons = splatoon.config.get('weapons', [])
        if weapons:
            if count == 1:
                weapon = rng.choice(weapons)
                await self.bot.say(splatoon.weapon_to_string(weapon))
            else:
                sample = rng.sample(weapons, count)
                msg = '\n'.join(map(lambda w: w['name'], sample))
                await self.bot.say(msg)

        del splatoon

    @random.command()
    async def private(self):
        """Displays an all private Splatoon match.

        The map and mode is randomised along with both team's weapons.
        """
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await self.bot.say('Splatoon cog is not loaded.')
            return

        maps = splatoon.config.get('maps', [])
        stage = rng.choice(maps) if maps else 'Random Stage'
        modes = [ 'Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control' ]
        mode = rng.choice(modes)
        result = ['Playing {} on {}'.format(mode, stage), '', '**Team Alpha**']
        weapons = rng.sample(splatoon.config.get('weapons'), 8)
        for i in range(8):
            if i == 4:
                result.append('')
                result.append('**Team Bravo**')

            result.append('Player {}: {[name]}'.format(i + 1, weapons[i]))

        await self.bot.say('\n'.join(result))

    @random.command(pass_context=True)
    async def tag(self, ctx):
        """Displays a random tag.

        A tag showing up in this does not get its usage count increased.
        """
        tags = self.bot.get_cog('Tags')
        if tags is None:
            await self.bot.say('Tags cog is not loaded.')
            return

        db = tags.get_possible_tags(ctx.message.server)
        name = rng.sample(list(db), 1)[0]
        await self.bot.say('Random tag found: {}\n{}'.format(name, db[name]))
        del tags

    @random.command(name='map')
    async def _map(self):
        """Displays a random Splatoon map."""
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await self.bot.say('Splatoon cog is not loaded.')
            return

        maps = splatoon.config.get('maps', [])
        if maps:
            await self.bot.say(rng.choice(maps))

        del splatoon

    @random.command()
    async def mode(self):
        """Displays a random Splatoon mode."""
        mode = rng.choice(['Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control'])
        await self.bot.say(mode)

    @random.command()
    async def game(self):
        """Displays a random map/mode combination (no Turf War)"""
        splatoon = self.bot.get_cog('Splatoon')
        if splatoon is None:
            await self.bot.say('Splatoon cog is not loaded.')
            return

        maps = splatoon.config.get('maps', [])
        if maps:
            mode = rng.choice(['Splat Zones', 'Tower Control', 'Rainmaker'])
            stage = rng.choice(maps)
            await self.bot.say('{} on {}'.format(mode, stage))

        del splatoon

    @random.command()
    async def number(self, minimum=0, maximum=100):
        """Displays a random number within an optional range.

        The minimum must be smaller than the maximum and the maximum number
        accepted is 1000.
        """

        maximum = min(maximum, 1000)
        if minimum >= maximum:
            await self.bot.say('Maximum is smaller than minimum.')
            return

        await self.bot.say(rng.randint(minimum, maximum))

    @random.command()
    async def lenny(self):
        """Displays a random lenny face."""
        lenny = rng.choice([
            "( ͡° ͜ʖ ͡°)", "( ͠° ͟ʖ ͡°)", "ᕦ( ͡° ͜ʖ ͡°)ᕤ", "( ͡~ ͜ʖ ͡°)",
            "( ͡o ͜ʖ ͡o)", "͡(° ͜ʖ ͡ -)", "( ͡͡ ° ͜ ʖ ͡ °)﻿", "(ง ͠° ͟ل͜ ͡°)ง",
            "ヽ༼ຈل͜ຈ༽ﾉ"
        ])
        await self.bot.say(lenny)

    @commands.command()
    async def choose(self, *choices):
        """Chooses between multiple choices.

        To denote multiple choices, you should use double quotes.
        """
        if len(choices) < 2:
            return await self.bot.say('Not enough choices to pick from.')

        choice = rng.choice(choices).replace('@', '@\u200b') # this breaks user mentions
        await self.bot.say(choice)

def setup(bot):
    bot.add_cog(RNG(bot))
