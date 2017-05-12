from discord.ext import commands

def to_keycap(c):
    return '\N{KEYCAP TEN}' if c == 10 else str(c) + '\u20e3'

class Polls:
    """Poll voting system."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(pass_context=True, no_pm=True)
    async def poll(self, ctx, *, question):
        """Interactively creates a poll with the following question.

        To vote, use reactions!
        """

        # a list of messages to delete when we're all done
        messages = [ctx.message]
        answers = []
        for i in range(1, 11):
            messages.append(await self.bot.say('Say poll option or {.prefix}cancel to publish poll.'.format(ctx)))
            entry = await self.bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel, timeout=60.0,
                                                    check=lambda m: len(m.content) <= 100)

            if entry is None:
                break

            messages.append(entry)

            if entry.clean_content.startswith('%scancel' % ctx.prefix):
                break

            answers.append((to_keycap(i), entry.clean_content))

        try:
            await self.bot.delete_messages(messages)
        except:
            pass # oh well

        answer = '\n'.join(map(lambda t: '%s: %s' % t, answers))
        actual_poll = await self.bot.say('%s asks: %s\n\n%s' % (ctx.message.author, question, answer))
        for emoji, _ in answers:
            await self.bot.add_reaction(actual_poll, emoji)

    @poll.error
    async def poll_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            return await self.bot.say('Missing the question.')

    @commands.command(pass_context=True, no_pm=True)
    async def quickpoll(self, ctx, *questions_and_choices: str):
        """Makes a poll quickly.

        The first argument is the question and the rest
        are the choices.
        """

        if len(questions_and_choices) < 3:
            return await self.bot.say('Need at least 1 question with 2 choices.')
        elif len(questions_and_choices) > 11:
            return await self.bot.say('You can only have up to 10 choices.')

        perms = ctx.message.channel.permissions_for(ctx.message.server.me)
        if not (perms.read_message_history or perms.add_reactions):
            return await self.bot.say('Need Read Message History and Add Reactions permissions.')

        question = questions_and_choices[0]
        choices = [(to_keycap(e), v) for e, v in enumerate(questions_and_choices[1:], 1)]

        try:
            await self.bot.delete_message(ctx.message)
        except:
            pass

        fmt = '{0} asks: {1}\n\n{2}'
        answer = '\n'.join('%s: %s' % t for t in choices)
        poll = await self.bot.say(fmt.format(ctx.message.author, question, answer))
        for emoji, _ in choices:
            await self.bot.add_reaction(poll, emoji)

def setup(bot):
    bot.add_cog(Polls(bot))
