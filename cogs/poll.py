from discord.ext import commands
import asyncio

def to_keycap(c):
    return '\N{KEYCAP TEN}' if c == 10 else str(c) + '\u20e3'

class Polls:
    """Poll voting system."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.guild_only()
    async def poll(self, ctx, *, question):
        """Interactively creates a poll with the following question.

        To vote, use reactions!
        """

        # a list of messages to delete when we're all done
        messages = [ctx.message]
        answers = []

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and len(m.content) <= 100

        for i in range(1, 11):
            messages.append(await ctx.send(f'Say poll option or {ctx.prefix}cancel to publish poll.'))


            try:
                entry = await self.bot.wait_for('message', check=check, timeout=60.0)
            except asyncio.TimeoutError:
                break

            messages.append(entry)

            if entry.clean_content.startswith(f'{ctx.prefix}cancel'):
                break

            answers.append((to_keycap(i), entry.clean_content))

        try:
            await ctx.channel.delete_messages(messages)
        except:
            pass # oh well

        answer = '\n'.join(f'{keycap}: {content}' for keycap, content in answers)
        actual_poll = await ctx.send(f'{ctx.author} asks: {question}\n\n{answer}')
        for emoji, _ in answers:
            await actual_poll.add_reaction(emoji)

    @poll.error
    async def poll_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send('Missing the question.')

    @commands.command()
    @commands.guild_only()
    async def quickpoll(self, ctx, *questions_and_choices: str):
        """Makes a poll quickly.

        The first argument is the question and the rest are the choices.
        """

        if len(questions_and_choices) < 3:
            return await ctx.send('Need at least 1 question with 2 choices.')
        elif len(questions_and_choices) > 11:
            return await ctx.send('You can only have up to 10 choices.')

        perms = ctx.channel.permissions_for(ctx..me)
        if not (perms.read_message_history or perms.add_reactions):
            return await ctx.send('Need Read Message History and Add Reactions permissions.')

        question = questions_and_choices[0]
        choices = [(to_keycap(e), v) for e, v in enumerate(questions_and_choices[1:], 1)]

        try:
            await ctx.message.delete()
        except:
            pass

        poll = await ctx.send(f'{ctx.author} asks: {question}\n\n{"\n".join(f"{key}: {c}" for key, c in choices)}')
        for emoji, _ in choices:
            await poll.add_reaction(emoji)

def setup(bot):
    bot.add_cog(Polls(bot))
