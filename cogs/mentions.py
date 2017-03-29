from discord.ext import commands
import discord
from .utils import config
import enum, json
import asyncio

class MentionMode(enum.Enum):
    off = 0
    on = 1
    always = 2

    def __str__(self):
        return self.name

def mention_converter(argument):
    try:
        return MentionMode[argument.lower()]
    except:
        raise commands.BadArgument('\U0001f52b Valid modes: ' + ', '.join(MentionMode.__members__))

def object_hook(obj):
    if '__settings__' in obj:
        return { k: MentionMode(v) for k, v in obj.items() }
    else:
        return obj

class MentionsEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, MentionMode):
            return o.value

        super().default(o)

class Mentions:
    """Commands related to fetching mentions."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('mentions.json', object_hook=object_hook, encoder=MentionsEncoder)

    def format_message(self, message):
        prefix = '[{0.timestamp:%I:%M:%S %p} UTC] {0.author}: {0.clean_content}'.format(message)
        if message.attachments:
            return prefix + ' (attachment: {0[url]})'.format(message.attachments[0])
        return prefix

    async def handle_message_mention(self, origin, message):
        perms = origin.permissions_in(message.channel)
        if not perms.read_messages:
            # if you can't read the messages, then you shouldn't get PM'd.
            return

        messages = []

        async for msg in self.bot.logs_from(message.channel, limit=3, before=message):
            messages.append(msg)

        messages.reverse()
        messages.append(message)

        if origin.status != discord.Status.online:
            # wait 30 seconds for context if it's not always on
            await asyncio.sleep(30)

            # get an updated reference, server references don't change
            member = message.server.get_member(origin.id)
            if member is None or member.status == discord.Status.online:
                # they've come online, so they might have checked the mention
                return

            # get the messages after this one
            ext = []
            async for msg in self.bot.logs_from(message.channel, limit=3, after=message):
                ext.append(msg)

            ext.reverse()
            messages.extend(ext)

        try:
            fmt = 'You were mentioned in {0.channel.mention}:\n{1}'
            fmt = fmt.format(message, '\n'.join(map(self.format_message, messages)))
            await self.bot.send_message(origin, fmt)
        except:
            # silent failure best failure
            pass

    def members_mentioned_in(self, message):
        ret = set()
        for member in message.mentions:
            ret.add(member.id)

        for role in message.role_mentions:
            if role.is_everyone:
                # no smart asses
                continue

            # add the member ID of every member with said role
            for member in list(message.server.members):
                has_role = discord.utils.get(member.roles, id=role.id) is not None
                if has_role:
                    ret.add(member.id)

        return ret

    async def on_message(self, message):
        server = message.server
        if server is None:
            return

        settings = self.config.get(server.id)
        if settings is None:
            return

        author = message.author
        mentioned = self.members_mentioned_in(message)
        for member in mentioned:
            try:
                mode = settings[member]
            except KeyError:
                continue

            if mode is MentionMode.off:
                continue

            origin = server.get_member(member)

            if mode is MentionMode.on and origin.status in (discord.Status.online, discord.Status.dnd):
                continue

            coro = self.handle_message_mention(origin, message)
            self.bot.loop.create_task(coro)

    @commands.command(pass_context=True, no_pm=True)
    async def mentions(self, ctx, channel : discord.Channel = None, context : int = 3):
        """Tells you when you were mentioned in a channel.

        If a channel is not given, then it tells you when you were mentioned in a
        the current channel. The context is an integer that tells you how many messages
        before should be shown. The context cannot be greater than 5 or lower than 0.
        """
        if channel is None:
            channel = ctx.message.channel

        context = min(5, max(0, context))

        author = ctx.message.author
        logs = []
        mentioned_indexes = []
        async for message in self.bot.logs_from(channel, limit=1500, before=ctx.message):
            if author.mentioned_in(message):
                # we're mentioned so..
                mentioned_indexes.append(len(logs))

            # these logs are from newest message to oldest
            # so logs[0] is newest and logs[-1] is oldest
            logs.append(message)

        if len(mentioned_indexes) == 0:
            await self.bot.say('You were not mentioned in the past 1500 messages.')
            return

        for index in mentioned_indexes:
            view = reversed(logs[index - context - 1:index + context])
            try:
                await self.bot.whisper('\n'.join(map(self.format_message, view)))
            except discord.HTTPException:
                await self.bot.whisper('An error happened while fetching mentions.')

    @commands.command(pass_context=True, no_pm=True)
    async def pmmentions(self, ctx, mode : mention_converter = None):
        """Private messages you when you get mentioned.

        The settings only apply on a per-server basis.

        everyone and here mentions are not kept track of.

        The current supported modes are as follows:

        - on: PMs you only if you are away or offline.
        - off: Doesn't PM you at all.
        - always: PMs you regardless of your current status.
        """

        server = ctx.message.server
        author = ctx.message.author
        settings = self.config.get(server.id, { '__settings__': True })
        if mode is None:
            value = settings.get(author.id, 'off')
            await self.bot.say('Your pmmentions setting is set to ' + str(value))
            return

        settings[author.id] = mode
        await self.config.put(server.id, settings)
        await self.bot.say('Updated pmmentions settings to ' + str(mode))

    @pmmentions.error
    async def pmmentions_error(self, error, ctx):
        if isinstance(error, commands.BadArgument):
            await self.bot.say(str(error))

def setup(bot):
    bot.add_cog(Mentions(bot))
