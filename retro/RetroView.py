import asyncio
import io
import logging
import re
import time
import typing

import discord
from redbot.core import commands

from .emulator import CLIP_SECONDS, FRAMES_PER_SECOND, EmulatorError, RetroEmulator

log = logging.getLogger("red.robloach.retro")

# Frames to hold a button down at the start of a clip (60 frames is one
# second of game time), and frames to run before the first clip so the boot
# logo is out of the way. The configured clip length of each press is
# recorded as the GIF that gets posted.
HOLD_FRAMES = 8
BOOT_FRAMES = 180
DEFAULT_TIMEOUT_MINUTES = 10

# Writing a save state costs a few milliseconds and a couple of hundred
# kilobytes of disk, so it happens every few presses rather than every press.
# A crash or a power cut therefore costs at most this many presses of play.
SAVE_STATE_EVERY_PRESSES = 3

# Every button needs a custom_id that survives a restart, because that is how
# Discord routes a click back to a persistent view. They are scoped per
# message by bot.add_view(view, message_id=...), so fixed ids are fine.
CUSTOM_ID_PREFIX = "libretro"


class RetroView(discord.ui.View):
    """
    An interactive Game Boy controller.

    The view *is* the session: it outlives the emulator. When the emulator is
    freed (idle timeout, Stop, cog unload, bot restart) the session
    hibernates, the controls stay enabled, and the next press transparently
    boots the core again from the cached ROM plus the last save state.

    Anyone in the channel can press the buttons (it's a social feature);
    only the person who started the game, moderators, and the bot owner can
    stop the session.
    """

    def __init__(
        self,
        cog: commands.Cog,
        *,
        game_name: str,
        slug: str,
        rom_filename: str,
        channel_id: int,
        guild_id: typing.Optional[int] = None,
        starter_id: typing.Optional[int] = None,
        source: str = "",
        message_id: typing.Optional[int] = None,
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
        clip_seconds: int = CLIP_SECONDS,
    ) -> None:
        # Persistent views must not time out; idle sessions are hibernated by
        # the cog's background task instead.
        super().__init__(timeout=None)
        self.cog: commands.Cog = cog
        self.game_name: str = game_name
        self.slug: str = slug
        self.rom_filename: str = rom_filename
        self.channel_id: int = channel_id
        self.guild_id: typing.Optional[int] = guild_id
        self.starter_id: typing.Optional[int] = starter_id
        self.source: str = source
        self.message_id: typing.Optional[int] = message_id
        self.timeout_minutes: int = timeout_minutes
        self.clip_seconds: int = clip_seconds
        self.screen_filename: str = self._screen_filename(game_name)

        # The live emulator, or None while hibernated.
        self.emulator: typing.Optional[RetroEmulator] = None
        # The most recent clip, kept in memory so Replay can re-post it.
        self.last_gif: typing.Optional[bytes] = None
        self.press_count: int = 0
        self.last_active: float = time.time()
        # Set when this session is replaced or the cog goes away, so an old
        # message's buttons can never bring its emulator back.
        self.closed: bool = False

        self.message: typing.Optional[discord.Message] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self._colour: typing.Optional[discord.Colour] = None
        self._sync_children()

    # -- Session records ----------------------------------------------------

    def to_record(self) -> dict:
        """The part of this session that is worth writing to Config."""
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "game_name": self.game_name,
            "slug": self.slug,
            "rom_filename": self.rom_filename,
            "source": self.source,
            "starter_id": self.starter_id,
            "last_active": self.last_active,
        }

    @classmethod
    def from_record(
        cls,
        cog: commands.Cog,
        record: dict,
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
        clip_seconds: int = CLIP_SECONDS,
    ) -> "RetroView":
        """Rebuild a hibernated session from Config after a restart."""
        view = cls(
            cog,
            game_name=record.get("game_name") or "Game Boy",
            slug=record.get("slug") or "game",
            rom_filename=record.get("rom_filename") or "",
            channel_id=int(record["channel_id"]),
            guild_id=record.get("guild_id"),
            starter_id=record.get("starter_id"),
            source=record.get("source") or "",
            message_id=record.get("message_id"),
            timeout_minutes=timeout_minutes,
            clip_seconds=clip_seconds,
        )
        view.last_active = float(record.get("last_active") or time.time())
        return view

    # -- State --------------------------------------------------------------

    @property
    def live(self) -> bool:
        """Whether an emulator is currently loaded for this session."""
        return self.emulator is not None

    def retire(self) -> None:
        """
        Make this session's controls inert for good.

        Used when a channel switches to a different game: the old message may
        still be sitting in the channel with working-looking buttons, and
        waking its emulator back up would put two cores in the air at once.
        """
        self.closed = True
        self._set_disabled(True)

    def touch(self) -> None:
        self.last_active = time.time()

    @staticmethod
    def _screen_filename(game_name: str) -> str:
        """A stable, Discord-safe attachment name for this session's clips."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game_name).strip("-")[:48]
        return f"{safe or 'screen'}.gif"

    def _sync_children(self) -> None:
        """
        Show the Stop button only while the emulator is live.

        A hibernated session is already stopped, so offering Stop would be
        confusing. The button carries an explicit ``row``, so removing and
        re-adding it puts it back in the same place.
        """
        present = self.stop_button in self.children
        if self.live and not present:
            self.add_item(self.stop_button)
        elif not self.live and present:
            self.remove_item(self.stop_button)

    def _set_disabled(self, disabled: bool) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = disabled

    # -- Messages -----------------------------------------------------------

    async def _embed_colour(self) -> discord.Colour:
        if self._colour is None:
            channel = self.cog.bot.get_channel(self.channel_id)
            try:
                self._colour = await self.cog.bot.get_embed_colour(channel)
            except Exception:
                # No channel in cache (e.g. straight after a restart), or the
                # bot lost access; the colour is cosmetic either way.
                self._colour = discord.Colour.default()
        return self._colour

    async def _make_embed(self, footer: typing.Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(title=self.game_name, colour=await self._embed_colour())
        embed.set_image(url=f"attachment://{self.screen_filename}")
        if footer is None:
            footer = (
                "Anyone can press the buttons. The game sleeps after "
                f"{self.timeout_minutes} minutes without input and wakes up "
                "on the next press."
            )
        embed.set_footer(text=footer)
        return embed

    async def resolve_message(self) -> typing.Optional[discord.Message]:
        """The session's message, fetched from Discord if it isn't cached."""
        if self.message is not None:
            return self.message
        if self.message_id is None:
            return None
        channel = self.cog.bot.get_channel(self.channel_id)
        if channel is None:
            return None
        try:
            self.message = await channel.fetch_message(self.message_id)
        except discord.HTTPException:
            log.warning(
                "Could not fetch the Libretro message %s in channel %s.",
                self.message_id,
                self.channel_id,
            )
            return None
        return self.message

    async def refresh(self, footer: typing.Optional[str] = None) -> None:
        """
        Re-edit the message with the current controls, keeping the last clip.

        ``attachments`` is deliberately not passed, so Discord keeps the GIF
        that is already on the message.
        """
        message = await self.resolve_message()
        if message is None:
            return
        try:
            await message.edit(embed=await self._make_embed(footer), view=self)
        except discord.HTTPException:
            # The message may have been deleted, or the bot may have lost
            # access to the channel; the session state is still correct.
            log.warning("Failed to refresh the Libretro message.", exc_info=True)

    # -- Starting -----------------------------------------------------------

    async def start(self, ctx: commands.Context, emulator: RetroEmulator) -> discord.Message:
        """Boot the emulator and post the first clip with the controls."""
        self.starter_id = ctx.author.id
        self._colour = await ctx.embed_colour()
        gif = await asyncio.to_thread(self._boot, emulator)
        self.emulator = emulator
        self.last_gif = gif
        self.touch()
        self._sync_children()
        self.message = await ctx.send(
            embed=await self._make_embed(),
            file=discord.File(io.BytesIO(gif), filename=self.screen_filename),
            view=self,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        self.message_id = self.message.id
        return self.message

    @property
    def clip_frames(self) -> int:
        """How many emulated frames one clip covers."""
        return FRAMES_PER_SECOND * self.clip_seconds

    def _boot(self, emulator: RetroEmulator) -> bytes:
        emulator.start()
        # Get past the boot logo first, then record the opening of the game.
        emulator.advance(BOOT_FRAMES)
        return emulator.record(self.clip_frames)

    def run_press(self, button: typing.Optional[str]) -> bytes:
        """Emulate one press and return the clip. Runs in a worker thread."""
        # The press happens inside the recording, so the clip shows the game
        # reacting to it. A button of None is the "Wait" button: four seconds
        # of gameplay with no input at all.
        if self.emulator is None:
            raise EmulatorError("The emulator is not running.")
        press = None if button is None else (button, HOLD_FRAMES)
        return self.emulator.record(self.clip_frames, press=press)

    # -- Interactions -------------------------------------------------------

    async def _press(
        self, interaction: discord.Interaction, button: typing.Optional[str]
    ) -> None:
        if self.closed or self.lock.locked():
            # Either someone else's press is still being emulated, or this
            # message belongs to a session that has been replaced.
            # Acknowledge the click so Discord never shows "interaction
            # failed", but say nothing: nagging everyone who taps a button is
            # just spam.
            await self._silent_ack(interaction)
            return
        async with self.lock:
            resuming = not self.live
            await self._disable_now(interaction, resuming)
            try:
                gif = await self.cog.run_press(self, button)
            except EmulatorError as error:
                log.warning(
                    "Emulation failed in channel %s: %s", self.channel_id, error
                )
                await self._recover(interaction, str(error))
                return
            except Exception:
                log.exception("Unexpected emulator failure in channel %s", self.channel_id)
                await self._recover(interaction, "The emulator hit an unexpected error.")
                return
            self.last_gif = gif
            self.touch()
            await self._show(interaction, gif)

    @staticmethod
    async def _silent_ack(interaction: discord.Interaction) -> None:
        """Acknowledge a click without changing or posting anything."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("Could not acknowledge a dropped Libretro press.", exc_info=True)

    async def _disable_now(self, interaction: discord.Interaction, resuming: bool) -> None:
        """
        Respond to the click immediately by greying out the controls.

        Emulating and encoding a clip takes a second or two, which feels
        broken with no feedback. Editing the message in the interaction
        *response* is instant, and doubles as the "Resuming..." indicator for
        a hibernated session. ``attachments`` is not touched, so this costs
        no upload and the current clip stays put.
        """
        if self.message is None and interaction.message is not None:
            # After a restart the view is rebuilt from Config and has never
            # seen its message; the interaction carries it.
            self.message = interaction.message
            self.message_id = interaction.message.id
        self._set_disabled(True)
        footer = "Resuming where you left off..." if resuming else None
        try:
            await interaction.response.edit_message(
                embed=await self._make_embed(footer), view=self
            )
        except discord.HTTPException:
            log.warning("Could not disable the Libretro controls.", exc_info=True)

    async def _show(self, interaction: discord.Interaction, gif: bytes) -> None:
        """Re-enable the controls and swap in the new clip, in one edit."""
        self._set_disabled(False)
        self._sync_children()
        try:
            # edit_original_response targets the same component message that
            # response.edit_message just updated. attachments= replaces the
            # message's files; omitting it would keep the previous clip.
            self.message = await interaction.edit_original_response(
                embed=await self._make_embed(),
                attachments=[
                    discord.File(io.BytesIO(gif), filename=self.screen_filename)
                ],
                view=self,
            )
        except discord.HTTPException:
            log.warning("Failed to update the Libretro screen.", exc_info=True)

    async def _recover(self, interaction: discord.Interaction, reason: str) -> None:
        """Put the controls back after a failed press and explain why."""
        self._set_disabled(False)
        self._sync_children()
        try:
            await interaction.edit_original_response(
                embed=await self._make_embed(reason), view=self
            )
        except discord.HTTPException:
            log.warning("Failed to report a Libretro failure.", exc_info=True)

    async def can_stop(self, user: typing.Union[discord.Member, discord.User]) -> bool:
        """Whether this user may stop the session."""
        if user.id == self.starter_id:
            return True
        if await self.cog.bot.is_owner(user):
            return True
        if isinstance(user, discord.Member) and user.guild_permissions.manage_messages:
            return True
        return False

    # -- Buttons ------------------------------------------------------------

    @discord.ui.button(emoji="\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0, custom_id=f"{CUSTOM_ID_PREFIX}:press:up")
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "up")

    @discord.ui.button(emoji="\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0, custom_id=f"{CUSTOM_ID_PREFIX}:press:down")
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "down")

    @discord.ui.button(emoji="\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0, custom_id=f"{CUSTOM_ID_PREFIX}:press:left")
    async def left(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "left")

    @discord.ui.button(emoji="\N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.secondary, row=0, custom_id=f"{CUSTOM_ID_PREFIX}:press:right")
    async def right(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "right")

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary, row=1, custom_id=f"{CUSTOM_ID_PREFIX}:press:a")
    async def a(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "a")

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary, row=1, custom_id=f"{CUSTOM_ID_PREFIX}:press:b")
    async def b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "b")

    @discord.ui.button(label="Start", style=discord.ButtonStyle.secondary, row=1, custom_id=f"{CUSTOM_ID_PREFIX}:press:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "start")

    @discord.ui.button(label="Select", style=discord.ButtonStyle.secondary, row=1, custom_id=f"{CUSTOM_ID_PREFIX}:press:select")
    async def select_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._press(interaction, "select")

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE}", label="Wait", style=discord.ButtonStyle.secondary, row=2, custom_id=f"{CUSTOM_ID_PREFIX}:wait")
    async def wait_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Run the game for a few seconds without pressing anything.
        await self._press(interaction, None)

    @discord.ui.button(emoji="\N{CLOCKWISE OPEN CIRCLE ARROW}", label="Replay", style=discord.ButtonStyle.secondary, row=2, custom_id=f"{CUSTOM_ID_PREFIX}:replay")
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Re-post the last clip so it animates again."""
        if self.closed or self.lock.locked():
            await self._silent_ack(interaction)
            return
        if self.last_gif is None:
            # Clips only live in memory, so a restart loses the last one.
            await interaction.response.send_message(
                "That clip is no longer in memory. Press a button to record "
                "a new one.",
                ephemeral=True,
            )
            return
        # Re-uploading the same bytes creates a new attachment, and Discord
        # plays a freshly loaded GIF from the start. The clips are encoded
        # without a loop extension, so this is the only way to see it twice.
        try:
            await interaction.response.edit_message(
                embed=await self._make_embed(),
                attachments=[
                    discord.File(
                        io.BytesIO(self.last_gif), filename=self.screen_filename
                    )
                ],
                view=self,
            )
        except discord.HTTPException:
            log.warning("Failed to replay the Libretro clip.", exc_info=True)

    @discord.ui.button(emoji="\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}", label="Stop", style=discord.ButtonStyle.danger, row=2, custom_id=f"{CUSTOM_ID_PREFIX}:stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.closed:
            await self._silent_ack(interaction)
            return
        if not await self.can_stop(interaction.user):
            await interaction.response.send_message(
                "Only the person who started the game, moderators, or the "
                "bot owner can stop it.",
                ephemeral=True,
            )
            return
        await self._silent_ack(interaction)
        if self.message is None and interaction.message is not None:
            self.message = interaction.message
            self.message_id = interaction.message.id
        async with self.lock:
            await self.cog.hibernate(
                self,
                f"Stopped by {interaction.user.display_name}. "
                "Press a button to pick up where you left off.",
            )
