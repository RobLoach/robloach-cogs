import asyncio
import io
import logging
import re
import time
import typing
from pathlib import Path

import discord
from redbot.core import commands

from .emulator import (
    CLIP_SECONDS,
    DEFAULT_CLIP_FORMAT,
    EmulatorError,
    RetroEmulator,
    clip_extension,
)
from .systems import (
    CONTROL_BUTTONS,
    MAX_ACTION_ROWS,
    MAX_BUTTONS_PER_ROW,
    MAX_COMPONENTS,
    MAX_LAYOUT_ROWS,
    REPLAY_EMOJI,
    SYSTEMS,
    WAIT_EMOJI,
    System,
    is_spacer,
    system_by_key,
    system_for_extension,
)

log = logging.getLogger("red.robloach.retro")

# How long a button is held down at the start of a clip, in milliseconds, for
# every button including the directions.
#
# Measured against Pokemon Red under Gambatte (59.73 fps), with the player
# already facing the way they were pushed and two clear tiles ahead:
#
#     hold    frames   tiles walked
#     400ms       24        2          <- the old d-pad hold
#     300ms       18        2
#     250ms       15        1
#     160ms       10        1
#     100ms        6        1
#
# A Game Boy walk cycle is 16 frames, so any hold that outlasts it starts a
# second step and the character crosses two tiles for one button press. 160ms
# is ten frames: long enough that a game polling its controller a few times a
# second cannot miss it (the old 8-frame, ~133ms hold could), and short enough
# that one press is always one step and one menu entry.
DEFAULT_HOLD_MS = 160
MIN_HOLD_MS = 50
MAX_HOLD_MS = 2000

# Directions used to be held twice as long as a face button, on the theory
# that moving needs sustained input. On the consoles here it does not: the
# game commits to a whole tile as soon as the step begins, so the only thing
# the extra hold bought was a second step nobody asked for. There is no
# direction multiplier any more, and no set of fields that needs one.
#
# The repeat button taps the console's confirm button this many times, spaced
# this far apart, so text boxes and menus take one round trip instead of
# three. The taps all land in the first second or so, leaving the rest of the
# clip to show where they got you.
REPEAT_TAPS = 3
REPEAT_GAP_MS = 250

# Seconds of emulation to run before the first clip, so the console's boot
# logo is out of the way. Converted to frames with the core's real frame rate.
BOOT_SECONDS = 3

DEFAULT_TIMEOUT_MINUTES = 10

# Writing a save state costs a few milliseconds and a couple of hundred
# kilobytes of disk, so it happens every few presses rather than every press.
# A crash or a power cut therefore costs at most this many presses of play.
SAVE_STATE_EVERY_PRESSES = 3

# Every button needs a custom_id that survives a restart, because that is how
# Discord routes a click back to a persistent view. They are scoped per
# message by bot.add_view(view, message_id=...), so fixed ids are fine.
#
# The prefix is deliberately still "libretro" and must stay that way. It is
# baked into the custom_id of every button on every message this cog has ever
# posted, and Discord routes a click by that exact string; renaming it to
# "retro" would orphan every live game in every channel. The cog's name is
# cosmetic, this is not.
CUSTOM_ID_PREFIX = "libretro"

# The controller grid comes from systems.py (see the row plan there); this
# view adds the three control buttons -- Wait, confirm x3, Replay -- to the
# last row if they fit and to a row of their own if they do not.
#
# The Stop button that used to sit here is gone: `[p]retrostop` is the way to
# put a game to sleep. A message posted before it was removed still has the
# button drawn on it until its next press redraws the row, and a click on that
# stale button resolves to a custom_id this view no longer has -- which
# discord.py's ViewStore.dispatch_view drops silently rather than raising.
_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


class _GameButton(discord.ui.Button):
    """One console button. Clicking it emulates a press and posts a clip."""

    def __init__(self, spec, row: int) -> None:
        super().__init__(
            label=spec.label,
            emoji=spec.emoji,
            style=_STYLES.get(spec.style, discord.ButtonStyle.secondary),
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:press:{spec.field}",
        )
        self.field: str = spec.field

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._press(interaction, self.field)


class _WaitButton(discord.ui.Button):
    """Run the game for a clip's worth of time without pressing anything."""

    def __init__(self, row: int) -> None:
        super().__init__(
            label="Wait",
            emoji=WAIT_EMOJI,
            style=discord.ButtonStyle.secondary,
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:wait",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._press(interaction, None)


class _RepeatButton(discord.ui.Button):
    """Tap the console's confirm button several times in one clip."""

    def __init__(self, spec, row: int) -> None:
        super().__init__(
            label=f"{spec.label} x{REPEAT_TAPS}",
            style=discord.ButtonStyle.primary,
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:repeat",
        )
        self.field: str = spec.field

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._press(interaction, self.field, repeat=REPEAT_TAPS)


class _ReplayButton(discord.ui.Button):
    def __init__(self, row: int) -> None:
        super().__init__(
            label="Replay",
            emoji=REPLAY_EMOJI,
            style=discord.ButtonStyle.secondary,
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:replay",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._replay(interaction)


class _SpacerButton(discord.ui.Button):
    """
    A disabled button that holds a column open in the controller grid.

    Discord has no empty grid cell, so the only way to indent a row is to put
    something inert in front of it. This is permanently disabled, so it is
    never clickable and never re-enabled by _set_disabled(); it still carries
    an explicit custom_id, because a persistent view requires every child to
    have one (discord.ui.Item.is_persistent).
    """

    def __init__(self, label: str, row: int, column: int) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=True,
            custom_id=f"{CUSTOM_ID_PREFIX}:spacer:{row}:{column}",
        )


class RetroView(discord.ui.View):
    """
    An interactive game controller, laid out for whichever console is running.

    The view *is* the session: it outlives the emulator. When the emulator is
    freed (idle timeout, `[p]retrostop`, cog unload, bot restart) the session
    hibernates, the controls stay enabled, and the next press transparently
    boots the core again from the cached ROM plus the last save state.

    Anyone in the channel can press the buttons (it's a social feature); only
    the person who started the game, moderators, and the bot owner can stop
    the session, which is what `[p]retrostop` is for.

    The message carries the clip and nothing else. There is no status card:
    the buttons say what they do, and the only text that ever appears is the
    occasional sentence that has to be said (the game went to sleep, a save
    state could not be restored, an emulator error).
    """

    def __init__(
        self,
        cog: commands.Cog,
        *,
        game_name: str,
        slug: str,
        rom_filename: str,
        channel_id: int,
        system: typing.Optional[System] = None,
        guild_id: typing.Optional[int] = None,
        starter_id: typing.Optional[int] = None,
        source: str = "",
        message_id: typing.Optional[int] = None,
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
        clip_seconds: int = CLIP_SECONDS,
        hold_ms: int = DEFAULT_HOLD_MS,
        clip_format: str = DEFAULT_CLIP_FORMAT,
    ) -> None:
        # Persistent views must not time out; idle sessions are hibernated by
        # the cog's background task instead.
        super().__init__(timeout=None)
        self.cog: commands.Cog = cog
        self.game_name: str = game_name
        self.slug: str = slug
        self.rom_filename: str = rom_filename
        self.channel_id: int = channel_id
        self.system: System = system or system_for_extension(
            Path(rom_filename).suffix
        ) or SYSTEMS[0]
        self.guild_id: typing.Optional[int] = guild_id
        self.starter_id: typing.Optional[int] = starter_id
        self.source: str = source
        self.message_id: typing.Optional[int] = message_id
        self.timeout_minutes: int = timeout_minutes
        self.clip_seconds: int = clip_seconds
        self.hold_ms: int = hold_ms
        self.clip_format: str = clip_format
        self.screen_filename: str = self._screen_filename(game_name, clip_format)

        # The live emulator, or None while hibernated.
        self.emulator: typing.Optional[RetroEmulator] = None
        # The most recent clip, kept in memory so Replay can re-post it.
        self.last_clip: typing.Optional[bytes] = None
        self.press_count: int = 0
        self.last_active: float = time.time()
        # Set when this session is replaced or the cog goes away, so an old
        # message's buttons can never bring its emulator back.
        self.closed: bool = False
        # A one-off sentence to show with the next clip, then forget. Used
        # when waking a session tells the player something they need to know
        # -- that the save state was rejected after a core update and the game
        # came back from its battery save instead, for instance. It rides on
        # the next message edit rather than being a second message, so it
        # lands with the clip it explains, and is cleared as it is shown.
        self.notice: typing.Optional[str] = None

        self.message: typing.Optional[discord.Message] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self._build_controls()

    # -- Layout -------------------------------------------------------------

    def _build_controls(self) -> None:
        """
        Lay this console's controller out; see the row plan in systems.py.

        The console's own grid comes first, spacers and all, and the three
        control buttons go on the end of the last row if they fit there and on
        a row of their own if they do not.
        """
        rows = self.system.rows
        if len(rows) > MAX_LAYOUT_ROWS:
            raise ValueError(
                f"{self.system.name} declares {len(rows)} controller rows; "
                f"at most {MAX_LAYOUT_ROWS} fit alongside the controls."
            )
        for index, row in enumerate(rows):
            if len(row) > MAX_BUTTONS_PER_ROW:
                raise ValueError(
                    f"{self.system.name} row {index} has {len(row)} "
                    f"components; Discord allows {MAX_BUTTONS_PER_ROW}."
                )
            for column, spec in enumerate(row):
                if is_spacer(spec):
                    self.add_item(_SpacerButton(spec.label, index, column))
                else:
                    self.add_item(_GameButton(spec, row=index))

        # Wait / confirm x3 / Replay. They share the last row when there is
        # space, which is how Start and Select end up beside them.
        last = len(rows) - 1
        if len(rows[last]) + CONTROL_BUTTONS <= MAX_BUTTONS_PER_ROW:
            control_row = last
        else:
            control_row = len(rows)
        if control_row >= MAX_ACTION_ROWS:
            raise ValueError(
                f"{self.system.name} leaves no room for the Wait/Replay row; "
                f"Discord allows {MAX_ACTION_ROWS} rows."
            )
        self.add_item(_WaitButton(control_row))
        confirm = self.system.button(self.system.confirm)
        if confirm is not None:
            self.add_item(_RepeatButton(confirm, control_row))
        self.add_item(_ReplayButton(control_row))
        if len(self.children) > MAX_COMPONENTS:
            raise ValueError(
                f"{self.system.name} needs {len(self.children)} components; "
                f"Discord allows {MAX_COMPONENTS}."
            )

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
            # The console (and therefore the core) this session was started
            # with, so a restart resumes it on the same emulator rather than
            # guessing from the file extension again.
            "system": self.system.key,
            "core": self.system.core,
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
        hold_ms: int = DEFAULT_HOLD_MS,
    ) -> "RetroView":
        """Rebuild a hibernated session from Config after a restart."""
        rom_filename = record.get("rom_filename") or ""
        # Sessions written before multi-console support have no "system", so
        # fall back to the ROM's extension and then to the Game Boy.
        system = system_by_key(record.get("system") or "") or system_for_extension(
            Path(rom_filename).suffix
        )
        view = cls(
            cog,
            game_name=record.get("game_name") or "Game",
            slug=record.get("slug") or "game",
            rom_filename=rom_filename,
            channel_id=int(record["channel_id"]),
            system=system,
            guild_id=record.get("guild_id"),
            starter_id=record.get("starter_id"),
            source=record.get("source") or "",
            message_id=record.get("message_id"),
            timeout_minutes=timeout_minutes,
            clip_seconds=clip_seconds,
            hold_ms=hold_ms,
        )
        view.last_active = float(record.get("last_active") or time.time())
        return view

    # -- State --------------------------------------------------------------

    @property
    def live(self) -> bool:
        """Whether an emulator is currently loaded for this session."""
        return self.emulator is not None

    @property
    def core(self) -> str:
        """The libretro core this session needs."""
        return self.system.core

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
    def _screen_filename(game_name: str, clip_format: str = DEFAULT_CLIP_FORMAT) -> str:
        """A stable, Discord-safe attachment name for this session's clips."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game_name).strip("-")[:48]
        return f"{safe or 'screen'}{clip_extension(clip_format)}"

    def _set_disabled(self, disabled: bool) -> None:
        """Grey the controls out, or bring them back. Spacers stay inert."""
        for child in self.children:
            if isinstance(child, _SpacerButton):
                continue
            if hasattr(child, "disabled"):
                child.disabled = disabled

    # -- Messages -----------------------------------------------------------

    def _content(self, message: typing.Optional[str] = None) -> typing.Optional[str]:
        """
        The text to put on the message with the clip, which is usually none.

        The clip and the buttons are the whole interface: a card repeating the
        console's name over a picture of that console is noise. Text appears
        only when there is something to say -- ``message`` from the caller
        (the game went to sleep, the emulator failed), or a pending one-off
        notice, which is cleared as it is shown so it appears exactly once.

        Returning None is meaningful rather than lazy: discord.py sends an
        explicit null for it, which *clears* whatever the message said before,
        so yesterday's "asleep" line does not linger over today's clip.
        """
        if message is not None:
            return message
        notice, self.notice = self.notice, None
        return notice

    def _clip_file(self, data: bytes) -> discord.File:
        """
        The clip, as a plain attachment.

        It has to be one: Discord only animates an animated WebP when it is
        attached directly. Put the same bytes in an embed's image and it is
        shown as a single still frame, which is why the message has never
        carried the clip inside an embed (and now carries no embed at all).
        """
        return discord.File(io.BytesIO(data), filename=self.screen_filename)

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

    async def refresh(self, note: typing.Optional[str] = None) -> None:
        """
        Re-edit the message with the current controls, keeping the last clip.

        ``attachments`` is deliberately not passed, so Discord keeps the clip
        that is already on the message.
        """
        message = await self.resolve_message()
        if message is None:
            return
        try:
            await message.edit(content=self._content(note), view=self)
        except discord.HTTPException:
            # The message may have been deleted, or the bot may have lost
            # access to the channel; the session state is still correct.
            log.warning("Failed to refresh the Libretro message.", exc_info=True)

    # -- Starting -----------------------------------------------------------

    async def start(self, ctx: commands.Context, emulator: RetroEmulator) -> discord.Message:
        """Boot the emulator and post the first clip with the controls."""
        self.starter_id = ctx.author.id
        clip = await asyncio.to_thread(self._boot, emulator)
        self.emulator = emulator
        self.last_clip = clip
        self.touch()
        self.message = await ctx.send(
            self._content(),
            file=self._clip_file(clip),
            view=self,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        self.message_id = self.message.id
        return self.message

    def clip_frames(self, emulator: RetroEmulator) -> int:
        """How many emulated frames one clip covers on this console."""
        return emulator.frames_for_seconds(self.clip_seconds)

    def _boot(self, emulator: RetroEmulator) -> bytes:
        emulator.start()
        # Get past the boot logo first, then record the opening of the game.
        emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
        return self._record(emulator, None)

    def _schedule(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int
    ) -> typing.List[tuple]:
        """
        Work out when, and for how long, to hold a button during a clip.

        Every button, direction or not, is held for exactly ``hold_ms``; see
        DEFAULT_HOLD_MS for why the directions no longer get a multiplier.
        """
        if field is None:
            return []
        hold = emulator.frames_for_ms(self.hold_ms)
        gap = emulator.frames_for_ms(REPEAT_GAP_MS)
        return [(field, tap * (hold + gap), hold) for tap in range(max(1, repeat))]

    def _record(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int = 1
    ) -> bytes:
        return emulator.record(
            self.clip_frames(emulator),
            presses=self._schedule(emulator, field, repeat),
            clip_format=self.clip_format,
        )

    def run_press(self, field: typing.Optional[str], repeat: int = 1) -> bytes:
        """Emulate one press and return the clip. Runs in a worker thread."""
        # The press happens inside the recording, so the clip shows the game
        # reacting to it. A field of None is the "Wait" button: a clip's worth
        # of gameplay with no input at all.
        if self.emulator is None:
            raise EmulatorError("The emulator is not running.")
        return self._record(self.emulator, field, repeat)

    # -- Interactions -------------------------------------------------------

    async def _press(
        self,
        interaction: discord.Interaction,
        field: typing.Optional[str],
        repeat: int = 1,
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
                clip = await self.cog.run_press(self, field, repeat)
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
            self.last_clip = clip
            self.touch()
            await self._show(interaction, clip)

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
        *response* is instant, and doubles as the "Resuming..." line for a
        hibernated session, which has a core to load and a save state to
        restore before it can even start emulating. ``attachments`` is not
        touched, so this costs no upload and the current clip stays put.
        """
        if self.message is None and interaction.message is not None:
            # After a restart the view is rebuilt from Config and has never
            # seen its message; the interaction carries it.
            self.message = interaction.message
            self.message_id = interaction.message.id
        self._set_disabled(True)
        note = "Resuming where you left off\N{HORIZONTAL ELLIPSIS}" if resuming else None
        try:
            await interaction.response.edit_message(
                content=self._content(note), view=self
            )
        except discord.HTTPException:
            log.warning("Could not disable the Libretro controls.", exc_info=True)

    @staticmethod
    async def _whisper(interaction: discord.Interaction, message: str) -> None:
        """
        Tell just the person who clicked, without ever raising.

        Used when the message edit itself failed, so there is nowhere else to
        put the explanation and no point making a second failure louder.
        """
        followup = getattr(interaction, "followup", None)
        if followup is None:
            return
        try:
            await followup.send(message, ephemeral=True)
        except Exception:
            log.debug("Could not deliver a Libretro failure notice.", exc_info=True)

    async def _show(self, interaction: discord.Interaction, clip: bytes) -> None:
        """Re-enable the controls and swap in the new clip, in one edit."""
        self._set_disabled(False)
        try:
            # edit_original_response targets the same component message that
            # response.edit_message just updated. attachments= replaces the
            # message's files; omitting it would keep the previous clip, and
            # content=None clears whatever was said before it.
            self.message = await interaction.edit_original_response(
                content=self._content(),
                attachments=[self._clip_file(clip)],
                view=self,
            )
        except discord.HTTPException as error:
            # The game itself is fine, so say so rather than leaving the
            # controls looking broken. The traceback goes to the log: this is
            # how an invalid button emoji shows up in production.
            log.exception(
                "Failed to update the Libretro screen in channel %s.", self.channel_id
            )
            await self._whisper(
                interaction,
                "Discord would not accept the new clip "
                f"(HTTP {getattr(error, 'status', '?')}). The game is safe "
                "and was saved; try another press.",
            )

    async def _recover(self, interaction: discord.Interaction, reason: str) -> None:
        """Put the controls back after a failed press and explain why."""
        self._set_disabled(False)
        try:
            await interaction.edit_original_response(
                content=self._content(reason), view=self
            )
        except discord.HTTPException:
            log.warning("Failed to report a Libretro failure.", exc_info=True)

    async def _replay(self, interaction: discord.Interaction) -> None:
        """Re-post the last clip so it animates again."""
        if self.closed or self.lock.locked():
            await self._silent_ack(interaction)
            return
        if self.last_clip is None:
            # Clips only live in memory, so a restart loses the last one.
            await interaction.response.send_message(
                "That clip is no longer in memory. Press a button to record "
                "a new one.",
                ephemeral=True,
            )
            return
        # Re-uploading the same bytes creates a new attachment, and Discord
        # plays a freshly loaded clip from the start. The clips are encoded to
        # play through exactly once, so this is the only way to see it twice.
        try:
            await interaction.response.edit_message(
                content=self._content(),
                attachments=[self._clip_file(self.last_clip)],
                view=self,
            )
        except discord.HTTPException:
            log.exception(
                "Failed to replay the Libretro clip in channel %s.", self.channel_id
            )
            await self._whisper(
                interaction, "Discord would not accept that clip again."
            )

    async def can_stop(self, user: typing.Union[discord.Member, discord.User]) -> bool:
        """
        Whether this user may stop the session with `[p]retrostop`.

        Anyone in the channel can play; ending someone else's game is the one
        thing that is not open to everybody.
        """
        if user.id == self.starter_id:
            return True
        if await self.cog.bot.is_owner(user):
            return True
        if isinstance(user, discord.Member) and user.guild_permissions.manage_messages:
            return True
        return False
