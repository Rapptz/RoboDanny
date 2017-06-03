from discord.ext import commands
import discord
import datetime
from .utils import checks, config
import json
import copy
import random
import asyncio
import logging
import weakref
from collections import Counter

log = logging.getLogger(__name__)

class StarError(commands.CommandError):
    pass

class MockContext:
    pass

def requires_starboard():
    def predicate(ctx):
        if ctx.cog is None or ctx.message.server is None:
            return True

        ctx.guild_id = ctx.message.server.id
        ctx.db = ctx.cog.stars.get(ctx.guild_id, {})
        ctx.starboard = ctx.bot.get_channel(ctx.db.get('channel'))
        if ctx.starboard is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')
        return True
    return commands.check(predicate)

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
        # locked: <boolean indicating locked status>
        # message_id: [bot_message, [starred_user_ids]]
        self.stars = config.Config('stars.json')

        # cache message objects to save Discord some HTTP requests.
        self._message_cache = {}
        self._cleaner = self.bot.loop.create_task(self.clean_message_cache())

        self._locks = weakref.WeakValueDictionary()

        self.janitor_tasks = {
            guild_id: self.bot.loop.create_task(self.janitor(guild_id))
            for guild_id in self.stars.all()
            if self.stars.get(guild_id).get('janitor') is not None
        }

    def __unload(self):
        self._cleaner.cancel()
        for task in self.janitor_tasks.values():
            try:
                task.cancel()
            except:
                pass

    async def clean_message_cache(self):
        try:
            while not self.bot.is_closed:
                self._message_cache.clear()
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def clean_starboard(self, ctx, stars):
        dead_messages = {
            data[0]
            for _, data in ctx.db.items()
            if isinstance(data, list) and len(data[1]) <= stars and data[0] is not None
        }

        # delete all the keys from the dict
        for msg_id in dead_messages:
            ctx.db.pop(msg_id, None)

        await self.stars.put(ctx.guild_id, ctx.db)
        await self.bot.purge_from(ctx.starboard, limit=100, check=lambda m: m.id in dead_messages)

    async def run_janitor(self, guild_id):
        ctx = MockContext()
        ctx.guild_id = guild_id
        try:
            ctx.db = self.stars[guild_id]
            ctx.starboard = self.bot.get_channel(ctx.db.get('channel'))
            await self.clean_starboard(ctx, 1)
            await asyncio.sleep(ctx.db['janitor'])
        except KeyError:
            pass

    async def janitor(self, guild_id):
        try:
            await self.bot.wait_until_ready()
            while not self.bot.is_closed:
                await self.run_janitor(guild_id)
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

    def star_gradient_colour(self, stars):
        # We define as 13 stars to be 100% of the star gradient (half of the 26 emoji threshold)
        # So X / 13 will clamp to our percentage,
        # We start out with 0xfffdf7 for the beginning colour
        # Gradually evolving into 0xffc20c
        # rgb values are (255, 253, 247) -> (255, 194, 12)
        # To create the gradient, we use a linear interpolation formula
        # Which for reference is X = X_1 * p + X_2 * (1 - p)
        p = stars / 13
        if p > 1.0:
            p = 1.0

        red = 255
        green = int((194 * p) + (253 * (1 - p)))
        blue = int((12 * p) + (247 * (1 - p)))
        return (red << 16) + (green << 8) + blue

    def emoji_message(self, msg, starrers):
        emoji = self.star_emoji(starrers)
        # base = '%s ID: %s' % (msg.channel.mention, msg.id)

        if starrers > 1:
            base = '%s **%s** %s ID: %s' % (emoji, starrers, msg.channel.mention, msg.id)
        else:
            base = '%s %s ID: %s' % (emoji, msg.channel.mention, msg.id)


        content = msg.content
        e = discord.Embed(description=content)
        if msg.embeds:
            data = discord.Embed.from_data(msg.embeds[0])
            if data.type == 'image':
                e.set_image(url=data.url)

        if msg.attachments:
            url = msg.attachments[0]['url']
            if url.lower().endswith(('png', 'jpeg', 'jpg', 'gif')):
                e.set_image(url=url)
            else:
                attachments = '[Attachment](%s)' % url
                if content:
                    e.description = content + '\n' + attachments
                else:
                    e.description = attachments

        # build the embed
        author = msg.author
        avatar = author.default_avatar_url if not author.avatar else author.avatar_url
        avatar = avatar.replace('.gif', '.jpg')
        e.set_author(name=author.display_name, icon_url=avatar)
        e.timestamp = msg.timestamp
        e.colour = self.star_gradient_colour(starrers)
        return base, e

    async def _star_message(self, message, starrer_id, message_id, *, reaction=True):
        guild_id = message.server.id
        db = self.stars.get(guild_id, {})
        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        if db.get('locked'):
            raise StarError('\N{NO ENTRY SIGN} Starboard is locked.')

        stars = db.get(message_id, [None, []]) # ew, I know.
        starrers = stars[1]

        if starrer_id in starrers:
            raise StarError('\N{NO ENTRY SIGN} You already starred this message.')

        if reaction:
            mod = self.bot.get_cog('Mod')
            if mod:
                member = message.server.get_member(starrer_id)
                if member and mod.is_plonked(message.server, member):
                    raise StarError('\N{NO ENTRY SIGN} Plonked Member')

        # if the IDs are the same, then they were probably starred using the reaction interface
        if message.id != message_id:
            msg = await self.get_message(message.channel, message_id)
            if msg is None:
                raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')
        else:
            msg = message

        if msg.channel.id == starboard.id:
            if not reaction:
                raise StarError('\N{NO ENTRY SIGN} Cannot star messages in the starboard without reacting.')

            # If we star a message in the starboard then we can do a reverse lookup to check
            # what message to star in reality.

            # first remove the reaction if applicable:
            try:
                await self.bot.http.remove_reaction(msg.id, msg.channel.id, '\N{WHITE MEDIUM STAR}', starrer_id)
            except:
                pass # oh well

            # do the reverse lookup and update the references
            tup = discord.utils.find(lambda t: isinstance(t[1], list) and t[1][0] == message_id, db.items())
            if tup is None:
                raise StarError('\N{NO ENTRY SIGN} Could not find this message ID in the starboard.')

            msg = await self.get_message(msg.channel_mentions[0], tup[0])
            if msg is None:
                raise StarError('\N{BLACK QUESTION MARK ORNAMENT} This message could not be found.')

            # god bless recursion
            return await self._star_message(msg, starrer_id, msg.id, reaction=True)

        if (len(msg.content) == 0 and len(msg.attachments) == 0) or msg.type is not discord.MessageType.default:
            raise StarError('\N{NO ENTRY SIGN} This message cannot be starred.')

        if starrer_id == msg.author.id:
            raise StarError('\N{NO ENTRY SIGN} You cannot star your own message.')


        # check if the message is older than 7 days
        seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        if msg.timestamp < seven_days_ago:
            raise StarError('\N{NO ENTRY SIGN} This message is older than 7 days.')

        # at this point we can assume that the user did not star the message
        # and that it is relatively safe to star
        content, embed = self.emoji_message(msg, len(starrers) + 1)

        # try to remove the star message since it's 'spammy'
        if not reaction:
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

    async def star_message(self, message, starrer_id, message_id, *, reaction=True):
        lock = self._locks.get(message.server.id)
        if lock is None:
            self._locks[message.server.id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            await self._star_message(message, starrer_id, message_id, reaction=reaction)

    async def _unstar_message(self, message, starrer_id, message_id):
        guild_id = message.server.id
        db = self.stars.get(guild_id, {})
        starboard = self.bot.get_channel(db.get('channel'))
        if starboard is None:
            raise StarError('\N{WARNING SIGN} Starboard channel not found.')

        if db.get('locked'):
            raise StarError('\N{NO ENTRY SIGN} Starboard is locked.')

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

    async def unstar_message(self, message, starrer_id, message_id):
        lock = self._locks.get(message.server.id)
        if lock is None:
            self._locks[message.server.id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            await self._unstar_message(message, starrer_id, message_id)

    @commands.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def starboard(self, ctx, *, name='starboard'):
        """Sets up the starboard for this server.

        This creates a new channel with the specified name
        and makes it into the server's "starboard". If no
        name is passed in then it defaults to "starboard".
        If the channel is deleted then the starboard is
        deleted as well.

        You must have Manage Server permissions to use this
        command or the Bot Admin role.
        """

        server = ctx.message.server

        stars = self.stars.get(server.id, {})
        old_starboard = self.bot.get_channel(stars.get('channel'))
        if old_starboard is not None:
            fmt = 'This server already has a starboard ({.mention})'
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

    async def on_command_error(self, error, ctx):
        if isinstance(error, StarError):
            await self.bot.send_message(ctx.message.channel, error)

    # a custom message events
    async def on_socket_raw_receive(self, data):
        # no binary frames
        if isinstance(data, bytes):
            return

        data = json.loads(data)
        event = data.get('t')
        payload = data.get('d')
        if event not in ('MESSAGE_DELETE', 'MESSAGE_REACTION_ADD',
                         'MESSAGE_REACTION_REMOVE', 'MESSAGE_REACTION_REMOVE_ALL'):
            return

        is_message_delete = event == 'MESSAGE_DELETE'
        is_reaction_clear = event == 'MESSAGE_REACTION_REMOVE_ALL'
        is_reaction = event == 'MESSAGE_REACTION_ADD'

        # make sure the reaction is proper
        if not is_message_delete and not is_reaction_clear:
            emoji = payload['emoji']
            if emoji['name'] != '\N{WHITE MEDIUM STAR}':
                return # not a star reaction

        channel = self.bot.get_channel(payload.get('channel_id'))
        if channel is None or channel.is_private:
            return

        # everything past this point is pointless if we're adding a reaction,
        # so let's just see if we can star the message and get it over with.
        if not is_message_delete and not is_reaction_clear:
            message = await self.get_message(channel, payload['message_id'])
            member = channel.server.get_member(payload['user_id'])
            if member is None or member.bot:
                return # denied

            verb = 'star' if is_reaction else 'unstar'
            coro = getattr(self, '%s_message' % verb)
            try:
                await coro(message, member.id, message.id)
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
        if starboard is None or (is_message_delete and channel.id != starboard.id):
            # the starboard might have gotten deleted?
            # or it might not be a delete worth dealing with
            return

        # see if the message being deleted is in the starboard
        if is_message_delete:
            msg_id = payload['id']
            exists = discord.utils.find(lambda k: isinstance(db[k], list) and db[k][0] == msg_id, db)
            if exists:
                db.pop(exists)
                await self.stars.put(server.id, db)
        else:
            msg_id = payload['message_id']
            try:
                value = db.pop(msg_id)
            except KeyError:
                pass
            else:
                await self.bot.http.delete_message(starboard.id, value[0], guild_id=server.id)
                await self.stars.put(server.id, db)

    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def star(self, ctx, message: int):
        """Stars a message via message ID.

        To star a message you should right click on the
        on a message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        It is recommended that you react to a message with
        '\N{WHITE MEDIUM STAR}' instead since this will
        make it easier.

        You can only star a message once. You cannot star
        messages older than 7 days.
        """
        try:
            await self.star_message(ctx.message, ctx.message.author.id, str(message), reaction=False)
        except StarError as e:
            await self.bot.say(e)

    @star.error
    async def star_error(self, error, ctx):
        if isinstance(error, commands.BadArgument):
            await self.bot.say('That is not a valid message ID. Use Developer Mode to get the Copy ID option.')

    @commands.command(pass_context=True, no_pm=True)
    async def unstar(self, ctx, message: int):
        """Unstars a message via message ID.

        To unstar a message you should right click on the
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
    @checks.admin_or_permissions(manage_server=True)
    @requires_starboard()
    async def star_janitor(self, ctx, minutes: float = 0.0):
        """Set the starboard's janitor clean rate.

        The clean rate allows the starboard to cleared from single star
        messages. By setting a clean rate, every N minutes the bot will
        routinely cleanup single starred messages from the starboard.

        Setting the janitor's clean rate to 0 (or below) disables it.

        This command requires the Manage Server permission or the Bot
        Admin role.
        """
        def cleanup_task():
            task = self.janitor_tasks.pop(ctx.guild_id, None)
            if task:
                task.cancel()
            ctx.db.pop('janitor', None)

        if minutes <= 0.0:
            cleanup_task()
            await self.bot.say('\N{SQUARED OK} No more cleaning up.')
        else:
            if 'janitor' in ctx.db:
                cleanup_task()

            ctx.db['janitor'] = minutes * 60.0
            self.janitor_tasks[ctx.guild_id] = self.bot.loop.create_task(self.janitor(ctx.guild_id))
            await self.bot.say('Remember to \N{PUT LITTER IN ITS PLACE SYMBOL}')

        await self.stars.put(ctx.guild_id, ctx.db)

    @star.command(name='clean', pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    @requires_starboard()
    async def star_clean(self, ctx, stars:int = 1):
        """Cleans the starboard

        This removes messages in the starboard that only have less
        than or equal to the number of specified stars. This defaults to 1.

        To continuously do this over a period of time see
        the `janitor` subcommand.

        This command requires the Manage Server permission or the
        Bot Admin role.
        """

        stars = 1 if stars < 0 else stars
        await self.clean_starboard(ctx, stars)
        await self.bot.say('\N{PUT LITTER IN ITS PLACE SYMBOL}')

    @star.command(name='update', no_pm=True, pass_context=True, hidden=True)
    @checks.admin_or_permissions(manage_server=True)
    @requires_starboard()
    @commands.cooldown(rate=1, per=5.0*60, type=commands.BucketType.server)
    async def star_update(self, ctx):
        """Updates the starboard's content to the latest format.

        If a message referred in the starboard was deleted then
        the message will be untouched.

        To prevent abuse, only the last 100 messages are updated.

        Warning: This operation takes a long time. As a consequence,
        only those with Manage Server permission can use this command
        and it has a cooldown of one use per 5 minutes.
        """
        reconfigured_cache = {
            v[0]: (k, v[1]) for k, v in ctx.db.items()
        }

        async for msg in self.bot.logs_from(ctx.starboard, limit=100):
            try:
                original_id, starrers = reconfigured_cache[msg.id]
                original_channel = msg.channel_mentions[0]
            except Exception:
                continue

            original_message = await self.get_message(original_channel, original_id)
            if original_message is None:
                continue

            content, embed = self.emoji_message(original_message, len(starrers))
            try:
                await self.bot.edit_message(msg, content, embed=embed)
            except:
                pass # somehow this failed, so ignore it

        await self.bot.say('\N{BLACK UNIVERSAL RECYCLING SYMBOL}')

    @star_update.error
    async def star_update_error(self, error, ctx):
        if isinstance(error, commands.CommandOnCooldown):
            if checks.is_owner_check(ctx.message):
                await ctx.invoke(self.star_update)
            else:
                await self.bot.say(error)

    async def show_message(self, ctx, key, value):
        # Unfortunately, we don't store the channel_id internally, so this
        # requires an extra lookup to parse the channel mentions to get the
        # original channel. A consequence of mediocre design I suppose.
        bot_message = await self.get_message(ctx.starboard, value[0])
        if bot_message is None:
            raise RuntimeError('Somehow referring to a deleted message in the starboard?')

        try:
            original_channel = bot_message.channel_mentions[0]
            msg = await self.get_message(original_channel, key)
        except Exception as e:
            raise RuntimeError('An error occurred while fetching message.')

        if msg is None:
            raise RuntimeError('Could not find message. Possibly deleted.')

        content, embed = self.emoji_message(msg, len(value[1]))
        await self.bot.say(content, embed=embed)

    @star.command(name='show', no_pm=True, pass_context=True)
    @commands.cooldown(rate=1, per=10.0, type=commands.BucketType.user)
    @requires_starboard()
    async def star_show(self, ctx, message: int):
        """Shows a starred via message ID.

        To get the ID of a message you should right click on the
        message and then click "Copy ID". You must have
        Developer Mode enabled to get that functionality.

        You can only use this command once per 10 seconds.
        """
        message = str(message)

        try:
            entry = ctx.db[message]
        except KeyError:
            return await self.bot.say('This message has not been starred.')

        try:
            await self.show_message(ctx, message, entry)
        except Exception as e:
            await self.bot.say(e)

    @star_show.error
    async def star_show_error(self, error, ctx):
        if isinstance(error, commands.CommandOnCooldown):
            if checks.is_owner_check(ctx.message):
                await ctx.invoke(self.star_show)
            else:
                await self.bot.say(error)
        elif isinstance(error, commands.BadArgument):
            await self.bot.say('That is not a valid message ID. Use Developer Mode to get the Copy ID option.')

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
            found = discord.utils.find(lambda v: isinstance(v, list) and v[0] == message, db.values())
            if found is None:
                await self.bot.say('No one did.')
                return
            starrers = found[1]

        members = filter(None, map(server.get_member, starrers))
        await self.bot.say(', '.join(map(str, members)))

    @star.command(pass_context=True, no_pm=True, name='stats')
    @requires_starboard()
    async def star_stats(self, ctx):
        """Shows statistics on the starboard usage."""
        e = discord.Embed()
        e.timestamp = ctx.starboard.created_at
        e.set_footer(text='Adding stars since')

        all_starrers = [(v[1], k) for k, v in ctx.db.items() if isinstance(v, list)]
        e.add_field(name='Messages Starred', value=str(len(all_starrers)))
        e.add_field(name='Stars Given', value=str(sum(len(x) for x, _ in all_starrers)))

        most_stars = max(all_starrers, key=lambda t: len(t[0]))
        e.add_field(name='Most Stars Given', value='{} stars\nID: {}'.format(len(most_stars[0]), most_stars[1]))

        c = Counter(author for x, _ in all_starrers for author in x)
        common = c.most_common(3)

        if len(common) >= 1:
            e.add_field(name='\U0001f947 Starrer', value='<@!%s> with %s stars' % common[0])
        if len(common) >= 2:
            e.add_field(name='\U0001f948 Starrer', value='<@!%s> with %s stars' % common[1])
        if len(common) >= 3:
            e.add_field(name='\U0001f949 Starrer', value='<@!%s> with %s stars' % common[2])

        await self.bot.say(embed=e)

    @star.command(pass_context=True, no_pm=True, name='random')
    @requires_starboard()
    async def star_random(self, ctx):
        entries = [(k, v) for k, v in ctx.db.items() if isinstance(v, list)]
        # try at most 5 times to get a non-deleted starboard message
        for i in range(5):
            try:
                (k, v) = random.choice(entries)
                await self.show_message(ctx, k, v)
            except Exception:
                continue
            else:
                return

        await self.bot.say('Sorry, all I could find are deleted messages. Try again?')

    @star.command(pass_context=True, no_pm=True, name='lock')
    @checks.admin_or_permissions(manage_server=True)
    @requires_starboard()
    async def star_lock(self, ctx):
        """Locks the starboard from being processed.

        This is a moderation tool that allows you to temporarily
        disable the starboard to aid in dealing with star spam.

        When the starboard is locked, no new entries are added to
        the starboard as the bot will no longer listen to reactions or
        star/unstar commands.

        To unlock the starboard, use the `unlock` subcommand.

        To use this command you need Bot Admin role or Manage Server
        permission.
        """

        ctx.db['locked'] = True
        await self.stars.put(ctx.guild_id, ctx.db)
        await self.bot.say('Starboard is now locked.')

    @star.command(pass_context=True, no_pm=True, name='unlock')
    @checks.admin_or_permissions(manage_server=True)
    @requires_starboard()
    async def star_unlock(self, ctx):
        """Unlocks the starboard for re-processing.

        To use this command you need Bot Admin role or Manage Server
        permission.
        """

        ctx.db['locked'] = False
        await self.stars.put(ctx.guild_id, ctx.db)
        await self.bot.say('Starboard is now unlocked.')

def setup(bot):
    bot.add_cog(Stars(bot))
