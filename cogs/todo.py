from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands, tasks, menus
from discord.utils import MISSING
from discord import app_commands, ui

import asyncpg

from cogs.utils import fuzzy

from .utils import time, cache
from .utils.formats import plural
from .utils.paginator import RoboPages, FieldPageSource
from .utils.context import ConfirmationView

import datetime
import textwrap
import logging
import random
import re

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import GuildContext, Context

log = logging.getLogger(__name__)
MESSAGE_URL_REGEX = re.compile(
    r'https?://(?:(ptb|canary|www)\.)?discord(?:app)?\.com/channels/'
    r'(?P<guild_id>[0-9]{15,20}|@me)'
    r'/(?P<channel_id>[0-9]{15,20})/(?P<message_id>[0-9]{15,20})/?$'
)


class InvalidTime(RuntimeError):
    pass


def get_shortened_string(length: int, start: int, string: str) -> str:
    full_length = len(string)
    if full_length <= 100:
        return string

    todo_id, _, remaining = string.partition(' - ')
    start_index = len(todo_id) + 3
    max_remaining_length = 100 - start_index

    end = start + length
    if start < start_index:
        start = start_index

    # If the match is near the beginning then just extend it to the end
    if end < 100:
        if full_length > 100:
            return string[:99] + '…'
        return string[:100]

    has_end = end < full_length
    excess = (end - start) - max_remaining_length + 1
    if has_end:
        return f'{todo_id} - …{string[start + excess + 1:end]}…'
    return f'{todo_id} - …{string[start + excess:end]}'


def ensure_future_time(argument: str, now: datetime.datetime) -> datetime.datetime:
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)

    try:
        converter = time.Time(argument, now=now)
    except commands.BadArgument:
        random_future = now + datetime.timedelta(days=random.randint(3, 60))
        raise InvalidTime(f'Due date could not be parsed, sorry. Try something like "tomorrow" or "{random_future.date()}".')

    minimum_time = now + datetime.timedelta(minutes=5)
    if converter.dt < minimum_time:
        raise InvalidTime('Due date must be at least 5 minutes in the future.')

    return converter.dt.replace(tzinfo=None)


def state_emoji(opt: Optional[bool]) -> str:
    lookup = {
        True: '<:completed:1013824278535340113>',
        # False: '<:overdue:1013827216183926806>',
        False: '<:overdue:1013830796748005428>',
        None: '<:pending:1013824279265165313>',
    }
    return lookup[opt]


class TodoItem:
    cog: Todo
    bot: RoboDanny
    id: int
    user_id: int
    channel_id: Optional[int]
    guild_id: Optional[int]
    message_id: Optional[int]
    due_date: Optional[datetime.datetime]
    content: Optional[str]
    completed_at: Optional[datetime.datetime]
    cached_content: Optional[str]
    reminder_triggered: bool
    message: Optional[discord.Message]

    def __init__(self, cog: Todo, record: Any) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.id = record['id']
        self.user_id = record['user_id']
        self.channel_id = record.get('channel_id')
        self.guild_id = record.get('guild_id')
        self.message_id = record.get('message_id')
        self.due_date = record.get('due_date')
        self.content = record.get('content')
        self.cached_content = record.get('cached_content')
        self.completed_at = record.get('completed_at')
        self.message = None
        self.reminder_triggered = record.get('reminder_triggered', False)

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} id={self.id} user_id={self.user_id} due_date={self.due_date} content={self.content} completed_at={self.completed_at} triggered={self.reminder_triggered}>'

    @property
    def jump_url(self) -> Optional[str]:
        if self.message is not None:
            return self.message.jump_url

        if self.message_id and self.channel_id:
            guild = self.guild_id or '@me'
            return f'https://discord.com/channels/{guild}/{self.channel_id}/{self.message_id}'

        return None

    @property
    def choice_text(self) -> str:
        content = []
        if self.content is not None:
            content.append(self.content)
        if self.message is None:
            if self.cached_content:
                content.append(self.cached_content)
        else:
            content.append(self.message.content)
        return f'{self.id} - {" | ".join(content)}'

    def to_select_option(self, value: Any, *, default: bool = False) -> discord.SelectOption:
        description = 'No message'
        if self.cached_content:
            description = textwrap.shorten(self.cached_content, width=100, placeholder='…')
        if self.content:
            label = textwrap.shorten(f'{self.id}: {self.content}', width=100, placeholder='…')
        else:
            label = f'{self.id}: No content'

        return discord.SelectOption(
            label=label, value=str(value), description=description, emoji=self.emoji, default=default
        )

    @property
    def completion_state(self) -> Optional[bool]:
        """
        None => pending/undone
        True => completed
        False => overdue
        """
        state = None
        if self.due_date is not None and self.due_date <= datetime.datetime.utcnow():
            state = False
        if self.completed_at:
            state = True
        return state

    @property
    def emoji(self) -> str:
        return state_emoji(self.completion_state)

    @property
    def field_tuple(self) -> tuple[str, str]:
        state = self.completion_state
        if self.content is None:
            name = f'Todo {self.id}: No content'
        else:
            name = f'Todo {self.id}: {textwrap.shorten(self.content, width=100, placeholder="…")}'

        value = ''
        if state is False:
            if self.due_date:
                value = f'Overdue: {time.format_dt(self.due_date, "R")}'
        elif state is None and self.due_date:
            value = f'Due: {time.format_dt(self.due_date, "R")}'
        elif self.completed_at:
            value = f'Completed: {time.format_dt(self.completed_at)}'

        if self.cached_content:
            url = self.jump_url
            shortened = textwrap.shorten(self.cached_content, width=100, placeholder="…")
            if url:
                value = f'[Jump!]({url}) \N{EN DASH} {shortened}\n{value}'
            else:
                value = f'{shortened}\n{value}'

        return name, value or '...'

    @property
    def embed(self) -> discord.Embed:
        # Colours are...
        # 0x7d7d7d grey, for pending
        # 0xfe5944 red, for overdue
        # 0x40af7c green, for completed

        embed = discord.Embed(
            title=f'Todo {self.id}',
            color=0x7D7D7D,
        )

        url = self.jump_url
        if self.message is not None:
            embed.description = self.message.content
            author = self.message.author
            embed.set_author(name=author, icon_url=author.display_avatar)
            if self.content:
                embed.add_field(name='Content', value=self.content, inline=False)
        else:
            if self.cached_content is not None:
                embed.description = self.cached_content
                if self.content:
                    embed.add_field(name='Content', value=self.content, inline=False)
            else:
                embed.description = self.content

        if url:
            embed.add_field(name='Jump to Message', value=f'[Jump!]({url})', inline=False)

        if self.due_date:
            embed.set_footer(text='Due').timestamp = self.due_date
            if datetime.datetime.utcnow() > self.due_date:
                embed.colour = 0xFE5944
                embed.set_footer(text='Overdue')

        if self.completed_at:
            embed.set_footer(text='Completed').timestamp = self.completed_at
            embed.colour = 0x40AF7C

        return embed

    @property
    def channel(self) -> Optional[discord.PartialMessageable]:
        if self.channel_id is not None:
            return self.bot.get_partial_messageable(self.channel_id, guild_id=self.guild_id)
        return None

    async def fetch_message(self) -> None:
        channel = self.channel
        if channel is not None and self.message_id is not None:
            self.message = await self.cog.get_message(channel, self.message_id)
            if self.message and self.message.content != self.cached_content:
                self.cached_content = self.message.content
                query = 'UPDATE todo SET cached_content = $1 WHERE id = $2'
                await self.bot.pool.execute(query, self.cached_content, self.id)

    async def edit(
        self,
        *,
        content: Optional[str] = MISSING,
        due_date: Optional[datetime.datetime] = MISSING,
        message: Optional[discord.Message] = MISSING,
        completed_at: Optional[datetime.datetime] = MISSING,
    ) -> None:
        # This is taking advantage of the fact dicts are ordered.
        columns: dict[str, Any] = {}

        if content is not MISSING:
            columns['content'] = content

        if due_date is not MISSING:
            columns['due_date'] = due_date
            columns['reminder_triggered'] = False

        if message is not MISSING:
            if message is None:
                columns['message_id'] = None
                columns['channel_id'] = None
                columns['guild_id'] = None
                columns['cached_content'] = None
            else:
                columns['message_id'] = message.id
                columns['channel_id'] = message.channel.id
                if isinstance(message.channel, discord.PartialMessageable):
                    columns['guild_id'] = message.channel.guild_id
                else:
                    columns['guild_id'] = message.guild and message.guild.id
                columns['cached_content'] = message.content

        if completed_at is not MISSING:
            columns['completed_at'] = completed_at

        query = f'UPDATE todo SET {", ".join(f"{k} = ${i}" for i, k in enumerate(columns, start=1))} WHERE id = ${len(columns) + 1}'
        await self.bot.pool.execute(query, *columns.values(), self.id)

        if due_date is not MISSING:
            self.cog.check_for_task_resync(self.id, due_date)

        for attr, value in columns.items():
            setattr(self, attr, value)

        if message is not MISSING:
            self.message = message

    async def delete(self) -> None:
        query = 'DELETE FROM todo WHERE id = $1'
        await self.bot.pool.execute(query, self.id)


class ActiveDueTodo(TodoItem):
    due_date: datetime.datetime


class EditDueDateModal(ui.Modal, title='Edit Due Date'):
    due_date = ui.TextInput(label='Due Date', placeholder='e.g. 5m, 2022-12-31, tomorrow, etc.', max_length=100)

    def __init__(self, item: TodoItem, *, required: bool = False) -> None:
        super().__init__()
        self.item: TodoItem = item
        if required:
            self.due_date.min_length = 2

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.due_date.value
        if not value:
            due_date = None
        else:
            try:
                due_date = ensure_future_time(value, interaction.created_at)
            except InvalidTime as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)
        await self.item.edit(due_date=due_date)
        if due_date is None:
            msg = 'Removed due date.'
        else:
            msg = f'Set due date to {time.format_dt(due_date)} ({time.format_dt(due_date, "R")}).'

        await interaction.followup.send(msg, ephemeral=True)


class EditTodoModal(ui.Modal, title='Edit Todo'):
    due_date = ui.TextInput(
        label='Due Date', placeholder='e.g. 5m, 2022-12-31, tomorrow, etc.', max_length=100, required=False
    )
    message_url = ui.TextInput(
        label='Message',
        placeholder='https://discord.com/channels/182325885867786241/182328002154201088/182331989766963200',
        max_length=120,
        required=False,
    )
    content = ui.TextInput(label='Content', max_length=1024, style=discord.TextStyle.long, required=False)

    def __init__(self, item: TodoItem) -> None:
        super().__init__(custom_id=f'todo-edit-{item.id}')
        self.title = f'Edit Todo {item.id}'
        self.item: TodoItem = item
        if item.due_date is not None:
            self.due_date.default = item.due_date.isoformat(' ', 'minutes')

        url = item.jump_url
        if url is not None:
            self.message_url.default = url

        if item.content is not None:
            self.content.default = item.content

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        kwargs: dict[str, Any] = {}
        due_date = self.due_date.value
        if due_date != self.due_date.default:
            if not due_date:
                due_date = None
            else:
                try:
                    due_date = ensure_future_time(due_date, interaction.created_at)
                except InvalidTime as e:
                    await interaction.response.send_message(str(e), ephemeral=True)
                    return

            kwargs['due_date'] = due_date

        message_url = self.message_url.value
        if message_url != self.message_url.default:
            if not message_url:
                message = None
            else:
                match = MESSAGE_URL_REGEX.match(message_url)
                if match is None:
                    await interaction.followup.send(
                        'Message URL could not be parsed, sorry. Be sure to use the "Copy Message Link" context menu!',
                        ephemeral=True,
                    )
                    return

                message_id = int(match.group('message_id'))
                channel_id = int(match.group('channel_id'))
                guild_id = match.group('guild_id')
                guild_id = None if guild_id == '@me' else int(guild_id)
                channel = self.item.bot.get_partial_messageable(channel_id, guild_id=guild_id)
                message = await self.item.cog.get_message(channel, message_id)
                if message is None:
                    await interaction.followup.send(
                        'That message was not found, sorry. Maybe it was deleted or I can\'t see it.', ephemeral=True
                    )

            kwargs['message'] = message

        note = self.content.value
        if note != self.content.default:
            kwargs['content'] = note

        if kwargs:
            await self.item.edit(**kwargs)

        await interaction.followup.send('Successfully edited todo!', ephemeral=True)


class AddTodoModal(ui.Modal, title='Add Todo'):
    content = ui.TextInput(label='Content (optional)', max_length=1024, required=False, style=discord.TextStyle.long)

    due_date = ui.TextInput(
        label='Due Date (optional)', placeholder='e.g. 5m, 2022-12-31, tomorrow, etc.', max_length=100, required=False
    )

    def __init__(self, cog: Todo, message: discord.Message) -> None:
        super().__init__(custom_id=f'todo-add-{message.id}')
        self.cog: Todo = cog
        self.message: discord.Message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        due_date = self.due_date.value
        if not due_date:
            due_date = None
        else:
            try:
                due_date = ensure_future_time(due_date, interaction.created_at)
            except InvalidTime as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return

        note = self.content.value or None
        await interaction.response.defer(ephemeral=True)
        item = await self.cog.add_todo(user_id=interaction.user.id, message=self.message, due_date=due_date, content=note)
        await interaction.followup.send(
            content=f'<a:agreenTick:1011968947949666324> Added todo item {item.id}.',
            embed=item.embed,
            ephemeral=True,
        )


class EditDueDateButton(ui.Button):
    def __init__(
        self,
        todo: TodoItem,
        *,
        label: str = 'Add Due Date',
        style: discord.ButtonStyle = discord.ButtonStyle.green,
        required: bool = False,
    ) -> None:
        super().__init__(label=label, style=style)
        self.todo = todo
        self.required: bool = required

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.todo.user_id:
            await interaction.response.send_message('This button is not meant for you, sorry.', ephemeral=True)
            return

        modal = EditDueDateModal(self.todo, required=self.required)
        await interaction.response.send_modal(modal)


class TodoPageSource(menus.ListPageSource):
    def __init__(self, todos: list[TodoItem]) -> None:
        super().__init__(entries=todos, per_page=1)

    async def format_page(self, menu: TodoPages, page: TodoItem):
        if page.channel is not None and page.message is None:
            await page.fetch_message()
        return page.embed


class BriefTodoPageSource(FieldPageSource):
    def __init__(self, todos: list[TodoItem]) -> None:
        super().__init__(entries=[todo.field_tuple for todo in todos], per_page=12)


class TodoPages(RoboPages):
    def __init__(self, todos: list[TodoItem], ctx: Context) -> None:
        self.todos: list[TodoItem] = todos
        self.select_menu: Optional[ui.Select] = None
        if 25 >= len(todos) > 1:
            select = ui.Select(
                placeholder=f'Select a todo ({len(todos)} todos found)',
                options=[todo.to_select_option(idx) for idx, todo in enumerate(todos)],
            )
            select.callback = self.selected
            self.select_menu = select

        super().__init__(TodoPageSource(todos), ctx=ctx, compact=True)

    @property
    def active_todo(self) -> TodoItem:
        return self.todos[self.current_page]

    def _update_labels(self, page_number: int) -> None:
        super()._update_labels(page_number)
        is_complete = self.active_todo.completed_at is not None
        button = self.complete_todo
        if is_complete:
            button.style = discord.ButtonStyle.grey
            button.label = 'Mark as not complete'
        else:
            button.style = discord.ButtonStyle.green
            button.label = 'Mark as complete'

        if self.select_menu:
            self.select_menu.options = [todo.to_select_option(idx) for idx, todo in enumerate(self.todos)]

    def fill_items(self) -> None:
        super().fill_items()
        if self.select_menu:
            self.clear_items()
            self.add_item(self.select_menu)

        self.add_item(self.complete_todo)
        self.add_item(self.edit_todo)
        self.add_item(self.delete_todo)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await super().on_error(interaction, error, item)
        log.error('Error in todo menu', exc_info=error)

    async def selected(self, interaction: discord.Interaction) -> None:
        assert self.select_menu is not None
        page = int(self.select_menu.values[0])
        await self.show_page(interaction, page)

    @ui.button(label='Mark as complete', style=discord.ButtonStyle.green, row=2)
    async def complete_todo(self, interaction: discord.Interaction, button: ui.Button):
        active = self.active_todo
        if active.completed_at is not None:
            completed_at = None
            text = f'Successfully marked {active.id} as not complete'
        else:
            completed_at = interaction.created_at.replace(tzinfo=None)
            text = f'Successfully marked {active.id} as complete'

        await active.edit(completed_at=completed_at)
        self._update_labels(self.current_page)
        await interaction.response.edit_message(embed=active.embed, view=self)
        await interaction.followup.send(text, ephemeral=True)

    @ui.button(label='Edit', style=discord.ButtonStyle.grey, row=2)
    async def edit_todo(self, interaction: discord.Interaction, button: ui.Button):
        modal = EditTodoModal(self.active_todo)
        await interaction.response.send_modal(modal)
        await modal.wait()

        assert interaction.message is not None
        await interaction.message.edit(view=self, embed=modal.item.embed)

    @ui.button(label='Delete', style=discord.ButtonStyle.red, row=2)
    async def delete_todo(self, interaction: discord.Interaction, button: ui.Button):
        assert interaction.message is not None
        confirm = ConfirmationView(timeout=60.0, author_id=interaction.user.id, delete_after=True)
        await interaction.response.send_message('Are you sure you want to delete this todo?', view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            await interaction.followup.send('Aborting', ephemeral=True)
            return

        todo = self.active_todo
        await todo.delete()
        del self.todos[self.current_page]

        if len(self.todos) == 0:
            await interaction.message.edit(view=None, content='No todos found!', embeds=[])
            self.stop()
            return

        previous = max(0, self.current_page - 1)
        await self.show_page(interaction, previous)
        todo.cog.get_todos.invalidate(self, interaction.user.id)


class AddAnywayButton(ui.Button):
    def __init__(self, cog: Todo, message: discord.Message, row: int = 2):
        super().__init__(label='Add Anyway', style=discord.ButtonStyle.blurple, row=row)
        self.cog = cog
        self.message = message

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddTodoModal(self.cog, self.message))


class ShowTodo(ui.View):
    def __init__(self, item: TodoItem) -> None:
        super().__init__(timeout=600.0)
        self.item: TodoItem = item

        if item.completed_at is not None:
            self.complete_todo.style = discord.ButtonStyle.grey
            self.complete_todo.label = 'Mark as not complete'

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.item.user_id:
            await interaction.response.send_message('This button is not meant for you, sorry.', ephemeral=True)
            return False
        return True

    @ui.button(label='Mark as complete', style=discord.ButtonStyle.green)
    async def complete_todo(self, interaction: discord.Interaction, button: ui.Button):
        if button.style is discord.ButtonStyle.grey:
            completed_at = None
            button.style = discord.ButtonStyle.green
            button.label = 'Mark as complete'
            text = f'Successfully marked {self.item.id} as not complete'
        else:
            completed_at = interaction.created_at.replace(tzinfo=None)
            button.style = discord.ButtonStyle.grey
            button.label = 'Mark as not complete'
            text = f'Successfully marked {self.item.id} as complete'

        await self.item.edit(completed_at=completed_at)
        await interaction.response.edit_message(embed=self.item.embed, view=self)
        await interaction.followup.send(text, ephemeral=True)

    @ui.button(label='Edit', style=discord.ButtonStyle.grey)
    async def edit_todo(self, interaction: discord.Interaction, button: ui.Button):
        modal = EditTodoModal(self.item)
        await interaction.response.send_modal(modal)
        await modal.wait()
        assert interaction.message is not None
        await interaction.message.edit(view=self, embed=modal.item.embed)

    @ui.button(label='Delete', style=discord.ButtonStyle.red)
    async def delete_todo(self, interaction: discord.Interaction, button: ui.Button):
        assert interaction.message is not None
        confirm = ConfirmationView(timeout=60.0, author_id=interaction.user.id, delete_after=True)
        await interaction.response.send_message('Are you sure you want to delete this todo?', view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            await interaction.followup.send('Aborting', ephemeral=True)
            return

        await self.item.delete()
        await interaction.followup.send('Successfully deleted todo', ephemeral=True)
        await interaction.message.delete()
        self.stop()
        self.item.cog.get_todos.invalidate(self, interaction.user.id)


class DueTodoView(ShowTodo):
    message: discord.Message

    async def on_timeout(self) -> None:
        try:
            await self.message.edit(view=None)
        except:
            pass

    @ui.button(label='Snooze', style=discord.ButtonStyle.blurple)
    async def edit_todo(self, interaction: discord.Interaction, button: ui.Button):
        modal = EditDueDateModal(self.item, required=True)
        modal.title = 'Snooze Todo'
        modal.due_date.placeholder = '10 minutes'
        modal.due_date.default = '10 minutes'
        modal.due_date.label = 'Duration'
        await interaction.response.send_modal(modal)
        await modal.wait()

        assert interaction.message is not None
        await interaction.message.edit(view=self, embed=modal.item.embed)


class AmbiguousTodo(ShowTodo):
    def __init__(self, todos: list[TodoItem], message: discord.Message) -> None:
        todo = todos[0]
        super().__init__(todo)
        self.todos = todos

        if len(todos) > 25:
            placeholder = f'Select a todo (only 25 out of {len(todos)} todos shown)'
        else:
            placeholder = f'Select a todo ({len(todos)} todos found)'

        self.select = ui.Select(
            placeholder=placeholder,
            options=[todo.to_select_option(idx) for idx, todo in enumerate(todos[:25])],
        )
        self.select.callback = self.selected
        self.clear_items()
        self.add_item(self.select)
        self.add_item(self.complete_todo)
        self.add_item(self.edit_todo)
        self.add_item(self.delete_todo)
        self.add_item(AddAnywayButton(todo.cog, message))

    async def selected(self, interaction: discord.Interaction) -> None:
        index = int(self.select.values[0])
        self.item = self.todos[index]
        button = self.complete_todo
        if self.item.completed_at is not None:
            button.style = discord.ButtonStyle.grey
            button.label = 'Mark as not complete'
        else:
            button.style = discord.ButtonStyle.green
            button.label = 'Mark as complete'

        await interaction.response.edit_message(embed=self.item.embed, view=self)


class ListFlags(commands.FlagConverter):
    completed: bool = commands.flag(
        description='Include completed todos, defaults to False', default=False, aliases=['complete']
    )
    pending: bool = commands.flag(description='Include pending todos, defaults to True', default=True)
    overdue: bool = commands.flag(description='Include overdue todos, defaults to True', default=True)
    brief: bool = commands.flag(
        description='Show a brief summary rather than detailed pages of todos, defaults to False',
        default=False,
        aliases=['compact'],
    )
    private: bool = commands.flag(description='Hide the todo list from others, defaults to False', default=False)


class Todo(commands.Cog):
    """Manage a todo list."""

    def __init__(self, bot: RoboDanny) -> None:
        self.bot: RoboDanny = bot
        self.active_todo: Optional[ActiveDueTodo] = None
        self._task: asyncio.Task[None] = MISSING
        self._message_cache: dict[int, discord.Message] = {}
        self.ctx_menu = app_commands.ContextMenu(name='Add todo', callback=self.todo_add_context_menu)
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_load(self) -> None:
        self._task = self.bot.loop.create_task(self.run_due_date_reminders())
        self.cleanup_message_cache.start()
        self.cleanup_todo.start()

    def cog_unload(self):
        if self._task:
            self._task.cancel()
        self.cleanup_message_cache.cancel()
        self.cleanup_todo.cancel()
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CLIPBOARD}')

    async def get_message(self, channel: discord.abc.Messageable, message_id: int) -> Optional[discord.Message]:
        try:
            return self._message_cache[message_id]
        except KeyError:
            try:
                msg = await channel.fetch_message(message_id)
            except discord.HTTPException:
                return None
            else:
                self._message_cache[message_id] = msg
                return msg

    @tasks.loop(hours=1.0)
    async def cleanup_message_cache(self):
        self._message_cache.clear()

    @tasks.loop(time=datetime.time(0, 0, 0, tzinfo=datetime.timezone.utc))
    async def cleanup_todo(self):
        status = await self.bot.pool.execute(
            """DELETE FROM todo WHERE completed_at IS NOT NULL AND completed_at < CURRENT_TIMESTAMP - '90 days'::interval"""
        )
        count = status.replace('DELETE ', '')
        log.info('Cleaned up %s todo items.', count)

    def check_for_task_resync(self, todo_id: int, new_date: Optional[datetime.datetime] = None) -> bool:
        if self.active_todo is None:
            if new_date is not None:
                self._task.cancel()
                self._task = self.bot.loop.create_task(self.run_due_date_reminders())
                return True
        else:
            is_earlier = new_date is not None and self.active_todo.due_date > new_date
            if self.active_todo.id == todo_id or is_earlier:
                self._task.cancel()
                self._task = self.bot.loop.create_task(self.run_due_date_reminders())
                return True

        return False

    async def get_earliest_due_todo(self) -> Optional[ActiveDueTodo]:
        query = """SELECT * FROM todo
                   WHERE completed_at IS NULL AND due_date IS NOT NULL AND NOT reminder_triggered
                   ORDER BY due_date LIMIT 1
                """
        record = await self.bot.pool.fetchrow(query)
        if record is None:
            return None
        return ActiveDueTodo(self, record)

    async def run_due_date_reminders(self) -> None:
        try:
            while not self.bot.is_closed():
                todo = self.active_todo = await self.get_earliest_due_todo()
                if todo is None:
                    # Nothing is currently due for some reason so just wait 5 minutes to check again.
                    await asyncio.sleep(300)
                else:
                    assert todo.due_date is not None  # This is asserted by the earlier query
                    await discord.utils.sleep_until(todo.due_date.replace(tzinfo=datetime.timezone.utc))
                    await self.send_due_date_reminder(todo)
        except (OSError, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.run_due_date_reminders())

    async def send_due_date_reminder(self, todo: ActiveDueTodo) -> None:
        await self.bot.pool.execute('UPDATE todo SET reminder_triggered = TRUE WHERE id = $1', todo.id)
        if todo.message_id is not None and todo.message is None:
            await todo.fetch_message()

        try:
            user = self.bot.get_user(todo.user_id)
            if user is None:
                dm = await self.bot.create_dm(discord.Object(todo.user_id))
            else:
                dm = await user.create_dm()
            view = DueTodoView(todo)
            view.message = await dm.send(f'You asked to be reminded of this todo', embed=todo.embed, view=view)
        except:
            log.warning('Could not send due date reminder to %s', todo.user_id)

    async def todo_id_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        todos = await self.get_todos(interaction.user.id)
        results = fuzzy.finder(current, todos, key=lambda t: t.choice_text, raw=True)
        return [
            app_commands.Choice(name=get_shortened_string(length, start, todo.choice_text), value=todo.id)
            for length, start, todo in results[:20]
        ]

    async def add_todo(
        self,
        *,
        user_id: int,
        content: Optional[str] = None,
        message: Optional[discord.Message] = None,
        due_date: Optional[datetime.datetime] = None,
    ) -> TodoItem:
        parameters: list[Any] = [user_id]
        query = """INSERT INTO todo (
                    user_id,
                    channel_id,
                    message_id,
                    guild_id,
                    cached_content,
                    due_date,
                    content
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """

        if message is not None:
            if isinstance(message.channel, discord.PartialMessageable):
                guild_id = message.channel.guild_id
            else:
                guild_id = message.guild and message.guild.id
            parameters.extend([message.channel.id, message.id, guild_id, message.content])
        else:
            parameters.extend([None, None, None, None])

        parameters.append(due_date)
        parameters.append(content)
        record = await self.bot.pool.fetchrow(query, *parameters)
        result = TodoItem(self, record)
        result.message = message
        self.get_todos.invalidate(self, user_id)
        return result

    @cache.cache()
    async def get_todos(self, user_id: int, /) -> list[TodoItem]:
        query = 'SELECT * FROM todo WHERE user_id = $1'
        return [TodoItem(self, record) for record in await self.bot.pool.fetch(query, user_id)]

    async def get_todo_for_message(self, user_id: int, message_id: int, /) -> list[TodoItem]:
        query = 'SELECT * FROM todo WHERE user_id = $1 AND message_id = $2'
        return [TodoItem(self, r) for r in await self.bot.pool.fetch(query, user_id, message_id)]

    @commands.hybrid_group()
    async def todo(self, ctx: Context) -> None:
        """Manage a todo list"""
        await ctx.send_help(ctx.command)

    @todo.command(name='create', with_app_command=False, aliases=['add'])
    async def todo_add(self, ctx: Context, *, content: Optional[str] = None) -> None:
        """Add a todo item. Can be used as a reply to another message."""

        reply = ctx.replied_message
        if reply is None and content is None:
            await ctx.send(
                "There's nothing to remind you of here. You can reply to a message to be reminded of a message or you can pass the text you want to reminded of"
            )
            return

        if content is not None and len(content) > 1024:
            await ctx.send('The todo content is too long. The maximum length is 1024 characters.')
            return

        item = await self.add_todo(user_id=ctx.author.id, content=content, message=reply)
        view = discord.ui.View()
        view.add_item(EditDueDateButton(item))
        await ctx.send(content=f'<a:agreenTick:1011968947949666324> Added todo item {item.id}.', view=view, embed=item.embed)

    @todo.app_command.command(name='add')
    async def todo_add_slash(
        self,
        interaction: discord.Interaction,
        content: str,
        due_date: Optional[app_commands.Transform[datetime.datetime, time.TimeTransformer]] = None,
    ):
        """Adds a todo item

        Parameters
        -----------
        content: :class:`str`
            The content of the todo item
        due_date: Optional[:class:`datetime.datetime`]
            The due date (in UTC) of the todo item, e.g. 1h or tomorrow
        """

        await interaction.response.defer(ephemeral=True)
        item = await self.add_todo(user_id=interaction.user.id, content=content, due_date=due_date)
        if due_date is None:
            view = discord.ui.View()
            view.add_item(EditDueDateButton(item))
        else:
            view = discord.utils.MISSING

        await interaction.followup.send(
            content=f'<a:agreenTick:1011968947949666324> Added todo item {item.id}.',
            embed=item.embed,
            view=view,
            ephemeral=True,
        )

    async def todo_add_context_menu(self, interaction: discord.Interaction, message: discord.Message):
        # We have to make sure the following query takes <3s in order to meet the response window
        todos = await self.get_todo_for_message(interaction.user.id, message.id)
        if todos:
            todo = todos[0]
            for t in todos:
                t.message = message

            if len(todos) == 1:
                view = ShowTodo(todo)
                view.add_item(AddAnywayButton(self, message))
                msg = 'A todo was already found for you for this message, what would you like to do?'
            else:
                view = AmbiguousTodo(todos, message)
                msg = 'Multiple todo were found for this message, what would you like to do?'

            await interaction.response.send_message(
                msg,
                view=view,
                embed=todo.embed,
                ephemeral=True,
            )
        else:
            await interaction.response.send_modal(AddTodoModal(self, message))

    @todo.command(name='delete', aliases=['remove'])
    @app_commands.describe(id='The todo item ID')
    @app_commands.autocomplete(id=todo_id_autocomplete)  # type: ignore (dpy bug)
    async def todo_delete(self, ctx: Context, *, id: int):
        """Removes a todo by its ID"""

        query = 'DELETE FROM todo WHERE id = $1 AND user_id = $2'
        status = await self.bot.pool.execute(query, id, ctx.author.id)
        if status == 'DELETE 0':
            await ctx.send('Could not delete a todo item by this ID, are you sure it\'s yours?', ephemeral=True)
        else:
            await ctx.send('Successfully deleted todo', ephemeral=True)
            self.check_for_task_resync(id)

    @todo.command(name='clear')
    async def todo_clear(self, ctx: Context):
        """Clears all todos you've made"""

        await ctx.defer(ephemeral=True)

        todos = await self.get_todos(ctx.author.id)

        if len(todos) == 0:
            await ctx.send('You have no todos to clear', ephemeral=True)
            return

        confirm = await ctx.prompt(f'Are you sure you want to delete {plural(len(todos)):todo}?', delete_after=True)
        if not confirm:
            await ctx.send('Aborting', ephemeral=True, delete_after=15.0)
            return

        query = 'DELETE FROM todo WHERE user_id = $1'
        await self.bot.pool.execute(query, ctx.author.id)
        await ctx.send('Successfully cleared all todos', ephemeral=True)

        for todo in todos:
            if self.check_for_task_resync(todo.id):
                return

    @todo.command(name='list', aliases=['brief', 'compact'])
    async def todo_list(self, ctx: Context, *, flags: ListFlags):
        """List your todos

        This command uses a syntax similar to Discord's search bar.

        The following flags are valid.

        `overdue: yes` Include overdue todos, defaults to `yes`
        `completed: yes` Include completed todos, defaults to `no`
        `pending: yes` Include pending todos, defaults to `yes`
        `brief: yes` Show a brief summary of each todo, defaults to `no`
        """

        # default query: SELECT * FROM todo WHERE user_id = $1 AND completed_at IS NULL
        predicates = ['user_id = $1']
        if not flags.overdue:
            predicates.append('CURRENT_TIMESTAMP < due_date')
        if not flags.completed:
            predicates.append('completed_at IS NULL')
        if not flags.pending:
            predicates.append('completed_at IS NOT NULL')

        query = f'SELECT * FROM todo WHERE { " AND ".join(predicates)}'
        todos = await self.bot.pool.fetch(query, ctx.author.id)

        if len(todos) == 0:
            await ctx.send('No todos found!', ephemeral=True)

        todos = [TodoItem(self, record) for record in todos]
        if flags.brief or ctx.invoked_with in ('brief', 'compact'):
            pages = RoboPages(BriefTodoPageSource(todos), ctx=ctx, compact=True)
            await pages.start(ephemeral=flags.private)
        else:
            pages = TodoPages(todos, ctx=ctx)
            await pages.start(ephemeral=flags.private)

    @todo_list.error
    async def todo_list_error(self, ctx: Context, error: Exception):
        if isinstance(error, commands.FlagError):
            msg = (
                "There were some problems with the flags you passed in. Please note that only the following flags work:\n"
                "`completed`, `pending`, `overdue`, and `brief`\n\n"
                "Flags only accept 'yes', 'no', or 'true' or 'false' as values. For example, `brief: yes`"
            )
            await ctx.send(msg, ephemeral=True)

    @todo.command(name='show')
    @app_commands.describe(id='The todo item ID')
    @app_commands.autocomplete(id=todo_id_autocomplete)  # type: ignore (dpy bug)
    async def todo_show(self, ctx: Context, *, id: int):
        """Shows information about a todo by its ID."""

        query = 'SELECT * FROM todo WHERE id = $1 AND user_id = $2'
        record = await self.bot.pool.fetchrow(query, id, ctx.author.id)
        if record is None:
            await ctx.send('Could not find a todo item by this ID, are you sure it\'s yours?', ephemeral=True)
            return

        item = TodoItem(self, record)
        view = ShowTodo(item)
        await ctx.send(view=view, embed=item.embed, ephemeral=True)


async def setup(bot: RoboDanny):
    await bot.add_cog(Todo(bot))
