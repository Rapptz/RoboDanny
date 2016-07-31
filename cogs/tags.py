from .utils import config, checks, formats
from discord.ext import commands
import json
import discord.utils

class TagInfo:
    def __init__(self, name, content, owner_id, **kwargs):
        self.name = name
        self.content = content
        self.owner_id = owner_id
        self.uses = kwargs.pop('uses', 0)
        self.location = kwargs.pop('location')

    @property
    def is_generic(self):
        return self.location == 'generic'

    def __str__(self):
        return self.content

    def info_entries(self, ctx, db):
        popular = sorted(db.values(), key=lambda t: t.uses, reverse=True)
        try:
            rank = popular.index(self) + 1
        except:
            rank = '<Not found>'

        data = [
            ('Name', self.name),
            ('Uses', self.uses),
            ('Rank', rank),
            ('Type', 'Generic' if self.is_generic else 'Server-specific'),
        ]

        # we can make the assumption that if the tag requested is a server specific tag
        # then the server the message belongs to will be the server of the server specific tag.
        members = ctx.bot.get_all_members() if self.is_generic else ctx.message.server.members
        owner = discord.utils.get(members, id=self.owner_id)
        data.append(('Owner', owner.name if owner is not None else '<Not Found>'))
        data.append(('Owner ID', self.owner_id))
        return data


class TagEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, TagInfo):
            payload = obj.__dict__.copy()
            payload['__tag__'] = True
            return payload
        return json.JSONEncoder.default(self, obj)

def tag_decoder(obj):
    if '__tag__' in obj:
        return TagInfo(**obj)
    return obj

class Tags:
    """The tag related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('tags.json', encoder=TagEncoder, object_hook=tag_decoder,
                                                 loop=bot.loop, load_later=True)

    def get_tag(self, server, name):
        # Basically, if we're in a PM then we will use the generic tag database
        # if we aren't, we will check the server specific tag database.
        # If we don't have a server specific database, fallback to generic.
        # If it isn't found, fallback to generic.

        generic = self.config.get('generic', {})
        if server is None:
            return generic.get(name)

        db = self.config.get(server.id)
        if db is None:
            return generic.get(name)

        entry = db.get(name)
        if entry is None:
            return generic.get(name)
        return entry

    def get_database_location(self, message):
        return 'generic' if message.channel.is_private else message.server.id

    def clean_tag_content(self, content):
        return content.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')

    def get_possible_tags(self, server):
        """Returns a dict of possible tags that the server can execute.

        If this is a private message then only the generic tags are possible.
        Server specific tags will override the generic tags.
        """
        generic = self.config.get('generic', {}).copy()
        if server is None:
            return generic

        generic.update(self.config.get(server.id, {}))
        return generic

    @commands.group(pass_context=True, invoke_without_command=True)
    async def tag(self, ctx, *, name : str):
        """Allows you to tag text for later retrieval.

        If a subcommand is not called, then this will search the tag database
        for the tag requested.
        """
        lookup = name.lower()
        server = ctx.message.server
        tag = self.get_tag(server, lookup)
        if tag is None:
            await self.bot.say('Tag not found.')
            return

        tag.uses += 1
        await self.bot.say(tag)

        # update the database with the new tag reference
        db = self.config.get(tag.location)
        await self.config.put(tag.location, db)

    @tag.error
    async def tag_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('You need to pass in a tag name.')

    def verify_lookup(self, lookup):
        if '@everyone' in lookup or '@here' in lookup:
            raise RuntimeError('That tag is using blocked words.')

        if not lookup:
            raise RuntimeError('You need to actually pass in a tag name.')

        if len(lookup) > 100:
            raise RuntimeError('Tag name is a maximum of 100 characters.')

    @tag.command(pass_context=True, aliases=['add'])
    async def create(self, ctx, name : str, *, content : str):
        """Creates a new tag owned by you.

        If you create a tag via private message then the tag is a generic
        tag that can be accessed in all servers. Otherwise the tag you
        create can only be accessed in the server that it was created in.
        """
        content = self.clean_tag_content(content)
        lookup = name.lower().strip()
        try:
            self.verify_lookup(lookup)
        except RuntimeError as e:
            await self.bot.say(e)
            return

        location = self.get_database_location(ctx.message)
        db = self.config.get(location, {})
        if lookup in db:
            await self.bot.say('A tag with the name of "{}" already exists.'.format(name))
            return

        db[lookup] = TagInfo(name, content, ctx.message.author.id, location=location)
        await self.config.put(location, db)
        await self.bot.say('Tag "{}" successfully created.'.format(name))

    @tag.command(pass_context=True)
    async def generic(self, ctx, name : str, *, content : str):
        """Creates a new generic tag owned by you.

        Unlike the create tag subcommand,  this will always attempt to create
        a generic tag and not a server-specific one.
        """
        content = self.clean_tag_content(content)
        lookup = name.lower().strip()
        try:
            self.verify_lookup(lookup)
        except RuntimeError as e:
            await self.bot.say(str(e))
            return

        db = self.config.get('generic', {})
        if lookup in db:
            await self.bot.say('A tag with the name of "{}" already exists.'.format(name))
            return

        db[lookup] = TagInfo(name, content, ctx.message.author.id, location='generic')
        await self.config.put('generic', db)
        await self.bot.say('Tag "{}" successfully created.'.format(name))

    @tag.command(pass_context=True, ignore_extra=False)
    async def make(self, ctx):
        """Interactive makes a tag for you.

        This walks you through the process of creating a tag with
        its name and its content. This works similar to the tag
        create command.
        """
        message = ctx.message
        location = self.get_database_location(message)
        db = self.config.get(location, {})

        await self.bot.say('Hello. What would you like the name tag to be?')

        def check(msg):
            try:
                self.verify_lookup(msg.content.lower())
                return True
            except:
                return False

        name = await self.bot.wait_for_message(author=message.author, channel=message.channel, timeout=60.0, check=check)
        if name is None:
            await self.bot.say('You took too long. Goodbye.')
            return

        lookup = name.content.lower()
        if lookup in db:
            fmt = 'Sorry. A tag with that name exists already. Redo the command {0.prefix}tag make.'
            await self.bot.say(fmt.format(ctx))
            return

        await self.bot.say('Alright. So the name is {0.content}. What about the tag\'s content?'.format(name))
        content = await self.bot.wait_for_message(author=name.author, channel=name.channel, timeout=300.0)
        if content is None:
            await self.bot.say('You took too long. Goodbye.')
            return

        if len(content.content) == 0 and len(content.attachments) > 0:
            # we have an attachment
            content = content.attachments[0].get('url', '*Could not get attachment data*')
        else:
            content = self.clean_tag_content(content.content)

        db[lookup] = TagInfo(name.content, content, name.author.id, location=location)
        await self.config.put(location, db)
        await self.bot.say('Cool. I\'ve made your {0.content} tag.'.format(name))

    @make.error
    async def tag_make_error(self, error, ctx):
        if type(error) is commands.TooManyArguments:
            await self.bot.say('Please call just {0.prefix}tag make'.format(ctx))

    def generate_stats(self, db, label):
        yield '- Total {} tags: {}'.format(label, len(db))
        if db:
            popular = sorted(db.values(), key=lambda t: t.uses, reverse=True)
            total_uses = sum(t.uses for t in popular)
            yield '- Total {} tag uses: {}'.format(label, total_uses)
            for i, tag in enumerate(popular[:3], 1):
                yield '- Rank {0} tag: {1.name} with {1.uses} uses'.format(i, tag)

    @tag.command(pass_context=True)
    async def stats(self, ctx):
        """Gives stats about the tag database."""
        result = []
        server = ctx.message.server
        generic = self.config.get('generic', {})
        result.extend(self.generate_stats(generic, 'Generic'))

        if server is not None:
            result.extend(self.generate_stats(self.config.get(server.id, {}), 'Server Specific'))

        await self.bot.say('\n'.join(result))

    @tag.command(pass_context=True)
    async def edit(self, ctx, name : str, *, content : str):
        """Modifies an existing tag that you own.

        This command completely replaces the original text. If you edit
        a tag via private message then the tag is looked up in the generic
        tag database. Otherwise it looks at the server-specific database.
        """

        content = self.clean_tag_content(content)
        lookup = name.lower()
        server = ctx.message.server
        tag = self.get_tag(server, lookup)

        if tag is None:
            await self.bot.say('The tag does not exist.')
            return

        if tag.owner_id != ctx.message.author.id:
            await self.bot.say('Only the tag owner can edit this tag.')
            return

        db = self.config.get(tag.location)
        tag.content = content
        await self.config.put(tag.location, db)
        await self.bot.say('Tag successfully edited.')

    @tag.command(pass_context=True, aliases=['delete'])
    async def remove(self, ctx, *, name : str):
        """Removes a tag that you own.

        The tag owner can always delete their own tags. If someone requests
        deletion and has Manage Messages permissions or a Bot Mod role then
        they can also remove tags from the server-specific database. Generic
        tags can only be deleted by the bot owner or the tag owner.
        """
        lookup = name.lower()
        server = ctx.message.server
        tag = self.get_tag(server, lookup)

        if tag is None:
            await self.bot.say('Tag not found.')
            return

        if not tag.is_generic:
            can_delete = checks.role_or_permissions(ctx, lambda r: r.name == 'Bot Admin', manage_messages=True)
        else:
            can_delete = checks.is_owner_check(ctx.message)

        can_delete = can_delete or tag.owner_id == ctx.message.author.id

        if not can_delete:
            await self.bot.say('You do not have permissions to delete this tag.')
            return

        db = self.config.get(tag.location)
        del db[lookup]
        await self.config.put(tag.location, db)
        await self.bot.say('Tag successfully removed.')

    @tag.command(pass_context=True)
    async def info(self, ctx, *, name : str):
        """Retrieves info about a tag.

        The info includes things like the owner and how many times it was used.
        """

        lookup = name.lower()
        server = ctx.message.server
        tag = self.get_tag(server, lookup)

        if tag is None:
            await self.bot.say('Tag not found.')
            return

        entries = tag.info_entries(ctx, self.get_possible_tags(server))
        await formats.entry_to_code(self.bot, entries)

    @info.error
    async def info_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Missing tag name to get info of.')

    @tag.command(name='list', pass_context=True)
    async def _list(self, ctx, *, member : discord.Member = None):
        """Lists all the tags that belong to you or someone else.

        This includes the generic tags as well. If this is done in a private
        message then you will only get the generic tags you own and not the
        server specific tags.
        """

        owner = ctx.message.author if member is None else member
        server = ctx.message.server
        tags = [tag.name for tag in self.config.get('generic', {}).values() if tag.owner_id == owner.id]
        if server is not None:
            tags.extend(tag.name for tag in self.config.get(server.id, {}).values() if tag.owner_id == owner.id)

        if tags:
            fmt = '{0.name} has the following {1} tags:\n{2}'
            await self.bot.say(fmt.format(owner, len(tags), ', '.join(tags)))
        else:
            await self.bot.say('{0.name} has no tags.'.format(owner))

    @tag.command(pass_context=True)
    async def search(self, ctx, *, query : str):
        """Searches for a tag.

        This searches both the generic and server-specific database. If it's
        a private message, then only generic tags are searched.

        The query must be at least 2 characters.
        """

        server = ctx.message.server
        query = query.lower()
        if len(query) < 2:
            await self.bot.say('The query length must be at least two characters.')
            return

        generic = self.config.get('generic', {})
        results = {value.name for name, value in generic.items() if query in name}

        if server is not None:
            db = self.config.get(server.id, {})
            for name, value in db.items():
                if query in name:
                    results.add(value.name)

        fmt = '{} tag(s) found.\n{}'
        if results:
            await self.bot.say(fmt.format(len(results), ', '.join(results)))
        else:
            await self.bot.say('No tags found.')

    @search.error
    async def search_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Missing query to search for.')

def setup(bot):
    bot.add_cog(Tags(bot))
