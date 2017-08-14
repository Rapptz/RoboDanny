import asyncio
import discord

class CannotPaginate(Exception):
    pass

class Pages:
    """Implements a paginator that queries the user for the
    pagination interface.

    Pages are 1-index based, not 0-index based.

    If the user does not reply within 2 minutes then the pagination
    interface exits automatically.

    Parameters
    ------------
    ctx: Context
        The context of the command.
    entries: List[str]
        A list of entries to paginate.
    per_page: int
        How many entries show up per page.
    show_entry_count: bool
        Whether to show an entry count in the footer.

    Attributes
    -----------
    embed: discord.Embed
        The embed object that is being used to send pagination info.
        Feel free to modify this externally. Only the description,
        footer fields, and colour are internally modified.
    permissions: discord.Permissions
        Our permissions for the channel.
    """
    def __init__(self, ctx, *, entries, per_page=12, show_entry_count=True):
        self.bot = ctx.bot
        self.entries = entries
        self.message = ctx.message
        self.channel = ctx.channel
        self.author = ctx.author
        self.per_page = per_page
        pages, left_over = divmod(len(self.entries), self.per_page)
        if left_over:
            pages += 1
        self.maximum_pages = pages
        self.embed = discord.Embed(colour=discord.Colour.blurple())
        self.paginating = len(entries) > per_page
        self.show_entry_count = show_entry_count
        self.reaction_emojis = [
            ('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.first_page),
            ('\N{BLACK LEFT-POINTING TRIANGLE}', self.previous_page),
            ('\N{BLACK RIGHT-POINTING TRIANGLE}', self.next_page),
            ('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.last_page),
            ('\N{INPUT SYMBOL FOR NUMBERS}', self.numbered_page ),
            ('\N{BLACK SQUARE FOR STOP}', self.stop_pages),
            ('\N{INFORMATION SOURCE}', self.show_help),
        ]

        if ctx.guild is not None:
            self.permissions = self.channel.permissions_for(ctx.guild.me)
        else:
            self.permissions = self.channel.permissions_for(ctx.bot.user)

        if not self.permissions.embed_links:
            raise CannotPaginate('Bot does not have embed links permission.')

        if not self.permissions.send_messages:
            raise CannotPaginate('Bot cannot send messages.')

        if self.paginating:
            # verify we can actually use the pagination session
            if not self.permissions.add_reactions:
                raise CannotPaginate('Bot does not have add reactions permission.')

            if not self.permissions.read_message_history:
                raise CannotPaginate('Bot does not have Read Message History permission.')

    def get_page(self, page):
        base = (page - 1) * self.per_page
        return self.entries[base:base + self.per_page]

    async def show_page(self, page, *, first=False):
        self.current_page = page
        entries = self.get_page(page)
        p = []
        for index, entry in enumerate(entries, 1 + ((page - 1) * self.per_page)):
            p.append(f'{index}. {entry}')

        if self.maximum_pages > 1:
            if self.show_entry_count:
                text = f'Page {page}/{self.maximum_pages} ({len(self.entries)} entries)'
            else:
                text = f'Page {page}/{self.maximum_pages}'

            self.embed.set_footer(text=text)

        if not self.paginating:
            self.embed.description = '\n'.join(p)
            return await self.channel.send(embed=self.embed)

        if not first:
            self.embed.description = '\n'.join(p)
            await self.message.edit(embed=self.embed)
            return

        p.append('')
        p.append('Confused? React with \N{INFORMATION SOURCE} for more info.')
        self.embed.description = '\n'.join(p)
        self.message = await self.channel.send(embed=self.embed)
        for (reaction, _) in self.reaction_emojis:
            if self.maximum_pages == 2 and reaction in ('\u23ed', '\u23ee'):
                # no |<< or >>| buttons if we only have two pages
                # we can't forbid it if someone ends up using it but remove
                # it from the default set
                continue

            await self.message.add_reaction(reaction)

    async def checked_show_page(self, page):
        if page != 0 and page <= self.maximum_pages:
            await self.show_page(page)

    async def first_page(self):
        """goes to the first page"""
        await self.show_page(1)

    async def last_page(self):
        """goes to the last page"""
        await self.show_page(self.maximum_pages)

    async def next_page(self):
        """goes to the next page"""
        await self.checked_show_page(self.current_page + 1)

    async def previous_page(self):
        """goes to the previous page"""
        await self.checked_show_page(self.current_page - 1)

    async def show_current_page(self):
        if self.paginating:
            await self.show_page(self.current_page)

    async def numbered_page(self):
        """lets you type a page number to go to"""
        to_delete = []
        to_delete.append(await self.channel.send('What page do you want to go to?'))

        def message_check(m):
            return m.author == self.author and \
                   self.channel == m.channel and \
                   m.content.isdigit()

        try:
            msg = await self.bot.wait_for('message', check=message_check, timeout=30.0)
        except asyncio.TimeoutError:
            to_delete.append(await self.channel.send('Took too long.'))
            await asyncio.sleep(5)
        else:
            page = int(msg.content)
            to_delete.append(msg)
            if page != 0 and page <= self.maximum_pages:
                await self.show_page(page)
            else:
                to_delete.append(await self.channel.send(f'Invalid page given. ({page}/{self.maximum_pages})'))
                await asyncio.sleep(5)

        try:
            await self.channel.delete_messages(to_delete)
        except Exception:
            pass

    async def show_help(self):
        """shows this message"""
        messages = ['Welcome to the interactive paginator!\n']
        messages.append('This interactively allows you to see pages of text by navigating with ' \
                        'reactions. They are as follows:\n')

        for (emoji, func) in self.reaction_emojis:
            messages.append(f'{emoji} {func.__doc__}')

        self.embed.description = '\n'.join(messages)
        self.embed.clear_fields()
        self.embed.set_footer(text=f'We were on page {self.current_page} before this message.')
        await self.message.edit(embed=self.embed)

        async def go_back_to_current_page():
            await asyncio.sleep(60.0)
            await self.show_current_page()

        self.bot.loop.create_task(go_back_to_current_page())

    async def stop_pages(self):
        """stops the interactive pagination session"""
        await self.message.delete()
        self.paginating = False

    def react_check(self, reaction, user):
        if user is None or user.id != self.author.id:
            return False

        if reaction.message.id != self.message.id:
            return False

        for (emoji, func) in self.reaction_emojis:
            if reaction.emoji == emoji:
                self.match = func
                return True
        return False

    async def paginate(self):
        """Actually paginate the entries and run the interactive loop if necessary."""
        first_page = self.show_page(1, first=True)
        if not self.paginating:
            await first_page
        else:
            # allow us to react to reactions right away if we're paginating
            self.bot.loop.create_task(first_page)

        while self.paginating:
            try:
                reaction, user = await self.bot.wait_for('reaction_add', check=self.react_check, timeout=120.0)
            except asyncio.TimeoutError:
                self.paginating = False
                try:
                    await self.message.clear_reactions()
                except:
                    pass
                finally:
                    break

            try:
                await self.message.remove_reaction(reaction, user)
            except:
                pass # can't remove it so don't bother doing so

            await self.match()

class FieldPages(Pages):
    """Similar to Pages except entries should be a list of
    tuples having (key, value) to show as embed fields instead.
    """
    async def show_page(self, page, *, first=False):
        self.current_page = page
        entries = self.get_page(page)

        self.embed.clear_fields()
        self.embed.description = discord.Embed.Empty

        for key, value in entries:
            self.embed.add_field(name=key, value=value, inline=False)

        if self.maximum_pages > 1:
            if self.show_entry_count:
                text = f'Page {page}/{self.maximum_pages} ({len(self.entries)} entries)'
            else:
                text = f'Page {page}/{self.maximum_pages}'

            self.embed.set_footer(text=text)

        if not self.paginating:
            return await self.channel.send(embed=self.embed)

        if not first:
            await self.message.edit(embed=self.embed)
            return

        self.message = await self.channel.send(embed=self.embed)
        for (reaction, _) in self.reaction_emojis:
            if self.maximum_pages == 2 and reaction in ('\u23ed', '\u23ee'):
                # no |<< or >>| buttons if we only have two pages
                # we can't forbid it if someone ends up using it but remove
                # it from the default set
                continue

            await self.message.add_reaction(reaction)

import itertools
import inspect
import re

# ?help
# ?help Cog
# ?help command
#   -> could be a subcommand

_mention = re.compile(r'<@\!?([0-9]{1,19})>')

def cleanup_prefix(bot, prefix):
    m = _mention.match(prefix)
    if m:
        user = bot.get_user(int(m.group(1)))
        if user:
            return f'@{user.name} '
    return prefix

async def _can_run(cmd, ctx):
    try:
        return await cmd.can_run(ctx)
    except:
        return False

def _command_signature(cmd):
    # this is modified from discord.py source
    # which I wrote myself lmao

    result = [cmd.qualified_name]
    if cmd.usage:
        result.append(cmd.usage)
        return ' '.join(result)

    params = cmd.clean_params
    if not params:
        return ' '.join(result)

    for name, param in params.items():
        if param.default is not param.empty:
            # We don't want None or '' to trigger the [name=value] case and instead it should
            # do [name] since [name=None] or [name=] are not exactly useful for the user.
            should_print = param.default if isinstance(param.default, str) else param.default is not None
            if should_print:
                result.append(f'[{name}={param.default!r}]')
            else:
                result.append(f'[{name}]')
        elif param.kind == param.VAR_POSITIONAL:
            result.append(f'[{name}...]')
        else:
            result.append(f'<{name}>')

    return ' '.join(result)

class HelpPaginator(Pages):
    def __init__(self, ctx, entries, *, per_page=4):
        super().__init__(ctx, entries=entries, per_page=per_page)
        self.reaction_emojis.append(('\N{WHITE QUESTION MARK ORNAMENT}', self.show_bot_help))
        self.total = len(entries)

    @classmethod
    async def from_cog(cls, ctx, cog):
        cog_name = cog.__class__.__name__

        # get the commands
        entries = sorted(ctx.bot.get_cog_commands(cog_name), key=lambda c: c.name)

        # remove the ones we can't run
        entries = [cmd for cmd in entries if (await _can_run(cmd, ctx)) and not cmd.hidden]

        self = cls(ctx, entries)
        self.title = f'{cog_name} Commands'
        self.description = inspect.getdoc(cog)
        self.prefix = cleanup_prefix(ctx.bot, ctx.prefix)

        # no longer need the database
        await ctx.release()

        return self

    @classmethod
    async def from_command(cls, ctx, command):
        try:
            entries = sorted(command.commands, key=lambda c: c.name)
        except AttributeError:
            entries = []
        else:
            entries = [cmd for cmd in entries if (await _can_run(cmd, ctx)) and not cmd.hidden]

        self = cls(ctx, entries)
        self.title = command.signature

        if command.description:
            self.description = f'{command.description}\n\n{command.help}'
        else:
            self.description = command.help or 'No help given.'

        self.prefix = cleanup_prefix(ctx.bot, ctx.prefix)
        await ctx.release()
        return self

    @classmethod
    async def from_bot(cls, ctx):
        def key(c):
            return c.cog_name or '\u200bMisc'

        entries = sorted(ctx.bot.commands, key=key)
        nested_pages = []
        per_page = 9

        # 0: (cog, desc, commands) (max len == 9)
        # 1: (cog, desc, commands) (max len == 9)
        # ...

        for cog, commands in itertools.groupby(entries, key=key):
            plausible = [cmd for cmd in commands if (await _can_run(cmd, ctx)) and not cmd.hidden]
            if len(plausible) == 0:
                continue

            description = ctx.bot.get_cog(cog)
            if description is None:
                description = discord.Embed.Empty
            else:
                description = inspect.getdoc(description) or discord.Embed.Empty

            nested_pages.extend((cog, description, plausible[i:i + per_page]) for i in range(0, len(plausible), per_page))

        self = cls(ctx, nested_pages, per_page=1) # this forces the pagination session
        self.prefix = cleanup_prefix(ctx.bot, ctx.prefix)
        await ctx.release()

        # swap the get_page implementation with one that supports our style of pagination
        self.get_page = self.get_bot_page
        self._is_bot = True

        # replace the actual total
        self.total = sum(len(o) for _, _, o in nested_pages)
        return self

    def get_bot_page(self, page):
        cog, description, commands = self.entries[page - 1]
        self.title = f'{cog} Commands'
        self.description = description
        return commands

    async def show_page(self, page, *, first=False):
        self.current_page = page
        entries = self.get_page(page)

        self.embed.clear_fields()
        self.embed.description = self.description
        self.embed.title = self.title

        if hasattr(self, '_is_bot'):
            value ='For more help, join the official bot support server: https://discord.gg/pYuKF2Z'
            self.embed.add_field(name='Support', value=value, inline=False)

        self.embed.set_footer(text=f'Use "{self.prefix}help command" for more info on a command.')

        signature = _command_signature

        for entry in entries:
            self.embed.add_field(name=signature(entry), value=entry.short_doc or "No help given", inline=False)

        if self.maximum_pages:
            self.embed.set_author(name=f'Page {page}/{self.maximum_pages} ({self.total} commands)')

        if not self.paginating:
            return await self.channel.send(embed=self.embed)

        if not first:
            await self.message.edit(embed=self.embed)
            return

        self.message = await self.channel.send(embed=self.embed)
        for (reaction, _) in self.reaction_emojis:
            if self.maximum_pages == 2 and reaction in ('\u23ed', '\u23ee'):
                # no |<< or >>| buttons if we only have two pages
                # we can't forbid it if someone ends up using it but remove
                # it from the default set
                continue

            await self.message.add_reaction(reaction)

    async def show_help(self):
        """shows this message"""

        self.embed.title = 'Paginator help'
        self.embed.description = 'Hello! Welcome to the help page.'

        messages = [f'{emoji} {func.__doc__}' for emoji, func in self.reaction_emojis]
        self.embed.clear_fields()
        self.embed.add_field(name='What are these reactions for?', value='\n'.join(messages), inline=False)

        self.embed.set_footer(text=f'We were on page {self.current_page} before this message.')
        await self.message.edit(embed=self.embed)

        async def go_back_to_current_page():
            await asyncio.sleep(30.0)
            await self.show_current_page()

        self.bot.loop.create_task(go_back_to_current_page())

    async def show_bot_help(self):
        """shows how to use the bot"""

        self.embed.title = 'Using the bot'
        self.embed.description = 'Hello! Welcome to the help page.'
        self.embed.clear_fields()

        entries = (
            ('<argument>', 'This means the argument is __**required**__.'),
            ('[argument]', 'This means the argument is __**optional**__.'),
            ('[A|B]', 'This means the it can be __**either A or B**__.'),
            ('[argument...]', 'This means you can have multiple arguments.\n' \
                              'Now that you know the basics, it should be noted that...\n' \
                              '__**You do not type in the brackets!**__')
        )

        self.embed.add_field(name='How do I use this bot?', value='Reading the bot signature is pretty simple.')

        for name, value in entries:
            self.embed.add_field(name=name, value=value, inline=False)

        self.embed.set_footer(text=f'We were on page {self.current_page} before this message.')
        await self.message.edit(embed=self.embed)

        async def go_back_to_current_page():
            await asyncio.sleep(30.0)
            await self.show_current_page()

        self.bot.loop.create_task(go_back_to_current_page())
