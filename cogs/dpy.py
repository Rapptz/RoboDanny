from discord.ext import commands
from cogs.utils.formats import human_join
import discord
import asyncio
import yarl
import re

DISCORD_PY_GUILD_ID = 336642139381301249
DISCORD_PY_BOTS_ROLE = 381980817125015563
DISCORD_PY_REWRITE_ROLE = 381981861041143808
DISCORD_PY_JP_ROLE = 490286873311182850
DISCORD_PY_PROF_ROLE = 381978395270971407
DISCORD_PY_BOT_LIST = 579998326557114368

class GithubError(commands.CommandError):
    pass

class BotUser(commands.Converter):
    async def convert(self, ctx, argument):
        if not argument.isdigit():
            raise commands.BadArgument('Not a valid bot user ID.')
        try:
            user = await ctx.bot.fetch_user(argument)
        except discord.NotFound:
            raise commands.BadArgument('Bot user not found (404).')
        except discord.HTTPException as e:
            raise commands.BadArgument(f'Error fetching bot user: {e}')
        else:
            if not user.bot:
                raise commands.BadArgument('This is not a bot.')
            return user

def is_proficient():
    def predicate(ctx):
        return ctx.author._roles.has(DISCORD_PY_PROF_ROLE)
    return commands.check(predicate)

def in_testing():
    def predicate(ctx):
        #                         #testing            #playground         #mod-testing
        return ctx.channel.id in (381963689470984203, 559455534965850142, 568662293190148106)
    return commands.check(predicate)

class DPYExclusive(commands.Cog, name='discord.py'):
    def __init__(self, bot):
        self.bot = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')
        self._invite_cache = {}
        self.bot.loop.create_task(self._prepare_invites())
        self._req_lock = asyncio.Lock(loop=self.bot.loop)

    async def _prepare_invites(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(DISCORD_PY_GUILD_ID)
        invites = await guild.invites()
        self._invite_cache = {
            invite.code: invite.uses
            for invite in invites
        }

    def cog_check(self, ctx):
        return ctx.guild and ctx.guild.id == DISCORD_PY_GUILD_ID

    async def cog_command_error(self, ctx, error):
        if isinstance(error, GithubError):
            await ctx.send(f'Github Error: {error}')

    async def github_request(self, method, url, *, params=None, data=None, headers=None):
        hdrs = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'RoboDanny DPYExclusive Cog',
            'Authorization': f'token {self.bot.config.github_token}'
        }

        req_url = yarl.URL('https://api.github.com') / url

        if headers is not None and isinstance(headers, dict):
            hdrs.update(headers)

        await self._req_lock.acquire()
        try:
            async with self.bot.session.request(method, req_url, params=params, json=data, headers=hdrs) as r:
                remaining = r.headers.get('X-Ratelimit-Remaining')
                js = await r.json()
                if r.status == 429 or remaining == '0':
                    # wait before we release the lock
                    delta = discord.utils._parse_ratelimit_header(r)
                    await asyncio.sleep(delta)
                    self._req_lock.release()
                    return await self.github_request(method, url, params=params, data=data, headers=headers)
                elif 300 > r.status >= 200:
                    return js
                else:
                    raise GithubError(js['message'])
        finally:
            if self._req_lock.locked():
                self._req_lock.release()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.guild.id != DISCORD_PY_GUILD_ID:
            return

        if member.bot:
            await member.add_roles(discord.Object(id=DISCORD_PY_BOTS_ROLE))
            return

        JP_INVITE_CODES = ('y9Bm8Yx', 'nXzj3dg')
        invites = await member.guild.invites()
        for invite in invites:
            if invite.code in JP_INVITE_CODES and invite.uses > self._invite_cache[invite.code]:
                await member.add_roles(discord.Object(id=DISCORD_PY_JP_ROLE))
            self._invite_cache[invite.code] = invite.uses

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.guild.id != DISCORD_PY_GUILD_ID:
            return

        m = self.issue.search(message.content)
        if m is not None:
            url = 'https://github.com/Rapptz/discord.py/issues/'
            await message.channel.send(url + m.group('number'))

    async def toggle_role(self, ctx, role_id):
        if any(r.id == role_id for r in ctx.author.roles):
            try:
                await ctx.author.remove_roles(discord.Object(id=role_id))
            except:
                await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
            else:
                await ctx.message.add_reaction('\N{HEAVY MINUS SIGN}')
            finally:
                return

        try:
            await ctx.author.add_roles(discord.Object(id=role_id))
        except:
            await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
        else:
            await ctx.message.add_reaction('\N{HEAVY PLUS SIGN}')

    @commands.command(hidden=True, aliases=['日本語'])
    async def nihongo(self, ctx):
        """日本語チャットに参加したい場合はこの役職を付ける"""

        await self.toggle_role(ctx, DISCORD_PY_JP_ROLE)

    async def get_valid_labels(self):
        labels = await self.github_request('GET', 'repos/Rapptz/discord.py/labels')
        return {e['name'] for e in labels}

    async def edit_issue(self, number, *, labels=None, state=None):
        url_path = f'repos/Rapptz/discord.py/issues/{number}'
        issue = await self.github_request('GET', url_path)
        if issue.get('pull_request'):
            raise GithubError('That is a pull request, not an issue.')

        current_state = issue.get('state')
        if state == 'closed' and current_state == 'closed':
            raise GithubError('This issue is already closed.')

        data = {}
        if state:
            data['state'] = state

        if labels:
            current_labels = {e['name'] for e in issue.get('labels', [])}
            valid_labels = await self.get_valid_labels()
            labels = set(labels)
            diff = [repr(x) for x in (labels - valid_labels)]
            if diff:
                raise GithubError(f'Invalid labels passed: {human_join(diff, final="and")}')
            data['labels'] = list(current_labels | labels)

        return await self.github_request('PATCH', url_path, data=data)

    @commands.group(aliases=['gh'])
    @is_proficient()
    async def github(self, ctx):
        """Github administration commands."""
        pass

    @github.command(name='close')
    async def github_close(self, ctx, number: int, *labels):
        """Closes and optionally labels an issue."""
        js = await self.edit_issue(number, labels=labels, state='closed')
        await ctx.send(f'Successfully closed <{js["html_url"]}>')

    @github.command(name='open')
    async def github_open(self, ctx, number: int):
        """Re-open an issue"""
        js = await self.edit_issue(number, state='open')
        await ctx.send(f'Successfully closed <{js["html_url"]}>')

    @github.command(name='label')
    async def github_label(self, ctx, number: int, *labels):
        """Adds labels to an issue."""
        if not labels:
            await ctx.send('Missing labels to assign.')
        js = await self.edit_issue(number, labels=labels)
        await ctx.send(f'Successfully labelled <{js["html_url"]}>')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.channel_id != DISCORD_PY_BOT_LIST:
            return

        if str(payload.emoji) not in ('\N{WHITE HEAVY CHECK MARK}', '\N{CROSS MARK}'):
            return

        channel = self.bot.get_guild(payload.guild_id).get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except (AttributeError, discord.HTTPException):
            return

        if len(message.embeds) != 1:
            return

        embed = message.embeds[0]
        user = self.bot.get_user(payload.user_id)
        if user is None or user.bot:
            return

        # Already been handled.
        if embed.colour != discord.Colour.blurple():
            return

        author_id = int(embed.footer.text)
        bot_id = embed.author.name
        if str(payload.emoji) == '\N{WHITE HEAVY CHECK MARK}':
            to_send = f"Your bot, <@{bot_id}>, has been added."
            colour = discord.Colour.dark_green()
        else:
            to_send = f"Your bot, <@{bot_id}>, has been rejected."
            colour = discord.Colour.dark_magenta()

        try:
            await self.bot.get_user(author_id).send(to_send)
        except (AttributeError, discord.HTTPException):
            colour = discord.Colour.gold()

        as_dict = embed.to_dict()
        as_dict['color'] = colour.value
        await self.bot.http.edit_message(payload.channel_id, payload.message_id, embed=as_dict)

    @commands.command()
    @in_testing()
    async def addbot(self, ctx, *, user: BotUser):
        """Requests your bot to be added to the server.

        To request your bot you must pass your bot's user ID.

        You will get a DM regarding the status of your bot, so make sure you
        have them on.
        """

        confirm = None
        def terms_acceptance(msg):
            nonlocal confirm
            if msg.author.id != ctx.author.id:
                return False
            if msg.channel.id != ctx.channel.id:
                return False
            if msg.content in ('**I agree**', 'I agree'):
                confirm = True
                return True
            elif msg.content in ('**Abort**', 'Abort'):
                confirm = False
                return True
            return False

        msg = 'By requesting to add your bot, you must agree to the guidelines presented in the <#381974649019432981>.\n\n' \
              'If you agree, reply to this message with **I agree** within 1 minute. If you do not, reply with **Abort**.'
        prompt = await ctx.send(msg)

        try:
            await self.bot.wait_for('message', check=terms_acceptance, timeout=60.0)
        except asyncio.TimeoutError:
            return await ctx.send('Took too long. Aborting.')
        finally:
            await prompt.delete()

        if not confirm:
            return await ctx.send('Aborting.')

        embed = discord.Embed(title='Bot Request', colour=discord.Colour.blurple())
        embed.description = f'[Invite URL](https://discordapp.com/oauth2/authorize?client_id={user.id}&scope=bot)'
        embed.add_field(name='Author', value=f'{ctx.author} (ID: {ctx.author.id})', inline=False)
        embed.add_field(name='Bot', value=f'{user} (ID: {user.id})', inline=False)
        embed.timestamp = ctx.message.created_at

        # data for the bot to retrieve later
        embed.set_footer(text=ctx.author.id)
        embed.set_author(name=user.id, icon_url=user.avatar_url_as(format='png'))

        channel = ctx.guild.get_channel(DISCORD_PY_BOT_LIST)
        try:
            msg = await channel.send(embed=embed)
            await msg.add_reaction('\N{WHITE HEAVY CHECK MARK}')
            await msg.add_reaction('\N{CROSS MARK}')
        except discord.HTTPException as e:
            return await ctx.send(f'Failed to request your bot somehow. Tell Danny, {str(e)!r}')

        await ctx.send('Your bot has been requested to the moderators. It will DM you the status of your request.')

def setup(bot):
    bot.add_cog(DPYExclusive(bot))
