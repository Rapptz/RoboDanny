from .utils import db, checks, formats, cache
from .utils.paginator import Pages

from discord.ext import commands
import json
import re
import datetime
import discord
import asyncio
import traceback
import asyncpg

class UnavailableTagCommand(commands.CheckFailure):
    def __str__(self):
        return 'Sorry. This command is unavailable in private messages.\n' \
               'Consider browsing or using the tag box instead.\nSee ?tag box for more info.'

class UnableToUseBox(commands.CheckFailure):
    def __str__(self):
        return 'You do not have permissions to use the tag box. Manage Messages required!'

def suggest_box():
    """Custom commands.guild_only with different error checking."""
    def pred(ctx):
        if ctx.guild is None:
            raise UnavailableTagCommand()
        return True
    return commands.check(pred)

def can_use_box():
    def pred(ctx):
        if ctx.guild is None:
            return True
        if ctx.author.id == ctx.bot.owner_id:
            return True

        has_perms = ctx.channel.permissions_for(ctx.author).manage_messages
        if not has_perms:
            raise UnableToUseBox()

        return True
    return commands.check(pred)

# The tag data is heavily duplicated (denormalized) and heavily indexed to speed up
# retrieval at the expense of making inserts a little bit slower. This is a fine trade-off
# because tags are retrieved much more often than created.

class TagsTable(db.Table, table_name='tags'):
    id = db.PrimaryKeyColumn()

    # we will create more indexes manually
    name = db.Column(db.String, index=True)

    content = db.Column(db.String)
    owner_id = db.Column(db.Integer(big=True))
    uses = db.Column(db.Integer, default=0)
    location_id = db.Column(db.Integer(big=True), index=True)
    created_at = db.Column(db.Datetime, default="now() at time zone 'utc'")

    @classmethod
    def create_table(cls, *, exists_ok=True):
        statement = super().create_table(exists_ok=exists_ok)

        # create the indexes
        sql = "CREATE INDEX IF NOT EXISTS tags_name_trgm_idx ON tags USING GIN (name gin_trgm_ops);\n" \
              "CREATE INDEX IF NOT EXISTS tags_name_lower_idx ON tags (LOWER(name));\n" \
              "CREATE UNIQUE INDEX IF NOT EXISTS tags_uniq_idx ON tags (LOWER(name), location_id);"

        return statement + '\n' + sql

class TagLookup(db.Table, table_name='tag_lookup'):
    id = db.PrimaryKeyColumn()

    # we will create more indexes manually
    name = db.Column(db.String, index=True)
    location_id = db.Column(db.Integer(big=True), index=True)

    owner_id = db.Column(db.Integer(big=True))
    created_at = db.Column(db.Datetime, default="now() at time zone 'utc'")
    tag_id = db.Column(db.ForeignKey('tags', 'id'))

    @classmethod
    def create_table(cls, *, exists_ok=True):
        statement = super().create_table(exists_ok=exists_ok)

        # create the indexes
        sql = "CREATE INDEX IF NOT EXISTS tag_lookup_name_trgm_idx ON tag_lookup USING GIN (name gin_trgm_ops);\n" \
              "CREATE INDEX IF NOT EXISTS tag_lookup_name_lower_idx ON tag_lookup (LOWER(name));\n" \
              "CREATE UNIQUE INDEX IF NOT EXISTS tag_lookup_uniq_idx ON tag_lookup (LOWER(name), location_id);"

        return statement + '\n' + sql

class TagName(commands.clean_content):
    def __init__(self, *, lower=False):
        self.lower = lower
        super().__init__()

    async def convert(self, ctx, argument):
        converted = await super().convert(ctx, argument)
        lower = converted.lower().strip()

        if not lower:
            raise commands.BadArgument('Missing tag name.')

        if len(lower) > 100:
            raise commands.BadArgument('Tag name is a maximum of 100 characters.')

        first_word, _, _ = lower.partition(' ')

        # get tag command.
        root = ctx.bot.get_command('tag')
        if first_word in root.all_commands:
            raise commands.BadArgument('This tag name starts with a reserved word.')

        return converted if not self.lower else lower

class Tags:
    """The tag related commands."""

    def __init__(self, bot):
        self.bot = bot

    async def __error(self, ctx, error):
        if isinstance(error, (UnavailableTagCommand, UnableToUseBox)):
            await ctx.send(error)
        elif isinstance(error, commands.UserInputError):
            if ctx.command.qualified_name == 'tag':
                await ctx.acquire()
                await ctx.show_help()
            else:
                await ctx.send(error)

    # @cache.cache()
    # async def get_tag_config(self, guild_id, *, connection=None):
    #     # tag config is stored as a special server-wide tag, 'config'
    #     # this 'config' value is serialised as JSON in the content

    #     query = """SELECT content FROM tags WHERE name = 'config' AND location_id = $1;"""
    #     con = connection if connection else self.bot.pool
    #     record = await con.fetchrow(query, guild_id)
    #     if record is None:
    #         return TagConfig({})
    #     return TagConfig(json.loads(record['content']))

    async def get_possible_tags(self, guild, *, connection=None):
        """Returns a list of Records of possible tags that the guild can execute.

        If this is a private message then only the generic tags are possible.
        Server specific tags will override the generic tags.
        """

        con = connection or self.bot.pool
        if guild is None:
            query = """SELECT name, content FROM tags WHERE location_id IS NULL;"""
            return await con.fetch(query)

        query = """SELECT name, content FROM tags WHERE location_id=$1;"""
        return con.fetch(query, guild.id)

    async def get_random_tag(self, guild, *, connection=None):
        """Returns a random tag."""

        con = connection or self.bot.pool
        pred = 'location_id IS NULL' if guild is None else 'location_id=$1'
        query = f"""SELECT name, content
                    FROM tags
                    WHERE {pred}
                    OFFSET FLOOR(RANDOM() * (
                        SELECT COUNT(*)
                        FROM tags
                        WHERE {pred}
                    ))
                    LIMIT 1;
                 """

        if guild is None:
            return await con.fetchrow(query)
        else:
            return await con.fetchrow(query, guild.id)

    async def get_tag(self, guild_id, name, *, connection=None):
        def disambiguate(rows, query):
            if rows is None or len(rows) == 0:
                raise RuntimeError('Tag not found. Maybe check the tag box?')

            names = '\n'.join(r['name'] for r in rows)
            raise RuntimeError(f'Tag not found. Did you mean...\n{names}')

        con = connection or self.bot.pool

        query = """SELECT tags.name, tags.content
                   FROM tag_lookup
                   INNER JOIN tags ON tags.id = tag_lookup.tag_id
                   WHERE tag_lookup.location_id=$1 AND LOWER(tag_lookup.name)=$2;
                """

        row = await con.fetchrow(query, guild_id, name)
        if row is None:
            query = """SELECT     tag_lookup.name
                       FROM       tag_lookup
                       WHERE      tag_lookup.location_id=$1 AND tag_lookup.name % $2
                       ORDER BY   similarity(tag_lookup.name, $2) DESC
                       LIMIT 3;
                    """

            return disambiguate(await con.fetch(query, guild_id, name), name)
        else:
            return row

    @commands.group(invoke_without_command=True)
    @suggest_box()
    async def tag(self, ctx, *, name: TagName(lower=True)):
        """Allows you to tag text for later retrieval.

        If a subcommand is not called, then this will search the tag database
        for the tag requested.
        """

        try:
            tag = await self.get_tag(ctx.guild.id, name, connection=ctx.db)
        except RuntimeError as e:
            return await ctx.send(e)

        await ctx.send(tag['content'])

        # update the usage
        query = "UPDATE tags SET uses = uses + 1 WHERE name = $1 AND (location_id=$2 OR location_id IS NULL);"
        await ctx.db.execute(query, tag['name'], ctx.guild.id)

    @tag.command(aliases=['add'])
    @suggest_box()
    async def create(self, ctx, name: TagName, *, content: commands.clean_content):
        """Creates a new tag owned by you.

        This tag is server-specific and cannot be used in other servers.
        For global tags that others can use, consider using the tag box.

        Note that server moderators can delete your tag.
        """

        # due to our denormalized design, I need to insert the tag in two different
        # tables, make sure it's in a transaction so if one of the inserts fail I
        # can act upon it
        query = """WITH tag_insert AS (
                        INSERT INTO tags (name, content, owner_id, location_id)
                        VALUES ($1, $2, $3, $4)
                        RETURNING id
                    )
                    INSERT INTO tag_lookup (name, owner_id, location_id, tag_id)
                    VALUES ($1, $3, $4, (SELECT id FROM tag_insert));
                """

        # since I'm checking for the exception type and acting on it, I need
        # to use the manual transaction blocks

        tr = ctx.db.transaction()
        await tr.start()

        try:
            await ctx.db.execute(query, name, content, ctx.author.id, ctx.guild.id)
        except asyncpg.UniqueViolationError:
            await tr.rollback()
            await ctx.send('This tag already exists.')
        except:
            await tr.rollback()
            await ctx.send('Could not create tag.')
        else:
            await tr.commit()
            await ctx.send(f'Tag {name} successfully created.')

    @tag.command()
    @suggest_box()
    async def alias(self, ctx, new_name: TagName, *, old_name: TagName):
        """Creates an alias for a pre-existing tag.

        You own the tag alias. However, when the original
        tag is deleted the alias is deleted as well.

        Tag aliases cannot be edited. You must delete
        the alias and remake it to point it to another
        location.
        """

        query = """INSERT INTO tag_lookup (name, owner_id, location_id, tag_id)
                   SELECT $1, $4, tag_lookup.location_id, tag_lookup.tag_id
                   FROM tag_lookup
                   WHERE tag_lookup.location_id=$3 AND LOWER(tag_lookup.name)=$2;
                """

        try:
            status = await ctx.db.execute(query, new_name, old_name.lower(), ctx.guild.id, ctx.author.id)
        except asyncpg.UniqueViolationError:
            await ctx.send('A tag with this name already exists.')
        else:
            # The status returns INSERT N M, where M is the number of rows inserted.
            if status[-1] == '0':
                await ctx.send(f'A tag with the name of "{old_name}" does not exist.')
            else:
                await ctx.send(f'Tag alias "{new_name}" that points to "{old_name}" successfully created.')

    @tag.command(ignore_extra=False)
    @suggest_box()
    async def make(self, ctx):
        """Interactive makes a tag for you.

        This walks you through the process of creating a tag with
        its name and its content. This works similar to the tag
        create command.
        """

        await ctx.send('Hello. What would you like the name tag to be?')

        converter = TagName()
        original = ctx.message

        def check(msg):
            return msg.author == ctx.author and ctx.channel == msg.channel

        # release the connection back to the pool to wait for our user
        await ctx.release()

        try:
            name = await self.bot.wait_for('message', timeout=30.0, check=check)
        except asyncio.TimeoutError:
            return await ctx.send('You took long. Goodbye.')

        try:
            ctx.message = name
            name = await converter.convert(ctx, name.content)
        except commands.BadArgument as e:
            return await ctx.send(f'{e}. Redo the command "{ctx.prefix}tag make" to retry.')
        finally:
            ctx.message = original

        # reacquire our connection since we need the query
        await ctx.acquire()

        # it's technically kind of expensive to do two queries like this
        # i.e. one to check if it exists and then another that does the insert
        # while also checking if it exists due to the constraints,
        # however for UX reasons I might as well do it.

        query = """SELECT 1 FROM tags WHERE location_id=$1 AND LOWER(name)=$2;"""
        row = await ctx.db.fetchrow(query, ctx.guild.id, name.lower())
        if row is not None:
            return await ctx.send('Sorry. A tag with that name already exists. ' \
                                 f'Redo the command "{ctx.prefix}tag make" to retry.')


        await ctx.send(f'Neat. So the name is {name}. What about the tag\'s content?')

        # release while we wait for response
        await ctx.release()

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=300.0)
        except asyncio.TimeoutError:
            return await ctx.send('You took too long. Goodbye.')

        if msg.content:
            clean_content = await commands.clean_content().convert(ctx, msg.content)
        else:
            # fast path I guess?
            clean_content = msg.content

        if msg.attachments:
            clean_content = f'{clean_content}\n{msg.attachments[0].url}'

        await ctx.acquire()
        await ctx.invoke(self.create, name=name, content=clean_content)

    @make.error
    async def tag_make_error(self, ctx, error):
        if isinstance(error, commands.TooManyArguments):
            await self.bot.say(f'Please call just {ctx.prefix}tag make')

    async def guild_tag_stats(self, ctx):
        # I'm not sure on how to do this with a single query
        # so I'm splitting it up into different queries

        e = discord.Embed(colour=discord.Colour.blurple(), title='Tag Stats')
        e.set_footer(text='These statistics are server-specific.')

        # top 3 commands
        query = """SELECT
                       name,
                       uses,
                       COUNT(*) OVER () AS "Count",
                       SUM(uses) OVER () AS "Total Uses"
                   FROM tags
                   WHERE location_id=$1
                   ORDER BY uses DESC
                   LIMIT 3;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)
        if not records:
            e.description = 'No tag statistics here.'
        else:
            total = records[0]
            e.description = f'{total["Count"]} tags, {total["Total Uses"]} tag uses'

        if len(records) < 3:
            # fill with data to ensure that we have a minimum of 3
            records.extend((None, None, None, None) for i in range(0, 3 - len(records)))


        def emojize(seq):
            emoji = 129351 # ord(':first_place:')
            for index, value in enumerate(seq):
                yield chr(emoji + index), value

        value = '\n'.join(f'{emoji}: {name} ({uses} uses)' if name else f'{emoji}: Nothing!'
                          for (emoji, (name, uses, _, _)) in emojize(records))

        e.add_field(name='Top Tags', value=value, inline=False)

        # tag users
        query = """SELECT
                       COUNT(*) AS tag_uses,
                       author_id
                   FROM commands
                   WHERE guild_id=$1 AND command='tag'
                   GROUP BY author_id
                   ORDER BY COUNT(*) DESC
                   LIMIT 3;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        if len(records) < 3:
            # fill with data to ensure that we have a minimum of 3
            records.extend((None, None) for i in range(0, 3 - len(records)))

        value = '\n'.join(f'{emoji}: <@{author_id}> ({uses} times)' if author_id else f'{emoji}: No one!'
                          for (emoji, (uses, author_id)) in emojize(records))
        e.add_field(name='Top Tag Users', value=value, inline=False)

        # tag creators

        query = """SELECT
                       COUNT(*) AS "Tags",
                       owner_id
                   FROM tags
                   WHERE location_id=$1
                   GROUP BY owner_id
                   ORDER BY COUNT(*) DESC
                   LIMIT 3;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        if len(records) < 3:
            # fill with data to ensure that we have a minimum of 3
            records.extend((None, None) for i in range(0, 3 - len(records)))

        value = '\n'.join(f'{emoji}: <@{owner_id}> ({count} tags)' if owner_id else f'{emoji}: No one!'
                          for (emoji, (count, owner_id)) in emojize(records))
        e.add_field(name='Top Tag Creators', value=value, inline=False)

        await ctx.send(embed=e)

    async def member_tag_stats(self, ctx, member):
        e = discord.Embed(colour=discord.Colour.blurple())
        e.set_author(name=str(member), icon_url=member.avatar_url)
        e.set_footer(text='These statistics are server-specific.')

        query = """SELECT COUNT(*)
                   FROM commands
                   WHERE guild_id=$1 AND command='tag' AND author_id=$2
                """

        count = await ctx.db.fetchrow(query, ctx.guild.id, member.id)

        # top 3 commands and total tags/uses
        query = """SELECT
                       name,
                       uses,
                       COUNT(*) OVER() AS "Count",
                       SUM(uses) OVER () AS "Uses"
                   FROM tags
                   WHERE location_id=$1 AND owner_id=$2
                   ORDER BY uses DESC
                   LIMIT 3;
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)

        if len(records) > 1:
            owned = records[0]['Count']
            uses = records[0]['Uses']
        else:
            owned = 'None'
            uses = 0

        e.add_field(name='Owned Tags', value=owned)
        e.add_field(name='Owned Tag Uses', value=uses)
        e.add_field(name='Tag Command Uses', value=count[0])

        if len(records) < 3:
            # fill with data to ensure that we have a minimum of 3
            records.extend((None, None, None, None) for i in range(0, 3 - len(records)))

        emoji = 129351 # ord(':first_place:')

        for (offset, (name, uses, _, _)) in enumerate(records):
            if name:
                value = f'{name} ({uses} uses)'
            else:
                value = 'Nothing!'

            e.add_field(name=f'{chr(emoji + offset)} Owned Tag', value=value)

        await ctx.send(embed=e)

    @tag.command()
    @suggest_box()
    async def stats(self, ctx, *, member: discord.Member = None):
        """Gives tag statistics for a member or the server."""

        if member is None:
            await self.guild_tag_stats(ctx)
        else:
            await self.member_tag_stats(ctx, member)

    @tag.command()
    @suggest_box()
    async def edit(self, ctx, name: TagName(lower=True), *, content: commands.clean_content):
        """Modifies an existing tag that you own.

        This command completely replaces the original text. If
        you want to get the old text back, consider using the
        tag raw command.
        """

        query = "UPDATE tags SET content=$1 WHERE LOWER(name)=$2 AND location_id=$3 AND owner_id=$4;"
        status = await ctx.db.execute(query, content, name, ctx.guild.id, ctx.author.id)

        # the status returns UPDATE <count>
        # if the <count> is 0, then nothing got updated
        # probably due to the WHERE clause failing

        if status[-1] == '0':
            await ctx.send('Could not edit that tag. Are you sure it exists and you own it?')
        else:
            await ctx.send('Successfully edited tag.')

    @tag.command(aliases=['delete'])
    @suggest_box()
    async def remove(self, ctx, *, name: TagName(lower=True)):
        """Removes a tag that you own.

        The tag owner can always delete their own tags. If someone requests
        deletion and has Manage Server permissions then they can also
        delete it.

        Deleting a tag will delete all of its aliases as well.
        """

        bypass_owner_check = ctx.author.id == self.bot.owner_id or ctx.author.guild_permissions.manage_messages
        clause = 'LOWER(name)=$1 AND location_id=$2'

        if bypass_owner_check:
            args = [name, ctx.guild.id]
        else:
            args = [name, ctx.guild.id, ctx.author.id]
            clause = f'{clause} AND owner_id=$3'

        query = f'DELETE FROM tag_lookup WHERE {clause} RETURNING tag_id;'
        deleted = await ctx.db.fetchrow(query, *args)

        if deleted is None:
            await ctx.send('Could not delete tag. Either it does not exist or you do not have permissions to do so.')
            return

        args.append(deleted[0])
        query = f'DELETE FROM tags WHERE id=${len(args)} AND {clause};'
        status = await ctx.db.execute(query, *args)

        # the status returns DELETE <count>, similar to UPDATE above
        if status[-1] == '0':
            # this is based on the previous delete above
            await ctx.send('Tag alias successfully deleted.')
        else:
            await ctx.send('Tag and corresponding aliases successfully deleted.')

    async def _send_alias_info(self, ctx, record):
        embed = discord.Embed(colour=discord.Colour.blurple())

        owner_id = record['lookup_owner_id']
        embed.title = record['lookup_name']
        embed.timestamp = record['lookup_created_at']
        embed.set_footer(text='Alias created at')

        user = self.bot.get_user(owner_id) or (await self.bot.get_user_info(owner_id))
        embed.set_author(name=str(user), icon_url=user.avatar_url)

        embed.add_field(name='Owner', value=f'<@{owner_id}>')
        embed.add_field(name='Original', value=record['name'])
        await ctx.send(embed=embed)

    async def _send_tag_info(self, ctx, record):
        embed = discord.Embed(colour=discord.Colour.blurple())

        owner_id = record['owner_id']
        embed.title = record['name']
        embed.timestamp = record['created_at']
        embed.set_footer(text='Tag created at')

        user = self.bot.get_user(owner_id) or (await self.bot.get_user_info(owner_id))
        embed.set_author(name=str(user), icon_url=user.avatar_url)

        embed.add_field(name='Owner', value=f'<@{owner_id}>')
        embed.add_field(name='Uses', value=record['uses'])

        query = """SELECT (
                       SELECT COUNT(*)
                       FROM tags second
                       WHERE (second.uses, second.id) >= (first.uses, first.id)
                         AND second.location_id = first.location_id
                   ) AS rank
                   FROM tags first
                   WHERE first.id=$1
                """

        rank = await ctx.db.fetchrow(query, record['id'])

        if rank is not None:
            embed.add_field(name='Rank', value=rank['rank'])

        await ctx.send(embed=embed)

    @tag.command( aliases=['owner'])
    @suggest_box()
    async def info(self, ctx, *, name: TagName(lower=True)):
        """Retrieves info about a tag.

        The info includes things like the owner and how many times it was used.
        """

        query = """SELECT
                       tag_lookup.name <> tags.name AS "Alias",
                       tag_lookup.name AS lookup_name,
                       tag_lookup.created_at AS lookup_created_at,
                       tag_lookup.owner_id AS lookup_owner_id,
                       tags.*
                   FROM tag_lookup
                   INNER JOIN tags ON tag_lookup.tag_id = tags.id
                   WHERE LOWER(tag_lookup.name)=$1 AND tag_lookup.location_id=$2
                """

        record = await ctx.db.fetchrow(query, name, ctx.guild.id)
        if record is None:
            return await ctx.send('Tag not found.')

        if record['Alias']:
            await self._send_alias_info(ctx, record)
        else:
            await self._send_tag_info(ctx, record)

    @tag.command(pass_context=True)
    @suggest_box()
    async def raw(self, ctx, *, name: TagName(lower=True)):
        """Gets the raw content of the tag.

        This is with markdown escaped. Useful for editing.
        """

        try:
            tag = await self.get_tag(ctx.guild.id, name, connection=ctx.db)
        except RuntimeError as e:
            return await ctx.send(e)

        transformations = {
            re.escape(c): '\\' + c
            for c in ('*', '`', '_', '~', '\\', '<')
        }

        def replace(obj):
            return transformations.get(re.escape(obj.group(0)), '')

        pattern = re.compile('|'.join(transformations.keys()))
        await ctx.send(pattern.sub(replace, tag['content']))

    @tag.command(name='list')
    @suggest_box()
    async def _list(self, ctx, *, member: discord.Member = None):
        """Lists all the tags that belong to you or someone else."""

        member = member or ctx.author

        query = """SELECT name
                   FROM tag_lookup
                   WHERE location_id=$1 AND owner_id=$2
                   ORDER BY name
                """

        rows = await ctx.db.fetch(query, ctx.guild.id, member.id)
        await ctx.release()

        if rows:
            try:
                p = Pages(ctx, entries=tuple(r[0] for r in rows))
                p.embed.set_author(name=member.display_name, icon_url=member.avatar_url)
                await p.paginate()
            except Exception as e:
                await ctx.send(e)
        else:
            await ctx.send(f'{member} has no tags.')

    @commands.command()
    @suggest_box()
    async def tags(self, ctx, *, member: discord.Member = None):
        """An alias for tag list command."""
        await ctx.invoke(self._list, member=member)

    @tag.command(name='all')
    @suggest_box()
    async def _all(self, ctx):
        """Lists all server-specific tags for this server."""

        query = """SELECT name
                   FROM tag_lookup
                   WHERE location_id=$1
                """

        rows = await ctx.db.fetch(query, ctx.guild.id)
        await ctx.release()

        if rows:
            # PSQL orders this oddly for some reason
            entries = sorted(tuple(r[0] for r in rows))
            try:
                p = Pages(ctx, entries=entries, per_page=20)
                await p.paginate()
            except Exception as e:
                await ctx.send(e)
        else:
            await ctx.send('This server has no server-specific tags.')

    @tag.command()
    @suggest_box()
    @checks.has_guild_permissions(manage_messages=True)
    async def purge(self, ctx, member: discord.Member):
        """Removes all server-specific tags by a user.

        You must have server-wide Manage Messages permissions to use this.
        """

        # Though inefficient, for UX purposes we should do two queries

        query = "SELECT COUNT(*) FROM tags WHERE location_id=$1 AND owner_id=$2;"
        count = await ctx.db.fetchrow(query, ctx.guild.id, member.id)
        count = count[0] # COUNT(*) always returns 0 or higher

        if count == 0:
            return await ctx.send(f'{member} does not have any tags to purge.')

        confirm = await ctx.prompt(f'This will delete {count} tags are you sure? **This action cannot be reversed**.')
        if not confirm:
            return await ctx.send('Cancelling tag purge request.')

        query = "DELETE FROM tags WHERE location_id=$1 AND owner_id=$2;"
        await ctx.db.execute(query, ctx.guild.id, member.id)

        await ctx.send(f'Successfully removed all {count} tags that belong to {member}.')

    @tag.command()
    @suggest_box()
    async def search(self, ctx, *, query: commands.clean_content):
        """Searches for a tag.

        The query must be at least 3 characters.
        """

        if len(query) < 3:
            return await ctx.send('The query length must be at least three characters.')

        sql = """SELECT name
                 FROM tag_lookup
                 WHERE location_id=$1 AND name % $2
                 ORDER BY similarity(name, $2) DESC
                 LIMIT 100;
              """

        results = await ctx.db.fetch(sql, ctx.guild.id, query)

        if results:
            try:
                p = Pages(ctx, entries=tuple(r[0] for r in results), per_page=20)
            except Exception as e:
                await ctx.send(e)
            else:
                await ctx.release()
                await p.paginate()
        else:
            await ctx.send('No tags found.')

    @tag.command()
    @suggest_box()
    async def claim(self, ctx, *, tag: TagName):
        """Claims an unclaimed tag.

        An unclaimed tag is a tag that effectively
        has no owner because they have left the server.
        """

        # requires 2 queries for UX
        query = "SELECT id, owner_id FROM tags WHERE location_id=$1 AND LOWER(name)=$2;"
        row = await ctx.db.fetchrow(query, ctx.guild.id, tag.lower())
        if row is None:
            return await ctx.send(f'A tag with the name of "{tag}" does not exist.')

        # just to be sure
        if not ctx.guild.chunked:
            await self.bot.request_offline_members(ctx.guild)

        member = ctx.guild.get_member(row[1])
        if member is not None:
            return await ctx.send('Tag owner is still in server.')

        query = "UPDATE tags SET owner_id=$1 WHERE id=$2;"
        await ctx.db.execute(query, ctx.author.id, row[0])
        query = "UPDATE tag_lookup SET owner_id=$1 WHERE tag_id=$2;"
        await ctx.db.execute(query, ctx.author.id, row[0])
        await ctx.send('Successfully transferred tag ownership to you.')

    @tag.group()
    @can_use_box()
    async def box(self, ctx):
        """The tag box is where global tags are stored.

        The tags in the box are not part of your server's tag list
        unless you explicitly enable them. As a result, only those
        with Manage Messages can check out the tag box, or anyone
        if it's a private message.

        To play around with the tag box, you should use the subcommands
        provided.
        """

        if ctx.invoked_subcommand is None or ctx.subcommand_passed == 'box':
            await ctx.show_help('tag box')

    @box.command(name='put')
    async def box_put(self, ctx, name: TagName, *, content: commands.clean_content):
        """Puts a tag in the tag box.

        These are global tags that anyone can opt-in to receiving
        via the "tag box take" subcommand.
        """

        query = "INSERT INTO tags (name, content, owner_id) VALUES ($1, $2, $3);"

        try:
            await ctx.db.execute(query, name, content, ctx.author.id)
        except asyncpg.UniqueViolationError:
            await ctx.send('A tag with this name exists in the box already.')
        else:
            await ctx.send('Successfully put tag in the box.')

    @box.command(name='take')
    @commands.guild_only()
    async def box_take(self, ctx, *, name: TagName(lower=True)):
        """Takes a tag from the tag box.

        When you take a tag from the tag box, you essentially
        duplicate the tag for use for your own server. Any updates
        to the tag in the tag box does not affect your duplicated
        tag and your duplicated tag acts like a regular server
        specific tag that you now own.
        """

        query = "SELECT name, content FROM tags WHERE LOWER(name)=$1 AND location_id IS NULL;"

        tag = await ctx.db.fetchrow(query, name)

        if tag is None:
            return await ctx.send('A tag with this name cannot be found in the box.')

        await ctx.invoke(self.create, name=tag['name'], content=tag['content'])

    @box.command(name='show', aliases=['get'])
    async def box_show(self, ctx, *, name: TagName(lower=True)):
        """Shows a tag from the tag box."""

        query = "SELECT name, content FROM tags WHERE LOWER(name)=$1 AND location_id IS NULL;"

        tag = await ctx.db.fetchrow(query, name)

        if tag is None:
            return await ctx.send('A tag with this name cannot be found in the box.')

        await ctx.send(tag['content'])

        query = "UPDATE tags SET uses = uses + 1 WHERE name=$1 AND location_id IS NULL;"
        await ctx.db.execute(query, tag['name'])

    @box.command(name='edit', aliases=['change'])
    async def box_edit(self, ctx, name: TagName(lower=True), *, content: commands.clean_content):
        """Edits tag from the tag box.

        You must own the tag to edit it.

        Editing the tag does not affect tags where people
        took it for their own personal use.
        """

        query = "UPDATE tags SET content = $2 WHERE LOWER(name)=$1 AND owner_id=$3 AND location_id IS NULL;"
        status = await ctx.db.execute(query, name, content, ctx.author.id)

        if status[-1] == '0':
            await ctx.send('This tag is either not in the box or you do not own it.')
        else:
            await ctx.send('Successfully edited tag.')

    @box.command(name='delete', aliases=['remove'])
    async def box_delete(self, ctx, *, name: TagName(lower=True)):
        """Deletes a tag from the tag box.

        You must own the tag to delete it.

        Deleting the tag does not affect tags where people
        took it for their own personal use.
        """

        query = "DELETE FROM tags WHERE LOWER(name)=$1 AND owner_id=$2 AND location_id IS NULL;"
        status = await ctx.db.execute(query, name, ctx.author.id)

        if status[-1] == '0':
            await ctx.send('This tag is either not in the box or you do not own it.')
        else:
            await ctx.send('Successfully deleted tag.')

    @box.command(name='info')
    async def box_info(self, ctx, *, name: TagName(lower=True)):
        """Shows information about a tag in the box."""

        query = """SELECT first.*, (
                       SELECT COUNT(*)
                       FROM tags second
                       WHERE (second.uses, second.id) >= (first.uses, first.id)
                         AND second.location_id IS NULL
                   ) AS rank
                   FROM tags first
                   WHERE LOWER(first.name)=$1 AND first.location_id IS NULL;
                """

        data = await ctx.db.fetchrow(query, name)

        if data is None or data['name'] is None:
            return await ctx.send('This tag is not in the box.')

        embed = discord.Embed(colour=discord.Colour.blurple())

        owner_id = data['owner_id']
        embed.title = data['name']
        embed.timestamp = data['created_at']
        embed.set_footer(text='Tag added to box')

        user = self.bot.get_user(owner_id) or (await self.bot.get_user_info(owner_id))
        embed.set_author(name=str(user), icon_url=user.avatar_url)

        embed.add_field(name='Owner', value=f'<@{owner_id}>')
        embed.add_field(name='Uses', value=data['uses'])
        embed.add_field(name='Rank', value=data['rank'])

        await ctx.send(embed=embed)

    @box.command(name='search')
    async def box_search(self, ctx, *, query: commands.clean_content):
        """Searches for a tag in the tag box.

        The query must be at least 3 characters long.
        """

        if len(query) < 3:
            return await ctx.send('Query must be 3 characters or longer.')

        sql = "SELECT name FROM tags WHERE name % $1 AND location_id IS NULL LIMIT 100;"
        data = await ctx.db.fetch(sql, query)

        if len(data) == 0:
            return await ctx.send('No tags found.')

        await ctx.release()

        data = [r[0] for r in data]
        data.sort()

        try:
            p = Pages(ctx, entries=data, per_page=20)
            await p.paginate()
        except Exception as e:
            await ctx.send(e)

    @box.command(name='stats')
    async def box_stats(self, ctx):
        """Shows statistics about the tag box."""

        # This is the best I could split it to.
        # Originally it was 3 different queries but 2 is the best I could do
        # Splitting it into a single query incurred insane overhead for some reason.

        query = """SELECT
                       COUNT(*) AS "Creator Total",
                       SUM(uses) AS "Creator Uses",
                       owner_id AS "Creator ID",
                       COUNT(*) OVER () AS "Creator Count"
                   FROM tags
                   WHERE location_id IS NULL
                   GROUP BY owner_id
                   ORDER BY SUM(uses) DESC
                   LIMIT 3;
                """

        top_creators = await ctx.db.fetch(query)

        query = """SELECT
                       name AS "Tag Name",
                       uses AS "Tag Uses",
                       COUNT(*) OVER () AS "Total Tags",
                       SUM(uses) OVER () AS "Total Uses"
                   FROM tags
                   WHERE location_id IS NULL
                   ORDER BY uses DESC
                   LIMIT 3;
                """

        top_tags = await ctx.db.fetch(query)

        embed = discord.Embed(colour=discord.Colour.blurple(), title='Tag Box Stats')

        embed.add_field(name='Total Tags', value=top_tags[0]['Total Tags'])
        embed.add_field(name='Total Uses', value=top_tags[0]['Total Uses'])
        embed.add_field(name='Tag Creators', value=top_creators[0]['Creator Count'])

        emoji = 129351 # ord(':first_place:')

        for offset, (name, uses, _, _) in enumerate(top_tags):
            embed.add_field(name=f'{chr(emoji + offset)} Tag', value=f'{name} ({uses} uses)')

        values = []
        for offset, (total, uses, owner_id, _) in enumerate(top_creators):
            values.append(f'{chr(emoji + offset)}: {self.bot.get_user(owner_id) or owner_id} -- {total} tags ({uses} uses)')

        embed.add_field(name=f'Tag Creators', value='\n'.join(values), inline=False)
        embed.set_footer(text='These statistics are for the tag box.')
        await ctx.send(embed=embed)

    @box.command(name='list')
    async def box_list(self, ctx, *, user: discord.User = None):
        """Lists all the tags in the box that belong to you or someone else.

        Unlike the regular tag list command, this one is sorted by uses.
        """

        user = user or ctx.author

        query = """SELECT name, uses
                   FROM tags
                   WHERE location_id IS NULL AND owner_id=$1
                   ORDER BY uses DESC
                """

        rows = await ctx.db.fetch(query, user.id)
        await ctx.release()

        if rows:
            entries = [f'{name} ({uses} uses)' for name, uses in rows]
            try:
                p = Pages(ctx, entries=entries)
                p.embed.set_author(name=user.display_name, icon_url=user.avatar_url)
                p.embed.title = f'{sum(u for _, u in rows)} total uses'
                await p.paginate()
            except Exception as e:
                await ctx.send(e)
        else:
            await ctx.send(f'{user} has no tags.')

    @tag.command(hidden=True)
    async def config(self, ctx):
        """This is a reserved tag command. Check back later."""
        pass

def setup(bot):
    bot.add_cog(Tags(bot))
