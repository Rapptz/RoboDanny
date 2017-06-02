from discord.ext import commands
from .utils import config, checks

from collections import Counter

import discord
import asyncio
import datetime
import logging
import re
import io

log = logging.getLogger(__name__)

BLOB_GUILD_ID = '272885620769161216'
COUNCIL_LITE_ID = '300509002520068097'
EMOJI_REGEX = re.compile(r'<:.+?:([0-9]{15,21})>')

class BlobEmoji(commands.Converter):
    def convert(self):
        guild = self.ctx.bot.get_server(BLOB_GUILD_ID)
        emojis = {e.id: e for e in guild.emojis}

        m = EMOJI_REGEX.match(self.argument)
        if m is not None:
            emoji = emojis.get(m.group(1))
        elif self.argument.isdigit():
            emoji = emojis.get(self.argument)
        else:
            emoji = discord.utils.find(lambda e: e.name == self.argument, emojis.values())

        if emoji is None:
            raise commands.BadArgument('Not a valid blob emoji.')
        return emoji

def is_council_lite_or_higher():
    def pred(ctx):
        server = ctx.message.server
        if server is None or server.id != BLOB_GUILD_ID:
            return False

        council_lite = discord.utils.find(lambda r: r.id == COUNCIL_LITE_ID, server.roles)
        return ctx.message.author.top_role >= council_lite
    return commands.check(pred)

def usage_per_day(dt, usages):
    tracking_started = datetime.datetime(2017, 3, 31)
    now = datetime.datetime.utcnow()
    if dt < tracking_started:
        base = tracking_started
    else:
        base = dt

    days = (now - base).total_seconds() / 86400 # 86400 seconds in a day
    if int(days) == 0:
        return usages
    return usages / days

class Emoji:
    """Custom emoji tracking statistics for Wolfiri"""

    def __init__(self, bot):
        self.bot = bot

        # guild_id: data
        # where data is
        # emoji_id: count
        self.config = config.Config('emoji_statistics.json')

    # gonna lazily re-use this for disabling all ! prefixes people request lol
    def __check(self, ctx):
        server = ctx.message.server
        if server is not None:
            if server.id in (BLOB_GUILD_ID, '234463897829113856'):
                #                            ^ 187428134998507521
                return ctx.prefix == '?'
        return True

    async def do_redirect(self, message):
        if len(message.attachments) == 0:
            return

        async with self.bot.http.session.get(message.attachments[0]['url']) as resp:
            if resp.status != 200:
                return

            data = await resp.read()

        fmt = 'Suggestion from {0.author}: {0.clean_content}'.format(message)
        ch = self.bot.get_channel('305838206119575552')
        await self.bot.send_file(ch, io.BytesIO(data), filename='unknown.png', content=fmt)

    async def on_message(self, message):
        if message.server is None:
            return

        if message.author.bot:
            return # no bots.

        # handle the redirection from #suggestions
        if message.channel.id == '295012914564169728':
            await self.do_redirect(message)

        matches = EMOJI_REGEX.findall(message.content)
        if not matches:
            return

        db = self.config.get(message.server.id, {})
        for emoji_id in set(matches):
            try:
                count = db[emoji_id]
            except KeyError:
                db[emoji_id] = 1
            else:
                db[emoji_id] = count + 1

        await self.config.put(message.server.id, db)

    async def on_server_update(self, before, after):
        if before.id != BLOB_GUILD_ID:
            return

        await self.on_server_emojis_update(before.emojis, after.emojis)

    async def on_server_emojis_update(self, before, after):
        # I designed this event and I god damn hate it.
        # First, an exhibit on how annoying it is to fetch
        # the server:

        # we only care when an emoji is added
        lookup = { e.id for e in before }
        added = [e for e in after if e.id not in lookup and len(e.roles) == 0]
        if len(added) == 0:
            return

        server = added[0].server
        log.info('Server %s has added %s emojis.', server, len(added))
        if server.id != BLOB_GUILD_ID:
            return # not the server we care about

        # this is the backup channel
        channel = self.bot.get_channel('305841865293430795')
        if channel is None:
            return

        for emoji in added:
            async with self.bot.http.session.get(emoji.url) as resp:
                if resp.status != 200:
                    continue

                data = await resp.read()
                await self.bot.send_file(channel, io.BytesIO(data), filename=emoji.name + '.png', content=emoji.name)
                await asyncio.sleep(1)

    def get_all_blob_stats(self):
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis if len(e.roles) == 0 }
        total_usage = Counter()
        for data in self.config.all().values():
            total_usage.update(data)

        blob_usage = Counter({e: 0 for e in blob_ids})
        blob_usage.update(x for x in total_usage.elements() if x in blob_ids)

        e = discord.Embed(title='Blob Statistics', colour=0xf1c40f)

        common = blob_usage.most_common()
        total_count = sum(blob_usage.values())
        global_usage = sum(total_usage.values())
        fmt = '{0} ({1:.2%} of all emoji usage)'

        e.add_field(name='Total Usage', value=fmt.format(total_count, total_count / global_usage))

        def elem_to_string(key, count):
            elem = blob_ids.get(key)
            per_day = usage_per_day(elem.created_at, count)
            return '{0}: {1} times, {3:.2f}/day ({2:.2%})'.format(elem, count, count / total_count, per_day)

        top = [elem_to_string(key, count) for key, count in common[0:7]]
        bottom = [elem_to_string(key, count) for key, count in common[-7:]]
        e.add_field(name='Most Common', value='\n'.join(top), inline=False)
        e.add_field(name='Least Common', value='\n'.join(bottom), inline=False)
        return e

    def get_blob_stats_for(self, emoji):
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis if len(e.roles) == 0 }

        e = discord.Embed(colour=0xf1c40f, title='Statistics')
        total_usage = Counter()
        for data in self.config.all().values():
            total_usage.update(data)

        blob_usage = Counter({e: 0 for e in blob_ids})
        blob_usage.update(x for x in total_usage.elements() if x in blob_ids)
        usage = blob_usage.get(emoji.id)
        total = sum(blob_usage.values())

        rank = None
        for (index, (x, _)) in enumerate(blob_usage.most_common()):
            if x == emoji.id:
                rank = index + 1
                break

        e.add_field(name='Emoji', value=emoji)
        e.add_field(name='Usage', value='{0}, {2:.2f}/day ({1:.2%})'.format(usage, usage / total,
                                                                            usage_per_day(emoji.created_at, usage)))
        e.add_field(name='Rank', value=rank)
        return e

    @commands.group(hidden=True, invoke_without_command=True)
    async def blobstats(self, *, emoji: BlobEmoji = None):
        """Usage statistics of blobs."""
        if emoji is None:
            e = self.get_all_blob_stats()
        else:
            e = self.get_blob_stats_for(emoji)

        await self.bot.say(embed=e)

    @blobstats.error
    async def blobstats_error(self, error, ctx):
        if isinstance(error, commands.BadArgument):
            await self.bot.say(str(error))

    @blobstats.command(hidden=True, name='server')
    async def blobstats_server(self, *, server_id: str = None):
        """What server uses blobs the most?

        Useful for detecting abuse as well.
        """
        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        blob_ids = {e.id: e for e in blob_guild.emojis if len(e.roles) == 0 }

        per_guild = Counter()

        # RIP
        for guild_id, data in self.config.all().items():
            for key in blob_ids:
                per_guild[guild_id] += data.get(key, 0)

        total_usage = sum(per_guild.values())

        e = discord.Embed(colour=0xf1c40f, title='Server Statistics')

        if server_id is not None:
            guild = self.bot.get_server(server_id)
            total = per_guild[server_id]
            counter = Counter({k: v for k, v in self.config.get(server_id, {}).items() if k in blob_ids})
            top = counter.most_common(10)
            e.title = guild.name if guild else 'Unknown Guild?'
            e.add_field(name='Usage', value='{0} ({1:.2%})'.format(total, total / total_usage))
            if guild and guild.me:
                e.add_field(name='Per Day', value=usage_per_day(guild.me.joined_at, total))
                e.set_footer(text='Joined on')
                e.timestamp = guild.me.joined_at

                def elem_to_string(key, count):
                    elem = blob_ids.get(key)
                    per_day = usage_per_day(guild.me.joined_at, count)
                    return '{0}: {1} times, {3:.2f}/day ({2:.2%})'.format(elem, count, count / total, per_day)

                e.description = '\n'.join(elem_to_string(k, v) for k, v in top)

            return await self.bot.say(embed=e)

        top_ten = per_guild.most_common(10)
        for top, count in top_ten:
            guild = self.bot.get_server(top)
            percent = count / total_usage
            if guild is None:
                name = 'Unknown Guild: ' + top
                value = '{0} ({1:.2%})'.format(count, percent)
            else:
                name = '{0} (ID: {0.id})'.format(guild)
                value = '{0}, {1:.2f}/day ({2:.2%})'.format(count, usage_per_day(guild.me.joined_at, count), percent)
            e.add_field(name=name, value=value, inline=False)

        await self.bot.say(embed=e)

    @commands.command(hidden=True)
    @checks.is_owner()
    async def clear_emoji_data(self, *, server_id):
        """Deletes a server's emoji data."""

        if self.config.get(server_id) is not None:
            await self.config.remove(server_id)
            await self.bot.say('Deleted.')
        else:
            await self.bot.say('No data found.')

    @commands.command(pass_context=True, aliases=['blobpost'], hidden=True)
    @checks.is_in_servers(BLOB_GUILD_ID)
    @checks.admin_or_permissions(administrator=True)
    async def blobsort(self, ctx):
        """Sorts the blob post."""
        emojis = sorted([e.name for e in ctx.message.server.emojis if len(e.roles) == 0])
        fp = io.BytesIO()
        pages = [emojis[i:i + 30] for i in range(0, len(emojis), 30)]

        for number, page in enumerate(pages, 1):
            fmt = 'Page %s\n' % number
            fp.write(fmt.encode('utf-8'))
            for emoji in page:
                fmt = ':{0}: = `:{0}:`\n'.format(emoji)
                fp.write(fmt.encode('utf-8'))

            fp.write(b'\n')

        fp.seek(0)
        await self.bot.upload(fp, filename='blob_posts.txt')

    @commands.command(pass_context=True, hidden=True)
    @commands.cooldown(3, 60.0, commands.BucketType.server)
    async def blobprune(self, ctx):
        """Good candidates to prune."""

        await self.bot.type()

        blob_guild = self.bot.get_server(BLOB_GUILD_ID)
        seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        blob_ids = {e.id: e for e in blob_guild.emojis if len(e.roles) == 0 }
        total_usage = Counter()
        for data in self.config.all().values():
            total_usage.update(data)

        blob_usage = Counter({e: 0 for e in blob_ids})
        blob_usage.update(x for x in total_usage.elements() if x in blob_ids)

        common = blob_usage.most_common()
        total_count = sum(blob_usage.values())

        data = [(blob_ids.get(k), v) for k, v in common if blob_ids.get(k).created_at < seven_days_ago]

        def elem_to_string(elem, count):
            per_day = usage_per_day(elem.created_at, count)
            fmt = '{0.created_at} -- {0.name} -- {1} times -- {2:.2f}/day -- {3:.2%} blob usage'
            return fmt.format(elem, count, per_day, count / total_count)

        data = '\n'.join(elem_to_string(k, v) for k, v in data).encode('utf-8')

        # post to hastebin
        async with self.bot.http.session.post('https://hastebin.com/documents', data=data) as resp:
            if resp.status != 200:
                return await self.bot.say('Could not post to hastebin, sorry.')

            key = (await resp.json())['key']
            await self.bot.say('https://hastebin.com/' + key)

    @commands.command(pass_context=True, hidden=True)
    @is_council_lite_or_higher()
    async def blobpoll(self, ctx, message_id: str, *emojis: str):
        """React to a post with the emojis given."""

        # disambiguate the message ID...

        # check if it's in the message cache
        message = discord.utils.find(lambda m: m.id == message_id, self.bot.messages)
        if message is None:
            possible_channels = ('294924110130184193', '289847856033169409', '294928568532729856', BLOB_GUILD_ID)
                                 # blob-council-queue   #approval-queue       #blob-council-chat   #general
            for channel_id in possible_channels:
                try:
                    ch = self.bot.get_channel(channel_id)
                    message = await self.bot.get_message(ch, message_id)
                except Exception:
                    continue
                else:
                    break
            else:
                return await self.bot.say('Could not find message, sorry.')
        elif message.server is None or message.server.id != BLOB_GUILD_ID:
            return await self.bot.say('This message does not belong in the blob guild...')

        if not message.channel.permissions_for(message.server.me).add_reactions:
            return await self.bot.say('Do not have permissions to react.')

        for emoji in emojis:
            await self.bot.add_reaction(message, emoji.strip('<:>'))

        await self.bot.say('<:blobokhand:304461480802123777>')

def setup(bot):
    bot.add_cog(Emoji(bot))
