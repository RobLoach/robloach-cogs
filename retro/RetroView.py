import asyncio
import collections
import io
import logging
import re
import time
import typing
import zlib
from pathlib import Path

import discord
from redbot.core import commands

from .emulator import (
    CLIP_SECONDS,
    DEFAULT_CLIP_FORMAT,
    DEFAULT_FPS,
    MAX_REPLAY_BYTES,
    MAX_REPLAY_CLIPS,
    MAX_REPLAY_FRAMES,
    REPLAY_SECONDS,
    EmulatorError,
    RetroEmulator,
    clamp_clip_seconds,
    clip_extension,
    clip_frame_count,
    concatenate_clips,
    format_seconds,
    frame_count,
    input_budget,
)
from .systems import (
    CONTROL_BUTTONS,
    MAX_ACTION_ROWS,
    MAX_BUTTONS_PER_ROW,
    MAX_COMPONENTS,
    MAX_LAYOUT_ROWS,
    REPLAY_EMOJI,
    RESUME_EMOJI,
    SYSTEMS,
    UNDO_EMOJI,
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
#
# It is a ceiling, not a promise: a clip has to have room to show the button
# come back up, so on a short clip the hold is cut to fit. See press_plan.
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
# three.
#
# 3 x 160ms held with 250ms between them is 1.4 seconds of schedule, which
# was nothing inside a four second clip and does not fit inside a one second
# one at all -- the third tap would have been shoved onto the clip's last
# frame and the player would have seen two taps and a twitch. So the spacing
# is squeezed down to MIN_REPEAT_GAP_MS to make the taps fit (three taps
# closer together are still three taps), and only when even that will not fit
# is a tap dropped; the button then says how many it really does. See
# press_plan.
#
# The floor on the spacing is what a game needs to see the button come back
# up between taps: five frames at 59.73 fps, comfortably more than the one or
# two frames a per-frame input poll needs and still tight enough to fit three
# taps into a one second clip.
REPEAT_TAPS = 3
REPEAT_GAP_MS = 250
MIN_REPEAT_GAP_MS = 80

# Seconds of emulation to run before the first clip, so the console's boot
# logo is out of the way. Converted to frames with the core's real frame rate.
#
# Deliberately not tied to the clip length: how long a Game Boy takes to get
# past its logo has nothing to do with how much of the game a press shows, so
# a one second clip still gets three seconds of boot. It is skipped entirely
# when a save state comes back, since that is already past the title screen.
BOOT_SECONDS = 3

DEFAULT_TIMEOUT_MINUTES = 10

# What a press says when it had to wake the session up first. Past tense on
# purpose: it is shown *with* the clip rather than before it, because a press
# makes exactly one edit to the message now (see RetroView._ack_now), so by
# the time anybody reads it the game is already back. Cleared by the next
# press, like every other one-off line.
RESUMED_NOTE = "Resumed where you left off\N{HORIZONTAL ELLIPSIS}"

# Writing a save state costs a few milliseconds and a couple of hundred
# kilobytes of disk, so it happens every few presses rather than every press.
# A crash or a power cut therefore costs at most this many presses of play.
SAVE_STATE_EVERY_PRESSES = 3

# What the Undo button says when it has put the game back.
UNDONE_NOTE = "Undid the last press."

# -- Undo ----------------------------------------------------------------------
#
# In turn-based play by button the dominant frustration is a misclick: you
# press a direction, wait a second for the clip, and find you walked into the
# wrong room. So every press pushes the machine state it is about to change
# onto a bounded per-session stack first, and Undo pops it back.
#
# It is affordable because a save state is cheap in both directions.
# `save_state()` is sub-millisecond, and a state is almost all zeroes, so it
# compresses enormously. Measured on the real cores, two seconds after boot,
# on a Raspberry Pi 5:
#
#     console             state     zlib 1   ratio     save   compress
#     Game Boy          182,530     15,780    8.6%   0.19ms     0.92ms
#     NES                13,758        870    6.3%   0.11ms     0.10ms
#     Game Boy Advance  528,448      6,298    1.2%   0.79ms     1.01ms
#     Super Nintendo    823,407     12,606    1.5%   0.56ms     1.52ms
#     Genesis         1,036,288     19,524    1.9%   0.40ms     2.13ms
#     Master System   1,036,288      7,991    0.8%   0.41ms     1.70ms
#
# and over eight real undo points taken a second of play apart, which is
# what a session actually holds: 124 KiB on the Game Boy, 99 KiB on the
# SNES, 152 KiB on the Genesis, worst single entry 19.1 KiB. A couple of
# milliseconds and a hundred kilobytes per session, both on a press that is
# already spending tens of milliseconds recording a clip.
#
# Level 1 rather than the default 6 deliberately: it is roughly twice as
# fast and, on data this sparse, within a few kilobytes of the same size
# (the Game Boy state is 15,780 bytes at level 1 and 13,373 at level 6).
# Compressing happens in the worker thread that is emulating the press, so
# it never touches the event loop; see RetroView.run_press.
UNDO_COMPRESSION_LEVEL = 1

# How many presses back Undo can reach. Eight is enough to walk out of a
# corridor you should never have gone down, and small enough that the memory
# is not worth thinking about.
UNDO_DEPTH = 8

# ...and the same bound in bytes, because the count alone is not one: the
# numbers above are what today's consoles cost, and a core update or a bigger
# machine can change them without anybody editing this file. A quarter of the
# replay buffer's 8 MiB, which at the measured sizes is 100 times more than
# UNDO_DEPTH states ever need; it only bites if a state compresses to a
# quarter of a megabyte, and then it keeps fewer of them instead of growing.
# One entry is always kept, even if it is over the cap on its own, for the
# same reason _trim_clips never empties the replay buffer completely.
MAX_UNDO_BYTES = 2 * 1024 * 1024

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


def press_plan(
    fps: float,
    clip_seconds: float,
    hold_ms: float = DEFAULT_HOLD_MS,
    taps: int = 1,
) -> typing.List[typing.Tuple[int, int]]:
    """
    When to hold a button, and for how long, inside one clip.

    Returns ``(start frame, hold frames)`` pairs measured against the frames
    of the clip itself, which is exactly what ``RetroEmulator.record`` takes.
    A plain press is one pair starting at frame 0; the repeat button asks for
    ``REPEAT_TAPS`` of them.

    Everything it returns is released by :func:`input_budget`, i.e. by the
    last frame of the clip that is actually captured, so the clip's final
    picture always shows the game *after* the input. That is the whole reason
    this is not two lines: a clip used to be four seconds, where a 160ms hold
    and three taps 250ms apart fitted with two seconds to spare, and at a
    fifth of a second neither fits at all.

    Two things give, in this order:

    * **the spacing**, down to MIN_REPEAT_GAP_MS. Three taps 117ms apart are
      still three taps, and this is what keeps the repeat button useful at
      0.8-1s clips.
    * **the number of taps**, one at a time, when even the floor will not
      fit. Two taps is a worthwhile repeat button; one is just the confirm
      button, and the view greys it out rather than pretending (see
      :meth:`RetroView._update_repeat_label`).

    The hold itself is clamped last-ditch: it can never be longer than the
    budget, so a 0.2s clip (12 frames, budget 8) with a 400ms hold holds the
    button for 8 frames -- about 134ms -- and shows one picture of the
    release. The configured hold is a ceiling, not a promise, and
    `[p]retroset hold` says so when the clip is too short to honour it.
    """
    frames = clip_frame_count(fps, clip_seconds)
    budget = input_budget(fps, frames)
    hold = max(1, min(frame_count(fps, float(hold_ms) / 1000.0), budget))
    gap = frame_count(fps, REPEAT_GAP_MS / 1000.0)
    floor = min(gap, frame_count(fps, MIN_REPEAT_GAP_MS / 1000.0))
    taps = max(1, int(taps))
    while taps > 1:
        room = budget - taps * hold
        if room >= floor * (taps - 1):
            gap = min(gap, room // (taps - 1))
            break
        taps -= 1
    return [(tap * (hold + gap), hold) for tap in range(taps)]


class Progress(typing.NamedTuple):
    """
    One game's saved progress, in the order a boot is willing to try it.

    Four files rather than two, because every save keeps one previous
    generation (see BACKUP_SUFFIX in Retro.py): a successful write of a *bad*
    save is not something an atomic write can protect anybody from, and a save
    state is the thing players care most about. The backups are only ever
    reached when the newer file cannot be used.
    """

    #: The most recent save state, and the generation before it.
    state: typing.Optional[bytes] = None
    state_backup: typing.Optional[bytes] = None
    #: The cartridge's battery memory, and the generation before it.
    sram: typing.Optional[bytes] = None
    sram_backup: typing.Optional[bytes] = None

    @property
    def has_state(self) -> bool:
        """Whether there is any save state at all to try."""
        return bool(self.state or self.state_backup)

    @property
    def has_anything(self) -> bool:
        return bool(self.state or self.state_backup or self.sram or self.sram_backup)


#: What a restore did, in the order :func:`restore_into` tries them. Read by
#: ``Retro._settle_boot`` to decide what to say and what to throw away.
RESTORE_ORDER = ("state", "backup-state", "sram", "backup-sram", "fresh")


def restore_into(
    emulator: RetroEmulator,
    progress: typing.Optional[Progress] = None,
    slug: str = "",
) -> str:
    """
    Start a core and put back as much of a game as is restorable.

    The one implementation of the save state -> previous save state -> battery
    save -> previous battery save -> cold boot chain. Both paths that bring a
    game up go through it: starting a game the channel has played before
    (:meth:`RetroView._boot`) and waking a hibernated session
    (``Retro._wake_locked``). They used to have a copy each and were kept in
    step by the tests rather than by construction.

    The order is the order of confidence. A save state is the exact moment and
    is tried first; its previous generation is the same thing one autosave
    older, which is worth far more than the title screen. The cartridge's
    battery save is not a moment at all but it survives a core update, so it
    comes after both states, and its own previous generation after it.

    Returns what actually happened, which is what the cog reads to decide
    whether to say anything and whether to throw a save state away:

    * ``"state"`` - the exact moment came back;
    * ``"backup-state"`` - the newest state was unusable, the one before it
      was not;
    * ``"sram"`` - no state could be used, but the cartridge's battery save
      went in;
    * ``"backup-sram"`` - the same, from the previous generation of it;
    * ``"fresh"`` - there was nothing to restore, or none of it could be used.

    Blocking, so callers run it in a worker thread.
    """
    progress = progress if progress is not None else Progress()
    emulator.start()
    for data, outcome in ((progress.state, "state"), (progress.state_backup, "backup-state")):
        if not data:
            continue
        try:
            emulator.load_state(data)
            # Straight back to the exact moment, so none of the boot frames
            # below are wanted: the game is already past its title screen.
            return outcome
        except EmulatorError as error:
            # A state from a different build of the core, a truncated file, or
            # -- the case the battery save exists for -- a state whose size no
            # longer matches because the core was updated. That should cost
            # the exact moment, not the session and not the player's own
            # in-game save.
            log.warning(
                "Discarding an unusable Libretro save state (%s) for %s: %s",
                outcome,
                slug,
                error,
            )
    # Cold boot. The core only allocates the cartridge's save memory once it
    # has loaded the game, so the battery save goes in after start(), and the
    # boot frames run afterwards so the game reaches its own title screen with
    # the save already in place.
    restored = "fresh"
    for data, outcome in ((progress.sram, "sram"), (progress.sram_backup, "backup-sram")):
        # load_sram() answers False for a save that is the wrong size for this
        # cartridge, which is exactly when the generation before it is worth a
        # try: an import or a core update can leave a mismatched newest file.
        if data and emulator.load_sram(data):
            restored = outcome
            break
    emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
    return restored


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
    """
    Tap the console's confirm button several times in one clip.

    The label counts the taps the clip length can actually fit rather than
    always claiming three: a short clip has no room for three 160ms presses
    250ms apart, and a button that says "A x3" while doing two is a lie. It
    is rewritten whenever the controls are redrawn, so changing
    `[p]retroset cliplength` mid-game corrects it (see
    :meth:`RetroView._update_repeat_label`). The custom_id never changes, so
    Discord keeps routing clicks to it.
    """

    def __init__(self, spec, row: int, taps: int = REPEAT_TAPS) -> None:
        super().__init__(
            label=f"{spec.label} x{max(1, int(taps))}",
            style=discord.ButtonStyle.primary,
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:repeat",
        )
        self.field: str = spec.field
        self.name: str = spec.label

    async def callback(self, interaction: discord.Interaction) -> None:
        # REPEAT_TAPS is what is *asked* for; press_plan decides how many of
        # them fit, and the label above says which it was.
        await self.view._press(interaction, self.field, repeat=REPEAT_TAPS)


class _ReplayButton(discord.ui.Button):
    """
    Play the last few clips back as one animation.

    The label is rewritten every time the buffer changes (see
    :meth:`RetroView._update_replay_label`), because the buffer is memory-only:
    a message that survived a bot restart really can have nothing to replay,
    and a button that says "Replay 15s" when it holds nothing is a lie. The
    custom_id never changes, so Discord keeps routing clicks to it.
    """

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


class _UndoButton(discord.ui.Button):
    """
    Step the game back to just before the last press.

    Greyed out whenever there is nothing to undo, which is not a rare case:
    the history is memory-only (see :attr:`RetroView.history`), so a message
    that survived a bot restart has an empty one until somebody presses
    something. A click that gets through anyway -- a stale button on a
    message Discord has not re-rendered -- is answered with a private line
    rather than an error; see :meth:`RetroView._undo`.
    """

    def __init__(self, row: int) -> None:
        super().__init__(
            label="Undo",
            emoji=UNDO_EMOJI,
            style=discord.ButtonStyle.secondary,
            row=row,
            custom_id=f"{CUSTOM_ID_PREFIX}:undo",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._undo(interaction)


class _ResumeButton(discord.ui.Button):
    """The only control left on a message whose game has been replaced."""

    def __init__(self) -> None:
        super().__init__(
            label="Resume",
            emoji=RESUME_EMOJI,
            style=discord.ButtonStyle.success,
            row=0,
            custom_id=f"{CUSTOM_ID_PREFIX}:resume",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._resume(interaction)


class RetiredView(discord.ui.View):
    """
    What is left on a message after its channel moved on to another game.

    The game itself is not gone: its ROM, save state and battery save are all
    still cached under the channel's id, so this message keeps one button that
    starts it again right here. It is a persistent view with a fixed custom_id
    and a record in Config, so the button still works after a bot restart --
    which is the whole point, since a retired message can sit in a channel for
    weeks.

    The live controls cannot simply be left enabled instead: only one libretro
    core may be loaded at a time, so waking this session must go through the
    cog's start path (which hibernates whatever else is playing) rather than
    through a button that assumes it is still the channel's game.
    """

    def __init__(self, cog: commands.Cog, record: dict) -> None:
        super().__init__(timeout=None)
        self.cog: commands.Cog = cog
        self.record: dict = dict(record)
        self.channel_id: int = int(record.get("channel_id") or 0)
        self.message_id: typing.Optional[int] = record.get("message_id")
        self.game_name: str = record.get("game_name") or "that game"
        # Cleared when the cog takes this message back into service, so a
        # stale click routed by discord.py's per-message view store (which
        # keeps old custom_ids around after a new view is registered for the
        # same message) cannot start the game a second time.
        self.alive: bool = True
        self.lock: asyncio.Lock = asyncio.Lock()
        self.add_item(_ResumeButton())

    async def _resume(self, interaction: discord.Interaction) -> None:
        if self.lock.locked():
            # Already working on somebody else's click. Acknowledge it so
            # Discord does not show "interaction failed", and say nothing.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                log.debug("Could not acknowledge a repeat Resume click.", exc_info=True)
            return
        if not self.alive:
            # This message has been superseded: either this very button has
            # already brought the game back, or it was started again some
            # other way. Either way there is a newer message for it, and
            # starting a second copy would have two of them fighting over one
            # save state. discord.py still routes clicks here, because
            # registering a new view for a message keeps the old custom_ids.
            try:
                await interaction.response.send_message(
                    f"**{self.game_name}** has already been started again \N{EM DASH} "
                    "look for its newer message in this channel.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.debug("Could not answer a stale Resume click.", exc_info=True)
            return
        async with self.lock:
            await self.cog.resume_retired(self, interaction)


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
        clip_seconds: float = CLIP_SECONDS,
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
        # Fractional, and clamped on the way in: the setting is a float now,
        # an installation that set it before it was has an int in Config, and
        # neither must be able to ask for a clip of no frames at all.
        self.clip_seconds: float = clamp_clip_seconds(clip_seconds)
        self.hold_ms: int = hold_ms
        self.clip_format: str = clip_format
        self.screen_filename: str = self._screen_filename(game_name, clip_format)

        # The live emulator, or None while hibernated.
        self.emulator: typing.Optional[RetroEmulator] = None
        # The most recent clips, oldest first, so Replay can stitch the last
        # REPLAY_SECONDS back together. Memory only and deliberately so: these
        # are hundreds of kilobytes each, they are worthless the moment the
        # session moves on, and writing them to disk would multiply the cog's
        # storage by the number of channels for a button nobody presses twice.
        # A bot restart therefore empties it, and Replay says so.
        self.clips: typing.Deque[typing.Tuple[bytes, float]] = collections.deque()
        # The machine states the last few presses started from, oldest first,
        # each one zlib-compressed. This is what the Undo button pops. Memory
        # only, exactly like the replay buffer above and for the same reasons
        # -- and unlike the buffer it is also *cheap* to lose, because the
        # authoritative save state is on disk either way. A restart therefore
        # empties it and Undo greys itself out until the next press.
        self.history: typing.Deque[bytes] = collections.deque()
        self._history_bytes: int = 0
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

        # How the last boot went: "state" (the exact moment came back), "sram"
        # (the cartridge's battery save came back but the moment did not) or
        # "fresh" (nothing to restore). Read by the cog after start().
        self.boot_outcome: str = "fresh"

        self.message: typing.Optional[discord.Message] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self._build_controls()
        self._update_replay_label()
        self._update_repeat_label()
        self._update_undo_button()

    # -- Layout -------------------------------------------------------------

    def _build_controls(self) -> None:
        """
        Lay this console's controller out; see the row plan in systems.py.

        The console's own grid comes first, spacers and all, and the control
        cluster -- Wait, confirm x3, Replay, Undo -- goes on the end of the
        last row if all of it fits there and on a row of its own if it does
        not. Which is most consoles now that the cluster is four wide: only a
        one-button bottom row (the Master System's Pause, the Neo Geo
        Pocket's Option) leaves room beside it.
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

        # Wait / confirm x3 / Replay / Undo. They share the last row when
        # there is space, which is how Pause ends up beside them.
        last = len(rows) - 1
        if len(rows[last]) + CONTROL_BUTTONS <= MAX_BUTTONS_PER_ROW:
            control_row = last
        else:
            control_row = len(rows)
        if control_row >= MAX_ACTION_ROWS:
            raise ValueError(
                f"{self.system.name} leaves no room for the controls row; "
                f"Discord allows {MAX_ACTION_ROWS} rows."
            )
        self.add_item(_WaitButton(control_row))
        confirm = self.system.button(self.system.confirm)
        if confirm is not None:
            # Built with the taps this clip length can fit, so a session
            # started on a short clip never shows a promise it cannot keep.
            # CONTROL_BUTTONS still reserves room for the whole cluster
            # either way, so the layout does not move when it is greyed out.
            self.add_item(_RepeatButton(confirm, control_row, self.repeat_taps))
        self.add_item(_ReplayButton(control_row))
        self.add_item(_UndoButton(control_row))
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
        clip_seconds: float = CLIP_SECONDS,
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

    # -- Timing -------------------------------------------------------------

    @property
    def fps(self) -> float:
        """
        The frame rate this session's clips are laid out against.

        The live core's own rate, which is the only honest answer (59.73 on a
        Game Boy, 60.10 on a NES, 50 on a PAL machine), falling back to
        DEFAULT_FPS while the session is hibernated. The fallback is only
        ever used for labelling -- how many taps the repeat button will do --
        and the two answers differ by less than half a percent, which is far
        too little to change a tap count; the label is written again from the
        real rate as soon as the core is up.
        """
        emulator = self.emulator
        return DEFAULT_FPS if emulator is None else emulator.fps

    def clip_frames(self, emulator: typing.Optional[RetroEmulator] = None) -> int:
        """How many emulated frames one clip covers on this console."""
        fps = self.fps if emulator is None else emulator.fps
        return clip_frame_count(fps, self.clip_seconds)

    def press_plan(
        self, taps: int = 1, emulator: typing.Optional[RetroEmulator] = None
    ) -> typing.List[typing.Tuple[int, int]]:
        """This session's ``(start, hold)`` frames; see :func:`press_plan`."""
        fps = self.fps if emulator is None else emulator.fps
        return press_plan(fps, self.clip_seconds, self.hold_ms, taps)

    @property
    def repeat_taps(self) -> int:
        """
        How many taps the repeat button really does at this clip length.

        Three when there is room for three, fewer when there is not, and one
        -- meaning "no more than the confirm button itself" -- on a clip too
        short for even two, which is when the button is greyed out.
        """
        return len(self.press_plan(REPEAT_TAPS))

    # -- The replay buffer --------------------------------------------------

    @property
    def last_clip(self) -> typing.Optional[bytes]:
        """The clip currently on the message, or None if there isn't one."""
        return self.clips[-1][0] if self.clips else None

    @last_clip.setter
    def last_clip(self, data: typing.Optional[bytes]) -> None:
        if data is None:
            self.clips.clear()
            self._update_replay_label()
        else:
            self.remember_clip(data)

    @property
    def buffered_seconds(self) -> float:
        """How much footage Replay would actually show, in seconds."""
        return min(float(REPLAY_SECONDS), sum(seconds for _, seconds in self.clips))

    def remember_clip(self, data: bytes) -> None:
        """Add a freshly recorded clip to the replay buffer."""
        if not data:
            return
        self.clips.append((bytes(data), float(self.clip_seconds)))
        self._trim_clips()
        self._update_replay_label()

    def _trim_clips(self) -> None:
        """
        Drop the oldest clips until the buffer fits all three of its bounds.

        Seconds is the bound that is meant to apply, and now does at every
        clip length: MAX_REPLAY_CLIPS is derived from REPLAY_SECONDS and the
        shortest clip allowed, so the count cannot cut the replay short the
        way a flat eight clips did once a clip became one second. The byte
        cap is a backstop for a game that somehow encodes enormous ones. The
        clip that straddles the fifteen-second edge is kept, because
        :func:`concatenate_clips` trims it frame by frame.
        """
        while len(self.clips) > MAX_REPLAY_CLIPS:
            self.clips.popleft()
        while len(self.clips) > 1 and sum(len(d) for d, _ in self.clips) > MAX_REPLAY_BYTES:
            self.clips.popleft()
        covered = 0.0
        keep = 0
        for _, seconds in reversed(self.clips):
            keep += 1
            covered += seconds
            if covered >= REPLAY_SECONDS:
                break
        while len(self.clips) > keep:
            self.clips.popleft()

    def _update_replay_label(self) -> None:
        """
        Make the Replay button say what it would actually replay.

        The buffer only lives in memory, so a message that survived a restart
        has nothing to replay at all; the button is greyed out until the next
        press rather than claiming otherwise. With more than one clip buffered
        it says how many seconds it will show, since that is the thing the
        player cannot otherwise know. The custom_id never changes, so none of
        this affects how Discord routes a click.

        The figure is written to one decimal place rather than rounded to a
        whole second: two 0.2s clips are 0.4 seconds of footage, and
        ``round()`` made that button say "Replay 0s".
        """
        button = next(
            (child for child in self.children if isinstance(child, _ReplayButton)), None
        )
        if button is None:
            return
        if not self.clips:
            button.label = "Replay"
            button.disabled = True
        elif len(self.clips) == 1:
            button.label = "Replay"
            button.disabled = False
        else:
            button.label = f"Replay {format_seconds(self.buffered_seconds, 1)}s"
            button.disabled = False

    def _update_repeat_label(self) -> None:
        """
        Make the repeat button say how many taps it will really do.

        Three taps need about 1.4 seconds of clip; :func:`press_plan` squeezes
        the spacing to fit a shorter one and then drops taps, so the label has
        to follow it rather than stating REPEAT_TAPS for ever. At one tap the
        button does nothing the console's own confirm button does not, so it
        is greyed out instead -- the same treatment Replay gets when there is
        nothing to replay, and for the same reason: better a dead button than
        a lying one. The layout never moves, because systems.py reserves room
        for three controls whether or not this one is usable.
        """
        button = next(
            (child for child in self.children if isinstance(child, _RepeatButton)), None
        )
        if button is None:
            return
        taps = self.repeat_taps
        button.label = f"{button.name} x{taps}"
        # Both ways round: a session built while hibernated laid the button
        # out against DEFAULT_FPS, and a 50 fps PAL core can fit a tap that
        # the fallback said would not fit (and vice versa).
        button.disabled = taps < 2

    # -- The undo history ---------------------------------------------------

    @property
    def can_undo(self) -> bool:
        """Whether there is a press to step back to."""
        return bool(self.history)

    @property
    def history_bytes(self) -> int:
        """How much memory the undo history is holding, compressed."""
        return self._history_bytes

    def remember_state(self, emulator: typing.Optional[RetroEmulator] = None) -> bool:
        """
        Push the state a press is about to change onto the undo history.

        Called from :meth:`run_press`, i.e. in the worker thread, *before*
        anything is emulated -- which is the whole contract: what Undo puts
        back is the machine exactly as it was when the button was clicked.

        Never raises. A core that cannot serialize (or one that fails to,
        once) must not cost anybody a press: the history simply does not
        grow, and Undo stays greyed out. Returns whether a state was stored.
        """
        emulator = emulator if emulator is not None else self.emulator
        if emulator is None:
            return False
        try:
            blob = zlib.compress(emulator.save_state(), UNDO_COMPRESSION_LEVEL)
        except Exception:
            # EmulatorError for a core with no save-state support, anything
            # else for a core that broke. Either way: not worth a press.
            log.debug("Could not record an undo point for %s.", self.slug, exc_info=True)
            return False
        self.history.append(blob)
        self._history_bytes += len(blob)
        self._trim_history()
        return True

    def _trim_history(self) -> None:
        """
        Drop the oldest undo points until the history fits both its bounds.

        Count *and* bytes, because the count alone bounds nothing: see
        MAX_UNDO_BYTES. The newest entry is always kept, even if it is over
        the byte cap by itself, since an Undo button that cannot undo the
        press somebody just made would be worse than the memory.
        """
        while len(self.history) > UNDO_DEPTH:
            self._history_bytes -= len(self.history.popleft())
        while len(self.history) > 1 and self._history_bytes > MAX_UNDO_BYTES:
            self._history_bytes -= len(self.history.popleft())

    def forget_history(self) -> None:
        """Throw the undo history away, leaving the game exactly as it is."""
        self.history.clear()
        self._history_bytes = 0
        self._update_undo_button()

    def _update_undo_button(self) -> None:
        """
        Grey Undo out when there is nothing to undo.

        Which is the state every session starts in, and the state a session
        comes back from a bot restart in: the history is memory only. Same
        treatment as Replay's empty buffer, and for the same reason -- better
        a dead button than a lying one. The custom_id never changes, so none
        of this affects how Discord routes a click.
        """
        button = next(
            (child for child in self.children if isinstance(child, _UndoButton)), None
        )
        if button is not None:
            button.disabled = not self.history

    def run_undo(self) -> bytes:
        """
        Put the last press back and record a clip of where it landed.

        Runs in a worker thread, called from ``Retro.run_undo``. Three things
        happen, in this order:

        1. the newest undo point is popped and loaded, so the machine is
           back where the undone press found it;
        2. the footage of that press is dropped from the replay buffer,
           because it is footage of something the game no longer did. The
           buffer rewinds with the game rather than showing a player walking
           into a room they are not in;
        3. a fresh clip is recorded with no input at all, so the channel can
           see where the game ended up. The caller puts *that* clip in the
           buffer in place of the one dropped, which is what keeps a
           stitched replay a contiguous account of the play that still
           stands: the undone second is gone and this second, run from the
           same starting point, took its place.

        That third step means an undo costs one clip's worth of emulated
        time, exactly as the Wait button does, and that is deliberate. The
        alternative -- recording the clip and then reloading the state, so
        the machine is byte-for-byte where it was -- would leave the clip on
        the message a second *ahead* of the game, and the next press would
        re-emulate that second and play it again from the start. Which is
        precisely the "the clip jumps backwards when I press a button" bug
        that :meth:`_ack_now` exists to prevent, so Undo does not
        reintroduce it. One second of a game that is frozen between presses
        is a cheap price for a clip that still carries on where the last one
        stopped.

        Raises EmulatorError if there is nothing to undo, if the core is not
        running, or if the state will not load -- which is a real case: a
        core update mid-session invalidates every state it wrote, so the
        whole history is dropped rather than retried press after press.
        """
        emulator = self.emulator
        if emulator is None:
            raise EmulatorError("The emulator is not running.")
        if not self.history:
            raise EmulatorError("There is nothing to undo.")
        blob = self.history.pop()
        self._history_bytes -= len(blob)
        try:
            emulator.load_state(zlib.decompress(blob))
        except Exception as error:
            # Every entry came from the same core, so if one will not go back
            # in, none of them will.
            self.forget_history()
            raise EmulatorError(
                "That press could not be undone: the emulator would not take "
                "the state back (most likely its core was updated). The game "
                "itself is untouched."
            ) from error
        # The press did not happen, so neither did its footage. The newest
        # clip is always the one the undone press recorded: every press
        # appends exactly one and _trim_clips only ever drops from the left.
        if self.clips:
            self.clips.pop()
        self._update_replay_label()
        return self._record(emulator, None)

    @staticmethod
    def _screen_filename(game_name: str, clip_format: str = DEFAULT_CLIP_FORMAT) -> str:
        """A stable, Discord-safe attachment name for this session's clips."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game_name).strip("-")[:48]
        return f"{safe or 'screen'}{clip_extension(clip_format)}"

    def _set_disabled(self, disabled: bool) -> None:
        """
        Grey the controls out, or bring them back. Spacers stay inert.

        ``True`` is only :meth:`retire` now: a press used to grey the controls
        out for the second it took to emulate, and that cost an extra edit of
        the message, which is what made the previous clip play again from the
        beginning (see :meth:`_ack_now`). ``False`` is still called on every
        redraw, because that is also where Replay and the repeat button are
        made to say what they will really do.
        """
        for child in self.children:
            if isinstance(child, _SpacerButton):
                continue
            if hasattr(child, "disabled"):
                child.disabled = disabled
        if not disabled:
            # Replay, the repeat button and Undo are the three controls that
            # can have nothing to do: Replay comes back only if there is
            # something in the buffer, the repeat button only if the clip is
            # long enough to fit more than one tap, and Undo only if a press
            # this process saw has something to step back to. This runs on
            # every redraw, so changing the clip length mid-game corrects the
            # repeat button and every press re-arms Undo.
            self._update_replay_label()
            self._update_repeat_label()
            self._update_undo_button()

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

    async def start(
        self,
        ctx: commands.Context,
        emulator: RetroEmulator,
        progress: typing.Optional[Progress] = None,
        on_booted: typing.Optional[typing.Callable[["RetroView"], None]] = None,
    ) -> discord.Message:
        """
        Boot the emulator and post the first clip with the controls.

        ``progress`` is this channel's saved progress for this game, if it has
        played it before. Starting a game the channel already has a save for
        picks up where it left off rather than cold-booting over the top of
        it, through the same chain a hibernated session is woken with.

        ``on_booted`` is called once the core is up and :attr:`boot_outcome`
        says how much of the game came back with it, and *before* the message
        goes out -- which is what lets the cog put the sentence explaining the
        restore on the very message the first clip arrives on, rather than in
        a second one after it.
        """
        self.starter_id = ctx.author.id
        clip = await asyncio.to_thread(self._boot, emulator, progress)
        self.emulator = emulator
        # Now that there is a core, the repeat button's label can be written
        # from its real frame rate rather than DEFAULT_FPS -- and it is
        # written before the message goes out, so the first thing anybody
        # sees is already correct.
        self._update_repeat_label()
        self.last_clip = clip
        self.touch()
        if on_booted is not None:
            on_booted(self)
        self.message = await ctx.send(
            self._content(),
            file=self._clip_file(clip),
            view=self,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        self.message_id = self.message.id
        return self.message

    def _boot(
        self,
        emulator: RetroEmulator,
        progress: typing.Optional[Progress] = None,
    ) -> bytes:
        """
        Bring a game up, restoring as much of it as is restorable.

        Sets :attr:`boot_outcome` to what actually happened, which is what the
        cog reads to decide whether to say anything and whether to throw the
        save state away. The restoring itself is :func:`restore_into`, shared
        with the wake path so the two cannot drift. Runs in a worker thread.
        """
        self.boot_outcome = restore_into(emulator, progress, self.slug)
        return self._record(emulator, None)

    def _schedule(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int
    ) -> typing.List[tuple]:
        """
        Work out when, and for how long, to hold a button during a clip.

        Every button, direction or not, is held for the same ``hold_ms``; see
        DEFAULT_HOLD_MS for why the directions no longer get a multiplier,
        and :func:`press_plan` for how the schedule is made to fit inside the
        clip it is going to be recorded into. ``field`` of None is the Wait
        button: no input at all.
        """
        if field is None:
            return []
        return [
            (field, start, hold) for start, hold in self.press_plan(repeat, emulator)
        ]

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
        # Before anything is emulated: this is the moment Undo puts back.
        # Wait counts as a press here -- it moves the game on, so it is
        # something to step back from.
        self.remember_state(self.emulator)
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
            # A hibernated session has a core to load and a save state to
            # restore before it can emulate anything, which is the one delay
            # worth explaining. It is said *with* the clip rather than before
            # it, because a press only gets one edit now; see _ack_now.
            resuming = not self.live
            await self._ack_now(interaction)
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
            await self._show(interaction, clip, RESUMED_NOTE if resuming else None)

    @staticmethod
    async def _silent_ack(interaction: discord.Interaction) -> None:
        """Acknowledge a click without changing or posting anything."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("Could not acknowledge a dropped Libretro press.", exc_info=True)

    async def _ack_now(self, interaction: discord.Interaction) -> None:
        """
        Acknowledge the click without changing the message in any way.

        One press has to cause exactly **one** visible change, and that is
        why this defers rather than editing.

        A Discord client re-renders a message from scratch on *any* edit to
        it, and re-rendering restarts the animation that is already attached.
        Clips are encoded with ``loop=1`` (see encode_animation) so they play
        through once and hold their last frame; editing the message to grey
        the buttons out therefore played the *previous* clip again from frame
        zero, and a second later the new clip replaced it. What that looks
        like from the player's side is the game jumping backwards a few
        frames every time they press a button -- which is exactly how it was
        reported.

        So the instant "your click landed" feedback the controls used to give
        is gone, and it cannot come back: any component response that shows
        something either edits this message (the rewind) or posts another one
        (an ephemeral "still emulating" notice, which was removed for being
        spam). ``defer()`` on a component interaction is a
        DEFERRED_UPDATE_MESSAGE, which changes nothing on screen at all, and
        ``edit_original_response`` below then makes the one edit that swaps
        the clip in and redraws the buttons.

        Overlapping clicks are unaffected: the lock in :meth:`_press` drops
        them through :meth:`_silent_ack`, which is the same defer, so a second
        presser never sees "interaction failed" and never sees a message
        either.
        """
        if self.message is None and interaction.message is not None:
            # After a restart the view is rebuilt from Config and has never
            # seen its message; the interaction carries it.
            self.message = interaction.message
            self.message_id = interaction.message.id
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.warning("Could not acknowledge the Libretro press.", exc_info=True)

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

    async def _show(
        self,
        interaction: discord.Interaction,
        clip: bytes,
        note: typing.Optional[str] = None,
    ) -> None:
        """
        Swap in the new clip and redraw the controls. The *only* edit a press
        makes; see :meth:`_ack_now` for why there is not a second one.

        ``note`` is a line the *press* wants said (that the game was asleep
        and has come back). A pending :attr:`notice` beats it, because that is
        something the wake itself discovered and has to report -- that the
        save state was rejected after a core update, for instance -- and only
        one of the two can be shown.
        """
        self._set_disabled(False)
        content = self._content()
        if content is None:
            content = note
        try:
            # edit_original_response targets the message the component is on,
            # which response.defer() acknowledged without touching.
            # attachments= replaces the message's files; omitting it would
            # keep the previous clip, and content=None clears whatever was
            # said before it.
            self.message = await interaction.edit_original_response(
                content=content,
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
        """
        Play the last few clips back as one animation.

        Re-uploading bytes creates a new attachment, and Discord plays a
        freshly loaded clip from the start; the clips are encoded to run
        through exactly once, so re-uploading is the only way to see one twice.

        With one clip buffered that is all this does, and it stays a single
        instant edit. With more, the buffered clips are decoded and stitched
        into one animation covering the last REPLAY_SECONDS -- a second or so
        of work on a real game -- and that too is one edit and no more, for
        exactly the reason a press is (see :meth:`_ack_now`): the edit that
        used to grey the controls out while the stitching ran also made the
        clip already on the message play again from its first frame, so
        pressing Replay showed the old clip, then the stitched one.
        """
        if self.closed or self.lock.locked():
            await self._silent_ack(interaction)
            return
        if not self.clips:
            # The buffer only lives in memory, so a restart empties it.
            await interaction.response.send_message(
                "There is nothing to replay: this game's clips are kept in "
                "memory only, and the bot has restarted since the last one. "
                "Press a button to record one.",
                ephemeral=True,
            )
            return
        if len(self.clips) == 1:
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
            return

        async with self.lock:
            clips = [data for data, _ in self.clips]
            # The clips' own nominal lengths go with them: a clip in which
            # nothing moved carries no timing of its own, and the session is
            # the only thing that knows it stood for a second of play.
            lengths = [seconds for _, seconds in self.clips]
            await self._ack_now(interaction)
            try:
                clip, seconds = await asyncio.to_thread(
                    concatenate_clips,
                    clips,
                    seconds=lengths,
                    max_seconds=REPLAY_SECONDS,
                    max_frames=MAX_REPLAY_FRAMES,
                    clip_format=self.clip_format,
                )
            except Exception as error:
                # Stitching is a nicety on top of a game that is running
                # perfectly well, so a failure falls back to the single clip
                # the old Replay button would have shown.
                log.warning(
                    "Could not stitch the replay for channel %s: %s",
                    self.channel_id,
                    error,
                )
                clip, seconds = self.last_clip, 0.0
            # Never disabled, so this is only here to redraw the two controls
            # that can have nothing to do; see _set_disabled.
            self._set_disabled(False)
            note = (
                None
                if not seconds
                else f"The last {format_seconds(seconds, 1)} seconds, replayed."
            )
            try:
                self.message = await interaction.edit_original_response(
                    content=self._content(note),
                    attachments=[self._clip_file(clip)],
                    view=self,
                )
            except discord.HTTPException:
                log.exception(
                    "Failed to replay the Libretro clip in channel %s.", self.channel_id
                )
                await self._whisper(
                    interaction, "Discord would not accept that clip again."
                )

    async def _undo(self, interaction: discord.Interaction) -> None:
        """
        Step the game back to just before the last press.

        One edit of the message, like a press: a plain ``defer`` that changes
        nothing on screen, then the single ``edit_original_response`` that
        swaps the clip in. See :meth:`_ack_now` for why it cannot be two.

        An empty history is the ordinary case rather than an error -- the
        button is greyed out for it, and a bot restart empties it -- so a
        click that gets through anyway is answered privately and the message
        is not touched at all. That is one interaction response and zero
        edits, exactly as Replay does with an empty buffer.
        """
        if self.closed or self.lock.locked():
            await self._silent_ack(interaction)
            return
        if not self.history:
            try:
                await interaction.response.send_message(
                    "There is nothing to undo yet. Undo steps back through "
                    f"the last {UNDO_DEPTH} presses, and that history is kept "
                    "in memory only \N{EM DASH} so it is empty until somebody "
                    "presses something, and the bot has restarted since the "
                    "last press here. The game itself is exactly where you "
                    "left it.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.debug("Could not answer an empty Undo click.", exc_info=True)
            return
        async with self.lock:
            await self._ack_now(interaction)
            try:
                clip = await self.cog.run_undo(self)
            except EmulatorError as error:
                log.warning("Undo failed in channel %s: %s", self.channel_id, error)
                await self._recover(interaction, str(error))
                return
            except Exception:
                log.exception("Unexpected undo failure in channel %s", self.channel_id)
                await self._recover(interaction, "The emulator hit an unexpected error.")
                return
            # run_undo dropped the undone press's footage from the buffer;
            # this clip takes its place, so the buffer stays a contiguous
            # record of the play that actually stands and Replay cannot show
            # somebody walking into a room they are not in.
            self.last_clip = clip
            self.touch()
            await self._show(interaction, clip, UNDONE_NOTE)

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
