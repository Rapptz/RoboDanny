from discord.ext import commands
from .utils import db, checks
from .utils.paginator import Pages

class LazyEntity:
    """This is meant for use with the Paginator.

    It lazily computes __str__ when requested and
    caches it so it doesn't do the lookup again.
    """
    __slots__ = ('entity_id', 'guild', '_cache')

    def __init__(self, guild, entity_id):
        self.entity_id = entity_id
        self.guild = guild
        self._cache = None

    def __str__(self):
        if self._cache:
            return self._cache

        e = self.entity_id
        g = self.guild
        resolved = g.get_channel(e) or g.get_member(e)
        if resolved is None:
            self._cache = f'<Not Found: {e}>'
        else:
            self._cache = resolved.mention
        return self._cache

class ChannelOrMember(commands.Converter):
    async def convert(self, ctx, argument):
        try:
            return await commands.TextChannelConverter().convert(ctx, argument)
        except commands.BadArgument:
            return await commands.MemberConverter().convert(ctx, argument)

class Plonks(db.Table):
    id = db.PrimaryKeyColumn()
    guild_id = db.Column(db.Integer(big=True), index=True)

    # this can either be a channel_id or an author_id
    entity_id = db.Column(db.Integer(big=True), index=True, unique=True)

class Config:
    """Handles the bot's configuration system.

    This is how you disable or enable certain commands
    for your server or block certain channels or members.
    """

    def __init__(self, bot):
        self.bot = bot

    async def is_plonked(self, guild, member, *, channel=None, connection=None, check_bypass=True):
        if check_bypass and member.guild_permissions.manage_guild:
            return False

        connection = connection or self.bot.pool

        if channel is None:
            query = "SELECT 1 FROM plonks WHERE guild_id=$1 AND entity_id=$2;"
            row = await connection.fetchrow(query, guild.id, member.id)
        else:
            query = "SELECT 1 FROM plonks WHERE guild_id=$1 AND entity_id IN ($2, $3);"
            row = await connection.fetchrow(query, guild.id, member.id, channel.id)

        return row is not None

    async def __global_check_once(self, ctx):
        if ctx.guild is None:
            return True

        is_owner = await ctx.bot.is_owner(ctx.author)
        if is_owner:
            return True

        # see if they can bypass:
        bypass = ctx.author.guild_permissions.manage_guild
        if bypass:
            return True

        # check if we're plonked
        is_plonked = await self.is_plonked(ctx.guild, ctx.author, channel=ctx.channel,
                                                                  connection=ctx.db, check_bypass=False)

        return not is_plonked

    def __check(self, ctx):
        msg = ctx.message
        if checks.is_owner_check(msg):
            return True

        try:
            entry = self.config[msg.server.id]
        except (KeyError, AttributeError):
            return True
        else:
            name = ctx.command.qualified_name.split(' ')[0]
            return name not in entry

    async def _bulk_ignore_entries(self, ctx, entries):
        async with ctx.db.transaction():
            query = "SELECT entity_id FROM plonks WHERE guild_id=$1;"
            records = await ctx.db.fetch(query, ctx.guild.id)

            # we do not want to insert duplicates
            current_plonks = {r[0] for r in records}
            guild_id = ctx.guild.id
            to_insert = [(guild_id, e.id) for e in entries if e.id not in current_plonks]

            # do a bulk COPY
            await ctx.db.copy_records_to_table('plonks', columns=('guild_id', 'entity_id'), records=to_insert)

    @commands.group()
    async def config(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.invoke(ctx.bot.get_command('help'), cmd='config')

    @config.group(invoke_without_command=True, aliases=['plonk'])
    @checks.is_mod()
    async def ignore(self, ctx, *entities: ChannelOrMember):
        """Ignores text channels or members from using the bot.

        If no channel or member is specified, the current channel is ignored.

        Users with Manage Server can still use the bot, regardless of ignore
        status.

        To use this command you must have Manage Server permissions.
        """

        if len(entities) == 0:
            # shortcut for a single insert
            query = "INSERT INTO plonks (guild_id, entity_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;"
            await ctx.db.execute(query, ctx.guild.id, ctx.channel.id)
        else:
            await self._bulk_ignore_entries(ctx, entities)

        await ctx.send(ctx.tick(True))

    @ignore.command(name='list')
    @checks.is_mod()
    @commands.cooldown(2.0, 60.0, commands.BucketType.guild)
    async def ignore_list(self, ctx):
        """Tells you what channels or members are currently ignored in this server.

        To use this command you must have Manage Server permissions.
        """

        query = "SELECT entity_id FROM plonks WHERE guild_id=$1;"

        guild = ctx.guild
        records = await ctx.db.fetch(query, guild.id)

        if len(records) == 0:
            return await ctx.send('I am not ignoring anything here.')

        entries = [LazyEntity(guild, r[0]) for r in records]
        await ctx.release()

        try:
            p = Pages(self.bot, message=ctx.message, entries=entries, per_page=20)
            await p.paginate()
        except Exception as e:
            await ctx.send(str(e))

    @ignore.command(name='all')
    @checks.is_mod()
    async def _all(self, ctx):
        """Ignores every channel in the server from being processed.

        This works by adding every channel that the server currently has into
        the ignore list. If more channels are added then they will have to be
        ignored by using the ignore command.

        To use this command you must have Manage Server permissions.
        """
        await self._bulk_ignore_entries(ctx, ctx.guild.text_channels)
        await ctx.send('Successfully blocking all channels here.')

    @ignore.command(name='clear')
    @checks.is_mod()
    async def ignore_clear(self, ctx):
        """Clears all the currently set ignores.

        To use this command you must have Manage Server permissions.
        """

        query = "DELETE FROM plonks WHERE guild_id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        await ctx.send('Successfully cleared all ignores.')

    @config.group(pass_context=True, invoke_without_command=True, aliases=['unplonk'])
    @checks.is_mod()
    async def unignore(self, ctx, *entities: ChannelOrMember):
        """Allows channels or members to use the bot again.

        If nothing is specified, it unignores the current channel.

        To use this command you must have the Manage Server permission.
        """

        if len(entities) == 0:
            query = "DELETE FROM plonks WHERE guild_id=$1 AND entity_id=$2;"
            await ctx.db.execute(query, ctx.guild.id, ctx.channel.id)
        else:
            query = "DELETE FROM plonks WHERE guild_id=$1 AND entity_id = ANY($2::bigint[]);"
            entities = [c.id for c in entities]
            await ctx.db.execute(query, ctx.guild.id, entities)

        await ctx.send(ctx.tick(True))

    @unignore.command(name='all')
    @checks.is_mod()
    async def unignore_all(self, ctx):
        """An alias for ignore clear command."""
        await ctx.invoke(self.ignore_clear)

def setup(bot):
    bot.add_cog(Config(bot))
