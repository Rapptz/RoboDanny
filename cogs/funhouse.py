from __future__ import annotations
from typing_extensions import Annotated
from typing import TYPE_CHECKING, Optional

from discord.ext import commands
import discord
import googletrans
import io

if TYPE_CHECKING:
    from bot import RoboDanny
    from .utils.context import Context

GUILD_ID = 81883016288276480
VOICE_ROOM_ID = 633466718035116052
GENERAL_VOICE_ID = 81883016309248000


class Funhouse(commands.Cog):
    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self.trans = googletrans.Translator()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{MAPLE LEAF}')

    def is_outside_voice(self, state: discord.VoiceState) -> bool:
        return state.channel is None or state.channel.id != GENERAL_VOICE_ID

    def is_inside_voice(self, state: discord.VoiceState) -> bool:
        return state.channel is not None and state.channel.id == GENERAL_VOICE_ID

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.guild.id != GUILD_ID:
            return

        voice_room: Optional[discord.TextChannel] = member.guild.get_channel(VOICE_ROOM_ID)  # type: ignore
        if voice_room is None:
            return

        if self.is_outside_voice(before) and self.is_inside_voice(after):
            # joined a channel
            await voice_room.set_permissions(member, read_messages=True)
        elif self.is_outside_voice(after) and self.is_inside_voice(before):
            # left the channel
            await voice_room.set_permissions(member, read_messages=None)

    @commands.command(hidden=True)
    async def cat(self, ctx: Context):
        """Gives you a random cat."""
        async with ctx.session.get('https://api.thecatapi.com/v1/images/search') as resp:
            if resp.status != 200:
                return await ctx.send('No cat found :(')
            js = await resp.json()
            await ctx.send(embed=discord.Embed(title='Random Cat').set_image(url=js[0]['url']))

    @commands.command(hidden=True)
    async def dog(self, ctx: Context):
        """Gives you a random dog."""
        async with ctx.session.get('https://random.dog/woof') as resp:
            if resp.status != 200:
                return await ctx.send('No dog found :(')

            filename = await resp.text()
            url = f'https://random.dog/{filename}'
            filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
            if filename.endswith(('.mp4', '.webm')):
                async with ctx.typing():
                    async with ctx.session.get(url) as other:
                        if other.status != 200:
                            return await ctx.send('Could not download dog video :(')

                        if int(other.headers['Content-Length']) >= filesize:
                            return await ctx.send(f'Video was too big to upload... See it here: {url} instead.')

                        fp = io.BytesIO(await other.read())
                        await ctx.send(file=discord.File(fp, filename=filename))
            else:
                await ctx.send(embed=discord.Embed(title='Random Dog').set_image(url=url))

    @commands.command(hidden=True)
    async def translate(self, ctx: Context, *, message: Annotated[Optional[str], commands.clean_content] = None):
        """Translates a message to English using Google translate."""

        loop = self.bot.loop
        if message is None:
            ref = ctx.message.reference
            if ref and isinstance(ref.resolved, discord.Message):
                message = ref.resolved.content
            else:
                return await ctx.send('Missing a message to translate')

        try:
            ret = await loop.run_in_executor(None, self.trans.translate, message)
        except Exception as e:
            return await ctx.send(f'An error occurred: {e.__class__.__name__}: {e}')

        embed = discord.Embed(title='Translated', colour=0x4284F3)
        src = googletrans.LANGUAGES.get(ret.src, '(auto-detected)').title()
        dest = googletrans.LANGUAGES.get(ret.dest, 'Unknown').title()
        embed.add_field(name=f'From {src}', value=ret.origin, inline=False)
        embed.add_field(name=f'To {dest}', value=ret.text, inline=False)
        await ctx.send(embed=embed)


async def setup(bot: RoboDanny):
    await bot.add_cog(Funhouse(bot))
