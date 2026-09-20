import asyncio
import io
import logging
import re
import typing

import discord
from redbot.core import commands

from .emulator import CLIP_FRAMES, EmulatorError, GameBoyEmulator

log = logging.getLogger("red.robloach.pyboy")

# Frames to hold a button down at the start of a clip (60 frames is one
# second of game time), and frames to run before the first clip so the boot
# logo is out of the way. CLIP_FRAMES (240, four seconds) of each press is
# recorded as the GIF that gets posted.
HOLD_FRAMES = 8
BOOT_FRAMES = 180
DEFAULT_TIMEOUT_MINUTES = 10


class PyBoyView(discord.ui.View):
    """
    An interactive Game Boy controller.

    Anyone in the channel can press the buttons (it's a social feature);
    only the person who started the game, moderators, and the bot owner can
    stop the session.
    """

    def __init__(
        self,
        cog: commands.Cog,
        emulator: GameBoyEmulator,
        game_name: str,
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    ) -> None:
        super().__init__(timeout=timeout_minutes * 60)
        self.cog: commands.Cog = cog
        self.emulator: GameBoyEmulator = emulator
        self.game_name: str = game_name
        self.timeout_minutes: int = timeout_minutes
        self.screen_filename: str = self._screen_filename(game_name)
        self.ctx: typing.Optional[commands.Context] = None
        self.starter_id: typing.Optional[int] = None
        self.message: typing.Optional[discord.Message] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self.closed: bool = False

    @staticmethod
    def _screen_filename(game_name: str) -> str:
        """A stable, Discord-safe attachment name for this session's clips."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game_name).strip("-")[:48]
        return f"{safe or 'screen'}.gif"

    async def start(self, ctx: commands.Context) -> discord.Message:
        """Boot the emulator and post the first clip with the controls."""
        self.ctx = ctx
        self.starter_id = ctx.author.id
        gif = await asyncio.to_thread(self._boot)
        embed = await self._make_embed()
        self.message = await ctx.send(
            embed=embed,
            file=discord.File(io.BytesIO(gif), filename=self.screen_filename),
            view=self,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        return self.message

    def _boot(self) -> bytes:
        self.emulator.start()
        # Get past the boot logo first, then record the opening of the game.
        self.emulator.advance(BOOT_FRAMES)
        return self.emulator.record(CLIP_FRAMES)

    async def _make_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.game_name,
            colour=await self.ctx.embed_colour(),
        )
        embed.set_image(url=f"attachment://{self.screen_filename}")
        embed.set_footer(
            text="Anyone can press the buttons. The session ends after "
            f"{self.timeout_minutes} minutes without input."
        )
        return embed

    async def _press(self, interaction: discord.Interaction, button: typing.Optional[str]) -> None:
        if self.closed:
            return
        # Acknowledge the click first so Discord never shows "interaction
        # failed", even when the press is dropped below.
        await interaction.response.defer()
        if self.lock.locked():
            # Someone else's press is still being emulated; drop this one
            # silently rather than nagging everyone who taps a button.
            return
        async with self.lock:
            if self.closed:
                return
            try:
                gif = await asyncio.to_thread(self._run_press, button)
            except EmulatorError as error:
                log.exception("Emulation failed in channel %s", interaction.channel_id)
                await self.close(f"The emulator crashed: {error}")
                return
            await self._update_screen(gif)

    def _run_press(self, button: typing.Optional[str]) -> bytes:
        # The press happens inside the recording, so the clip shows the game
        # reacting to it. A button of None is the "Wait" button: four seconds
        # of gameplay with no input at all.
        press = None if button is None else (button, HOLD_FRAMES)
        return self.emulator.record(CLIP_FRAMES, press=press)

    async def _update_screen(self, gif: bytes) -> None:
        if self.message is None:
            return
        try:
            # attachments= replaces the message's files; omitting it (or
            # passing file=) would keep the previous clip instead.
            await self.message.edit(
                embed=await self._make_embed(),
                attachments=[
                    discord.File(io.BytesIO(gif), filename=self.screen_filename)
                ],
                view=self,
            )
        except discord.HTTPException:
            log.warning("Failed to update the PyBoy screen.", exc_info=True)

    async def can_stop(self, user: typing.Union[discord.Member, discord.User]) -> bool:
        """Whether this user may stop the session."""
        if user.id == self.starter_id:
            return True
        if await self.cog.bot.is_owner(user):
            return True
        if isinstance(user, discord.Member) and user.guild_permissions.manage_messages:
            return True
        return False

    async def close(self, reason: str) -> None:
        """Stop the session, free the emulator, and disable the controls."""
        if self.closed:
            return
        self.closed = True
        self.stop()
        if self.ctx is not None:
            self.cog.sessions.pop(self.ctx.channel.id, None)
        await asyncio.to_thread(self.emulator.stop)
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is not None:
            try:
                embed = await self._make_embed()
                embed.set_footer(text=reason)
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                # The message may have been deleted, or the bot may have
                # lost access to the channel; the session is closed anyway.
                log.warning("Failed to edit the PyBoy message on close.", exc_info=True)

    async def on_timeout(self) -> None:
        await self.close("The Game Boy session timed out.")

    # -- Buttons ------------------------------------------------------------

    @discord.ui.button(emoji="\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0)
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "up")

    @discord.ui.button(emoji="\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0)
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "down")

    @discord.ui.button(emoji="\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0)
    async def left(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "left")

    @discord.ui.button(emoji="\N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0)
    async def right(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "right")

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary, row=1)
    async def a(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "a")

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary, row=1)
    async def b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "b")

    @discord.ui.button(label="Start", style=discord.ButtonStyle.secondary, row=1)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "start")

    @discord.ui.button(label="Select", style=discord.ButtonStyle.secondary, row=1)
    async def select_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "select")

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE}", label="Wait", style=discord.ButtonStyle.secondary, row=2)
    async def wait_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Run the game for a few seconds without pressing anything.
        await self._press(interaction, None)

    @discord.ui.button(emoji="\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}", label="Stop", style=discord.ButtonStyle.danger, row=2)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.can_stop(interaction.user):
            await interaction.response.send_message(
                "Only the person who started the game, moderators, or the "
                "bot owner can stop it.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        async with self.lock:
            await self.close(f"Stopped by {interaction.user.display_name}.")
