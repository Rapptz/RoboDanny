from .utils import config, checks, formats
from .utils.paginator import Pages

from discord.ext import commands
import json
import re
import datetime
import discord
import difflib

class TagInfo:
    __slots__ = ('name', 'content', 'owner_id', 'uses', 'location', 'created_at')

    def __init__(self, name, content, owner_id, **kwargs):
        self.name = name
        self.content = content
        self.owner_id = owner_id
        self.uses = kwargs.pop('uses', 0)
        self.location = kwargs.pop('location')
        self.created_at = kwargs.pop('created_at', 0.0)

    @property
    def is_generic(self):
        return self.location == 'generic'

    def __str__(self):
        return self.content

    async def embed(self, ctx, db):
        e = discord.Embed(title=self.name)
        e.add_field(name='Owner', value='<@!%s>' % self.owner_id)
        e.add_field(name='Uses', value=self.uses)

        popular = sorted(db.values(), key=lambda t: t.uses, reverse=True)
        try:
            e.add_field(name='Rank', value=popular.index(self) + 1)
        except:
            e.add_field(name='Rank', value='Unknown')

        if self.created_at:
            e.timestamp = datetime.datetime.fromtimestamp(self.created_at)

        owner = discord.utils.find(lambda m: m.id == self.owner_id, ctx.bot.get_all_members())
        if owner is None:
            owner = await ctx.bot.get_user_info(self.owner_id)

        e.set_author(name=str(owner), icon_url=owner.avatar_url or owner.default_avatar_url)
        e.set_footer(text='Generic' if self.is_generic else 'Server-specific')
        return e

class TagAlias:
    __slots__ = ('name', 'original', 'owner_id', 'created_at')

    def __init__(self, **kwargs):
        self.name = kwargs.pop('name')
        self.original = kwargs.pop('original')
        self.owner_id = kwargs.pop('owner_id')
        self.created_at = kwargs.pop('created_at', 0.0)

    @property
    def is_generic(self):
        return False

    @property
    def uses(self):
        return 0 # compatibility with TagInfo

    async def embed(self, ctx, db):
        e = discord.Embed(title=self.name)
        e.add_field(name='Owner', value='<@!%s>' % self.owner_id)
        e.add_field(name='Original Tag', value=self.original)

        if self.created_at:
            e.timestamp = datetime.datetime.fromtimestamp(self.created_at)

        owner = discord.utils.find(lambda m: m.id == self.owner_id, ctx.bot.get_all_members())
        if owner is None:
            owner = await ctx.bot.get_user_info(self.owner_id)

        e.set_author(name=str(owner), icon_url=owner.avatar_url or owner.default_avatar_url)
        return e

class TagEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, TagInfo):
            payload = {
                attr: getattr(obj, attr)
                for attr in TagInfo.__slots__
            }
            payload['__tag__'] = True
            return payload
        if isinstance(obj, TagAlias):
            payload = {
                attr: getattr(obj, attr)
                for attr in TagAlias.__slots__
            }
            payload['__tag_alias__'] = True
            return payload
        return json.JSONEncoder.default(self, obj)

def tag_decoder(obj):
    if '__tag__' in obj:
        return TagInfo(**obj)
    if '__tag_alias__' in obj:
        return TagAlias(**obj)
    return obj

class Tags:
    """The tag related commands."""

    def __init__(self, bot):
        self.bot = bot
        self.config = config.Config('tags.json', encoder=TagEncoder, object_hook=tag_decoder,
                                                 loop=bot.loop, load_later=True)

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

    def get_tag(self, server, name, *, redirect=True):
        # Basically, if we're in a PM then we will use the generic tag database
        # if we aren't, we will check the server specific tag database.
        # If we don't have a server specific database, fallback to generic.
        # If it isn't found, fallback to generic.
        all_tags = self.get_possible_tags(server)
        try:
            tag = all_tags[name]
            if isinstance(tag, TagInfo):
                return tag
            elif redirect:
                return all_tags[tag.original.lower()]
            else:
                return tag
        except KeyError:
            possible_matches = difflib.get_close_matches(name, tuple(all_tags.keys()))
            if not possible_matches:
                raise RuntimeError('Tag not found.')
            raise RuntimeError('Tag not found. Did you mean...\n' + '\n'.join(possible_matches))

    @commands.group(pass_context=True, invoke_without_command=True)
    async def tag(self, ctx, *, name : str):
        """Allows you to tag text for later retrieval.

        If a subcommand is not called, then this will search the tag database
        for the tag requested.
        """
        lookup = name.lower()
        server = ctx.message.server
        try:
            tag = self.get_tag(server, lookup)
        except RuntimeError as e:
            return await self.bot.say(e)

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
            return await self.bot.say(e)

        location = self.get_database_location(ctx.message)
        db = self.config.get(location, {})
        if lookup in db:
            await self.bot.say('A tag with the name of "{}" already exists.'.format(name))
            return

        db[lookup] = TagInfo(name, content, ctx.message.author.id,
                             location=location,
                             created_at=datetime.datetime.utcnow().timestamp())
        await self.config.put(location, db)
        await self.bot.say('Tag "{}" successfully created.'.format(name))

    @create.error
    async def create_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Tag ' + str(error))

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

        db[lookup] = TagInfo(name, content, ctx.message.author.id,
                             location='generic',
                             created_at=datetime.datetime.utcnow().timestamp())
        await self.config.put('generic', db)
        await self.bot.say('Tag "{}" successfully created.'.format(name))

    @generic.error
    async def generic_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Tag ' + str(error))

    @tag.command(pass_context=True, no_pm=True)
    async def alias(self, ctx, new_name: str, *, old_name: str):
        """Creates an alias for a pre-existing tag.

        You own the tag alias. However, when the original
        tag is deleted the alias is deleted as well.

        Tag aliases cannot be edited. You must delete
        the alias and remake it to point it to another
        location.

        You cannot have generic aliases.
        """

        message = ctx.message
        server = ctx.message.server
        lookup = new_name.lower().strip()
        old = old_name.lower()

        tags = self.get_possible_tags(server)
        db = self.config.get(server.id, {})
        try:
            original = tags[old]
        except KeyError:
            return await self.bot.say('Pointed to tag does not exist.')

        if isinstance(original, TagAlias):
            return await self.bot.say('Cannot make an alias to an alias.')

        try:
            self.verify_lookup(lookup)
        except RuntimeError as e:
            return await self.bot.say(e)

        if lookup in db:
            return await self.bot.say('A tag with this name already exists.')

        db[lookup] = TagAlias(name=new_name, original=old, owner_id=ctx.message.author.id,
                              created_at=datetime.datetime.utcnow().timestamp())

        await self.config.put(server.id, db)
        await self.bot.say('Tag alias "{}" that points to "{.name}" successfully created.'.format(new_name, original))

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

        db[lookup] = TagInfo(name.content, content, name.author.id,
                             location=location,
                             created_at=datetime.datetime.utcnow().timestamp())
        await self.config.put(location, db)
        await self.bot.say('Cool. I\'ve made your {0.content} tag.'.format(name))

    @make.error
    async def tag_make_error(self, error, ctx):
        if isinstance(error, commands.TooManyArguments):
            await self.bot.say('Please call just {0.prefix}tag make'.format(ctx))

    def top_three_tags(self, db):
        emoji = 129351 # ord(':first_place:')
        popular = sorted(db.values(), key=lambda t: t.uses, reverse=True)
        for tag in popular[:3]:
            yield (chr(emoji), tag)
            emoji += 1

    @tag.command(pass_context=True)
    async def stats(self, ctx):
        """Gives stats about the tag database."""
        server = ctx.message.server
        generic = self.config.get('generic', {})
        e = discord.Embed()
        e.add_field(name='Generic', value='%s tags\n%s uses' % (len(generic), sum(t.uses for t in generic.values())))

        total_tags = sum(len(c) for c in self.config.all().values())
        total_uses = sum(sum(t.uses for t in c.values()) for c in self.config.all().values())
        e.add_field(name='Global', value='%s tags\n%s uses' % (total_tags, total_uses))

        if server is not None:
            db = self.config.get(server.id, {})
            e.add_field(name='Server-Specific', value='%s tags\n%s uses' % (len(db), sum(t.uses for t in db.values())))
        else:
            db = {}
            e.add_field(name='Server-Specific', value='No Info')

        fmt = '{0.name} ({0.uses} uses)'
        for emoji, tag in self.top_three_tags(generic):
            e.add_field(name=emoji + ' Generic Tag', value=fmt.format(tag))

        for emoji, tag in self.top_three_tags(db):
            e.add_field(name=emoji + ' Server Tag', value=fmt.format(tag))

        await self.bot.say(embed=e)

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
        try:
            tag = self.get_tag(server, lookup, redirect=False)
        except RuntimeError as e:
            return await self.bot.say(e)

        if isinstance(tag, TagAlias):
            return await self.bot.say('Cannot edit tag aliases. Remake it if you want to re-point it.')

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

        Deleting a tag will delete all of its aliases as well.
        """
        lookup = name.lower()
        server = ctx.message.server
        try:
            tag = self.get_tag(server, lookup, redirect=False)
        except RuntimeError as e:
            return await self.bot.say(e)

        if not tag.is_generic:
            can_delete = checks.role_or_permissions(ctx, lambda r: r.name == 'Bot Admin', manage_messages=True)
        else:
            can_delete = checks.is_owner_check(ctx.message)

        can_delete = can_delete or tag.owner_id == ctx.message.author.id

        if not can_delete:
            await self.bot.say('You do not have permissions to delete this tag.')
            return

        if isinstance(tag, TagAlias):
            location = server.id
            db = self.config.get(location)
            del db[lookup]
            msg = 'Tag alias successfully removed.'
        else:
            location = tag.location
            db = self.config.get(location)
            msg = 'Tag and all corresponding aliases successfully removed.'

            if server is not None:
                alias_db = self.config.get(server.id)
                aliases = [key for key, t in alias_db.items() if isinstance(t, TagAlias) and t.original == lookup]
                for alias in aliases:
                    alias_db.pop(alias, None)

            del db[lookup]

        await self.config.put(location, db)
        await self.bot.say(msg)

    @tag.command(pass_context=True, aliases=['owner'])
    async def info(self, ctx, *, name : str):
        """Retrieves info about a tag.

        The info includes things like the owner and how many times it was used.
        """

        lookup = name.lower()
        server = ctx.message.server
        try:
            tag = self.get_tag(server, lookup, redirect=False)
        except RuntimeError as e:
            return await self.bot.say(e)

        embed = await tag.embed(ctx, self.get_possible_tags(server))
        await self.bot.say(embed=embed)

    @info.error
    async def info_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Missing tag name to get info of.')

    @tag.command(pass_context=True)
    async def raw(self, ctx, *, name: str):
        """Gets the raw content of the tag.

        This is with markdown escaped. Useful for editing.
        """

        lookup = name.lower()
        server = ctx.message.server
        try:
            tag = self.get_tag(server, lookup)
        except RuntimeError as e:
            return await self.bot.say(e)

        transformations = {
            re.escape(c): '\\' + c
            for c in ('*', '`', '_', '~', '\\', '<')
        }

        def replace(obj):
            return transformations.get(re.escape(obj.group(0)), '')

        pattern = re.compile('|'.join(transformations.keys()))
        await self.bot.say(pattern.sub(replace, tag.content))

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

        tags.sort()

        if tags:
            try:
                p = Pages(self.bot, message=ctx.message, entries=tags)
                p.embed.colour = 0x738bd7 # blurple
                p.embed.set_author(name=owner.display_name, icon_url=owner.avatar_url or owner.default_avatar_url)
                await p.paginate()
            except Exception as e:
                await self.bot.say(e)
        else:
            await self.bot.say('{0.name} has no tags.'.format(owner))

    @tag.command(name='all', pass_context=True, no_pm=True)
    async def _all(self, ctx):
        """Lists all server-specific tags for this server."""

        tags = [tag.name for tag in self.config.get(ctx.message.server.id, {}).values()]
        tags.sort()

        if tags:
            try:
                p = Pages(self.bot, message=ctx.message, entries=tags, per_page=15)
                p.embed.colour =  0x738bd7 # blurple
                await p.paginate()
            except Exception as e:
                await self.bot.say(e)
        else:
            await self.bot.say('This server has no server-specific tags.')

    @tag.command(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def purge(self, ctx, member: discord.Member):
        """Removes all server-specific tags by a user.

        You must have Manage Messages permissions to use this.
        """

        db = self.config.get(ctx.message.server.id, {})
        tags = [key for key, tag in db.items() if tag.owner_id == member.id]

        if not ctx.message.channel.permissions_for(ctx.message.server.me).add_reactions:
            return await self.bot.say('Bot cannot add reactions.')

        if not tags:
            return await self.bot.say('This user has no server-specific tags.')

        msg = await self.bot.say('This will delete %s tags are you sure? **This action cannot be reversed**.\n\n' \
                                 'React with either \N{WHITE HEAVY CHECK MARK} to confirm or \N{CROSS MARK} to deny.' % len(tags))

        cancel = False
        author_id = ctx.message.author.id
        def check(reaction, user):
            nonlocal cancel
            if user.id != author_id:
                return False

            if reaction.emoji == '\N{WHITE HEAVY CHECK MARK}':
                return True
            elif reaction.emoji == '\N{CROSS MARK}':
                cancel = True
                return True
            return False

        for emoji in ('\N{WHITE HEAVY CHECK MARK}', '\N{CROSS MARK}'):
            await self.bot.add_reaction(msg, emoji)

        react = await self.bot.wait_for_reaction(message=msg, check=check, timeout=60.0)
        if react is None or cancel:
            await self.bot.delete_message(msg)
            return await self.bot.say('Cancelling.')

        for key in tags:
            db.pop(key)

        await self.config.put(ctx.message.server.id, db)
        await self.bot.delete_message(msg)
        await self.bot.say('Successfully removed all %s tags that belong to %s' % (len(tags), member.display_name))

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
            return await self.bot.say('The query length must be at least two characters.')

        tags = self.get_possible_tags(server)
        results = [tag.name for key, tag in tags.items() if query in key]

        if results:
            try:
                p = Pages(self.bot, message=ctx.message, entries=results, per_page=15)
                p.embed.colour = 0x738bd7 # blurple
                await p.paginate()
            except Exception as e:
                await self.bot.say(e)
        else:
            await self.bot.say('No tags found.')

    @search.error
    async def search_error(self, error, ctx):
        if isinstance(error, commands.MissingRequiredArgument):
            await self.bot.say('Missing query to search for.')

def setup(bot):
    bot.add_cog(Tags(bot))
