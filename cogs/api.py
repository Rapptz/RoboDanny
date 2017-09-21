from discord.ext import commands
from .utils import checks, db, fuzzy, cache, time
import asyncio
import discord
import re
import lxml.etree as etree
from collections import Counter

DISCORD_API_ID    = 81384788765712384
DISCORD_BOTS_ID   = 110373943822540800
USER_BOTS_ROLE    = 178558252869484544
CONTRIBUTORS_ROLE = 111173097888993280
DISCORD_PY_ID     = 84319995256905728

def is_discord_api():
    return checks.is_in_guilds(DISCORD_API_ID)

def contributor_or_higher():
    def predicate(ctx):
        guild = ctx.guild
        if guild is None:
            return False

        role = discord.utils.find(lambda r: r.id == CONTRIBUTORS_ROLE, guild.roles)
        if role is None:
            return False

        return ctx.author.top_role >= role
    return commands.check(predicate)

class Feeds(db.Table):
    id = db.PrimaryKeyColumn()
    channel_id = db.Column(db.Integer(big=True))
    role_id = db.Column(db.Integer(big=True))
    name = db.Column(db.String)

class RTFM(db.Table):
    id = db.PrimaryKeyColumn()
    user_id = db.Column(db.Integer(big=True), unique=True, index=True)
    count = db.Column(db.Integer, default=1)

class API:
    """Discord API exclusive things."""

    def __init__(self, bot):
        self.bot = bot
        self.issue = re.compile(r'##(?P<number>[0-9]+)')
        self._recently_blocked = set()

    async def on_member_join(self, member):
        if member.guild.id != DISCORD_API_ID:
            return

        if member.bot:
            role = discord.Object(id=USER_BOTS_ROLE)
            await member.add_roles(role)

    async def on_message(self, message):
        channel = message.channel
        author = message.author

        if channel.id != DISCORD_PY_ID:
            return

        if author.status is discord.Status.offline:
            fmt = f'{author.mention} has been blocked for being invisible until they change their status or for 5 minutes.'

            try:
                await channel.set_permissions(author, read_messages=False, reason='invisible block')
                self._recently_blocked.add(author.id)
                await channel.send(fmt)
                msg = f'Heya. You have been automatically blocked from <#{DISCORD_PY_ID}> for 5 minutes for being ' \
                       'invisible.\nTry chatting again in 5 minutes or when you change your status. If you\'re curious ' \
                       'why invisible users are blocked, it is because they tend to break the client and cause them to ' \
                       'be hard to mention. Since we want to help you usually, we expect mentions to work without ' \
                       'headaches.\n\nSorry for the trouble.'
                await author.send(msg)
            except discord.HTTPException:
                pass

            await asyncio.sleep(300)
            self._recently_blocked.discard(author.id)
            await channel.set_permissions(author, overwrite=None, reason='invisible unblock')
            return

        m = self.issue.search(message.content)
        if m is not None:
            url = 'https://github.com/Rapptz/discord.py/issues/'
            await channel.send(url + m.group('number'))

    async def on_member_update(self, before, after):
        if after.guild.id != DISCORD_API_ID:
            return

        if before.status is discord.Status.offline and after.status is not discord.Status.offline:
            if after.id in self._recently_blocked:
                self._recently_blocked.discard(after.id)
                channel = after.guild.get_channel(DISCORD_PY_ID)
                await channel.set_permissions(after, overwrite=None, reason='invisible unblock')

    async def build_rtfm_lookup_table(self):
        cache = {}

        page_types = {
            'rewrite': (
                'http://discordpy.rtfd.io/en/rewrite/api.html',
                'http://discordpy.rtfd.io/en/rewrite/ext/commands/api.html'
            ),
            'latest': (
                'http://discordpy.rtfd.io/en/latest/api.html',
            )
        }

        for key, pages in page_types.items():
            sub = cache[key] = {}
            for page in pages:
                async with self.bot.session.get(page) as resp:
                    if resp.status != 200:
                        raise RuntimeError('Cannot build rtfm lookup table, try again later.')

                    text = await resp.text(encoding='utf-8')
                    root = etree.fromstring(text, etree.HTMLParser())
                    nodes = root.findall(".//dt/a[@class='headerlink']")

                    for node in nodes:
                        href = node.get('href', '')
                        as_key = href.replace('#discord.', '').replace('ext.commands.', '')
                        sub[as_key] = page + href

        self._rtfm_cache = cache

    async def do_rtfm(self, ctx, key, obj):
        base_url = f'http://discordpy.rtfd.io/en/{key}/'

        if obj is None:
            await ctx.send(base_url)
            return

        if not hasattr(self, '_rtfm_cache'):
            await ctx.trigger_typing()
            await self.build_rtfm_lookup_table()

        # identifiers don't have spaces
        obj = obj.replace(' ', '_')

        if key == 'rewrite':
            pit_of_success_helpers = {
                'vc': 'VoiceClient',
                'msg': 'Message',
                'color': 'Colour',
                'perm': 'Permissions',
                'channel': 'TextChannel',
                'chan': 'TextChannel',
            }

            # point the abc.Messageable types properly:
            q = obj.lower()
            for name in dir(discord.abc.Messageable):
                if name[0] == '_':
                    continue
                if q == name:
                    obj = f'abc.Messageable.{name}'
                    break

            def replace(o):
                return pit_of_success_helpers.get(o.group(0), '')

            pattern = re.compile('|'.join(fr'\b{k}\b' for k in pit_of_success_helpers.keys()))
            obj = pattern.sub(replace, obj)

        cache = list(self._rtfm_cache[key].items())
        def transform(tup):
            return tup[0]

        matches = fuzzy.finder(obj, cache, key=lambda t: t[0], lazy=False)[:5]

        e = discord.Embed(colour=discord.Colour.blurple())
        if len(matches) == 0:
            return await ctx.send('Could not find anything. Sorry.')

        e.description = '\n'.join(f'[{key}]({url})' for key, url in matches)
        await ctx.send(embed=e)

        if ctx.guild and ctx.guild.id == DISCORD_API_ID:
            query = 'INSERT INTO rtfm (user_id) VALUES ($1) ON CONFLICT (user_id) DO UPDATE SET count = rtfm.count + 1;'
            await ctx.db.execute(query, ctx.author.id)

    @commands.group(aliases=['rtfd'], invoke_without_command=True)
    async def rtfm(self, ctx, *, obj: str = None):
        """Gives you a documentation link for a discord.py entity.

        Events, objects, and functions are all supported through a
        a cruddy fuzzy algorithm.
        """
        await self.do_rtfm(ctx, 'latest', obj)

    @rtfm.command(name='rewrite')
    async def rtfm_rewrite(self, ctx, *, obj: str = None):
        """Gives you a documentation link for a rewrite discord.py entity."""
        await self.do_rtfm(ctx, 'rewrite', obj)

    async def _member_stats(self, ctx, member, total_uses):
        e = discord.Embed(title='RTFM Stats')
        e.set_author(name=str(member), icon_url=member.avatar_url)

        query = 'SELECT count FROM rtfm WHERE user_id=$1;'
        record = await ctx.db.fetchrow(query, member.id)

        if record is None:
            count = 0
        else:
            count = record['count']

        e.add_field(name='Uses', value=count)
        e.add_field(name='Percentage', value=f'{count/total_uses:.2%} out of {total_uses}')
        e.colour = discord.Colour.blurple()
        await ctx.send(embed=e)

    @rtfm.command()
    async def stats(self, ctx, *, member: discord.Member = None):
        """Tells you stats about the ?rtfm command."""
        query = 'SELECT SUM(count) AS total_uses FROM rtfm;'
        record = await ctx.db.fetchrow(query)
        total_uses = record['total_uses']

        if member is not None:
            return await self._member_stats(ctx, member, total_uses)

        query = 'SELECT user_id, count FROM rtfm ORDER BY count DESC LIMIT 10;'
        records = await ctx.db.fetch(query)

        output = []
        output.append(f'**Total uses**: {total_uses}')

        # first we get the most used users
        if records:
            output.append(f'**Top {len(records)} users**:')

            for rank, (user_id, count) in enumerate(records, 1):
                user = self.bot.get_user(user_id)
                if rank != 10:
                    output.append(f'{rank}\u20e3 {user}: {count}')
                else:
                    output.append(f'\N{KEYCAP TEN} {user}: {count}')

        await ctx.send('\n'.join(output))

    def library_name(self, channel):
        # language_<name>
        name = channel.name
        index = name.find('_')
        if index != -1:
            name = name[index + 1:]
        return name.replace('-', '.')

    @commands.command()
    @commands.has_permissions(manage_roles=True)
    @is_discord_api()
    async def block(self, ctx, *, member: discord.Member):
        """Blocks a user from your channel."""

        reason = f'Block by {ctx.author} (ID: {ctx.author.id})'

        try:
            await ctx.channel.set_permissions(member, send_messages=False, reason=reason)
        except:
            await ctx.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.send('\N{THUMBS UP SIGN}')

    @commands.command()
    @commands.has_permissions(manage_roles=True)
    @is_discord_api()
    async def tempblock(self, ctx, duration: time.FutureTime, *, member: discord.Member):
        """Temporarily blocks a user from your channel.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2017-12-31".

        Note that times are in UTC.
        """

        reminder = self.bot.get_cog('Reminder')
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        timer = await reminder.create_timer(duration.dt, 'tempblock', ctx.guild.id, ctx.author.id,
                                                                      ctx.channel.id, member.id,
                                                                      connection=ctx.db)

        reason = f'Tempblock by {ctx.author} (ID: {ctx.author.id}) until {duration.dt}'

        try:
            await ctx.channel.set_permissions(member, send_messages=False, reason=reason)
        except:
            await ctx.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.send(f'Blocked {member} for {time.human_timedelta(duration.dt)}.')

    async def on_tempblock_timer_complete(self, timer):
        guild_id, mod_id, channel_id, member_id = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            # RIP x2
            return

        to_unblock = guild.get_member(member_id)
        if to_unblock is None:
            # RIP x3
            return

        moderator = guild.get_member(mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.get_user_info(mod_id)
            except:
                # request failed somehow
                moderator = f'Mod ID {mod_id}'
            else:
                moderator = f'{moderator} (ID: {mod_id})'
        else:
            moderator = f'{moderator} (ID: {mod_id})'


        reason = f'Automatic unblock from timer made on {timer.created_at} by {moderator}.'

        try:
            await channel.set_permissions(to_unblock, send_messages=None, reason=reason)
        except:
            pass

    @cache.cache()
    async def get_feeds(self, channel_id, *, connection=None):
        con = connection or self.bot.pool
        query = 'SELECT name, role_id FROM feeds WHERE channel_id=$1;'
        feeds = await con.fetch(query, channel_id)
        return {f['name']: f['role_id'] for f in feeds}

    @commands.group(name='feeds', invoke_without_command=True)
    @commands.guild_only()
    async def _feeds(self, ctx):
        """Shows the list of feeds that the channel has.

        A feed is something that users can opt-in to
        to receive news about a certain feed by running
        the `sub` command (and opt-out by doing the `unsub` command).
        You can publish to a feed by using the `publish` command.
        """

        feeds = await self.get_feeds(ctx.channel.id)

        if len(feeds) == 0:
            await ctx.send('This channel has no feeds.')
            return

        names = '\n'.join(f'- {r}' for r in feeds)
        await ctx.send(f'Found {len(feeds)} feeds.\n{names}')

    @_feeds.command(name='create')
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def feeds_create(self, ctx, *, name: str):
        """Creates a feed with the specified name.

        You need Manage Roles permissions to create a feed.
        """

        name = name.lower()

        if name in ('@everyone', '@here'):
            return await ctx.send('That is an invalid feed name.')

        query = 'SELECT role_id FROM feeds WHERE channel_id=$1 AND name=$2;'

        exists = await ctx.db.fetchrow(query, ctx.channel.id, name)
        if exists is not None:
            await ctx.send('This feed already exists.')
            return

        # create the role
        if ctx.guild.id == DISCORD_API_ID:
            role_name = self.library_name(ctx.channel) + ' ' + name
        else:
            role_name = name

        role = await ctx.guild.create_role(name=role_name, permissions=discord.Permissions.none())
        query = 'INSERT INTO feeds (role_id, channel_id, name) VALUES ($1, $2, $3);'
        await ctx.db.execute(query, role.id, ctx.channel.id, name)
        self.get_feeds.invalidate(self, ctx.channel.id)
        await ctx.send(f'{ctx.tick(True)} Successfully created feed.')

    @_feeds.command(name='delete', aliases=['remove'])
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def feeds_delete(self, ctx, *, feed: str):
        """Removes a feed from the channel.

        This will also delete the associated role so this
        action is irreversible.
        """

        query = 'DELETE FROM feeds WHERE channel_id=$1 AND name=$2 RETURNING *;'
        records = await ctx.db.fetch(query, ctx.channel.id, feed)
        self.get_feeds.invalidate(self, ctx.channel.id)

        if len(records) == 0:
            return await ctx.send('This feed does not exist.')

        for record in records:
            role = discord.utils.find(lambda r: r.id == record['role_id'], ctx.guild.roles)
            if role is not None:
                try:
                    await role.delete()
                except discord.HTTPException:
                    continue

        await ctx.send(f'{ctx.tick(True)} Removed feed.')

    async def do_subscription(self, ctx, feed, action):
        feeds = await self.get_feeds(ctx.channel.id)
        if len(feeds) == 0:
            await ctx.send('This channel has no feeds set up.')
            return

        if feed not in feeds:
            await ctx.send(f'This feed does not exist.\nValid feeds: {", ".join(feeds)}')
            return

        role_id = feeds[feed]
        role = discord.utils.find(lambda r: r.id == role_id, ctx.guild.roles)
        if role is not None:
            await action(role)
            await ctx.message.add_reaction(ctx.tick(True).strip('<:>'))
        else:
            await ctx.message.add_reaction(ctx.tick(False).strip('<:>'))

    @commands.command()
    @commands.guild_only()
    async def sub(self, ctx, *, feed: str):
        """Subscribes to the publication of a feed.

        This will allow you to receive updates from the channel
        owner. To unsubscribe, see the `unsub` command.
        """
        await self.do_subscription(ctx, feed, ctx.author.add_roles)

    @commands.command()
    @commands.guild_only()
    async def unsub(self, ctx, *, feed: str):
        """Unsubscribe to the publication of a feed.

        This will remove you from notifications of a feed you
        are no longer interested in. You can always sub back by
        using the `sub` command.
        """
        await self.do_subscription(ctx, feed, ctx.author.remove_roles)

    @commands.command()
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def publish(self, ctx, feed: str, *, content: str):
        """Publishes content to a feed.

        Everyone who is subscribed to the feed will be notified
        with the content. Use this to notify people of important
        events or changes.
        """
        feeds = await self.get_feeds(ctx.channel.id)
        feed = feed.lower()
        if feed not in feeds:
            await ctx.send('This feed does not exist.')
            return

        role = discord.utils.get(ctx.guild.roles, id=feeds[feed])
        if role is None:
            fmt = 'Uh.. a fatal error occurred here. The role associated with ' \
                  'this feed has been removed or not found. ' \
                  'Please recreate the feed.'
            await ctx.send(fmt)
            return

        # delete the message we used to invoke it
        try:
            await ctx.message.delete()
        except:
            pass

        # make the role mentionable
        await role.edit(mentionable=True)

        # then send the message..
        await ctx.send(f'{role.mention}: {content}'[:2000])

        # then make the role unmentionable
        await role.edit(mentionable=False)

    async def refresh_faq_cache(self):
        self.faq_entries = {}
        base_url = 'http://discordpy.readthedocs.io/en/latest/faq.html'
        async with self.bot.session.get(base_url) as resp:
            text = await resp.text(encoding='utf-8')

            root = etree.fromstring(text, etree.HTMLParser())
            nodes = root.findall(".//div[@id='questions']/ul[@class='simple']//ul/li/a")
            for node in nodes:
                self.faq_entries[''.join(node.itertext()).strip()] = base_url + node.get('href').strip()

    @commands.command()
    async def faq(self, ctx, *, query: str = None):
        """Shows an FAQ entry from the discord.py documentation"""
        if not hasattr(self, 'faq_entries'):
            await self.refresh_faq_cache()

        if query is None:
            return await ctx.send('http://discordpy.readthedocs.io/en/latest/faq.html')

        matches = fuzzy.extract_matches(query, self.faq_entries, scorer=fuzzy.partial_ratio, score_cutoff=40)
        if len(matches) == 0:
            return await ctx.send('Nothing found...')

        fmt = '\n'.join(f'**{key}**\n{value}' for key, _, value in matches)
        await ctx.send(fmt)

def setup(bot):
    bot.add_cog(API(bot))
