from __future__ import annotations
from typing import Optional

from dataclasses import dataclass
import discord
import random


@dataclass()
class Cell:
    emoji: Optional[str]
    # True -> hit, False -> miss, None -> inactive
    # Applies to both
    # enemy_state is how this cell has been interacted with an enemy hit
    # bomb_state is how this cell has been interacted with your hit
    enemy_state: Optional[bool]
    bomb_state: Optional[bool]
    button: Optional[Button] = None

    @property
    def ship(self) -> bool:
        return self.emoji is not None

    @classmethod
    def empty(cls) -> Cell:
        return cls(emoji=None, enemy_state=None, bomb_state=None)

    @property
    def display_emoji(self) -> Optional[str]:
        if self.enemy_state is None:
            return self.emoji
        if self.enemy_state:
            return '\N{COLLISION SYMBOL}'
        return '\N{CYCLONE}'


class PlayerState:
    def __init__(self, member: discord.abc.User):
        self.member: discord.abc.User = member
        self.view: Optional[BoardView] = None
        self.ready: bool = False
        self.current_player: bool = False
        empty = Cell.empty
        self.board: list[list[Cell]] = [
            [empty(), empty(), empty(), empty(), empty()],
            [empty(), empty(), empty(), empty(), empty()],
            [empty(), empty(), empty(), empty(), empty()],
            [empty(), empty(), empty(), empty(), empty()],
            [empty(), empty(), empty(), empty(), empty()],
        ]
        # self.generate_board()

    def generate_board(self) -> None:
        for (size, emoji) in ((4, '\N{SHIP}'), (3, '\N{SAILBOAT}'), (2, '\N{CANOE}')):
            dx, dy = (1, 0) if random.randint(0, 1) else (0, 1)
            positions = self.get_available_positions(dx, dy, size)
            x, y = random.choice(positions)
            for _ in range(0, size):
                self.board[y][x].emoji = emoji
                x += dx
                y += dy

    def can_place_ship(self, x: int, y: int, dx: int, dy: int, size: int) -> bool:
        bounds = range(0, 5)
        for _ in range(0, size):
            if x not in bounds or y not in bounds:
                return False

            cell = self.board[y][x]
            if cell.ship:
                return False

            x += dx
            y += dy

        return True

    def get_available_positions(self, dx: int, dy: int, size: int) -> list[tuple[int, int]]:
        return [(x, y) for x in range(0, 5) for y in range(0, 5) if self.can_place_ship(x, y, dx, dy, size)]

    def is_dead(self) -> bool:
        for y in range(5):
            for x in range(5):
                cell = self.board[y][x]
                if cell.ship and not cell.enemy_state:
                    return False

        return True

    def is_ship_sunk(self, emoji: str) -> bool:
        for y in range(5):
            for x in range(5):
                cell = self.board[y][x]
                if cell.emoji == emoji and not cell.enemy_state:
                    return False
        return True


# Red button (disabled) -> Your bomb hit (bomb_state: True)
# Blue button (enabled) -> Potential hit (bomb_state: None)
# Blue button (disabled) -> Bomb missed (bomb_state: False)
# Ship emoji -> You have a ship (enemy_state: None)
# Bomb emoji -> Enemy hit that spot and missed (enemy_state: False)
# Boom emoji -> Enemy hit that spot and succeeded (enemy_state: True)


class Button(discord.ui.Button['BoardView']):
    def __init__(self, cell: Cell, x: int, y: int) -> None:
        super().__init__(
            label='\u200b',
            style=discord.ButtonStyle.red if cell.bomb_state else discord.ButtonStyle.blurple,
            disabled=cell.bomb_state is not None,
            emoji=cell.display_emoji,
            row=y,
        )
        self.x: int = x
        self.y: int = y
        self.cell: Cell = cell
        cell.button = self

    def update(self) -> None:
        self.style = discord.ButtonStyle.red if self.cell.bomb_state else discord.ButtonStyle.blurple
        self.disabled = self.cell.bomb_state is not None
        self.emoji = self.cell.display_emoji

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None

        enemy = self.view.enemy
        player = self.view.player
        enemy_cell = enemy.board[self.y][self.x]

        # Update our state
        self.cell.bomb_state = enemy_cell.ship
        enemy_cell.enemy_state = enemy_cell.ship
        self.update()

        # Swap players
        player.current_player = not player.current_player
        enemy.current_player = not enemy.current_player

        if enemy.is_dead():
            self.view.disable()
            await interaction.response.edit_message(content='You win!', view=self.view)
            # Update the enemy state as well
            if enemy_cell.button and enemy_cell.button.view:
                enemy_cell.button.update()
                view = enemy_cell.button.view
                await view.message.edit(content=f'You lose :(', view=view)

            await self.view.parent_message.edit(
                content=f'{player.member.mention} wins this game of Battleship! Congratulations.'
            )
            return

        content = f"{enemy.member.mention}'s turn."
        enemy_content = f'Your ({enemy.member.mention}) turn!'
        if enemy_cell.emoji is not None and enemy.is_ship_sunk(enemy_cell.emoji):
            content = f'{content}\n\nYou sunk their {enemy_cell.emoji}!'
            enemy_content = f'{enemy_content}\n\nYour {enemy_cell.emoji} was sunk :('

        await interaction.response.edit_message(content=content, view=self.view)
        self.view.message = await interaction.original_response()

        # Update the enemy state as well
        if enemy_cell.button and enemy_cell.button.view:
            enemy_cell.button.update()
            view = enemy_cell.button.view
            await view.message.edit(content=enemy_content, view=view)


class BoardView(discord.ui.View):
    message: discord.InteractionMessage
    parent_message: discord.Message
    children: list[Button]

    def __init__(self, player: PlayerState, enemy: PlayerState) -> None:
        super().__init__(timeout=None)
        self.player: PlayerState = player
        self.enemy: PlayerState = enemy

        for x in range(5):
            for y in range(5):
                self.add_item(Button(self.player.board[y][x], x, y))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.enemy.ready:
            await interaction.response.send_message(
                'Your enemy is not ready yet, please wait until they are', ephemeral=True
            )
            return False

        if not self.player.current_player:
            await interaction.response.send_message('It is not your turn yet.', ephemeral=True)
            return False
        return True

    def disable(self) -> None:
        for button in self.children:
            button.disabled = True


class BoardSetupButton(discord.ui.Button['BoardSetupView']):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(label='\u200b', style=discord.ButtonStyle.blurple, row=y)
        self.x: int = x
        self.y: int = y

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None

        try:
            self.view.place_at(self.x, self.y)
        except RuntimeError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
        else:
            if self.view.is_done():
                await self.view.commit(interaction)
            else:
                await interaction.response.edit_message(view=self.view)


class BoardSetupView(discord.ui.View):
    children: list[BoardSetupButton]

    def __init__(self, player: PlayerState, enemy: PlayerState, parent_button: ReadyButton) -> None:
        super().__init__()
        self.player: PlayerState = player
        self.enemy: PlayerState = enemy
        self.parent_button: ReadyButton = parent_button
        assert parent_button.view
        self.parent_view: Prompt = parent_button.view
        self.last_location: Optional[tuple[int, int]] = None
        # A total list of placements for the board
        # This is a tuple of (x, y, emoji).
        # The total length must be equal to 9 (4 + 3 + 2)
        self.placements: list[tuple[int, int, str]] = []
        self.taken_lengths: set[int] = set()

        for y in range(5):
            for x in range(5):
                self.add_item(BoardSetupButton(x, y))

    def can_place_ship(self, x: int, y: int, dx: int, dy: int, size: int) -> bool:
        bounds = range(0, 5)
        for _ in range(0, size):
            if x not in bounds or y not in bounds:
                return False

            if any(a == x and b == y for a, b, _ in self.placements):
                return False

            x += dx
            y += dy

        return True

    async def commit(self, interaction: discord.Interaction) -> None:
        for x, y, emoji in self.placements:
            self.player.board[y][x].emoji = emoji

        self.player.ready = True

        board = BoardView(self.player, self.enemy)
        place = 'first' if self.player.current_player else 'second'
        content = f"Alright you're ready! This is your board. You go {place}! Please do not dismiss this message!"
        await interaction.response.edit_message(content=content, view=board)
        board.message = await interaction.original_response()
        board.parent_message = self.parent_view.message

        self.player.view = board
        self.parent_button.disabled = True
        self.parent_button.label = f'{self.player.member} is ready!'

        content = discord.utils.MISSING
        if self.parent_view.both_players_ready():
            self.parent_view.clear_items()
            self.parent_view.timeout = None
            self.parent_view.add_item(ReopenBoardButton())
            content = (
                f'Game currently in progress between {self.player.member.mention} and {self.enemy.member.mention}...\n\n'
                f'{CHEATSHEET_GUIDE}\n'
                'If you accidentally dismissed your board, press the button below to bring it back. '
                'Note that it invalidates your previous board'
            )

        await self.parent_view.message.edit(content=content, view=self.parent_view)

    def place_at(self, x: int, y: int):
        if self.last_location is None:
            self.last_location = (x, y)
            self.children[x + y * 5].emoji = '\N{CONSTRUCTION SIGN}'
        elif self.last_location == (x, y):
            self.last_location = None
            self.children[x + y * 5].emoji = None
        else:
            (old_x, old_y) = self.last_location
            # If both x and y inputs are different then we're trying a diagonal boat
            # This is forbidden
            if old_x != x and old_y != y:
                raise RuntimeError("Sorry, you can't have diagonal pieces")

            if old_x != x:
                size = abs(old_x - x) + 1
                dx, dy = (1, 0)
                start_x, start_y = min(old_x, x), y
            elif old_y != y:
                size = abs(old_y - y) + 1
                dx, dy = (0, 1)
                start_x, start_y = x, min(old_y, y)
            else:
                raise RuntimeError("Sorry, couldn't figure out what you wanted to do here")

            boats = {
                4: '\N{SHIP}',
                3: '\N{SAILBOAT}',
                2: '\N{CANOE}',
            }

            if size not in boats:
                raise RuntimeError('Sorry, this ship is too big. Only ships sizes 4, 3, or 2 are supported')

            if size in self.taken_lengths:
                raise RuntimeError(f'You already have a boat that is {size} units long.')

            if not self.can_place_ship(start_x, start_y, dx, dy, size):
                raise RuntimeError('This ship would be blocked off')

            emoji = boats[size]
            for _ in range(size):
                self.placements.append((start_x, start_y, emoji))
                button = self.children[start_x + start_y * 5]
                button.emoji = emoji
                button.disabled = True

                start_x += dx
                start_y += dy

            self.taken_lengths.add(size)
            self.last_location = None

    def is_done(self) -> bool:
        return len(self.placements) == 9


CHEATSHEET_GUIDE = """**Guide**
Red button → You hit the enemy ship successfully.
Disabled blue button → Your hit missed the enemy ship.
\N{CYCLONE} → The enemy's hit missed.
\N{COLLISION SYMBOL} → The enemy hit your ship.
"""


class ReopenBoardButton(discord.ui.Button['Prompt']):
    def __init__(self) -> None:
        super().__init__(label='Reopen Your Board', style=discord.ButtonStyle.blurple)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        view = self.view
        player = view.first if interaction.user.id == view.first.member.id else view.second
        enemy = view.second if interaction.user.id == view.first.member.id else view.first

        if player.view is not None:
            player.view.stop()

        board = BoardView(player, enemy)
        await interaction.response.send_message('This is your board!', view=board, ephemeral=True)
        player.view = board
        board.message = await interaction.original_response()
        board.parent_message = view.message


class ReadyButton(discord.ui.Button['Prompt']):
    def __init__(self, player: PlayerState, enemy: PlayerState) -> None:
        super().__init__(label=f"{player.member.display_name}'s Button", style=discord.ButtonStyle.blurple)
        self.player: PlayerState = player
        self.enemy: PlayerState = enemy

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        assert interaction.message is not None

        if interaction.user.id != self.player.member.id:
            await interaction.response.send_message('This ready button is not for you, sorry', ephemeral=True)
            return

        setup = BoardSetupView(self.player, self.enemy, self)
        content = (
            'Set up your board below. In order to set up your board, '
            'just press 2 points and a ship will automatically be created for you. You cannot have diagonal boats.\n\n'
            'There are 3 boats, \N{SHIP}, \N{SAILBOAT}, and \N{CANOE}. You can only have one of each. '
            'They have the following lengths:\n'
            '\N{SHIP} → 4\n\N{SAILBOAT} → 3\n\N{CANOE} → 2\n\n'
            'You can press a button again to undo an in-progress placement. '
            'You cannot move boats once they have been placed. '
            'When you finish setting up all boats you will be ready to play!'
        )
        await interaction.response.send_message(content, view=setup, ephemeral=True)


class Prompt(discord.ui.View):
    message: discord.Message
    children: list[discord.ui.Button]

    def __init__(self, first: discord.abc.User, second: discord.abc.User):
        super().__init__(timeout=300.0)
        self.first: PlayerState = PlayerState(first)
        self.second: PlayerState = PlayerState(second)

        current_player_id = random.choice([first, second]).id
        if current_player_id == first.id:
            self.first.current_player = True
        else:
            self.second.current_player = True

        self.add_item(ReadyButton(self.first, self.second))
        self.add_item(ReadyButton(self.second, self.first))

    def disable(self) -> None:
        for button in self.children:
            button.disabled = True

    async def on_timeout(self) -> None:
        self.disable()
        await self.message.edit(content='This prompt has timed out...', view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.first.member.id, self.second.member.id):
            await interaction.response.send_message('This prompt is not for you', ephemeral=True)
            return False
        return True

    def both_players_ready(self) -> bool:
        return self.first.ready and self.second.ready
