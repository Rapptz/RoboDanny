from __future__ import annotations
from typing import Optional

from dataclasses import dataclass
import enum
import discord
import random


class BoardKind(enum.Enum):
    Empty = 0
    X = -1
    O = 1

    def __str__(self) -> str:
        if self is self.X:
            return '\N{LARGE BLUE SQUARE}'
        if self is self.O:
            return '\N{LARGE GREEN SQUARE}'
        return '\u200b'

    @property
    def style(self) -> discord.ButtonStyle:
        if self is self.X:
            return discord.ButtonStyle.blurple
        if self is self.O:
            return discord.ButtonStyle.green
        return discord.ButtonStyle.grey


@dataclass()
class BoardState:
    strength: int
    kind: BoardKind

    @classmethod
    def empty(cls) -> BoardState:
        return BoardState(strength=0, kind=BoardKind.Empty)


@dataclass()
class Player:
    member: discord.abc.User
    kind: BoardKind
    pieces: set[int]
    current_selection: Optional[tuple[int, int]] = None

    @property
    def available_strength(self) -> int:
        return max(self.pieces)

    @property
    def content(self) -> str:
        return f'It is now {self.kind} {self.member.mention}\'s turn.'


class PlayerPromptButton(discord.ui.Button['PlayerPrompt']):
    def __init__(self, style: discord.ButtonStyle, number: int, disabled: bool, row: int) -> None:
        super().__init__(style=style, disabled=disabled, label=str(number), row=row)
        self.number: int = number

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        if self.view:
            self.view.selected_number = self.number
            self.view.stop()

        await interaction.delete_original_response()


class PlayerPrompt(discord.ui.View):
    def __init__(self, player: Player, state: BoardState):
        super().__init__(timeout=300.0)
        for x in range(0, 6):
            y = x // 3
            number = x + 1
            disabled = number not in player.pieces or number <= state.strength
            self.add_item(PlayerPromptButton(player.kind.style, number, disabled, row=y))
        self.player: Player = player
        self.selected_number: Optional[int] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.player.member:
            await interaction.response.send_message('This is not meant for you', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.red, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.delete_original_response()


class Button(discord.ui.Button['Gobblers']):
    def __init__(self, x: int, y: int):
        super().__init__(style=discord.ButtonStyle.grey, label='\u200b', row=y)
        self.x: int = x
        self.y: int = y

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        assert interaction.message is not None

        player = self.view.current_player
        state = self.view.get_board_state(self.x, self.y)
        if state.kind == player.kind:
            await interaction.response.send_message('You already have this piece', ephemeral=True)
            return

        if player.available_strength <= state.strength:
            await interaction.response.send_message(
                'You do not have the strength necessary to take down this piece', ephemeral=True
            )
            return

        if player.current_selection is not None:
            await interaction.response.send_message(
                "You've already selected a piece, you can't select multiple pieces.", ephemeral=True
            )
            return

        player.current_selection = (self.x, self.y)

        prompt = PlayerPrompt(player, state)
        await interaction.response.send_message('Select a piece strength', view=prompt)
        await prompt.wait()

        player.current_selection = None
        if prompt.selected_number is None:
            return

        state.strength = prompt.selected_number
        state.kind = player.kind
        self.label = str(state.strength)
        self.style = state.kind.style
        player.pieces.discard(prompt.selected_number)
        next_player = self.view.swap_player()
        content = next_player.content

        winner = self.view.get_winner()
        if winner is not None:
            if winner is not BoardKind.Empty:
                winning_player = next_player if next_player.kind is winner else player
                content = f'{winner} {winning_player.member.mention} won!'
            else:
                content = "It's a tie!"

            self.view.disable_all()
            self.view.stop()

        await interaction.message.edit(content=content, view=self.view)


class Gobblers(discord.ui.View):
    children: list[Button]

    def __init__(self, players: tuple[Player, ...]) -> None:
        super().__init__(timeout=36000.0)
        self.players: tuple[Player, ...] = players
        self.current_player_index: int = 0
        self.board: list[list[BoardState]] = [
            [BoardState.empty(), BoardState.empty(), BoardState.empty()],
            [BoardState.empty(), BoardState.empty(), BoardState.empty()],
            [BoardState.empty(), BoardState.empty(), BoardState.empty()],
        ]

        for x in range(3):
            for y in range(3):
                self.add_item(Button(x, y))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user not in (self.players[0].member, self.players[1].member):
            await interaction.response.send_message('This is a game between two other people, sorry.', ephemeral=True)
            return False

        if interaction.user != self.current_player.member:
            await interaction.response.send_message("It's not your turn", ephemeral=True)
            return False

        return True

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def get_winner(self) -> Optional[BoardKind]:
        # Check across (i.e. horizontal)
        for across in self.board:
            value = sum(p.kind.value for p in across)
            if value == 3:
                return BoardKind.O
            elif value == -3:
                return BoardKind.X

        # Check vertical
        for line in range(3):
            value = self.board[0][line].kind.value + self.board[1][line].kind.value + self.board[2][line].kind.value
            if value == 3:
                return BoardKind.O
            elif value == -3:
                return BoardKind.X

        # Check diagonals
        diag = self.board[0][2].kind.value + self.board[1][1].kind.value + self.board[2][0].kind.value
        if diag == 3:
            return BoardKind.O
        elif diag == -3:
            return BoardKind.X

        diag = self.board[0][0].kind.value + self.board[1][1].kind.value + self.board[2][2].kind.value
        if diag == 3:
            return BoardKind.O
        elif diag == -3:
            return BoardKind.X

        # If we're here, we need to check if a tie was made
        if all(i.kind is not BoardKind.Empty for row in self.board for i in row):
            return BoardKind.Empty

        return None

    def get_board_state(self, x: int, y: int) -> BoardState:
        return self.board[y][x]

    @property
    def current_player(self) -> Player:
        return self.players[self.current_player_index]

    def swap_player(self) -> Player:
        self.current_player_index = not self.current_player_index
        return self.players[self.current_player_index]


class Prompt(discord.ui.View):
    def __init__(self, first: discord.abc.User, second: discord.abc.User):
        super().__init__(timeout=180.0)
        self.first: discord.abc.User = first
        self.second: discord.abc.User = second
        self.confirmed: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.second:
            await interaction.response.send_message('This prompt is not meant for you', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        coin = random.randint(0, 1)
        if coin == 0:
            order = (self.first, self.second)
        else:
            order = (self.second, self.first)

        players = (
            Player(member=order[0], kind=BoardKind.X, pieces={1, 2, 3, 4, 5, 6}),
            Player(member=order[1], kind=BoardKind.O, pieces={1, 2, 3, 4, 5, 6}),
        )

        await interaction.response.send_message(
            f'Challenge accepted! {order[0].mention} goes first and {order[1].mention} goes second.\n\n'
            f"It is now \N{LARGE BLUE SQUARE} {order[0].mention}'s turn",
            view=Gobblers(players),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.confirmed = True
        self.stop()

    @discord.ui.button(label='Decline', style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Challenge declined :(')
        self.stop()
