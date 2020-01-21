from discord.ext import commands
from cogs.utils.formats import human_join
import discord
import asyncio
import yarl
import re

DISCORD_PY_GUILD_ID = 336642139381301249
DISCORD_PY_BOTS_ROLE = 381980817125015563
DISCORD_PY_REWRITE_ROLE = 381981861041143808
DISCORD_PY_TESTER_ROLE = 669155135829835787
DISCORD_PY_JP_ROLE = 490286873311182850
DISCORD_PY_PROF_ROLE = 381978395270971407

class GithubError(commands.CommandError):
    pass

def is_proficient():
    def predicate(ctx):
        return ctx.author._roles.has(DISCORD_PY_PROF_ROLE)
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

        # Handle some #emoji-suggestions auto moderator and things
        # Process is mainly informal anyway
        if message.channel.id == 596308497671520256:
            emoji = self.bot.get_cog('Emoji')
            if emoji is None:
                return
            matches = emoji.find_all_emoji(message)
            # Don't want multiple emoji per message
            if len(matches) > 1:
                return await message.delete()
            elif len(message.attachments) > 1:
                # Nor multiple attachments
                return await message.delete()

            # Add voting reactions
            await message.add_reaction('<:greenTick:330090705336664065>')
            await message.add_reaction('<:redTick:330090723011592193>')

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

    @commands.command(hidden=True)
    async def tester(self, ctx):
        """Allows you to opt-in to being a tester for discord.py"""
        await self.toggle_role(ctx, DISCORD_PY_TESTER_ROLE)

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

    @commands.command(hidden=True)
    @commands.is_owner()
    async def emojipost(self, ctx):
        """Fancy post the emoji lists"""
        emojis = sorted([e for e in ctx.guild.emojis if len(e.roles) == 0 and e.available], key=lambda e: e.name)
        paginator = commands.Paginator(suffix='', prefix='')
        channel = ctx.guild.get_channel(596549678393327616)

        for emoji in emojis:
            paginator.add_line(f'{emoji} -- `{emoji}`')

        await channel.purge()
        for page in paginator.pages:
            await channel.send(page)

        await ctx.send(ctx.tick(True))

def setup(bot):
    bot.add_cog(DPYExclusive(bot))
