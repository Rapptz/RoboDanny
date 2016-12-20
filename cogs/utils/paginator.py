import asyncio

class StopPagination(Exception):
    pass

class CannotPaginate(Exception):
    pass

class Pages:
    """Implements a paginator that queries the user for the
    pagination interface.

    Pages are 1-index based, not 0-index based.

    If the user does not reply within 1 minute then the pagination
    interface exits automatically.

    Parameters
    ------------
    bot
        The bot instance.
    message
        The message that initiated this session.
    entries
        A list of entries to paginate.
    per_page
        How many entries show up per page.
    """
    def __init__(self, bot, *, message, entries, per_page=12):
        self.bot = bot
        self.entries = entries
        self.message = message
        self.author = message.author
        self.per_page = per_page
        pages, left_over = divmod(len(self.entries), self.per_page)
        if left_over:
            pages += 1
        self.maximum_pages = pages
        self.reaction_emojis = [
            ('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.first_page, ':track_previous:'),
            ('\N{BLACK LEFT-POINTING TRIANGLE}', self.previous_page, ':arrow_backward:'),
            ('\N{BLACK RIGHT-POINTING TRIANGLE}', self.next_page, ':arrow_forward:'),
            ('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', self.last_page, ':track_next:'),
            ('\N{INPUT SYMBOL FOR NUMBERS}', self.numbered_page , ':1234:'),
            ('\N{BLACK SQUARE FOR STOP}', self.stop_pages, ':stop_button:'),
            ('\N{INFORMATION SOURCE}', self.show_help, ':information_source:'),
        ]

    def get_page(self, page):
        base = (page - 1) * self.per_page
        return self.entries[base:base + self.per_page]

    async def show_page(self, page, *, first=False, use_reactions=True):
        self.current_page = page
        entries = self.get_page(page)
        p = ['```']
        for t in enumerate(entries, 1 + ((page - 1) * self.per_page)):
            p.append('%s. %s' % t)
        p.append('')
        p.append('Page %s/%s (%s entries)' % (page, self.maximum_pages, len(self.entries)))

        if not use_reactions:
            p.append('```')
            self.message = await self.bot.send_message(self.message.channel, '\n'.join(p))
            return

        if first:
            # verify we can actually use the pagination session
            server = self.message.server
            if server is not None:
                permissions = self.message.channel.permissions_for(server.me)
                if not permissions.add_reactions:
                    raise CannotPaginate()

            p.append('')
            p.append('Confused? React with \N{INFORMATION SOURCE} for more info.')
            p.append('```')
            self.message = await self.bot.send_message(self.message.channel, '\n'.join(p))
            for (reaction, _, __) in self.reaction_emojis:
                await self.bot.add_reaction(self.message, reaction)
        else:
            p.append('```')
            await self.bot.edit_message(self.message, '\n'.join(p))

    async def checked_show_page(self, page):
        if page == 0:
            return await self.bot.send_message(self.message.channel, 'Page 0 does not exist.')
        elif page > self.maximum_pages:
            return await self.bot.send_message(self.message.channel, 'Too far ahead (%s/%s)' % (page, self.maximum_pages))
        else:
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

    async def page_c(self):
        await self.checked_show_page(self.current_page)

    async def numbered_page(self):
        """lets you type a page number to go to"""
        to_delete = []
        to_delete.append(await self.bot.send_message(self.message.channel, 'What page do you want to go to?'))
        msg = await self.bot.wait_for_message(author=self.author, channel=self.message.channel,
                                              check=lambda m: m.content.isdigit(), timeout=30.0)
        if msg is not None:
            page = int(msg.content)
            to_delete.append(msg)
            ret = await self.checked_show_page(page)
            if ret is not None:
                to_delete.append(ret)
        else:
            to_delete.append(await self.bot.send_message(self.message.channel, 'Took too long.'))
            await asyncio.sleep(5)

        try:
            await self.bot.delete_messages(to_delete)
        except Exception:
            pass

    async def show_help(self):
        """shows this message"""
        messages = ['```\nWelcome to the interactive paginator!\n']
        messages.append('This interactively allows you to see pages of text by navigating with\n'
                        'reactions. They are as follows:\n')

        alignment = len(max(self.reaction_emojis, key=lambda t: len(t[2]))[2])
        fmt = '{:<{width}} -- {}'
        for (_, func, emoji) in self.reaction_emojis:
            messages.append(fmt.format(emoji, func.__doc__, width=alignment))

        messages.append('```')
        msg = await self.bot.send_message(self.message.channel, '\n'.join(messages))

        async def delete_later():
            await asyncio.sleep(120)
            await self.bot.delete_message(msg)

        self.bot.loop.create_task(delete_later())

    async def stop_pages(self):
        """stops the interactive pagination session"""
        await self.bot.delete_message(self.message)
        raise StopPagination()

    def react_check(self, reaction, user):
        if user.id != self.author.id:
            return False

        for (emoji, func, _) in self.reaction_emojis:
            if reaction.emoji == emoji:
                self.match = func
                return True
        return False

    async def paginate(self):
        """Actually paginate the entries and run the interactive loop if necessary."""

        # Only do the interactive session if necessary (i.e. page count is more than 1)
        use_reactions = len(self.entries) > self.per_page
        await self.show_page(1, first=True, use_reactions=use_reactions)
        while use_reactions:
            react = await self.bot.wait_for_reaction(message=self.message, check=self.react_check, timeout=60.0)
            if react is None:
                break

            try:
                await self.bot.remove_reaction(self.message, react.reaction.emoji, react.user)
            except:
                pass # can't remove it so don't bother doing so

            try:
                await self.match()
            except StopPagination:
                break
