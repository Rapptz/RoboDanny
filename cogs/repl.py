from discord.ext import commands
from .utils import checks
import asyncio
import traceback

class REPL:
    def __init__(self, bot):
        self.bot = bot

    def cleanup_code(self, content):
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    def get_syntax_error(self, e):
        return '```py\n{0.text}{1:>{0.offset}}\n{2}: {0}```'.format(e, '^', type(e).__name__)

    @commands.command(pass_context=True, hidden=True)
    @checks.is_owner()
    async def repl(self, ctx):
        msg = ctx.message

        repl_locals = {}
        repl_globals = {
            'ctx': ctx,
            'bot': self.bot,
            'message': msg
        }

        await self.bot.say('Enter code to execute or evaluate. `exit()` or `quit` to exit\n'
                           'Code must be `code` or in code blocks.\n'
                           'After 10 minutes of no replying I will auto-exit.\n'
                           'I will auto remove \`\`\`py blocks for you.')
        while True:
            response = await self.bot.wait_for_message(author=msg.author, channel=msg.channel,
                                                       timeout=600.0, check=lambda m: m.content.startswith('`'))
            if response is None:
                await self.bot.say('Auto exiting.')
                return

            cleaned = self.cleanup_code(response.content)

            if cleaned in ('quit', 'exit', 'exit()'):
                await self.bot.say('Exiting.')
                return

            use_eval = False

            if cleaned.count('\n') == 0:
                # single statement, potentially 'eval'
                try:
                    code = compile(cleaned, '<repl session>', 'eval')
                except SyntaxError:
                    pass
                else:
                    use_eval = True

            if not use_eval:
                try:
                    code = compile(cleaned, '<repl session>', 'exec')
                except SyntaxError as e:
                    await self.bot.say(self.get_syntax_error(e))
                    continue

            executor = exec if not use_eval else eval
            try:
                result = executor(code, repl_globals, repl_locals)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as e:
                await self.bot.say('```py\n{}\n```'.format(traceback.format_exc()))
            else:
                if result is not None:
                    await self.bot.say('```py\n{}\n```'.format(result))

def setup(bot):
    bot.add_cog(REPL(bot))
