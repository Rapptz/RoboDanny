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
        emoji = self.star_emoji(starrers)
        base = '%s ID: %s' % (msg.channel.mention, msg.id)

        content = msg.clean_content
        if msg.attachments:
            attachments = '[Attachment]({[url]})'.format(msg.attachments[0])
            if content:
                content = content + '\n' + attachments
            else:
                content = attachments

        # build the embed
        e = discord.Embed()
        if starrers > 1:
            e.description = '%s **%s** %s' % (emoji, starrers, content)
        else:
            e.description = '%s %s' % (emoji, content)

        author = msg.author
        avatar = author.default_avatar_url if not author.avatar else author.avatar_url
        e.set_footer(text=author.display_name, icon_url=avatar)
        e.timestamp = msg.timestamp
        c = author.colour
        if c.value:
            e.colour = c
        return base, e

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
        content, embed = self.emoji_message(msg, len(starrers) + 1)

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
            sent = await self.bot.send_message(starboard, content, embed=embed)
            stars[0] = sent.id
            await self.stars.put(guild_id, db)
            return

        bot_msg = await self.get_message(starboard, stars[0])
        if bot_msg is None:
            await self.bot.say('\N{BLACK QUESTION MARK ORNAMENT} Expected to be in {0.mention} but is not.'.format(starboard))

            # remove the entry from the starboard cache since someone deleted it.
            # i.e. they did a 'clear' on the stars.
            # they can go through this process again if they *truly* want to star it.
            db.pop(message_id, None)
            await self.stars.put(guild_id, db)
            return

        await self.stars.put(guild_id, db)
        await self.bot.edit_message(bot_msg, content, embed=embed)

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

                content, e = self.emoji_message(msg, len(starrers))
                await self.stars.put(guild_id, db)
                await self.bot.edit_message(bot_msg, content, embed=e)

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
            mine = discord.PermissionOverwrite(send_messages=True, manage_messages=True, embed_links=True)
            everyone = discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True)
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
        if event not in ('MESSAGE_DELETE', 'MESSAGE_REACTION_ADD', 'MESSAGE_REACTION_REMOVE'):
            return

        is_message_delete = event[8] == 'D'
        is_reaction = event.endswith('_ADD')

        # make sure the reaction is proper
        if not is_message_delete:
            emoji = payload['emoji']
            if emoji['name'] != '\N{WHITE MEDIUM STAR}':
                return # not a star reaction

        channel = self.bot.get_channel(payload.get('channel_id'))
        if channel is None or channel.is_private:
            return

        # everything past this point is pointless if we're adding a reaction,
        # so let's just see if we can star the message and get it over with.
        if not is_message_delete:
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
        if starboard is None or channel.id != starboard.id:
            # the starboard might have gotten deleted?
            # or it might not be a delete worth dealing with
            return

        # see if the message being deleted is in the starboard
        msg_id = payload['id']
        exists = discord.utils.find(lambda k: isinstance(db[k], list) and db[k][0] == msg_id, db)
        if exists:
            db.pop(exists)
            await self.stars.put(server.id, db)

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def star(self, ctx, message: int):
        """Stars a message via message ID.

        To star a message you should click on the cog
        on a message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        It is recommended that you react to a message with
        '\N{WHITE MEDIUM STAR}' instead since this will
        make it easier.

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

    @star.command(name='update', no_pm=True, pass_context=True, hidden=True)
    @checks.admin_or_permissions(administrator=True)
    @commands.cooldown(rate=1, per=5.0*60, type=commands.BucketType.server)
    async def star_update(self, ctx):
        """Updates the starboard's content to the latest format.

        If a message referred in the starboard was deleted then
        the message will be untouched.

        To prevent abuse, only the last 100 messages are updated.

        Warning: This operation takes a long time. As a consequence,
        only those with Administrator permission can use this command
        and it has a cooldown of one use per 5 minutes.
        """
        guild_id = ctx.message.server.id
        db = self.stars.get(guild_id, {})

        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            return await self.bot.say('\N{WARNING SIGN} Starboard channel not found.')

        reconfigured_cache = {
            v[0]: (k, v[1]) for k, v in db.items()
        }

        async for msg in self.bot.logs_from(starboard, limit=100):
            try:
                original_id, starrers = reconfigured_cache[msg.id]
                original_channel = msg.channel_mentions[0]
            except Exception:
                continue

            original_message = await self.get_message(original_channel, original_id)
            if original_message is None:
                continue

            content, embed = self.emoji_message(original_message, len(starrers))
            await self.bot.edit_message(msg, content, embed=embed)

        await self.bot.say('\N{BLACK UNIVERSAL RECYCLING SYMBOL}')

    @star_update.error
    async def star_update_error(self, error, ctx):
        if isinstance(error, commands.CommandOnCooldown):
            if checks.is_owner_check(ctx.message):
                await ctx.invoke(self.star_update)
            else:
                await self.bot.say(error)

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
