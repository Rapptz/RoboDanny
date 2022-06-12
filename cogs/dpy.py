from __future__ import annotations

from discord.ext import commands
from typing import TYPE_CHECKING, Any, Optional, Union
from cogs.utils.formats import human_join
from cogs.utils.paginator import FieldPageSource, RoboPages
import binascii
import discord
import asyncio
import base64
import yarl
import re

DISCORD_API_GUILD_ID = 81384788765712384
DISCORD_PY_API_CHANNEL_ID = 381889733053251584
DISCORD_PY_GUILD_ID = 336642139381301249
DISCORD_PY_BOTS_ROLE = 381980817125015563
DISCORD_PY_REWRITE_ROLE = 381981861041143808
DISCORD_PY_TESTER_ROLE = 859169678966784031
DISCORD_PY_JP_ROLE = 490286873311182850
DISCORD_PY_PROF_ROLE = 381978395270971407
DISCORD_PY_HELP_CHANNELS = (381965515721146390, 564950631455129636, 490289254757564426, 738572311107469354)
DISCORD_PY_HELP_FORUM = 985299059441025044

GITHUB_TODO_COLUMN = 9341868
GITHUB_PROGRESS_COLUMN = 9341869
GITHUB_DONE_COLUMN = 9341870

TOKEN_REGEX = re.compile(r'[a-zA-Z0-9_-]{23,28}\.[a-zA-Z0-9_-]{6,7}\.[a-zA-Z0-9_-]{27,}')


if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import Context, GuildContext
    from cogs.emoji import Emoji as EmojiCog


def validate_token(token: str) -> bool:
    try:
        # Just check if the first part validates as a user ID
        (user_id, _, _) = token.split('.')
        user_id = int(base64.b64decode(user_id, validate=True))
    except (ValueError, binascii.Error):
        return False
    else:
        return True


class GithubError(commands.CommandError):
    pass


def is_proficient():
    def predicate(ctx: GuildContext) -> bool:
        return ctx.author.get_role(DISCORD_PY_PROF_ROLE) is not None

    return commands.check(predicate)


def is_doc_helper():
    def predicate(ctx: GuildContext) -> bool:
        return ctx.author.get_role(714516281293799438) is not None

    return commands.check(predicate)


class GistContent:
    source: str
    language: Optional[str]

    def __init__(self, argument: str):
        try:
            block, code = argument.split('\n', 1)
        except ValueError:
            self.source = argument
            self.language = None
        else:
            if not block.startswith('```') and not code.endswith('```'):
                self.source = argument
                self.language = None
            else:
                self.language = block[3:]
                self.source = code.rstrip('`').replace('```', '')


def make_field_from_note(data: dict[str, Any], column_id: int) -> tuple[str, str]:
    id = data['id']
    value = data['note']
    issue = data.get('content_url')
    if issue:
        issue = issue.replace('api.github.com/repos', 'github.com')
        _, _, number = issue.rpartition('/')
        value = f'[#{number}]({issue})'

    if column_id == GITHUB_TODO_COLUMN:
        return (f'TODO: {id}', value)
    else:
        return (f'In Progress: {id}', value)


class DPYExclusive(commands.Cog, name='discord.py'):
    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')
        self._invite_cache: dict[str, int] = {}
        self.bot.loop.create_task(self._prepare_invites())
        self._req_lock = asyncio.Lock(loop=self.bot.loop)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='dpy', id=596577034537402378)

    async def _prepare_invites(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(DISCORD_PY_GUILD_ID)

        if guild is not None:
            invites = await guild.invites()
            self._invite_cache = {invite.code: invite.uses or 0 for invite in invites}

    def cog_check(self, ctx: Context):
        return ctx.guild and ctx.guild.id == DISCORD_PY_GUILD_ID

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, GithubError):
            await ctx.send(f'Github Error: {error}')

    async def github_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, Any]] = None,
    ) -> Any:
        hdrs = {
            'Accept': 'application/vnd.github.inertia-preview+json',
            'User-Agent': 'RoboDanny DPYExclusive Cog',
            'Authorization': f'token {self.bot.config.github_token}',
        }

        req_url = yarl.URL('https://api.github.com') / url

        if headers is not None and isinstance(headers, dict):
            hdrs.update(headers)

        async with self._req_lock:
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

    async def create_gist(
        self,
        content: str,
        *,
        description: Optional[str] = None,
        filename: Optional[str] = None,
        public: bool = True,
    ) -> str:
        headers = {
            'Accept': 'application/vnd.github.v3+json',
        }

        filename = filename or 'output.txt'
        data = {
            'public': public,
            'files': {
                filename: {
                    'content': content,
                }
            },
        }

        if description:
            data['description'] = description

        js = await self.github_request('POST', 'gists', data=data, headers=headers)
        return js['html_url']

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != DISCORD_PY_GUILD_ID:
            return

        if member.bot:
            await member.add_roles(discord.Object(id=DISCORD_PY_BOTS_ROLE))
            return

        JP_INVITE_CODES = ('y9Bm8Yx', 'nXzj3dg')
        invites = await member.guild.invites()
        for invite in invites:
            assert invite.uses is not None
            if invite.code in JP_INVITE_CODES and invite.uses > self._invite_cache[invite.code]:
                await member.add_roles(discord.Object(id=DISCORD_PY_JP_ROLE))
            self._invite_cache[invite.code] = invite.uses

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.guild.id not in (DISCORD_PY_GUILD_ID, DISCORD_API_GUILD_ID):
            return

        tokens = [token for token in TOKEN_REGEX.findall(message.content) if validate_token(token)]
        if tokens and message.author.id != self.bot.user.id:
            url = await self.create_gist('\n'.join(tokens), description='Discord tokens detected')
            msg = f'{message.author.mention}, I have found tokens and sent them to <{url}> to be invalidated for you.'
            return await message.channel.send(msg)

        if message.author.bot:
            return

        # Handle some #emoji-suggestions auto moderator and things
        # Process is mainly informal anyway
        if message.channel.id == 596308497671520256:
            emoji: Optional[EmojiCog] = self.bot.get_cog('Emoji')  # type: ignore
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

        if message.guild.id == DISCORD_PY_GUILD_ID or message.channel.id == DISCORD_PY_API_CHANNEL_ID:
            m = self.issue.search(message.content)
            if m is not None:
                url = f'<https://github.com/Rapptz/discord.py/issues/{m.group("number")}>'
                await message.channel.send(url)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """
        (Hopefully) temporary event handler for thread create to pin Forum post starting messages in the d.py server.
        """
        if not thread.parent_id == DISCORD_PY_HELP_FORUM:
            return

        message = thread.get_partial_message(thread.id)
        await message.pin()


    async def toggle_role(self, ctx: GuildContext, role_id: int) -> None:
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
    async def nihongo(self, ctx: GuildContext):
        """日本語チャットに参加したい場合はこの役職を付ける"""

        await self.toggle_role(ctx, DISCORD_PY_JP_ROLE)

    @commands.command(hidden=True)
    async def tester(self, ctx: GuildContext):
        """Allows you to opt-in to being a tester for discord.py"""
        await self.toggle_role(ctx, DISCORD_PY_TESTER_ROLE)

    async def get_valid_labels(self) -> set[str]:
        labels = await self.github_request('GET', 'repos/Rapptz/discord.py/labels')
        return {e['name'] for e in labels}

    async def edit_issue(self, number: int, *, labels: Optional[tuple[str, ...]] = None, state: Optional[str] = None) -> Any:
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
            label_set = set(labels)
            diff = [repr(x) for x in (label_set - valid_labels)]
            if diff:
                raise GithubError(f'Invalid labels passed: {human_join(diff, final="and")}')
            data['labels'] = list(current_labels | label_set)

        return await self.github_request('PATCH', url_path, data=data)

    @commands.group(aliases=['gh'])
    async def github(self, ctx: GuildContext):
        """Github administration commands."""
        pass

    @github.command(name='close')
    @is_proficient()
    async def github_close(self, ctx: GuildContext, number: int, *labels: str):
        """Closes and optionally labels an issue."""
        js = await self.edit_issue(number, labels=labels, state='closed')
        await ctx.send(f'Successfully closed <{js["html_url"]}>')

    @github.command(name='open')
    @is_proficient()
    async def github_open(self, ctx: GuildContext, number: int):
        """Re-open an issue"""
        js = await self.edit_issue(number, state='open')
        await ctx.send(f'Successfully closed <{js["html_url"]}>')

    @github.command(name='label')
    @is_proficient()
    async def github_label(self, ctx: GuildContext, number: int, *labels: str):
        """Adds labels to an issue."""
        if not labels:
            await ctx.send('Missing labels to assign.')
        js = await self.edit_issue(number, labels=labels)
        await ctx.send(f'Successfully labelled <{js["html_url"]}>')

    @github.group(name='todo')
    @is_doc_helper()
    async def github_todo(self, ctx: GuildContext):
        """Handles the board for the Documentation project."""
        pass

    async def get_cards_from_column(self, column_id: int) -> list[tuple[str, str]]:
        path = f'projects/columns/{column_id}/cards'
        js = await self.github_request('GET', path)
        return [make_field_from_note(card, column_id) for card in js]

    @github_todo.command(name='list')
    async def gh_todo_list(self, ctx: GuildContext):
        """Lists the current todos and in progress stuff."""
        todos = await self.get_cards_from_column(GITHUB_TODO_COLUMN)
        progress = await self.get_cards_from_column(GITHUB_PROGRESS_COLUMN)
        if progress:
            todos.extend(progress)

        source = FieldPageSource(todos, per_page=8)
        source.embed.colour = 0x28A745
        pages = RoboPages(source, ctx=ctx)
        await pages.start()

    @github_todo.command(name='create')
    async def gh_todo_create(self, ctx: GuildContext, *, content: Union[int, str]):
        """Creates a todo based on PR number or string content."""
        if isinstance(content, str):
            path = f'projects/columns/{GITHUB_TODO_COLUMN}/cards'
            payload = {'note': content}
        else:
            path = f'projects/columns/{GITHUB_PROGRESS_COLUMN}/cards'
            payload = {'content_id': content, 'content_type': 'PullRequest'}

        js = await self.github_request('POST', path, data=payload)
        await ctx.send(f'Created note with ID {js["id"]}')

    async def move_card_to_column(self, note_id, column_id):
        path = f'projects/columns/cards/{note_id}/moves'
        payload = {
            'position': 'top',
            'column_id': column_id,
        }

        await self.github_request('POST', path, data=payload)

    @github_todo.command(name='complete')
    async def gh_todo_complete(self, ctx: GuildContext, note_id: int):
        """Moves a note to the complete column"""
        await self.move_card_to_column(note_id, GITHUB_DONE_COLUMN)
        await ctx.send(ctx.tick(True))

    @github_todo.command(name='progress')
    async def gh_todo_progress(self, ctx: GuildContext, note_id: int):
        """Moves a note to the progress column"""
        await self.move_card_to_column(note_id, GITHUB_PROGRESS_COLUMN)
        await ctx.send(ctx.tick(True))

    @commands.command(hidden=True)
    @commands.is_owner()
    async def emojipost(self, ctx: GuildContext):
        """Fancy post the emoji lists"""
        emojis = sorted([e for e in ctx.guild.emojis if len(e.roles) == 0 and e.available], key=lambda e: e.name.lower())
        paginator = commands.Paginator(suffix='', prefix='')
        channel: Optional[discord.TextChannel] = ctx.guild.get_channel(596549678393327616)  # type: ignore

        if channel is None:
            return

        for emoji in emojis:
            paginator.add_line(f'{emoji} -- `{emoji}`')

        await channel.purge()
        for page in paginator.pages:
            await channel.send(page)

        await ctx.send(ctx.tick(True))

    @commands.command(name='gist', hidden=True)
    @commands.is_owner()
    async def gist(self, ctx: GuildContext, *, content: GistContent):
        """Posts a gist"""
        if content.language is None:
            url = await self.create_gist(content.source, filename='input.md', public=False)
        else:
            url = await self.create_gist(content.source, filename=f'input.{content.language}', public=False)

        await ctx.send(f'<{url}>')


async def setup(bot: RoboDanny):
    await bot.add_cog(DPYExclusive(bot))
