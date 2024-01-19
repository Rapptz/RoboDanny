from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Literal, MutableMapping, Optional, List, Union, Generic, TypeVar
from typing_extensions import Annotated

from discord.ext import commands, tasks
from discord import app_commands
from .utils import checks, time, cache, flags
from .utils.paginator import SimplePages
from .utils.formats import plural, human_join
from .utils.converters import Snowflake
from collections import Counter, defaultdict
from collections.abc import Hashable, Sequence
from lru import LRU

import re
import enum
import discord
import datetime
import asyncio
import argparse, shlex
import logging
import asyncpg
import io

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import GuildContext
    from cogs.reminder import Timer

    class ModGuildContext(GuildContext):
        cog: Mod
        guild_config: ModConfig


HashableT = TypeVar('HashableT', bound=Hashable)
K = TypeVar('K')
V = TypeVar('V')

log = logging.getLogger(__name__)

## Misc utilities


class Arguments(argparse.ArgumentParser):
    def error(self, message: str):
        raise RuntimeError(message)


class AutoModFlags(flags.BaseFlags):
    @flags.flag_value
    def joins(self) -> int:
        """Whether the server is broadcasting joins"""
        return 1

    @flags.flag_value
    def raid(self) -> int:
        """Whether the server is autobanning spammers"""
        return 2


## Configuration


class ModConfig:
    __slots__ = (
        'automod_flags',
        'id',
        'bot',
        'broadcast_channel_id',
        'broadcast_webhook_url',
        'mention_count',
        'safe_automod_entity_ids',
        'mute_role_id',
        'muted_members',
        '_cs_broadcast_webhook',
    )

    bot: RoboDanny
    automod_flags: AutoModFlags
    id: int
    broadcast_channel_id: Optional[int]
    broadcast_webhook_url: Optional[str]
    mention_count: Optional[int]
    safe_automod_entity_ids: set[int]
    muted_members: set[int]
    mute_role_id: Optional[int]

    @classmethod
    def from_record(cls, record: Any, bot: RoboDanny):
        self = cls()

        # the basic configuration
        self.bot = bot
        self.automod_flags = AutoModFlags(record['automod_flags'] or 0)
        self.id = record['id']
        self.broadcast_channel_id = record['broadcast_channel']
        self.broadcast_webhook_url = record['broadcast_webhook_url']
        self.mention_count = record['mention_count']
        self.safe_automod_entity_ids = set(record['safe_automod_entity_ids'] or [])
        self.muted_members = set(record['muted_members'] or [])
        self.mute_role_id = record['mute_role_id']
        return self

    @property
    def broadcast_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.broadcast_channel_id)  # type: ignore

    @property
    def requires_migration(self) -> bool:
        return self.broadcast_channel_id is not None and self.broadcast_webhook_url is None

    @discord.utils.cached_slot_property('_cs_broadcast_webhook')
    def broadcast_webhook(self) -> Optional[discord.Webhook]:
        if self.broadcast_webhook_url is None:
            return None
        return discord.Webhook.from_url(self.broadcast_webhook_url, session=self.bot.session)

    @property
    def mute_role(self) -> Optional[discord.Role]:
        guild = self.bot.get_guild(self.id)
        return guild and self.mute_role_id and guild.get_role(self.mute_role_id)  # type: ignore

    def is_muted(self, member: discord.abc.Snowflake) -> bool:
        return member.id in self.muted_members

    async def apply_mute(self, member: discord.Member, reason: Optional[str]):
        if self.mute_role_id:
            await member.add_roles(discord.Object(id=self.mute_role_id), reason=reason)


## Views


class MigrateJoinLogView(discord.ui.View):
    def __init__(self, cog: Mod):
        super().__init__(timeout=None)
        self.cog: Mod = cog

    @discord.ui.button(label='Migrate', custom_id='migrate_robomod_join_logs', style=discord.ButtonStyle.green)
    async def migrate(self, interaction: discord.Interaction, button: discord.ui.Button):
        assert interaction.message is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self.cog.migrate_automod_broadcast(interaction.user, interaction.channel, interaction.guild_id)  # type: ignore
        except RuntimeError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        else:
            await interaction.message.edit(content=None, view=None)
            await interaction.followup.send('Successfully migrated to new join logs!', ephemeral=True)


class PreExistingMuteRoleView(discord.ui.View):
    message: discord.Message

    def __init__(self, user: discord.abc.User):
        super().__init__(timeout=120.0)
        self.user: discord.abc.User = user
        self.merge: Optional[bool] = None

    async def on_timeout(self) -> None:
        try:
            await self.message.reply('Aborting.')
            await self.message.delete()
        except:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Sorry, these buttons aren't for you", ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Merge', style=discord.ButtonStyle.blurple)
    async def merge_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.merge = True

    @discord.ui.button(label='Replace', style=discord.ButtonStyle.grey)
    async def replace_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.merge = False

    @discord.ui.button(label='Quit', style=discord.ButtonStyle.red)
    async def abort_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message('Aborting', ephemeral=True)
        self.merge = None
        await self.message.delete()


class LockdownPermissionIssueView(discord.ui.View):
    message: discord.Message

    def __init__(self, me: discord.Member, channel: discord.abc.GuildChannel):
        super().__init__()
        self.channel: discord.abc.GuildChannel = channel
        self.me: discord.Member = me
        self.abort: bool = False

    async def on_timeout(self) -> None:
        self.abort = True
        try:
            await self.message.reply('Aborting.')
            await self.message.delete()
        except:
            pass

    @discord.ui.button(label='Resolve Permission Issue', style=discord.ButtonStyle.green)
    async def resolve_permissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        overwrites = self.channel.overwrites
        ow = overwrites.setdefault(self.me, discord.PermissionOverwrite())
        ow.send_messages = True
        ow.send_messages_in_threads = True

        try:
            await self.channel.set_permissions(self.me, overwrite=ow)
        except discord.HTTPException:
            await interaction.response.send_message(
                f'Could not successfully edit permissions, please give the bot Send Messages '
                f'and Send Messages in Threads in {self.channel.mention}'
            )
        else:
            await self.message.delete(delay=3)
            await interaction.response.send_message('Bot permissions have been updated... continuing', ephemeral=True)
        finally:
            self.stop()

    @discord.ui.button(label='Quit', style=discord.ButtonStyle.red)
    async def abort_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.abort = True
        await interaction.response.send_message(
            'Alright, feel free to edit the permissions yourself to give the bot Send Messages and Send Messages in Threads!'
        )
        self.stop()


## Converters


def can_execute_action(ctx: GuildContext, user: discord.Member, target: discord.Member) -> bool:
    return user.id == ctx.bot.owner_id or user == ctx.guild.owner or user.top_role > target.top_role


class MemberID(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                member_id = int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(f"{argument} is not a valid member or member ID.") from None
            else:
                m = await ctx.bot.get_or_fetch_member(ctx.guild, member_id)
                if m is None:
                    # hackban case
                    return type('_Hackban', (), {'id': member_id, '__str__': lambda s: f'Member ID {s.id}'})()

        if not can_execute_action(ctx, ctx.author, m):
            raise commands.BadArgument('You cannot do this action on this user due to role hierarchy.')
        return m


class BannedMember(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        if argument.isdigit():
            member_id = int(argument, base=10)
            try:
                return await ctx.guild.fetch_ban(discord.Object(id=member_id))
            except discord.NotFound:
                raise commands.BadArgument('This member has not been banned before.') from None

        entity = await discord.utils.find(lambda u: str(u.user) == argument, ctx.guild.bans(limit=None))

        if entity is None:
            raise commands.BadArgument('This member has not been banned before.')
        return entity


class ActionReason(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        ret = f'{ctx.author} (ID: {ctx.author.id}): {argument}'

        if len(ret) > 512:
            reason_max = 512 - len(ret) + len(argument)
            raise commands.BadArgument(f'Reason is too long ({len(argument)}/{reason_max})')
        return ret


IgnoreableEntity = Union[discord.TextChannel, discord.VoiceChannel, discord.Thread, discord.User, discord.Role]


class IgnoreEntity(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        # Hybrid commands (justifiably) does not support union types
        # since Discord doesn't support it, so we just need to run
        # the converter manually and that way the transformer is different
        assert ctx.current_parameter is not None
        return await commands.run_converters(ctx, IgnoreableEntity, argument, ctx.current_parameter)


def safe_reason_append(base: str, to_append: str) -> str:
    appended = f'{base} ({to_append})'
    if len(appended) > 512:
        return base
    return appended


class MassbanFlags(commands.FlagConverter):
    channel: Optional[Union[discord.TextChannel, discord.Thread, discord.VoiceChannel]] = commands.flag(
        description='The channel to search for message history', default=None
    )
    reason: Optional[str] = commands.flag(description='The reason to ban the members for', default=None)
    username: Optional[str] = commands.flag(description='The regex that usernames must match', default=None)
    created: Optional[int] = commands.flag(
        description='Matches users whose accounts were created less than specified minutes ago.', default=None
    )
    joined: Optional[int] = commands.flag(
        description='Matches users that joined less than specified minutes ago.', default=None
    )
    joined_before: Optional[discord.Member] = commands.flag(
        description='Matches users who joined before this member', default=None, name='joined-before'
    )
    joined_after: Optional[discord.Member] = commands.flag(
        description='Matches users who joined after this member', default=None, name='joined-after'
    )
    avatar: Optional[bool] = commands.flag(
        description='Matches users depending on whether they have avatars or not', default=None
    )
    roles: Optional[bool] = commands.flag(
        description='Matches users depending on whether they have roles or not', default=None
    )
    raid: bool = commands.flag(description='Matches users that are internally flagged as potential raiders', default=False)
    show: bool = commands.flag(description='Show members instead of banning them', default=False)

    # Message history related flags
    contains: Optional[str] = commands.flag(description='The substring to search for in the message.', default=None)
    starts: Optional[str] = commands.flag(description='The substring to search if the message starts with.', default=None)
    ends: Optional[str] = commands.flag(description='The substring to search if the message ends with.', default=None)
    match: Optional[str] = commands.flag(description='The regex to match the message content to.', default=None)
    search: commands.Range[int, 1, 2000] = commands.flag(description='How many messages to search for', default=100)
    after: Annotated[Optional[int], Snowflake] = commands.flag(
        description='Messages must come after this message ID.', default=None
    )
    before: Annotated[Optional[int], Snowflake] = commands.flag(
        description='Messages must come before this message ID.', default=None
    )
    files: Optional[bool] = commands.flag(description='Whether the message should have attachments.', default=None)
    embeds: Optional[bool] = commands.flag(description='Whether the message should have embeds.', default=None)


class PurgeFlags(commands.FlagConverter):
    user: Optional[discord.User] = commands.flag(description="Remove messages from this user", default=None)
    contains: Optional[str] = commands.flag(
        description='Remove messages that contains this string (case sensitive)', default=None
    )
    prefix: Optional[str] = commands.flag(
        description='Remove messages that start with this string (case sensitive)', default=None
    )
    suffix: Optional[str] = commands.flag(
        description='Remove messages that end with this string (case sensitive)', default=None
    )
    after: Annotated[Optional[int], Snowflake] = commands.flag(
        description='Search for messages that come after this message ID', default=None
    )
    before: Annotated[Optional[int], Snowflake] = commands.flag(
        description='Search for messages that come before this message ID', default=None
    )
    bot: bool = commands.flag(description='Remove messages from bots (not webhooks!)', default=False)
    webhooks: bool = commands.flag(description='Remove messages from webhooks', default=False)
    embeds: bool = commands.flag(description='Remove messages that have embeds', default=False)
    files: bool = commands.flag(description='Remove messages that have attachments', default=False)
    emoji: bool = commands.flag(description='Remove messages that have custom emoji', default=False)
    reactions: bool = commands.flag(description='Remove messages that have reactions', default=False)
    require: Literal['any', 'all'] = commands.flag(
        description='Whether any or all of the flags should be met before deleting messages. Defaults to "all"',
        default='all',
    )


## Spam detector


class FlaggedMember:
    __slots__ = ('id', 'joined_at', 'display_name', 'messages')

    def __init__(self, user: discord.abc.User, joined_at: datetime.datetime):
        self.id = user.id
        self.display_name = str(user)
        self.joined_at = joined_at
        self.messages: int = 0

    @property
    def created_at(self) -> datetime.datetime:
        return discord.utils.snowflake_time(self.id)

    def __str__(self) -> str:
        return self.display_name


class SpamCheckerResult:
    def __init__(self, reason: str) -> None:
        self.reason: str = reason

    def __str__(self) -> str:
        return self.reason

    @classmethod
    def spammer(cls) -> SpamCheckerResult:
        return cls('Auto-ban for spamming')

    @classmethod
    def flagged_mention(cls) -> SpamCheckerResult:
        return cls('Auto-ban for suspicious mentions')


class MultipleSpammers(SpamCheckerResult):
    def __init__(self, members: Sequence[discord.abc.Snowflake], *, reason: str = 'Auto-ban for spamming') -> None:
        super().__init__(reason)
        self.members: Sequence[discord.abc.Snowflake] = members


class RateLimit(Generic[V]):
    def __init__(self, rate: int, per: float, *, key: Callable[[discord.Message], V], maxsize: int = 256) -> None:
        self.lookup = LRU(maxsize)
        self.rate = rate
        self.per = per
        self.key = key

    @property
    def ratio(self) -> float:
        return self.per / self.rate

    def is_ratelimited(self, message: discord.Message) -> bool:
        now = message.created_at
        key = self.key(message)
        tat = max(self.lookup.get(key) or now, now)
        diff = (tat - now).total_seconds()
        max_interval = self.per - self.ratio
        if diff > max_interval:
            return True

        new_tat = max(tat, now) + datetime.timedelta(seconds=self.ratio)
        self.lookup[key] = new_tat
        return False


class TaggedRateLimit(Generic[V, HashableT]):
    def __init__(
        self,
        rate: int,
        per: float,
        *,
        key: Callable[[discord.Message], V],
        tagger: Callable[[discord.Message], HashableT],
        maxsize: int = 256,
    ) -> None:
        self.lookup: MutableMapping[V, tuple[datetime.datetime, set[HashableT]]] = LRU(maxsize)
        self.rate = rate
        self.per = per
        self.key = key
        self.tagger = tagger

    @property
    def ratio(self) -> float:
        return self.per / self.rate

    def is_ratelimited(self, message: discord.Message) -> Optional[list[HashableT]]:
        now = message.created_at
        key = self.key(message)
        value = self.lookup.get(key)
        if value is None:
            tat = now
            tagged = set()
        else:
            tat = max(value[0], now)
            tagged = value[1]

            # Clear tagged members that were there from the previous window
            # Honestly, unsure how this works but from testing it works as I expect
            if value[0] < now:
                tagged.clear()

        tag = self.tagger(message)
        tagged.add(tag)

        diff = (tat - now).total_seconds()
        max_interval = self.per - self.ratio
        if diff > max_interval:
            copy = list(tagged)
            tagged.clear()
            return copy

        new_tat = max(tat, now) + datetime.timedelta(seconds=self.ratio)
        self.lookup[key] = (new_tat, tagged)
        return None


class MemberJoinType(enum.Enum):
    fast = 1
    suspicious = 2


class SpamChecker:
    """This spam checker does a few things.

    1) It checks if a user has spammed more than 10 times in 12 seconds
    2) It checks if the content has been spammed 5 times in 10 seconds.
    3) It checks if new users have spammed 30 times in 35 seconds.
    4) It checks if "fast joiners" have spammed 5 times in 7 seconds.
    5) It checks if a member spammed `config.mention_count` mentions in 15 seconds.

    The second case is meant to catch alternating spam bots while the first one
    just catches regular singular spam bots.

    From experience these values aren't reached unless someone is actively spamming.
    """

    def __init__(self):
        self.by_content = RateLimit(5, 15.0, key=lambda msg: (msg.channel.id, msg.content))
        self.by_user = RateLimit(10, 12.0, key=lambda msg: msg.author.id)
        self.last_join: Optional[datetime.datetime] = None
        self.last_member: Optional[discord.Member] = None
        self.new_user = RateLimit(30, 35.0, key=lambda msg: msg.channel.id)
        self._by_mentions: Optional[commands.CooldownMapping] = None
        self._by_mentions_rate: Optional[int] = None

        # user_id flag mapping (for about 45 minutes)
        self.flagged_users: MutableMapping[int, FlaggedMember] = cache.ExpiringCache(seconds=2700.0)
        self.hit_and_run = TaggedRateLimit(5, 15, key=lambda msg: msg.channel.id, tagger=lambda msg: msg.author)

    def get_flagged_member(self, user_id: int, /) -> Optional[FlaggedMember]:
        return self.flagged_users.get(user_id)

    def flag_member(self, member: discord.Member, /) -> None:
        self.flagged_users[member.id] = FlaggedMember(member, member.joined_at or discord.utils.utcnow())

    def by_mentions(self, config: ModConfig) -> Optional[commands.CooldownMapping]:
        if not config.mention_count:
            return None

        mention_threshold = config.mention_count
        if self._by_mentions_rate != mention_threshold:
            self._by_mentions = commands.CooldownMapping.from_cooldown(mention_threshold, 15, commands.BucketType.member)
            self._by_mentions_rate = mention_threshold
        return self._by_mentions

    def is_new(self, member: discord.Member) -> bool:
        now = discord.utils.utcnow()
        seven_days_ago = now - datetime.timedelta(days=7)
        ninety_days_ago = now - datetime.timedelta(days=90)
        return member.created_at > ninety_days_ago and member.joined_at is not None and member.joined_at > seven_days_ago

    def is_spamming(self, message: discord.Message) -> Optional[SpamCheckerResult]:
        if message.guild is None:
            return None

        flagged = self.flagged_users.get(message.author.id)
        if flagged is not None:
            flagged.messages += 1
            spammers = self.hit_and_run.is_ratelimited(message)
            if spammers:
                return MultipleSpammers(spammers)

            # Special case for joining and just spamming mentions at some point
            if (
                flagged.messages <= 10
                and message.raw_mentions
                or '@everyone' in message.content
                or '@here' in message.content
            ):
                return SpamCheckerResult.flagged_mention()

        if self.is_new(message.author):  # type: ignore
            if self.new_user.is_ratelimited(message):
                return SpamCheckerResult.spammer()

        if self.by_user.is_ratelimited(message):
            return SpamCheckerResult.spammer()

        if self.by_content.is_ratelimited(message):
            return SpamCheckerResult.spammer()

        return None

    def get_join_type(self, member: discord.Member) -> Optional[MemberJoinType]:
        joined = member.joined_at or discord.utils.utcnow()

        if self.last_member is None:
            self.last_member = member
            self.last_join = joined
            return None

        # Check if the member is a fast joiner
        if self.last_join is not None:
            is_fast = (joined - self.last_join).total_seconds() <= 2.0
            self.last_join = joined
            if is_fast:
                self.flagged_users[member.id] = FlaggedMember(member, joined)
                if self.last_member.id not in self.flagged_users:
                    self.flag_member(self.last_member)
                self.last_member = member
                return MemberJoinType.fast

        # Check if the member is a suspicious joiner
        is_suspicious = abs((member.created_at - self.last_member.created_at).total_seconds()) <= 86400.0
        if is_suspicious:
            self.flagged_users[member.id] = FlaggedMember(member, joined)
            if self.last_member.id not in self.flagged_users:
                self.flag_member(self.last_member)
            self.last_member = member
            return MemberJoinType.suspicious

        self.last_member = member
        return None

    def is_mention_spam(self, message: discord.Message, config: ModConfig) -> bool:
        mapping = self.by_mentions(config)
        if mapping is None:
            return False

        current = message.created_at.timestamp()
        mention_bucket = mapping.get_bucket(message, current)
        mention_count = sum(not m.bot and m.id != message.author.id for m in message.mentions)
        return mention_bucket is not None and mention_bucket.update_rate_limit(current, tokens=mention_count) is not None

    def remove_member(self, user: discord.abc.User) -> None:
        self.flagged_users.pop(user.id, None)


## Checks


class NoMuteRole(commands.CommandError):
    def __init__(self):
        super().__init__('This server does not have a mute role set up.')


def can_mute():
    async def predicate(ctx: ModGuildContext) -> bool:
        is_owner = await ctx.bot.is_owner(ctx.author)
        if ctx.guild is None:
            return False

        if not ctx.author.guild_permissions.manage_roles and not is_owner:
            return False

        # This will only be used within this cog.
        ctx.guild_config = config = await ctx.cog.get_guild_config(ctx.guild.id)  # type: ignore
        role = config and config.mute_role
        if role is None:
            raise NoMuteRole()
        return ctx.author.top_role > role

    return commands.check(predicate)


## The actual cog


class Mod(commands.Cog):
    """Moderation related commands."""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot

        # guild_id: SpamChecker
        self._spam_check: defaultdict[int, SpamChecker] = defaultdict(SpamChecker)

        # guild_id: List[(member_id, insertion)]
        # A batch of data for bulk inserting mute role changes
        # True - insert, False - remove
        self._data_batch: defaultdict[int, list[tuple[int, Any]]] = defaultdict(list)
        self._batch_lock = asyncio.Lock()
        self._disable_lock = asyncio.Lock()
        self.batch_updates.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_updates.start()

        # (guild_id, channel_id): List[str]
        # A batch list of message content for message
        self.message_batches: defaultdict[tuple[int, int], list[str]] = defaultdict(list)
        self._batch_message_lock = asyncio.Lock()
        self.bulk_send_messages.start()

        self._automod_migration_view = MigrateJoinLogView(self)
        bot.add_view(self._automod_migration_view)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='DiscordCertifiedModerator', id=1055367895326130226)

    def __repr__(self) -> str:
        return '<cogs.Mod>'

    async def cog_load(self) -> None:
        self._avatar: bytes = await self.bot.user.display_avatar.read()

    def cog_unload(self) -> None:
        self.batch_updates.stop()
        self.bulk_send_messages.stop()
        self._automod_migration_view.stop()

    async def cog_command_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(
            error,
            (commands.BadArgument, commands.BotMissingPermissions, NoMuteRole, commands.UserInputError, commands.FlagError),
        ):
            await ctx.send(str(error))
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                await ctx.send('I do not have permission to execute this action.')
            elif isinstance(original, discord.NotFound):
                await ctx.send(f'This entity does not exist: {original.text}')
            elif isinstance(original, discord.HTTPException):
                await ctx.send('Somehow, an unexpected error occurred. Try again later?')

    async def bulk_insert(self):
        query = """UPDATE guild_mod_config
                   SET muted_members = x.result_array
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(guild_id BIGINT, result_array BIGINT[])
                   WHERE guild_mod_config.id = x.guild_id;
                """

        if not self._data_batch:
            return

        final_data = []
        for guild_id, data in self._data_batch.items():
            # If it's touched this function then chances are that this has hit cache before
            # so it's not actually doing a query, hopefully.
            config = await self.get_guild_config(guild_id)

            # Unsure what happened here, but this should be rare.
            if config is None:
                continue

            as_set = config.muted_members
            for member_id, insertion in data:
                func = as_set.add if insertion else as_set.discard
                func(member_id)

            final_data.append({'guild_id': guild_id, 'result_array': list(as_set)})
            self.get_guild_config.invalidate(self, guild_id)

        await self.bot.pool.execute(query, final_data)
        self._data_batch.clear()

    @tasks.loop(seconds=15.0)
    async def batch_updates(self):
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=10.0)
    async def bulk_send_messages(self):
        async with self._batch_message_lock:
            for (guild_id, channel_id), messages in self.message_batches.items():
                guild = self.bot.get_guild(guild_id)
                channel: Optional[discord.abc.Messageable] = guild and guild.get_channel(channel_id)  # type: ignore
                if channel is None:
                    continue

                paginator = commands.Paginator(suffix='', prefix='')
                for message in messages:
                    paginator.add_line(message)

                for page in paginator.pages:
                    try:
                        await channel.send(page)
                    except discord.HTTPException:
                        pass

            self.message_batches.clear()

    @cache.cache()
    async def get_guild_config(self, guild_id: int) -> Optional[ModConfig]:
        query = """SELECT * FROM guild_mod_config WHERE id=$1;"""
        async with self.bot.pool.acquire(timeout=300.0) as con:
            record = await con.fetchrow(query, guild_id)
            if record is not None:
                return ModConfig.from_record(record, self.bot)
            return None

    async def check_raid(
        self, config: ModConfig, guild: discord.Guild, member: discord.Member, message: discord.Message
    ) -> None:
        if not config.automod_flags.raid:
            return

        guild_id = guild.id
        checker = self._spam_check[guild_id]
        result = checker.is_spamming(message)
        if result is None:
            return

        if isinstance(result, MultipleSpammers):
            members = result.members
        else:
            members = [member]

        for user in members:
            try:
                await guild.ban(user, reason=result.reason)
            except discord.HTTPException:
                log.info('[RoboMod] Failed to ban %s (ID: %s) from server %s.', member, member.id, member.guild)
            else:
                log.info('[RoboMod] Banned %s (ID: %s) from server %s.', member, member.id, member.guild)

    async def ban_for_mention_spam(
        self,
        mention_count: int,
        guild_id: int,
        message: discord.Message,
        member: discord.Member,
        multiple: bool = False,
    ) -> None:
        if multiple:
            reason = f'Spamming mentions over multiple messages ({mention_count} mentions)'
        else:
            reason = f'Spamming mentions ({mention_count} mentions)'

        try:
            await member.ban(reason=reason)
        except Exception as e:
            log.info('[Mention Spam] Failed to ban member %s (ID: %s) in guild ID %s', member, member.id, guild_id)
        else:
            to_send = f'Banned {member} (ID: {member.id}) for spamming {mention_count} mentions.'
            async with self._batch_message_lock:
                self.message_batches[(guild_id, message.channel.id)].append(to_send)

            log.info('[Mention Spam] Member %s (ID: %s) has been banned from guild ID %s', member, member.id, guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        author = message.author
        if author.id in (self.bot.user.id, self.bot.owner_id):
            return

        if message.guild is None:
            return

        if not isinstance(author, discord.Member):
            return

        if author.bot:
            return

        # we're going to ignore members with manage messages
        if author.guild_permissions.manage_messages:
            return

        guild_id = message.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if message.channel.id in config.safe_automod_entity_ids:
            return

        if author.id in config.safe_automod_entity_ids:
            return

        if any(i in config.safe_automod_entity_ids for i in author._roles):
            return

        # check for raid mode stuff
        await self.check_raid(config, message.guild, author, message)

        if not config.mention_count:
            return

        checker = self._spam_check[guild_id]
        if checker.is_mention_spam(message, config):
            await self.ban_for_mention_spam(config.mention_count, guild_id, message, author, multiple=True)
            return

        # auto-ban tracking for mention spams begin here
        if len(message.mentions) <= 3:
            return

        # check if it meets the thresholds required
        mention_count = sum(not m.bot and m.id != author.id for m in message.mentions)
        if mention_count < config.mention_count:
            return

        await self.ban_for_mention_spam(mention_count, guild_id, message, author)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = member.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.is_muted(member):
            return await config.apply_mute(member, 'Member was previously muted.')

        if not config.automod_flags.joins:
            return

        now = discord.utils.utcnow()

        is_new = member.created_at > (now - datetime.timedelta(days=7))
        checker = self._spam_check[guild_id]

        # Do the broadcasted message to the channel
        title = 'Member Joined'
        flag = checker.get_join_type(member)
        if flag is MemberJoinType.fast:
            colour = 0xDD5F53  # red
            if is_new:
                title = 'Member Joined (Very New Member)'
        elif flag is MemberJoinType.suspicious:
            colour = 0xDDA453  # yellow
            title = 'Member Joined (Suspicious Member)'
        else:
            colour = 0x53DDA4  # green

            if is_new:
                checker.flag_member(member)
                colour = 0xDDA453  # yellow
                title = 'Member Joined (Very New Member)'

        e = discord.Embed(title=title, colour=colour)
        e.timestamp = now
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name='ID', value=member.id)
        assert member.joined_at is not None
        e.add_field(name='Joined', value=time.format_dt(member.joined_at, "F"))
        e.add_field(name='Created', value=time.format_relative(member.created_at), inline=False)

        if config.requires_migration:
            await self.suggest_automod_migration(config, e, guild_id)
            return

        if config.broadcast_webhook:
            try:
                await config.broadcast_webhook.send(embed=e)
            except (discord.Forbidden, discord.NotFound):
                async with self._disable_lock:
                    await self.disable_automod_broadcast(guild_id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        checker = self._spam_check.get(payload.guild_id)
        if checker is None:
            return

        checker.remove_member(payload.user)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Comparing roles in memory is faster than potentially fetching from
        # database, even if there's a cache layer
        if before.roles == after.roles:
            return

        guild_id = after.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.mute_role_id is None:
            return

        before_has = before.get_role(config.mute_role_id)
        after_has = after.get_role(config.mute_role_id)

        # No change in the mute role
        # both didn't have it or both did have it
        if before_has == after_has:
            return

        async with self._batch_lock:
            # If `after_has` is true, then it's an insertion operation
            # if it's false, then the role for removed
            self._data_batch[guild_id].append((after.id, after_has))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild_id = role.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role_id != role.id:
            return

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)

    @commands.command(aliases=['newmembers'])
    @commands.guild_only()
    async def newusers(self, ctx: GuildContext, *, count: int = 5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have
        joined.

        The count parameter can only be up to 25.
        """
        count = max(min(count, 25), 5)

        if not ctx.guild.chunked:
            members = await ctx.guild.chunk(cache=True)

        members = sorted(ctx.guild.members, key=lambda m: m.joined_at or ctx.guild.created_at, reverse=True)[:count]

        e = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            joined = member.joined_at or datetime.datetime(1970, 1, 1)
            body = f'Joined {time.format_relative(joined)}\nCreated {time.format_relative(member.created_at)}'
            e.add_field(name=f'{member} (ID: {member.id})', value=body, inline=False)

        await ctx.send(embed=e)

    async def suggest_automod_migration(self, config: ModConfig, embed: discord.Embed, guild_id: int) -> None:
        channel = config.broadcast_channel

        async with self._disable_lock:
            await self.disable_automod_broadcast(guild_id)

        if channel is None:
            return

        msg = (
            '**Notice**\n\n'
            'Join logs have been updated to use a webhook to prevent the bot from being '
            'heavily rate limited during join raids. As a result, **migration needs to be done '
            'in order for joins to start being broadcasted again**. Sorry for the inconvenience.\n\n'
            'For the migration to succeed, **the bot must have Manage Webhooks permission** both in '
            'the server *and* the channel.\n\n'
            'In order to migrate, **please press the button below**.'
        )

        try:
            await channel.send(embed=embed, content=msg, view=self._automod_migration_view)
        except discord.Forbidden:
            pass

    @commands.hybrid_group(aliases=['automod'], fallback='info')
    @checks.is_mod()
    async def robomod(self, ctx: GuildContext):
        """Show current RoboMod (automatic moderation) behaviour on the server.

        You must have Ban Members and Manage Messages permissions to use this
        command or its subcommands.
        """

        config = await self.get_guild_config(ctx.guild.id)
        if config is None:
            return await ctx.send('This server does not have RoboMod set up!')

        e = discord.Embed(title='RoboMod Information')
        if config.automod_flags.joins:
            channel = f'<#{config.broadcast_channel_id}>'
            if config.requires_migration:
                broadcast = (
                    f'{channel}\n\n\N{WARNING SIGN}\ufe0f '
                    'This server requires migration for this feature to continue working.\n'
                    f'Run "{ctx.prefix}robomod disable joins" followed by "{ctx.prefix}robomod join {channel}" '
                    'to ensure this feature continues working.'
                )
            else:
                broadcast = f'Enabled on {channel}'
        else:
            broadcast = 'Disabled'

        e.add_field(name='Join Logs', value=broadcast)
        e.add_field(name='Raid Protection', value='Enabled' if config.automod_flags.raid else 'Disabled')

        mention_spam = f'{config.mention_count} mentions' if config.mention_count else 'Disabled'
        e.add_field(name='Mention Spam Protection', value=mention_spam)

        if config.safe_automod_entity_ids:

            def resolve_entity_id(x: int):
                if ctx.guild.get_role(x):
                    return f'<@&{x}>'
                if ctx.guild.get_channel_or_thread(x):
                    return f'<#{x}>'
                return f'<@{x}>'

            if len(config.safe_automod_entity_ids) <= 5:
                ignored = '\n'.join(resolve_entity_id(c) for c in config.safe_automod_entity_ids)
            else:
                sliced = list(config.safe_automod_entity_ids)[:5]
                entities = '\n'.join(resolve_entity_id(c) for c in sliced)
                ignored = f'{entities}\n({len(config.safe_automod_entity_ids) - 5} more...)'
        else:
            ignored = 'Nothing'

        e.add_field(name='Ignored Entities', value=ignored, inline=False)
        await ctx.send(embed=e)

    @robomod.command(name='joins')
    @checks.is_mod()
    @app_commands.describe(
        channel='The channel to broadcast join messages to. The bot must be able to create webhooks in it.'
    )
    async def robomod_joins(self, ctx: GuildContext, *, channel: discord.TextChannel):
        """Enables join message logging in the given channel.

        The bot must have the ability to create webhooks in the given channel.
        """

        await ctx.defer()
        config = await self.get_guild_config(ctx.guild.id)
        if config and config.automod_flags.joins:
            await ctx.send(
                f'You already have join message logging enabled. To disable, use "{ctx.prefix}robomod disable joins"'
            )
            return

        channel_id = channel.id

        reason = f'{ctx.author} (ID: {ctx.author.id}) enabled RoboMod join logs'

        try:
            webhook = await channel.create_webhook(name='RoboMod Join Logs', avatar=self._avatar, reason=reason)
        except discord.Forbidden:
            await ctx.send(f'The bot does not have permissions to create webhooks in {channel.mention}.')
            return
        except discord.HTTPException:
            await ctx.send('An error occurred while creating the webhook. Note you can only have 10 webhooks per channel.')
            return

        query = """INSERT INTO guild_mod_config (id, automod_flags, broadcast_channel, broadcast_webhook_url)
                   VALUES ($1, $2, $3, $4) ON CONFLICT (id)
                   DO UPDATE SET
                        automod_flags = guild_mod_config.automod_flags | EXCLUDED.automod_flags,
                        broadcast_channel = EXCLUDED.broadcast_channel,
                        broadcast_webhook_url = EXCLUDED.broadcast_webhook_url;
                """

        flags = AutoModFlags()
        flags.joins = True
        await ctx.db.execute(query, ctx.guild.id, flags.value, channel_id, webhook.url)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Join logs enabled. Broadcasting join messages to <#{channel_id}>.')

    async def disable_automod_broadcast(self, guild_id: int):
        # Note: This is called when the webhook has been deleted
        query = """INSERT INTO guild_mod_config (id, automod_flags, broadcast_channel, broadcast_webhook_url)
                   VALUES ($1, 0, NULL, NULL) ON CONFLICT (id)
                   DO UPDATE SET
                        automod_flags = guild_mod_config.automod_flags & ~$2::SMALLINT,
                        broadcast_channel = NULL,
                        broadcast_webhook_url = NULL;
                """

        await self.bot.pool.execute(query, guild_id, AutoModFlags.joins.flag)
        self.get_guild_config.invalidate(self, guild_id)

    async def migrate_automod_broadcast(self, user: discord.abc.User, channel: discord.TextChannel, guild_id: int) -> None:
        reason = f'{user} (ID: {user.id}) migrated RoboMod join logs'

        config = await self.get_guild_config(guild_id)
        if config and config.broadcast_webhook_url is not None:
            # If someone's successfully migrated somehow, just return early
            # The message will hopefully edit.
            return

        try:
            webhook = await channel.create_webhook(name='RoboMod Join Logs', avatar=self._avatar, reason=reason)
        except discord.Forbidden:
            raise RuntimeError(f'The bot does not have permissions to create webhooks.') from None
        except discord.HTTPException:
            raise RuntimeError(
                'An error occurred while creating the webhook. Note you can only have 10 webhooks per channel.'
            ) from None

        query = "UPDATE guild_mod_config SET broadcast_webhook_url = $2 WHERE id = $1"
        await self.bot.pool.execute(query, guild_id, webhook.url)
        self.get_guild_config.invalidate(self, guild_id)

    @robomod.command(name='disable', aliases=['off'])
    @checks.is_mod()
    @app_commands.describe(protection='The protection to disable')
    @app_commands.choices(
        protection=[
            app_commands.Choice(name='Everything', value='all'),
            app_commands.Choice(name='Join logging', value='joins'),
            app_commands.Choice(name='Raid protection', value='raid'),
            app_commands.Choice(name='Mention spam protection', value='mentions'),
        ]
    )
    async def robomod_disable(self, ctx: GuildContext, *, protection: Literal['all', 'joins', 'raid', 'mentions'] = 'all'):
        """Disables RoboMod on the server.

        This can be one of these settings:

        - "all" to disable everything
        - "joins" to disable join logging
        - "raid" to disable raid protection
        - "mentions" to disable mention spam protection

        If not given then it defaults to "all".
        """

        if protection == 'all':
            updates = 'automod_flags = 0, mention_count = 0, broadcast_channel = NULL'
            message = 'RoboMod has been disabled.'
        elif protection == 'joins':
            updates = (
                f'automod_flags = guild_mod_config.automod_flags & ~{AutoModFlags.joins.flag}, broadcast_channel = NULL'
            )
            message = 'Join logs have been disabled.'
        elif protection == 'raid':
            updates = f'automod_flags = guild_mod_config.automod_flags & ~{AutoModFlags.raid.flag}'
            message = 'Raid protection has been disabled.'
        elif protection == 'mentions':
            updates = 'mention_count = NULL'
            message = 'Mention spam protection has been disabled'

        query = f'UPDATE guild_mod_config SET {updates} WHERE id=$1 RETURNING broadcast_webhook_url'

        guild_id = ctx.guild.id
        record: Optional[tuple[Optional[str]]] = await self.bot.pool.fetchrow(query, guild_id)
        self._spam_check.pop(guild_id, None)
        self.get_guild_config.invalidate(self, guild_id)
        if record is not None and record[0] is not None and protection in ('all', 'joins'):
            wh = discord.Webhook.from_url(record[0], session=self.bot.session)
            try:
                await wh.delete(reason=message)
            except discord.HTTPException:
                await ctx.send(f'{message} However the webhook could not be deleted for some reason.')
                return

        await ctx.send(message)

    @robomod.command(name='raid')
    @checks.is_mod()
    @app_commands.describe(enabled='Whether raid protection should be enabled or not, toggles if not given.')
    async def robomod_raid(self, ctx: GuildContext, enabled: Optional[bool] = None):
        """Toggles raid protection on the server.

        Raid protection automatically bans members that spam messages in your server.
        """

        perms = ctx.me.guild_permissions
        if not perms.ban_members:
            return await ctx.send('\N{NO ENTRY SIGN} I do not have permissions to ban members.')

        query = """INSERT INTO guild_mod_config (id, automod_flags)
                   VALUES ($1, $2) ON CONFLICT (id)
                   DO UPDATE SET
                        -- If we're toggling then we need to negate the previous result
                        automod_flags = CASE COALESCE($3, NOT (guild_mod_config.automod_flags & $2 = $2))
                                        WHEN TRUE THEN guild_mod_config.automod_flags | $2
                                        WHEN FALSE THEN guild_mod_config.automod_flags & ~$2
                                        END
                   RETURNING COALESCE($3, (automod_flags & $2 = $2));
                """

        row: Optional[tuple[bool]] = await ctx.db.fetchrow(query, ctx.guild.id, AutoModFlags.raid.flag, enabled)
        enabled = row and row[0]
        self.get_guild_config.invalidate(self, ctx.guild.id)
        fmt = 'enabled' if enabled else 'disabled'
        await ctx.send(f'Raid protection {fmt}.')

    @robomod.command(name='mentions')
    @commands.guild_only()
    @checks.is_mod()
    @app_commands.describe(count='The maximum amount of mentions before banning.')
    async def robomod_mentions(self, ctx: GuildContext, count: commands.Range[int, 3]):
        """Enables auto-banning accounts that spam more than "count" mentions.

        If a message contains `count` or more mentions then the
        bot will automatically attempt to auto-ban the member.
        The `count` must be greater than 3.

        This only applies for user mentions. Everyone or Role
        mentions are not included.
        """

        query = """INSERT INTO guild_mod_config (id, mention_count, safe_automod_entity_ids)
                   VALUES ($1, $2, '{}')
                   ON CONFLICT (id) DO UPDATE SET
                       mention_count = $2;
                """
        await ctx.db.execute(query, ctx.guild.id, count)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Mention spam protection threshold set to {count}.')

    @robomod_mentions.error
    async def robomod_mentions_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.RangeError):
            await ctx.send('\N{NO ENTRY SIGN} Mention spam protection threshold must be greater than three.')

    @robomod.command(name='ignore')
    @commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True)
    @app_commands.describe(entities='Space separated list of roles, members, or channels to ignore')
    async def robomod_ignore(
        self, ctx: GuildContext, entities: Annotated[List[IgnoreableEntity], commands.Greedy[IgnoreEntity]]
    ):
        """Specifies what roles, members, or channels ignore RoboMod auto-bans.

        To use this command you must have the Ban Members permission.
        """

        query = """UPDATE guild_mod_config
                   SET safe_automod_entity_ids =
                       ARRAY(SELECT DISTINCT * FROM unnest(COALESCE(safe_automod_entity_ids, '{}') || $2::bigint[]))
                   WHERE id = $1;
                """

        if len(entities) == 0:
            return await ctx.send('Missing entities to ignore.')

        ids = [c.id for c in entities]
        await ctx.db.execute(query, ctx.guild.id, ids)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(
            f'Updated ignore list to ignore {", ".join(c.mention for c in entities)}',
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @robomod.command(name='unignore')
    @commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True)
    @app_commands.describe(entities='Space separated list of roles, members, or channels to take off the ignore list')
    async def robomod_unignore(
        self, ctx: GuildContext, entities: Annotated[List[IgnoreableEntity], commands.Greedy[IgnoreEntity]]
    ):
        """Specifies what roles, members, or channels to take off the RoboMod ignore list.

        To use this command you must have the Ban Members permission.
        """

        if len(entities) == 0:
            return await ctx.send('Missing entities to unignore.')

        query = """UPDATE guild_mod_config
                   SET safe_automod_entity_ids =
                       ARRAY(SELECT element FROM unnest(safe_automod_entity_ids) AS element
                             WHERE NOT(element = ANY($2::bigint[])))
                   WHERE id = $1;
                """

        await ctx.db.execute(query, ctx.guild.id, [c.id for c in entities])
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(
            f'Updated ignore list to no longer ignore {", ".join(c.mention for c in entities)}',
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @robomod.command(name='ignored')
    @commands.guild_only()
    async def robomod_ignored(self, ctx: GuildContext):
        """Lists what channels, roles, and members are in the RoboMod ignore list"""

        config = await self.get_guild_config(ctx.guild.id)
        if config is None or not config.safe_automod_entity_ids:
            return await ctx.send('Nothing is ignored!')

        def resolve_entity_id(x: int, *, guild=ctx.guild):
            if guild.get_role(x):
                return f'<@&{x}>'
            if guild.get_channel_or_thread(x):
                return f'<#{x}>'
            return f'<@{x}>'

        entities = [resolve_entity_id(x) for x in config.safe_automod_entity_ids]
        pages = SimplePages(entities, ctx=ctx, per_page=20)
        await pages.start()

    async def _basic_cleanup_strategy(self, ctx: GuildContext, search: int):
        count = 0
        async for msg in ctx.history(limit=search, before=ctx.message):
            if msg.author == ctx.me and not (msg.mentions or msg.role_mentions):
                await msg.delete()
                count += 1
        return {'Bot': count}

    async def _complex_cleanup_strategy(self, ctx: GuildContext, search: int):
        prefixes = tuple(self.bot.get_guild_prefixes(ctx.guild))  # thanks startswith

        def check(m):
            return m.author == ctx.me or m.content.startswith(prefixes)

        deleted = await ctx.channel.purge(limit=search, check=check, before=ctx.message)
        return Counter(m.author.display_name for m in deleted)

    async def _regular_user_cleanup_strategy(self, ctx: GuildContext, search: int):
        prefixes = tuple(self.bot.get_guild_prefixes(ctx.guild))

        def check(m):
            return (m.author == ctx.me or m.content.startswith(prefixes)) and not (m.mentions or m.role_mentions)

        deleted = await ctx.channel.purge(limit=search, check=check, before=ctx.message)
        return Counter(m.author.display_name for m in deleted)

    @commands.command()
    async def cleanup(self, ctx: GuildContext, search: int = 100):
        """Cleans up the bot's messages from the channel.

        If a search number is specified, it searches that many messages to delete.
        If the bot has Manage Messages permissions then it will try to delete
        messages that look like they invoked the bot as well.

        After the cleanup is completed, the bot will send you a message with
        which people got their messages deleted and their count. This is useful
        to see which users are spammers.

        Members with Manage Messages can search up to 1000 messages.
        Members without can search up to 25 messages.
        """

        strategy = self._basic_cleanup_strategy
        is_mod = ctx.channel.permissions_for(ctx.author).manage_messages
        if ctx.channel.permissions_for(ctx.me).manage_messages:
            if is_mod:
                strategy = self._complex_cleanup_strategy
            else:
                strategy = self._regular_user_cleanup_strategy

        if is_mod:
            search = min(max(2, search), 1000)
        else:
            search = min(max(2, search), 25)

        spammers = await strategy(ctx, search)
        deleted = sum(spammers.values())
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'- **{author}**: {count}' for author, count in spammers)

        await ctx.send('\n'.join(messages), delete_after=10)

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def kick(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Kicks a member from the server.

        In order for this to work, the bot must have Kick Member permissions.

        To use this command you must have Kick Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.kick(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Bans a member from the server.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def multiban(
        self,
        ctx: GuildContext,
        members: Annotated[List[discord.abc.Snowflake], commands.Greedy[MemberID]],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Bans multiple members from the server.

        This only works through banning via ID.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        total_members = len(members)
        if total_members == 0:
            return await ctx.send('Missing members to ban.')

        confirm = await ctx.prompt(f'This will ban **{plural(total_members):member}**. Are you sure?')
        if not confirm:
            return await ctx.send('Aborting.')

        failed = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                failed += 1

        await ctx.send(f'Banned {total_members - failed}/{total_members} members.')

    @commands.hybrid_command(usage='[flags...]')
    @commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True)
    async def massban(self, ctx: GuildContext, *, args: MassbanFlags):
        """Mass bans multiple members from the server.

        This command uses a syntax similar to Discord's search bar. To use this command
        you and the bot must both have Ban Members permission. **Every option is optional.**

        Users are only banned **if and only if** all conditions are met.

        The following options are valid.

        `channel:` Channel to search for message history.
        `reason:` The reason for the ban.
        `regex:` Regex that usernames must match.
        `created:` Matches users whose accounts were created less than specified minutes ago.
        `joined:` Matches users that joined less than specified minutes ago.
        `joined-before:` Matches users who joined before the member ID given.
        `joined-after:` Matches users who joined after the member ID given.
        `avatar:` Matches users who have no avatar.
        `roles:` Matches users that have no role.
        `raid:` Matches users that are internally flagged as potential raiders.
        `show:` Show members instead of banning them.

        Message history filters (Requires `channel:`):

        `contains:` A substring to search for in the message.
        `starts:` A substring to search if the message starts with.
        `ends:` A substring to search if the message ends with.
        `match:` A regex to match the message content to.
        `search:` How many messages to search. Default 100. Max 2000.
        `after:` Messages must come after this message ID.
        `before:` Messages must come before this message ID.
        `files:` Checks if the message has attachments.
        `embeds:` Checks if the message has embeds.
        """

        await ctx.defer()
        author = ctx.author
        members = []

        if args.channel:
            before = discord.Object(id=args.before) if args.before else None
            after = discord.Object(id=args.after) if args.after else None
            predicates = []
            if args.contains:
                predicates.append(lambda m: args.contains in m.content)
            if args.starts:
                predicates.append(lambda m: m.content.startswith(args.starts))
            if args.ends:
                predicates.append(lambda m: m.content.endswith(args.ends))
            if args.match:
                try:
                    _match = re.compile(args.match)
                except re.error as e:
                    return await ctx.send(f'Invalid regex passed to `match:` flag: {e}')
                else:
                    predicates.append(lambda m, x=_match: x.match(m.content))
            if args.embeds:
                predicates.append(args.embeds)
            if args.files:
                predicates.append(args.files)

            async for message in args.channel.history(limit=args.search, before=before, after=after):
                if all(p(message) for p in predicates):
                    members.append(message.author)
        else:
            if ctx.guild.chunked:
                members = ctx.guild.members
            else:
                async with ctx.typing():
                    await ctx.guild.chunk(cache=True)
                members = ctx.guild.members

        # member filters
        predicates = [
            lambda m: isinstance(m, discord.Member) and can_execute_action(ctx, author, m),  # Only if applicable
            lambda m: not m.bot,  # No bots
            lambda m: m.discriminator != '0000',  # No deleted users
        ]

        if args.username:
            try:
                _regex = re.compile(args.username)
            except re.error as e:
                return await ctx.send(f'Invalid regex passed to `username:` flag: {e}')
            else:
                predicates.append(lambda m, x=_regex: x.match(m.name))

        if args.avatar is False:
            predicates.append(lambda m: m.avatar is None)
        if args.roles is False:
            predicates.append(lambda m: len(getattr(m, 'roles', [])) <= 1)

        now = discord.utils.utcnow()
        if args.created:

            def created(member, *, offset=now - datetime.timedelta(minutes=args.created)):
                return member.created_at > offset

            predicates.append(created)
        if args.joined:

            def joined(member, *, offset=now - datetime.timedelta(minutes=args.joined)):
                if isinstance(member, discord.User):
                    # If the member is a user then they left already
                    return True
                return member.joined_at and member.joined_at > offset

            predicates.append(joined)
        if args.joined_after:

            def joined_after(member, *, _other=args.joined_after):
                return member.joined_at and _other.joined_at and member.joined_at > _other.joined_at

            predicates.append(joined_after)
        if args.joined_before:

            def joined_before(member, *, _other=args.joined_before):
                return member.joined_at and _other.joined_at and member.joined_at < _other.joined_at

            predicates.append(joined_before)

        is_only_raid = args.raid and len(predicates) == 3
        if len(predicates) == 3 and not args.raid:
            return await ctx.send('Missing at least one filter to use')

        checker = self._spam_check[ctx.guild.id]
        if is_only_raid:
            members = checker.flagged_users
        else:
            members = {m.id: m for m in members if all(p(m) for p in predicates)}
            if args.raid:
                members.update(checker.flagged_users)

        if args.reason is None and args.raid:
            args.reason = await ActionReason().convert(ctx, 'Raid detected')

        if len(members) == 0:
            return await ctx.send('No members found matching criteria.')

        if args.show:
            members = sorted(members.values(), key=lambda m: m.joined_at or now)
            fmt = "\n".join(f'{m.id}\tJoined: {m.joined_at}\tCreated: {m.created_at}\t{m}' for m in members)
            content = f'Current Time: {discord.utils.utcnow()}\nTotal members: {len(members)}\n{fmt}'
            file = discord.File(io.BytesIO(content.encode('utf-8')), filename='members.txt')
            return await ctx.send(file=file)

        if args.reason is None:
            return await ctx.send('`reason:` flag is required.')
        else:
            reason = await ActionReason().convert(ctx, args.reason)

        confirm = await ctx.prompt(f'This will ban **{plural(len(members)):member}**. Are you sure?')
        if not confirm:
            return await ctx.send('Aborting.')

        count = 0
        total = len(members)
        for member in list(members.values()):
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await ctx.send(f'Banned {count}/{total}')

    @massban.error
    async def massban_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.FlagError):
            await ctx.send(str(error), ephemeral=True)

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def softban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Soft bans a member from the server.

        A softban is basically banning the member from the server but
        then unbanning the member as well. This allows you to essentially
        kick the member while removing their messages.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Kick Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.guild.unban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.BanEntry, BannedMember],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Unbans a member from the server.

        You can pass either the ID of the banned member or the Name#Discrim
        combination of the member. Typically the ID is easiest to use.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.unban(member.user, reason=reason)
        if member.reason:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}), previously banned for {member.reason}.')
        else:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}).')

    @commands.command(hidden=True)
    @commands.guild_only()
    @commands.is_owner()
    async def syncban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[str, ActionReason],
    ):
        """Bans a member from a few other servers."""

        guilds_to_ban: set[int] = {
            81384788765712384,  # Discord API
            336642139381301249,  # discord.py
            182325885867786241,  # R. Danny
            149998214810959872,  # Dannyware
        }

        if ctx.guild.id not in guilds_to_ban:
            confirm = await ctx.prompt('This guild is not in the sync list, are you sure you want to sync these bans?')
            if not confirm:
                return await ctx.send('Aborting.')

        await ctx.guild.ban(member, reason=reason)
        guilds_to_ban.discard(ctx.guild.id)

        reason = safe_reason_append(reason, 'synced ban')
        bans = 1
        for guild_id in guilds_to_ban:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue

            try:
                await guild.ban(member, reason=reason)
            except discord.HTTPException:
                continue
            else:
                bans += 1

        await ctx.send(f'Banned from {bans}/{len(guilds_to_ban) + 1} guilds.')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx: GuildContext,
        duration: time.FutureTime,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Temporarily bans a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC unless the timezone is
        specified using the "timezone set" command.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        until = f'until {time.format_dt(duration.dt, "F")}'
        heads_up_message = f'You have been banned from {ctx.guild.name} {until}. Reason: {reason}'

        try:
            await member.send(heads_up_message)  # type: ignore  # Guarded by AttributeError
        except (AttributeError, discord.HTTPException):
            # best attempt, oh well.
            pass

        reason = safe_reason_append(reason, until)
        await ctx.guild.ban(member, reason=reason)
        zone = await reminder.get_timezone(ctx.author.id)
        timer = await reminder.create_timer(
            duration.dt,
            'tempban',
            ctx.guild.id,
            ctx.author.id,
            member.id,
            created=ctx.message.created_at,
            timezone=zone or 'UTC',
        )
        await ctx.send(f'Banned {member} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempban_timer_complete(self, timer: Timer):
        guild_id, mod_id, member_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        moderator = await self.bot.get_or_fetch_member(guild, mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.fetch_user(mod_id)
            except:
                # request failed somehow
                moderator = f'Mod ID {mod_id}'
            else:
                moderator = f'{moderator} (ID: {mod_id})'
        else:
            moderator = f'{moderator} (ID: {mod_id})'

        reason = f'Automatic unban from timer made on {timer.created_at} by {moderator}.'
        await guild.unban(discord.Object(id=member_id), reason=reason)

    @commands.hybrid_command(aliases=['remove'], usage='[search] [flags...]')
    @commands.guild_only()
    @checks.hybrid_permissions_check(manage_messages=True)
    @app_commands.describe(search='How many messages to search for')
    async def purge(self, ctx: GuildContext, search: Optional[commands.Range[int, 1, 2000]] = None, *, flags: PurgeFlags):
        """Removes messages that meet a criteria.

        This command uses a syntax similar to Discord's search bar.
        The messages are only deleted if all options are met unless
        the `require:` flag is passed to override the behaviour.

        The following flags are valid.

        `user:` Remove messages from the given user.
        `contains:` Remove messages that contain a substring.
        `prefix:` Remove messages that start with a string.
        `suffix:` Remove messages that end with a string.
        `after:` Search for messages that come after this message ID.
        `before:` Search for messages that come before this message ID.
        `bot: yes` Remove messages from bots (not webhooks!)
        `webhooks: yes` Remove messages from webhooks
        `embeds: yes` Remove messages that have embeds
        `files: yes` Remove messages that have attachments
        `emoji: yes` Remove messages that have custom emoji
        `reactions: yes` Remove messages that have reactions
        `require: any or all` Whether any or all flags should be met before deleting messages.

        In order to use this command, you must have Manage Messages permissions.
        Note that the bot needs Manage Messages as well. These commands cannot
        be used in a private message.

        When the command is done doing its work, you will get a message
        detailing which users got removed and how many messages got removed.
        """

        predicates: list[Callable[[discord.Message], Any]] = []
        if flags.bot:
            if flags.webhooks:
                predicates.append(lambda m: m.author.bot)
            else:
                predicates.append(lambda m: (m.webhook_id is None or m.interaction is not None) and m.author.bot)
        elif flags.webhooks:
            predicates.append(lambda m: m.webhook_id is not None)

        if flags.embeds:
            predicates.append(lambda m: len(m.embeds))

        if flags.files:
            predicates.append(lambda m: len(m.attachments))

        if flags.reactions:
            predicates.append(lambda m: len(m.reactions))

        if flags.emoji:
            custom_emoji = re.compile(r'<a?:(\w+):(\d+)>')
            predicates.append(lambda m: custom_emoji.search(m.content))

        if flags.user:
            predicates.append(lambda m: m.author == flags.user)

        if flags.contains:
            predicates.append(lambda m: flags.contains in m.content)  # type: ignore

        if flags.prefix:
            predicates.append(lambda m: m.content.startswith(flags.prefix))  # type: ignore

        if flags.suffix:
            predicates.append(lambda m: m.content.endswith(flags.suffix))  # type: ignore

        require_prompt = False
        if not predicates:
            # If nothing is passed then default to `True` to emulate ?purge all behaviour
            require_prompt = True
            predicates.append(lambda m: True)

        op = all if flags.require == 'all' else any

        def predicate(m: discord.Message) -> bool:
            r = op(p(m) for p in predicates)
            return r

        if flags.after:
            if search is None:
                search = 2000

        if search is None:
            search = 100

        if require_prompt:
            confirm = await ctx.prompt(f'Are you sure you want to delete {plural(search):message}?', timeout=30)
            if not confirm:
                return await ctx.send('Aborting.')

        before = discord.Object(id=flags.before) if flags.before else None
        after = discord.Object(id=flags.after) if flags.after else None
        await ctx.defer()

        if before is None and ctx.interaction is not None:
            # If no before: is passed and we're in a slash command,
            # the deferred message will be deleted by purge and the followup will not show up.
            # To work around this, we need to get the deferred message's ID and avoid deleting it.
            before = await ctx.interaction.original_response()

        try:
            deleted = await ctx.channel.purge(limit=search, before=before, after=after, check=predicate)
        except discord.Forbidden as e:
            return await ctx.send('I do not have permissions to delete messages.')
        except discord.HTTPException as e:
            return await ctx.send(f'Error: {e} (try a smaller search?)')

        spammers = Counter(m.author.display_name for m in deleted)
        deleted = len(deleted)
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'**{name}**: {count}' for name, count in spammers)

        to_send = '\n'.join(messages)

        if len(to_send) > 2000:
            await ctx.send(f'Successfully removed {deleted} messages.', delete_after=10)
        else:
            await ctx.send(to_send, delete_after=10)

    @commands.command(name='clear-reactions', aliases=['clear_reactions'])
    @commands.guild_only()
    @checks.hybrid_permissions_check(manage_messages=True)
    async def clear_reactions(self, ctx: GuildContext, search: commands.Range[int, 1, 2000] = 100):
        """Removes all reactions from messages that have them.

        You must have Manage Messages to use this command.
        """

        total_reactions = 0
        async for message in ctx.history(limit=search, before=ctx.message):
            if len(message.reactions):
                total_reactions += sum(r.count for r in message.reactions)
                await message.clear_reactions()

        await ctx.send(f'Successfully removed {total_reactions} reactions.')

    # Mute related stuff

    async def update_mute_role(
        self, ctx: GuildContext, config: Optional[ModConfig], role: discord.Role, *, merge: bool = False
    ) -> None:
        guild = ctx.guild
        if config and merge:
            members = config.muted_members
            # If the roles are being merged then the old members should get the new role
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id}): Merging mute roles'
            async for member in self.bot.resolve_member_ids(guild, members):
                if not member._roles.has(role.id):
                    try:
                        await member.add_roles(role, reason=reason)
                    except discord.HTTPException:
                        pass
        else:
            members = set()

        members.update(map(lambda m: m.id, role.members))
        query = """INSERT INTO guild_mod_config (id, mute_role_id, muted_members)
                   VALUES ($1, $2, $3::bigint[]) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id,
                       muted_members = EXCLUDED.muted_members
                """
        await self.bot.pool.execute(query, guild.id, role.id, list(members))
        self.get_guild_config.invalidate(self, guild.id)

    @staticmethod
    async def update_mute_role_permissions(
        role: discord.Role, guild: discord.Guild, invoker: discord.abc.User
    ) -> tuple[int, int, int]:
        success = 0
        failure = 0
        skipped = 0
        reason = f'Action done by {invoker} (ID: {invoker.id})'
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.manage_roles:
                overwrite = channel.overwrites_for(role)
                overwrite.send_messages = False
                overwrite.add_reactions = False
                overwrite.use_application_commands = False
                overwrite.create_private_threads = False
                overwrite.create_public_threads = False
                overwrite.send_messages_in_threads = False
                try:
                    await channel.set_permissions(role, overwrite=overwrite, reason=reason)
                except discord.HTTPException:
                    failure += 1
                else:
                    success += 1
            else:
                skipped += 1
        return success, failure, skipped

    @commands.group(name='mute', invoke_without_command=True)
    @can_mute()
    async def _mute(
        self,
        ctx: ModGuildContext,
        members: commands.Greedy[discord.Member],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Mutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        assert ctx.guild_config.mute_role_id is not None
        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to mute.')

        failed = 0
        for member in members:
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Muted [{total - failed}/{total}]')

    @commands.command(name='unmute')
    @can_mute()
    async def _unmute(
        self,
        ctx: ModGuildContext,
        members: commands.Greedy[discord.Member],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Unmutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        assert ctx.guild_config.mute_role_id is not None
        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to unmute.')

        failed = 0
        for member in members:
            try:
                await member.remove_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Unmuted [{total - failed}/{total}]')

    @commands.command()
    @can_mute()
    async def tempmute(
        self,
        ctx: ModGuildContext,
        duration: time.FutureTime,
        member: discord.Member,
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Temporarily mutes a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC unless a timezone is specified
        using the "timezone set" command.

        This has the same permissions as the `mute` command.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        assert ctx.guild_config.mute_role_id is not None
        role_id = ctx.guild_config.mute_role_id
        await member.add_roles(discord.Object(id=role_id), reason=reason)
        zone = await reminder.get_timezone(ctx.author.id)
        timer = await reminder.create_timer(
            duration.dt,
            'tempmute',
            ctx.guild.id,
            ctx.author.id,
            member.id,
            role_id,
            created=ctx.message.created_at,
            timezone=zone or 'UTC',
        )
        await ctx.send(f'Muted {discord.utils.escape_mentions(str(member))} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempmute_timer_complete(self, timer):
        guild_id, mod_id, member_id, role_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        member = await self.bot.get_or_fetch_member(guild, member_id)
        if member is None or not member._roles.has(role_id):
            # They left or don't have the role any more so it has to be manually changed in the SQL
            # if applicable, of course
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))
            return

        if mod_id != member_id:
            moderator = await self.bot.get_or_fetch_member(guild, mod_id)
            if moderator is None:
                try:
                    moderator = await self.bot.fetch_user(mod_id)
                except:
                    # request failed somehow
                    moderator = f'Mod ID {mod_id}'
                else:
                    moderator = f'{moderator} (ID: {mod_id})'
            else:
                moderator = f'{moderator} (ID: {mod_id})'

            reason = f'Automatic unmute from timer made on {timer.created_at} by {moderator}.'
        else:
            reason = f'Expiring self-mute made on {timer.created_at} by {member}'

        try:
            await member.remove_roles(discord.Object(id=role_id), reason=reason)
        except discord.HTTPException:
            # if the request failed then just do it manually
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))

    @_mute.group(name='role', invoke_without_command=True)
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def _mute_role(self, ctx: GuildContext):
        """Shows configuration of the mute role.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is not None:
            members = config.muted_members.copy()  # type: ignore  # This is already narrowed
            members.update(map(lambda r: r.id, role.members))
            total = len(members)
            role = f'{role} (ID: {role.id})'
        else:
            total = 0
        await ctx.send(f'Role: {role}\nMembers Muted: {total}')

    @_mute_role.command(name='set')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    @commands.cooldown(1, 60.0, commands.BucketType.guild)
    async def mute_role_set(self, ctx: GuildContext, *, role: discord.Role):
        """Sets the mute role to a pre-existing role.

        This command can only be used once every minute.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        if role.is_default():
            return await ctx.send('Cannot use the @\u200beveryone role.')

        if role > ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send('This role is higher than your highest role.')

        if role > ctx.me.top_role:
            return await ctx.send('This role is higher than my highest role.')

        config = await self.get_guild_config(ctx.guild.id)
        has_pre_existing = config is not None and config.mute_role is not None
        merge: Optional[bool] = False

        if has_pre_existing:
            msg = (
                '\N{WARNING SIGN} **There seems to be a pre-existing mute role set up.**\n\n'
                'If you want to merge the pre-existing member data with the new member data press the Merge button.\n'
                'If you want to replace pre-existing member data with the new member data press the Replace button.\n\n'
                '**Note: Merging is __slow__. It will also add the role to every possible member that needs it.**'
            )

            view = PreExistingMuteRoleView(ctx.author)
            view.message = await ctx.send(msg, view=view)
            await view.wait()
            if view.merge is None:
                return
            merge = view.merge
        else:
            muted_members = len(role.members)
            if muted_members > 0:
                msg = f'Are you sure you want to make this the mute role? It has {plural(muted_members):member}.'
                confirm = await ctx.prompt(msg)
                if not confirm:
                    merge = None

        if merge is None:
            return await ctx.send('Aborting.')

        async with ctx.typing():
            await self.update_mute_role(ctx, config, role, merge=merge)
            escaped = discord.utils.escape_mentions(role.name)
            await ctx.send(
                f'Successfully set the {escaped} role as the mute role.\n\n'
                '**Note: Permission overwrites have not been changed.**'
            )

    @_mute_role.command(name='update', aliases=['sync'])
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_update(self, ctx: GuildContext):
        """Updates the permission overwrites of the mute role.

        This works by blocking the Send Messages and Add Reactions
        permission on every text channel that the bot can do.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is None:
            return await ctx.send('No mute role has been set up to update.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            total = success + failure + skipped
            await ctx.send(
                f'Attempted to update {total} channel permissions. '
                f'[Updated: {success}, Failed: {failure}, Skipped (no permissions): {skipped}]'
            )

    @_mute_role.command(name='create')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_create(self, ctx: GuildContext, *, name):
        """Creates a mute role with the given name.

        This also updates the channel overwrites accordingly
        if wanted.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is not None and config.mute_role is not None:
            return await ctx.send('A mute role already exists.')

        try:
            role = await ctx.guild.create_role(name=name, reason=f'Mute Role Created By {ctx.author} (ID: {ctx.author.id})')
        except discord.HTTPException as e:
            return await ctx.send(f'An error happened: {e}')

        query = """INSERT INTO guild_mod_config (id, mute_role_id)
                   VALUES ($1, $2) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id;
                """
        await ctx.db.execute(query, guild_id, role.id)
        self.get_guild_config.invalidate(self, guild_id)

        confirm = await ctx.prompt('Would you like to update the channel overwrites as well?')
        if not confirm:
            return await ctx.send('Mute role successfully created.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            await ctx.send(
                'Mute role successfully created. Overwrites: ' f'[Updated: {success}, Failed: {failure}, Skipped: {skipped}]'
            )

    @_mute_role.command(name='unbind')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_unbind(self, ctx: GuildContext):
        """Unbinds a mute role without deleting it.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role is None:
            return await ctx.send('No mute role has been set up.')

        muted_members = len(config.muted_members)
        if muted_members > 0:
            msg = f'Are you sure you want to unbind and unmute {plural(muted_members):member}?'
            confirm = await ctx.prompt(msg)
            if not confirm:
                return await ctx.send('Aborting.')

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)
        await ctx.send('Successfully unbound mute role.')

    @commands.command()
    @commands.guild_only()
    async def selfmute(self, ctx: GuildContext, *, duration: time.ShortTime):
        """Temporarily mutes yourself for the specified duration.

        The duration must be in a short time form, e.g. 4h. Can
        only mute yourself for a maximum of 24 hours and a minimum
        of 5 minutes.

        Do not ask a moderator to unmute you.
        """

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        config = await self.get_guild_config(ctx.guild.id)
        role_id = config and config.mute_role_id
        if role_id is None:
            raise NoMuteRole()

        if ctx.author._roles.has(role_id):
            return await ctx.send('Somehow you are already muted <:rooThink:596576798351949847>')

        created_at = ctx.message.created_at
        if duration.dt > (created_at + datetime.timedelta(days=1)):
            return await ctx.send('Duration is too long. Must be at most 24 hours.')

        if duration.dt < (created_at + datetime.timedelta(minutes=5)):
            return await ctx.send('Duration is too short. Must be at least 5 minutes.')

        delta = time.human_timedelta(duration.dt, source=created_at)
        warning = f'Are you sure you want to be muted for {delta}?\n**Do not ask the moderators to undo this!**'
        confirm = await ctx.prompt(warning)
        if not confirm:
            return await ctx.send('Aborting', delete_after=5.0)

        reason = f'Self-mute for {ctx.author} (ID: {ctx.author.id}) for {delta}'
        await ctx.author.add_roles(discord.Object(id=role_id), reason=reason)
        timer = await reminder.create_timer(
            duration.dt, 'tempmute', ctx.guild.id, ctx.author.id, ctx.author.id, role_id, created=created_at
        )

        await ctx.send(f'\N{OK HAND SIGN} Muted for {delta}. Be sure not to bother anyone about it.')

    @selfmute.error
    async def on_selfmute_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('Missing a duration to selfmute for.')

    async def get_lockdown_information(
        self, guild_id: int, channel_ids: Optional[list[int]] = None
    ) -> dict[int, discord.PermissionOverwrite]:
        rows: list[tuple[int, int, int]]
        if channel_ids is None:
            query = """SELECT channel_id, allow, deny FROM guild_lockdowns WHERE guild_id=$1;"""
            rows = await self.bot.pool.fetch(query, guild_id)
        else:
            query = """SELECT channel_id, allow, deny
                       FROM guild_lockdowns
                       WHERE guild_id=$1 AND channel_id = ANY($2::bigint[]);
                    """

            rows = await self.bot.pool.fetch(query, guild_id, channel_ids)

        return {
            channel_id: discord.PermissionOverwrite.from_pair(discord.Permissions(allow), discord.Permissions(deny))
            for channel_id, allow, deny in rows
        }

    async def start_lockdown(
        self, ctx: GuildContext, channels: list[discord.TextChannel | discord.VoiceChannel]
    ) -> tuple[list[discord.TextChannel | discord.VoiceChannel], list[discord.TextChannel | discord.VoiceChannel]]:
        guild_id = ctx.guild.id
        default_role = ctx.guild.default_role

        records = []
        success, failures = [], []
        reason = f'Lockdown request by {ctx.author} (ID: {ctx.author.id})'
        async with ctx.typing():
            for channel in channels:
                overwrite = channel.overwrites_for(default_role)
                allow, deny = overwrite.pair()

                overwrite.send_messages = False
                overwrite.connect = False
                overwrite.add_reactions = False
                overwrite.use_application_commands = False
                overwrite.create_private_threads = False
                overwrite.create_public_threads = False
                overwrite.send_messages_in_threads = False

                try:
                    await channel.set_permissions(default_role, overwrite=overwrite, reason=reason)
                except discord.HTTPException:
                    failures.append(channel)
                else:
                    success.append(channel)
                    records.append(
                        {
                            'guild_id': guild_id,
                            'channel_id': channel.id,
                            'allow': allow.value,
                            'deny': deny.value,
                        }
                    )

        query = """
            INSERT INTO guild_lockdowns(guild_id, channel_id, allow, deny)
            SELECT d.guild_id, d.channel_id, d.allow, d.deny
            FROM jsonb_to_recordset($1::jsonb) AS d(guild_id BIGINT, channel_id BIGINT, allow BIGINT, deny BIGINT)
            ON CONFLICT (guild_id, channel_id) DO NOTHING
        """
        await self.bot.pool.execute(query, records)
        return success, failures

    async def end_lockdown(
        self,
        guild: discord.Guild,
        *,
        channel_ids: Optional[list[int]] = None,
        reason: Optional[str] = None,
    ) -> list[discord.abc.GuildChannel]:
        get_channel = guild.get_channel
        http_fallback: Optional[dict[int, discord.abc.GuildChannel]] = None
        default_role = guild.default_role
        failures = []
        lockdowns = await self.get_lockdown_information(guild.id, channel_ids=channel_ids)
        for channel_id, permissions in lockdowns.items():
            channel = get_channel(channel_id)
            # If a channel isn't found, do an HTTP fallback instead of cache
            # This way we can ensure whether the channel is there or not without
            # making N invalid requests per deleted channel
            if channel is None:
                if http_fallback is None:
                    http_fallback = {c.id: c for c in await guild.fetch_channels()}
                    get_channel = http_fallback.get
                    channel = get_channel(channel_id)
                    if channel is None:
                        continue
                continue

            try:
                await channel.set_permissions(default_role, overwrite=permissions, reason=reason)
            except discord.HTTPException:
                failures.append(channel)

        return failures

    def is_potential_lockout(
        self, me: discord.Member, channel: Union[discord.Thread, discord.VoiceChannel, discord.TextChannel]
    ) -> bool:
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            if parent is None:
                # Better safe than sorry?
                return True

            overwrites = parent.overwrites
            for role in me.roles:
                ow = overwrites.get(role)
                if ow is None:
                    continue
                if ow.send_messages_in_threads:
                    return False
            return True

        overwrites = channel.overwrites
        for role in me.roles:
            ow = overwrites.get(role)
            if ow is None:
                continue
            if ow.send_messages:
                return False
        return True

    @commands.hybrid_group(fallback='start')
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True, manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    @commands.cooldown(1, 30.0, commands.BucketType.guild)
    @app_commands.describe(channels='A space separated list of text or voice channels to lock down')
    async def lockdown(self, ctx: GuildContext, channels: commands.Greedy[Union[discord.TextChannel, discord.VoiceChannel]]):
        """Locks down specific channels.

        A lockdown is done by forbidding users from communicating with the channels.
        This is implemented by blocking certain permissions for the default everyone
        role:

        - Send Messages
        - Add Reactions
        - Use Application Commands
        - Create Public Threads
        - Create Private Threads
        - Send Messages in Threads

        When the lockdown is over, the permissions are reverted into their previous
        state.

        To use this command you must have Manage Roles and Ban Members permissions.
        The bot must also have Manage Members permissions.
        """

        if not channels:
            return await ctx.send('Missing channels to lockdown')

        if ctx.channel in channels and self.is_potential_lockout(ctx.me, ctx.channel):
            parent = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
            if parent is None:
                await ctx.send(
                    'For some reason, I could not find an an appropriate channel to edit overwrites for.'
                    'Note that this lockdown will potentially lock the bot from sending messages. '
                    'Please explicitly give the bot permissions to send messages in threads and channels.'
                )
                return

            view = LockdownPermissionIssueView(ctx.me, parent)
            view.message = await ctx.send(
                '\N{WARNING SIGN} This will potentially lock the bot from sending messages.\n'
                'Would you like to resolve the permission issue?',
                view=view,
            )
            await view.wait()
            if view.abort:
                return

        success, failures = await self.start_lockdown(ctx, channels)
        if failures:
            await ctx.send(
                f'Successfully locked down {len(success)}/{len(failures)} channels.\n'
                f'Failed channels: {", ".join(c.mention for c in failures)}\n\n'
                f'Give the bot Manage Roles permissions in those channels and try again.'
            )
        else:
            await ctx.send(f'Successfully locked down {plural(len(success)):channel}')

    @lockdown.command(name='for')
    @commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True, manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    @commands.cooldown(1, 30.0, commands.BucketType.guild)
    @app_commands.describe(
        duration='A duration on how long to lock down for, e.g. 30m',
        channels='A space separated list of text or voice channels to lock down',
    )
    async def lockdown_for(
        self,
        ctx: GuildContext,
        duration: time.ShortTime,
        channels: commands.Greedy[Union[discord.TextChannel, discord.VoiceChannel]],
    ):
        """Locks down specific channels for a specified amount of time.

        A lockdown is done by forbidding users from communicating with the channels.
        This is implemented by blocking certain permissions for the default everyone
        role:

        - Send Messages
        - Add Reactions
        - Use Application Commands
        - Create Public Threads
        - Create Private Threads
        - Send Messages in Threads

        When the lockdown is over, the permissions are reverted into their previous
        state.

        To use this command you must have Manage Roles and Ban Members permissions.
        The bot must also have Manage Members permissions.
        """

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        if not channels:
            return await ctx.send('Missing channels to lockdown')

        if ctx.channel in channels and self.is_potential_lockout(ctx.me, ctx.channel):
            parent = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
            if parent is None:
                await ctx.send(
                    'For some reason, I could not find an an appropriate channel to edit overwrites for.'
                    'Note that this lockdown will potentially lock the bot from sending messages. '
                    'Please explicitly give the bot permissions to send messages in threads and channels.'
                )
                return

            view = LockdownPermissionIssueView(ctx.me, parent)
            view.message = await ctx.send(
                '\N{WARNING SIGN} This will potentially lock the bot from sending messages.\n'
                'Would you like to resolve the permission issue?',
                view=view,
            )
            await view.wait()
            if view.abort:
                return

        success, failures = await self.start_lockdown(ctx, channels)
        timer = await reminder.create_timer(
            duration.dt,
            'lockdown',
            ctx.guild.id,
            ctx.author.id,
            ctx.channel.id,
            [c.id for c in success],
            created=ctx.message.created_at,
        )
        long = duration.dt >= ctx.message.created_at + datetime.timedelta(days=1)
        formatted_time = discord.utils.format_dt(timer.expires, 'f' if long else 'T')
        if failures:
            await ctx.send(
                f'Successfully locked down {len(success)}/{len(channels)} channels until {formatted_time}.\n'
                f'Failed channels: {", ".join(c.mention for c in failures)}\n'
                f'Give the bot Manage Roles permissions in {plural(len(failures)):the channel|those channels} and try '
                f'the lockdown command on the failed {plural(len(failures)):channel} again.'
            )
        else:
            await ctx.send(f'Successfully locked down {plural(len(success)):channel} until {formatted_time}')

    @lockdown.command(name='end')
    @commands.guild_only()
    @checks.hybrid_permissions_check(ban_members=True, manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def lockdown_end(self, ctx: GuildContext):
        """Ends all lockdowns set.

        To use this command you must have Manage Roles and Ban Members permissions.
        The bot must also have Manage Members permissions.
        """

        reason = f'Lockdown ended by {ctx.author} (ID: {ctx.author.id})'
        async with ctx.typing():
            failures = await self.end_lockdown(ctx.guild, reason=reason)

        # Remove all the lockdown information...
        query = "DELETE FROM guild_lockdowns WHERE guild_id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        if failures:
            formatted = [c.mention for c in failures]
            await ctx.send(f'Lockdown ended. Failed to edit {human_join(formatted, final="and")}')
        else:
            await ctx.send('Lockdown successfully ended')

    @commands.Cog.listener()
    async def on_lockdown_timer_complete(self, timer: Timer):
        guild_id, mod_id, channel_id, channel_ids = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.unavailable:
            return

        member = await self.bot.get_or_fetch_member(guild, mod_id)
        if member is None:
            moderator = f'Mod ID {mod_id}'
        else:
            moderator = f'{member} (ID: {mod_id})'

        reason = f'Automatic lockdown ended from timer made on {timer.created_at} by {moderator}'
        failures = await self.end_lockdown(guild, channel_ids=channel_ids, reason=reason)

        query = "DELETE FROM guild_lockdowns WHERE guild_id=$1 AND channel_id = ANY($2::bigint[]);"
        await self.bot.pool.execute(query, guild_id, channel_ids)

        channel = guild.get_channel_or_thread(channel_id)
        if channel is not None:
            assert isinstance(channel, discord.abc.Messageable)
            if failures:
                formatted = [c.mention for c in failures]
                await channel.send(
                    f'Lockdown ended. However, I failed to properly edit {human_join(formatted, final="and")}'
                )
            else:
                valid = [f'<#{c}>' for c in channel_ids]
                await channel.send(f'Lockdown successfully ended for {human_join(valid, final="and")}')


async def setup(bot: RoboDanny):
    await bot.add_cog(Mod(bot))
