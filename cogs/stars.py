from discord.ext import commands
import discord
import datetime
from .utils import checks, config
import json
import copy
import asyncio
import logging

log = logging.getLogger(__name__)

class StarError(Exception):
    pass

class Stars:
    """A starboard to upvote posts obviously.

    There are two ways to make use of this feature, the first is
    via reactions, react to a message with \N{WHITE MEDIUM STAR} and
    the bot will automatically add (or remove) it to the starboard.

    The second way is via Developer Mode. Enable it under Settings >
    Appearance > Developer Mode and then you get access to Copy ID
    and using the star/unstar commands.
    """

    def __init__(self, bot):
        self.bot = bot

        # config format: (yeah, it's not ideal or really any good but whatever)
        # <guild_id> : <data> where <data> is
        # channel: <starboard channel id>
        # message_id: [bot_message, [starred_user_ids]]
        self.stars = config.Config('stars.json')

        # cache message objects to save Discord some HTTP requests.
        self._message_cache = {}

        self.janitor_tasks = {
            guild_id: self.bot.loop.create_task(self.janitor(guild_id))
            for guild_id in self.stars.all()
            if self.stars.get(guild_id).get('janitor') is not None
        }

    def __unload(self):
        for task in self.janitor_tasks.values():
            try:
                task.cancel()
            except:
                pass

    async def clean_starboard(self, guild_id, stars):
        db = self.stars.get(guild_id, {}).copy()
        starboard = self.bot.get_channel(db['channel'])
        dead_messages = {
            data[0]
            for _, data in db.items()
            if type(data) is list and len(data[1]) <= stars and data[0] is not None
        }

        await self.bot.purge_from(starboard, limit=1000, check=lambda m: m.id in dead_messages)

    async def janitor(self, guild_id):
        try:
            await self.bot.wait_until_ready()
            while not self.bot.is_closed:
                await self.clean_starboard(guild_id, 1)
                await asyncio.sleep(self.stars.get(guild_id)['janitor'])
        except asyncio.CancelledError:
            pass


    def star_emoji(self, stars):
        if 5 >= stars >= 0:
            return '\N{WHITE MEDIUM STAR}'
        elif 10 >= stars >= 6:
            return '\N{GLOWING STAR}'
        elif 25 >= stars >= 11:
            return '\N{DIZZY SYMBOL}'
        else:
            return '\N{SPARKLES}'

    def emoji_message(self, msg, starrers):
        # we should hope that the message length is not too big for this to work.
        emoji = self.star_emoji(starrers)
        content = msg.clean_content
        if msg.attachments:
            attachments = '(attachment: {[url]})'.format(msg.attachments[0])
            if content:
                content = content + ' ' + attachments
            else:
                content = attachments

        # <emoji> <star> <content> - <time> by <user> in <channel>
        if starrers > 1:
            base = '{0} **{1}**'
        else:
            base = '{0}'

        fmt = base + ' {2} - {3.timestamp:%Y-%m-%d %H:%M UTC} by {3.author} in {3.channel.mention} (ID: {3.id})'
        return fmt.format(emoji, starrers, content, msg)

    async def star_message(self, message, starrer_id, message_id, *, delete=False):
        guild_id = message.server.id
        db = self.stars.get(guild_id, {})
        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        stars = db.get(message_id, [None, []]) # ew, I know.
        starrers = stars[1]

        if starrer_id in starrers:
            raise StarError('\N{NO ENTRY SIGN} You already starred this message.')

        # if the IDs are the same, then they were probably starred using the reaction interface
        if message.id != message_id:
            msg = await self.get_message(message.channel, message_id)
            if msg is None:
                raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')
        else:
            msg = message

        if (len(msg.content) == 0 and len(msg.attachments) == 0) or msg.type is not discord.MessageType.default:
            raise StarError('\N{NO ENTRY SIGN} This message cannot be starred.')

        if starrer_id == msg.author.id:
            raise StarError('\N{NO ENTRY SIGN} You cannot star your own message.')

        if msg.channel.id == starboard.id:
            raise StarError('\N{NO ENTRY SIGN} You cannot star messages in the starboard.')

        # check if the message is older than 7 days
        seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        if msg.timestamp < seven_days_ago:
            raise StarError('\N{NO ENTRY SIGN} This message is older than 7 days.')

        # at this point we can assume that the user did not star the message
        # and that it is relatively safe to star
        to_send = self.emoji_message(msg, len(starrers) + 1)
        if len(to_send) > 2000:
            raise StarError('\N{NO ENTRY SIGN} This message is too big to be starred.')

        # try to remove the star message since it's 'spammy'
        if delete:
            try:
                await self.bot.delete_message(message)
            except:
                pass

        starrers.append(starrer_id)
        db[message_id] = stars

        # freshly starred
        if stars[0] is None:
            sent = await self.bot.send_message(starboard, to_send)
            stars[0] = sent.id
            await self.stars.put(guild_id, db)
            return

        bot_msg = await self.get_message(starboard, stars[0])
        if bot_msg is None:
            await self.bot.say('\N{BLACK QUESTION MARK ORNAMENT} Expected to be {0.mention} but is not.'.format(starboard))

            # remove the entry from the starboard cache since someone deleted it.
            # i.e. they did a 'clear' on the stars.
            # they can go through this process again if they *truly* want to star it.
            db.pop(message_id, None)
            await self.stars.put(guild_id, db)
            return

        await self.stars.put(guild_id, db)
        await self.bot.edit_message(bot_msg, to_send)

    async def unstar_message(self, message, starrer_id, message_id):
        guild_id = message.server.id
        db = self.stars.get(guild_id, {})
        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        stars = db.get(message_id)
        if stars is None:
            raise StarError('\N{NO ENTRY SIGN} This message has no stars.')

        starrers = stars[1]
        try:
            starrers.remove(starrer_id)
        except ValueError:
            raise StarError('\N{NO ENTRY SIGN} You have not starred this message.')

        db[message_id] = stars
        bot_msg = await self.get_message(starboard, stars[0])
        if bot_msg is not None:
            if len(starrers) == 0:
                # no more stars, so it's gone from the board
                db.pop(message_id, None)
                await self.stars.put(guild_id, db)
                await self.bot.delete_message(bot_msg)
            else:
                # if the IDs are the same, then they were probably starred using the reaction interface
                if message.id != message_id:
                    msg = await self.get_message(message.channel, message_id)
                    if msg is None:
                        raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')
                else:
                    msg = message

                to_send = self.emoji_message(msg, len(starrers))
                await self.stars.put(guild_id, db)
                await self.bot.edit_message(bot_msg, to_send)

    @commands.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(administrator=True)
    async def starboard(self, ctx, *, name: str = 'starboard'):
        """Sets up the starboard for this server.

        This creates a new channel with the specified name
        and makes it into the server's "starboard". If no
        name is passed in then it defaults to "starboard".
        If the channel is deleted then the starboard is
        deleted as well.

        You must have Administrator permissions to use this
        command or the Bot Admin role.
        """

        server = ctx.message.server

        stars = self.stars.get(server.id, {})
        old_starboard = self.bot.get_channel(stars.get('channel'))
        if old_starboard is not None:
            fmt = 'This channel already has a starboard ({.mention})'
            await self.bot.say(fmt.format(old_starboard))
            return

        # an old channel might have been deleted and thus we should clear all its star data
        stars = {}

        my_permissions = ctx.message.channel.permissions_for(server.me)
        args = [server, name]

        if my_permissions.manage_roles:
            mine = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            everyone = discord.PermissionOverwrite(send_messages=False, embed_links=False)
            args.append((server.me, mine))
            args.append((server.default_role, everyone))

        try:
            channel = await self.bot.create_channel(*args)
        except discord.Forbidden:
            await self.bot.say('\N{NO ENTRY SIGN} I do not have permissions to create a channel.')
        except discord.HTTPException:
            await self.bot.say('\N{PISTOL} This channel name is bad or an unknown error happened.')
        else:
            stars['channel'] = channel.id
            await self.stars.put(server.id, stars)
            await self.bot.say('\N{GLOWING STAR} Starboard created at ' + channel.mention)

    async def get_message(self, channel, mid):
        try:
            return self._message_cache[mid]
        except KeyError:
            try:
                msg = self._message_cache[mid] = await self.bot.get_message(channel, mid)
            except discord.HTTPException:
                return None
            else:
                return msg

    # a custom message events
    async def on_socket_raw_receive(self, data):
        # no binary frames
        if isinstance(data, bytes):
            return

        data = json.loads(data)
        event = data.get('t')
        payload = data.get('d')
        if event not in ('MESSAGE_UPDATE', 'MESSAGE_DELETE',
                         'MESSAGE_REACTION_ADD', 'MESSAGE_REACTION_REMOVE'):
            return

        is_message_delete = event[8] == 'D'
        is_message_update = event[8] == 'U'
        is_reaction = event.endswith('_ADD')
        is_reaction_remove = event.endswith('_REMOVE')

        # make sure the reaction is proper
        if is_reaction or is_reaction_remove:
            emoji = payload['emoji']
            if emoji['name'] != '\N{WHITE MEDIUM STAR}':
                return # not a star reaction
        elif is_message_update and 'content' not in payload:
            # embed only edit..
            return

        channel = self.bot.get_channel(payload.get('channel_id'))
        if channel is None or channel.is_private:
            return

        # everything past this point is pointless if we're adding a reaction,
        # so let's just see if we can star the message and get it over with.
        if is_reaction or is_reaction_remove:
            message = await self.get_message(channel, payload['message_id'])
            verb = 'star' if is_reaction else 'unstar'
            coro = getattr(self, '%s_message' % verb)
            try:
                await coro(message, payload['user_id'], message.id)
                log.info('User ID %s has %sred Message ID %s' % (payload['user_id'], verb, message.id))
            except StarError:
                pass
            finally:
                return

        server = channel.server
        db = self.stars.get(server.id)
        if db is None:
            return

        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            # the starboard might have gotten deleted?
            return

        msg_id = payload['id']

        if is_message_delete:
            if channel.id != starboard.id:
                return # not a delete we're interested in

            # see if the message being deleted is in the starboard
            exists = discord.utils.find(lambda k: type(db[k]) is list and db[k][0] == msg_id, db)
            if exists:
                db.pop(exists)
                await self.stars.put(server.id, db)

            return

        # at this point we're in MESSAGE_UPDATE
        # check if the edited message is even being tracked
        stars = db.get(msg_id)
        if stars is None:
            return


        star_msg = await self.get_message(starboard, stars[0])
        new_msg = copy.copy(star_msg)
        new_msg.content = payload['content']

        # edit the message with the new info
        try:
            await self.bot.edit_message(star_msg, self.emoji_message(new_msg, len(stars[1])))
        except:
            pass # the content was probably too big so just ignore this edit.

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def star(self, ctx, message: int):
        """Stars a message via message ID.

        To star a message you should click on the cog
        on a message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        You can only star a message once. You cannot star
        messages older than 7 days.
        """
        try:
            await self.star_message(ctx.message, ctx.message.author.id, str(message), delete=True)
        except StarError as e:
            await self.bot.say(e)

    @star.error
    async def star_error(self, error, ctx):
        if type(error) is commands.BadArgument:
            await self.bot.say('That is not a valid message ID. Use Developer Mode to get the Copy ID option.')

    @commands.command(pass_context=True, no_pm=True)
    async def unstar(self, ctx, message: int):
        """Unstars a message via message ID.

        To unstar a message you should click on the cog
        on a message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        You cannot unstar messages older than 7 days.
        """
        try:
            await self.unstar_message(ctx.message, ctx.message.author.id, str(message))
        except StarError as e:
            return await self.bot.say(e)
        else:
            await self.bot.delete_message(ctx.message)

    @star.command(name='janitor', pass_context=True, no_pm=True)
    @checks.admin_or_permissions(administrator=True)
    async def star_janitor(self, ctx, minutes: float = 0.0):
        """Set the starboard's janitor clean rate.

        The clean rate allows the starboard to cleared from single star
        messages. By setting a clean rate, every N minutes the bot will
        routinely cleanup single starred messages from the starboard.

        Setting the janitor's clean rate to 0 (or below) disables it.

        This command requires the Administrator permission or the Bot
        Admin role.
        """

        guild_id = ctx.message.server.id
        db = self.stars.get(guild_id, {})

        if db.get('channel') is None:
            await self.bot.say('\N{WARNING SIGN} Starboard channel not found.')
            return

        def cleanup_task():
            task = self.janitor_tasks.pop(guild_id)
            task.cancel()
            db.pop('janitor', None)

        if minutes <= 0.0:
            cleanup_task()
            await self.bot.say('\N{SQUARED OK} No more cleaning up.')
        else:
            if 'janitor' in db:
                cleanup_task()

            db['janitor'] = minutes * 60.0
            self.janitor_tasks[guild_id] = self.bot.loop.create_task(self.janitor(guild_id))
            await self.bot.say('Remember to \N{PUT LITTER IN ITS PLACE SYMBOL}')

        await self.stars.put(guild_id, db)

    @star.command(name='clean', pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_messages=True)
    async def star_clean(self, ctx, stars:int = 1):
        """Cleans the starboard

        This removes messages in the starboard that only have less
        than or equal to the number of specified stars. This defaults to 1.

        To continuously do this over a period of time see
        the `janitor` subcommand.

        This command requires the Manage Messages permission or the
        Bot Admin role.
        """

        guild_id = ctx.message.server.id
        db = self.stars.get(guild_id, {})
        stars = 1 if stars < 0 else stars

        if db.get('channel') is None:
            await self.bot.say('\N{WARNING SIGN} Starboard channel not found.')
            return

        await self.clean_starboard(guild_id, stars)
        await self.bot.say('\N{PUT LITTER IN ITS PLACE SYMBOL}')

    @star.command(pass_context=True, no_pm=True, name='who')
    async def star_who(self, ctx, message: int):
        """Show who starred a message.

        The ID can either be the starred message ID
        or the message ID in the starboard channel.
        """

        server = ctx.message.server
        db = self.stars.get(server.id, {})
        message = str(message)

        if message in db:
            # starred message ID so this one's rather easy.
            starrers = db[message][1]
        else:
            # this one requires extra look ups...
            found = discord.utils.find(lambda v: type(v) is list and v[0] == message, db.values())
            if found is None:
                await self.bot.say('No one did.')
                return
            starrers = found[1]

        members = filter(None, map(server.get_member, starrers))
        await self.bot.say(', '.join(map(str, members)))

def setup(bot):
    bot.add_cog(Stars(bot))
