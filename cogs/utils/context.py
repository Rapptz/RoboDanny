from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Generic, Iterable, Protocol, TypeVar, Union, Optional
from discord.ext import commands
import asyncio
import discord
import io


if TYPE_CHECKING:
    from bot import RoboDanny
    from aiohttp import ClientSession
    from asyncpg import Pool, Connection
    from types import TracebackType


T = TypeVar('T')


# For typing purposes, `Context.db` returns a Protocol type
# that allows us to properly type the return values via narrowing
# Right now, asyncpg is untyped so this is better than the current status quo
# To actually receive the regular Pool type `Context.pool` can be used instead.


class ConnectionContextManager(Protocol):
    async def __aenter__(self) -> Connection:
        ...

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        ...


class DatabaseProtocol(Protocol):
    async def execute(self, query: str, *args: Any, timeout: Optional[float] = None) -> str:
        ...

    async def fetch(self, query: str, *args: Any, timeout: Optional[float] = None) -> list[Any]:
        ...

    async def fetchrow(self, query: str, *args: Any, timeout: Optional[float] = None) -> Optional[Any]:
        ...

    def acquire(self, *, timeout: Optional[float] = None) -> ConnectionContextManager:
        ...

    def release(self, connection: Connection) -> None:
        ...


class ConfirmationView(discord.ui.View):
    def __init__(self, *, timeout: float, author_id: int, delete_after: bool) -> None:
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None
        self.delete_after: bool = delete_after
        self.author_id: int = author_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        else:
            await interaction.response.send_message('This confirmation dialog is not for you.', ephemeral=True)
            return False

    async def on_timeout(self) -> None:
        if self.delete_after and self.message:
            if not self.message.flags.ephemeral:
                await self.message.delete()
            else:
                await self.message.edit(view=None, content='This is safe to dismiss now.')

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        if self.delete_after and self.message:
            if not self.message.flags.ephemeral:
                await interaction.delete_original_response()
            else:
                await interaction.edit_original_response(view=None, content='This is safe to dismiss now.')

        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        if self.delete_after and self.message:
            if not self.message.flags.ephemeral:
                await interaction.delete_original_response()
            else:
                await interaction.edit_original_response(view=None, content='This is safe to dismiss now.')

        self.stop()


class DisambiguatorView(discord.ui.View, Generic[T]):
    message: discord.Message
    selected: T

    def __init__(self, ctx: Context, data: list[T], entry: Callable[[T], Any]):
        super().__init__()
        self.ctx: Context = ctx
        self.data: list[T] = data

        options = []
        for i, x in enumerate(data):
            opt = entry(x)
            if not isinstance(opt, discord.SelectOption):
                opt = discord.SelectOption(label=str(opt))
            opt.value = str(i)
            options.append(opt)

        select = discord.ui.Select(options=options)

        select.callback = self.on_select_submit
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message('This select menu is not meant for you, sorry.', ephemeral=True)
            return False
        return True

    async def on_select_submit(self, interaction: discord.Interaction):
        index = int(self.select.values[0])
        self.selected = self.data[index]
        await interaction.response.defer()
        if not self.message.flags.ephemeral:
            await self.message.delete()

        self.stop()


class Context(commands.Context):
    channel: Union[discord.VoiceChannel, discord.TextChannel, discord.Thread, discord.DMChannel]
    prefix: str
    command: commands.Command[Any, ..., Any]
    bot: RoboDanny

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pool: Pool = self.bot.pool

    async def entry_to_code(self, entries: Iterable[tuple[str, str]]) -> None:
        width = max(len(a) for a, b in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'{name:<{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    async def indented_entry_to_code(self, entries: Iterable[tuple[str, str]]) -> None:
        width = max(len(a) for a, b in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'\u200b{name:>{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    def __repr__(self) -> str:
        # we need this for our cache key strategy
        return '<Context>'

    @property
    def session(self) -> ClientSession:
        return self.bot.session

    @discord.utils.cached_property
    def replied_reference(self) -> Optional[discord.MessageReference]:
        ref = self.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            return ref.resolved.to_reference()
        return None

    @discord.utils.cached_property
    def replied_message(self) -> Optional[discord.Message]:
        ref = self.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            return ref.resolved
        return None

    async def disambiguate(self, matches: list[T], entry: Callable[[T], Any], *, ephemeral: bool = False) -> T:
        if len(matches) == 0:
            raise ValueError('No results found.')

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 25:
            raise ValueError('Too many results... sorry.')

        view = DisambiguatorView(self, matches, entry)
        view.message = await self.send(
            'There are too many matches... Which one did you mean?', view=view, ephemeral=ephemeral
        )
        await view.wait()
        return view.selected

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        author_id: Optional[int] = None,
    ) -> Optional[bool]:
        """An interactive reaction confirmation dialog.

        Parameters
        -----------
        message: str
            The message to show along with the prompt.
        timeout: float
            How long to wait before returning.
        delete_after: bool
            Whether to delete the confirmation message after we're done.
        author_id: Optional[int]
            The member who should respond to the prompt. Defaults to the author of the
            Context's message.

        Returns
        --------
        Optional[bool]
            ``True`` if explicit confirm,
            ``False`` if explicit deny,
            ``None`` if deny due to timeout
        """

        author_id = author_id or self.author.id
        view = ConfirmationView(
            timeout=timeout,
            delete_after=delete_after,
            author_id=author_id,
        )
        view.message = await self.send(message, view=view, ephemeral=delete_after)
        await view.wait()
        return view.value

    def tick(self, opt: Optional[bool], label: Optional[str] = None) -> str:
        lookup = {
            True: '<:greenTick:330090705336664065>',
            False: '<:redTick:330090723011592193>',
            None: '<:greyTick:563231201280917524>',
        }
        emoji = lookup.get(opt, '<:redTick:330090723011592193>')
        if label is not None:
            return f'{emoji}: {label}'
        return emoji

    @property
    def db(self) -> DatabaseProtocol:
        return self.pool  # type: ignore

    async def show_help(self, command: Any = None) -> None:
        """Shows the help command for the specified command if given.

        If no command is given, then it'll show help for the current
        command.
        """
        cmd = self.bot.get_command('help')
        command = command or self.command.qualified_name
        await self.invoke(cmd, command=command)  # type: ignore

    async def safe_send(self, content: str, *, escape_mentions: bool = True, **kwargs) -> discord.Message:
        """Same as send except with some safe guards.

        1) If the message is too long then it sends a file with the results instead.
        2) If ``escape_mentions`` is ``True`` then it escapes mentions.
        """
        if escape_mentions:
            content = discord.utils.escape_mentions(content)

        if len(content) > 2000:
            fp = io.BytesIO(content.encode())
            kwargs.pop('file', None)
            return await self.send(file=discord.File(fp, filename='message_too_long.txt'), **kwargs)
        else:
            return await self.send(content)


class GuildContext(Context):
    author: discord.Member
    guild: discord.Guild
    channel: Union[discord.VoiceChannel, discord.TextChannel, discord.Thread]
    me: discord.Member
    prefix: str
