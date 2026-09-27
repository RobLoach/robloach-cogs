"""
The controller under a game's message: its layout, its clicks and the one
edit each of them makes.

This file was 2,700 lines, and about a third of them were not about the view
at all -- a session is not a grid of buttons, and five things that had grown
up in here have moved out to modules of their own:

* :mod:`retro.timing` -- how long a button is held, how many taps fit in a
  clip, and how long an edit waits before it may replace the clip on screen;
* :mod:`retro.text` -- every line the session writes, and the sanitising a
  display name or a ROM's filename goes through before it can be one;
* :mod:`retro.restore` -- what a channel has saved for a game and the order a
  boot tries it in;
* :mod:`retro.permissions` -- who may sleep, reboot or end somebody else's
  game;
* :mod:`retro.session` -- the worker-thread drivers: booting a core,
  emulating a press, an undo or a reboot, encoding the clip that comes out,
  and the undo history all of that pushes onto.

The first four are plain functions. The fifth could not be, which is why it
was left behind when they went: every one of those methods reads and writes
the session's own state, so it moved as :class:`retro.session.SessionMixin`
-- inherited by :class:`RetroView` below -- rather than as a module of
functions. Its rule is worth knowing before touching anything it holds:
**nothing in it may be called on the event loop**, and this file is the other
side of that line. What is left here is the view proper -- the grid, the one
edit a click makes, the queue of intent behind it and the pacing that decides
when an edit may go out -- and it is all event loop, all the time.

**Every name they took with them is re-exported below**, so
``retro.RetroView.press_plan``, ``retro.RetroView.Progress``,
``retro.RetroView.UNDO_DEPTH`` and the rest still resolve exactly as they
did: this was a move, not a change, and no import site outside this package
had to be touched for it. New code should import from the module the thing
actually lives in.

One of those re-exports is load-bearing rather than merely convenient:
``pace_wait`` is looked up in *this* module's globals by
:meth:`RetroView.pace`, which is what lets the test suite swap it out and not
spend a real second per press.

The rest are convenient, and "merely" undersells how much of the package
goes through them, which is what makes deleting one a bigger change than it
looks. ``Retro.py`` imports **nine** names through here that live somewhere
else -- ``DEFAULT_HOLD_MS``, ``MAX_HOLD_MS``, ``MIN_HOLD_MS``,
``MIN_REPEAT_TAPS``, ``REPEAT_TAPS`` and ``press_plan`` from
:mod:`retro.timing`, ``Progress`` and ``restore_into`` from
:mod:`retro.restore`, and ``presser_name`` from :mod:`retro.text` -- and
``retro/saves.py`` imports ``may_manage`` the same way. This paragraph used
to say there were two of them and name ``restore_into`` as the one
``Retro.py`` used; that was true of the import list of the day.
"""

import asyncio
import collections
import io
import logging
import re
import time
import typing
from pathlib import Path

import discord
from redbot.core import commands

from .emulator import (
    CLIP_EXTENSION,
    CLIP_SECONDS,
    DEFAULT_FPS,
    EmulatorError,
    RetroEmulator,
    clamp_clip_seconds,
    clip_frame_count,
    playback_seconds,
)

# The five modules this file was split into. Most of what comes in from them
# is used right here; the rest is imported *because* it is re-exported, which
# is what the `noqa` on each of them says -- every name they took with them
# still resolves as `retro.RetroView.<name>`, so nothing outside this package
# had to be touched by the move. See the docstring above.
from .permissions import may_manage  # noqa: F401  (re-exported)
from .restore import (  # noqa: F401  (re-exported)
    BackupSource,
    Progress,
    backup_offered,
    read_backup,
    restore_into,
)
from .session import (  # noqa: F401  (re-exported)
    MAX_UNDO_BYTES,
    UNDO_COMPRESSION_LEVEL,
    UNDO_DEPTH,
    EncodedClip,
    SessionMixin,
)
from .systems import (
    CONTROL_BUTTONS,
    MAX_ACTION_ROWS,
    MAX_BUTTONS_PER_ROW,
    MAX_COMPONENTS,
    MAX_LAYOUT_ROWS,
    RESUME_EMOJI,
    SYSTEMS,
    UNDO_EMOJI,
    WAIT_EMOJI,
    System,
    is_spacer,
    system_by_key,
    system_for_extension,
)
from .text import (  # noqa: F401  (re-exported)
    ACTION_NOTES,
    DROPPED_NOTE,
    HEADER,
    HEADER_SEPARATOR,
    INVISIBLE_CATEGORIES,
    LEADING_ORDINAL,
    MARKDOWN_ESCAPES,
    MAX_GAME_NAME,
    MAX_PRESSER_NAME,
    NO_PINGS,
    QUEUE_ENTRY,
    QUEUE_ENTRY_ANONYMOUS,
    QUEUE_NOTE,
    QUEUED_WAIT,
    REPEAT_GONE_NOTE,
    REPEAT_STALE_NOTE,
    RESUMED_NOTE,
    action_note,
    escape_label,
    presser_name,
)
from .timing import (  # noqa: F401  (re-exported)
    BOOT_SECONDS,
    DEFAULT_HOLD_MS,
    MAX_HOLD_MS,
    MAX_PACE_SECONDS,
    MIN_HOLD_MS,
    MIN_PACE_SECONDS,
    MIN_REPEAT_GAP_MS,
    MIN_REPEAT_TAPS,
    REPEAT_GAP_MS,
    REPEAT_TAPS,
    pace_wait,
    press_plan,
)

log = logging.getLogger("red.robloach.retro")


# -- Answering a click --------------------------------------------------------
#
# **A click that goes unanswered is shown Discord's own red "This interaction
# failed" three seconds later.** That single fact is the whole reason these
# exist, it is why none of them raises, and it is why the one that says
# something falls back to the one that says nothing: saying nothing is a
# disappointment, saying nothing *and* looking broken is a bug report.
#
# There are three shapes and there used to be seven copies of them, spread
# across two classes and two files:
#
# * `defer` was `RetroView._silent_ack`, the tail of `RetroView._ack_now`, and
#   a hand-rolled third copy in `RetiredView._resume` -- hand-rolled purely
#   because `_silent_ack` was a staticmethod on the *other* class in this
#   file;
# * `ephemeral` was `_replaced_ack`, `_stale_repeat_ack`, `_undo`'s
#   empty-history branch and `RetiredView._resume`'s stale branch. Only two of
#   those four had the defer fallback, and `_undo`'s was one of the two that
#   did not -- so an empty-history click whose ephemeral Discord refused was a
#   click nobody answered, which is the exact failure the fallback is for.
#   Building the fallback into the helper is what makes that unforgettable
#   rather than remembered four times;
# * `whisper` was `RetroView._whisper` and `Retro._whisper_interaction`, and
#   the two were not the same: the copy here went straight to `followup`, so
#   on the one path that used it -- an edit Discord refused, reached after a
#   defer that may itself have failed -- a not-actually-deferred interaction
#   got a 404 and the player got nothing at all. What survives is
#   `_whisper_interaction`'s body, which tries the response first.
#
# Module-level functions rather than methods because both classes in this file
# need them and `Retro.py` needs the third; a method on `RetroView` is exactly
# what pushed `RetiredView` into writing its own. `Retro.py` imports `whisper`
# from here, which is what retired `Retro._whisper_interaction` -- the fifth
# copy, and the one the survivor's body came from.


async def defer(interaction: discord.Interaction) -> bool:
    """
    Acknowledge a click without changing or posting anything.

    A component ``defer()`` is a DEFERRED_UPDATE_MESSAGE: it changes nothing
    on screen, and it leaves ``edit_original_response`` available for the next
    fifteen minutes, which is what lets a queued press make its edit when its
    turn finally comes. See :meth:`RetroView._ack_now`.

    Returns whether it landed, and never raises. A failure here is worth a
    warning in every one of its callers rather than the debug line two of them
    used to write: whatever the reason, somebody is about to be shown "This
    interaction failed", and the cause is nearly always a double response or
    an expired token -- both of which are this cog's fault and neither of
    which shows up anywhere else.
    """
    try:
        await interaction.response.defer()
    except discord.HTTPException:
        log.warning("Could not acknowledge a click on the Libretro controls.", exc_info=True)
        return False
    return True


async def ephemeral(interaction: discord.Interaction, message: str) -> bool:
    """
    Tell just the person who clicked, and tell them nothing else on screen.

    One interaction response and **no edit of the session's message**, which
    is the point: an edit re-renders the message and rewinds the clip that is
    playing on it for everybody in the channel (see
    :meth:`RetroView._ack_now`), which is a real cost to a whole channel for
    one person's click on something that is not going to happen. Every answer
    this cog gives to a click it cannot act on is shaped this way.

    Falls back to :func:`defer` when Discord will not take the message,
    because the one thing that must not happen is the click going unanswered.
    Returns whether the message itself was delivered, so a caller that has a
    second thing to try can.

    For a click that has *already* been responded to -- an edit that failed
    after a defer -- the answer is :func:`whisper` instead.
    """
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.debug("Could not answer a Libretro click privately.", exc_info=True)
        await defer(interaction)
        return False
    return True


async def whisper(interaction: discord.Interaction, message: str) -> None:
    """
    Tell just the person who clicked, whether or not we have replied already.

    The response first and the followup second, because only one of the two
    works at any given moment and which one it is depends on what has already
    happened to this interaction. That order is the whole content of this
    function, and it is why the copy that went straight to ``followup`` was a
    bug: its one caller ran after a defer that is *allowed to have failed*,
    and a followup on an interaction that was never acknowledged is a 404 --
    so the one path that most needed to say something said nothing.

    Never raises, and never makes a second failure louder than the first:
    this is only ever reached because something else has already gone wrong.
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
            return
    except Exception:
        log.debug("Could not answer an interaction directly.", exc_info=True)
    try:
        await interaction.followup.send(message, ephemeral=True)
    except Exception:
        log.debug("Could not deliver a Libretro failure notice.", exc_info=True)


# How long a session may sit untouched before the cog hibernates it and
# frees its core, until `[p]retroset timeout` says otherwise.
#
# Nothing in this module reads it, and nothing in this module reads the
# setting either: the idle sweep asks Config for the current value on every
# pass (Retro._hibernate_idle), which is what makes a change to it apply to
# the sessions that are already running. It lives here because the view is
# where a session's defaults are written down, and Retro.py imports it for
# its Config defaults.
DEFAULT_TIMEOUT_MINUTES = 10


# -- Queued presses -----------------------------------------------------------
#
# A press takes about a second of real time, and for that second the session's
# lock is held. A click that arrives during it used to be *dropped*: deferred
# so Discord never said "interaction failed", and then silently forgotten. In
# a channel with two or three people playing, most clicks land in that second,
# so the controller felt intermittently dead -- press a direction, nothing
# happens, press it again.
#
# So a click that cannot run now is queued instead, under four rules that are
# each there for a reason:
#
# * **at most MAX_QUEUED_PRESSES waiting.** Each one costs a whole clip, and
#   a queued press is emulated against a game state its author has not seen
#   yet. Five waiting plus the one running is about six seconds of latency at
#   the one second default -- the last person to click waits that long to
#   find out what their press did.
#
#   It was three, on the reasoning that four seconds was the most that is
#   still recognisably "I pressed that". Five is a deliberate trade made
#   after the per-person limit came off: what people actually do with a
#   d-pad is tap it several times in a row, and a run of four or five taps
#   is one intent rather than five, so a depth that cannot hold a whole run
#   refuses the tail of it. The latency is the honest price and it is paid
#   by whoever queued the run.
#
#   It multiplies with the clip length, which is the part worth watching:
#   every edit now waits out the whole clip it replaces (see
#   MAX_PACE_SECONDS in retro/timing.py), so a full drain takes about
#   MAX_QUEUED_PRESSES * cliplength -- five seconds at the default, and
#   twenty-five at the five second ceiling. Both numbers in that product are
#   the owner's own settings.
# * **first come, first served, whoever it is.** There is deliberately no
#   per-person limit any more. There used to be one -- one waiting press
#   each -- on the theory that it made a roomful of people take turns
#   without a scheduler. What it actually did was break the commonest way
#   one person plays: a direction is rarely pressed once. Walking four tiles
#   is four clicks in a row, and the second, third and fourth were all
#   refused because the first was still waiting, so the controller went back
#   to feeling dead for exactly the person using it most. Taking turns is
#   what the cap above already does -- five waiting is five waiting whoever
#   queued them, and a fast clicker filling all five only ever costs
#   themselves the wait for their own presses to play.
# * **every waiting press is visible.** An input nobody can see is an input
#   that feels lost, which is the whole complaint. The queue is listed as a
#   suffix on the very line the running press is already rewriting, so it
#   costs no extra edit -- see RetroView.queue_note and _ack_now. It names
#   the person and then their buttons in order, so a run of presses reads as
#   one intent: `Rob up up down down`.
# * **the queue is intent, never work.** A pending entry is a button name and
#   a deferred interaction; nothing touches the emulator until the runner
#   takes the lock again for it. The one-core-at-a-time discipline is
#   untouched.
#
# A queued press makes its own single edit when it runs, through its own
# deferred interaction -- a component defer is a DEFERRED_UPDATE_MESSAGE and
# leaves edit_original_response available for the next fifteen minutes -- so
# "one press, one edit" still holds exactly.
MAX_QUEUED_PRESSES = 5

#: How many different people one replaced controller will explain itself to.
#:
#: A closed view's buttons do nothing, and somebody who has not noticed the
#: channel moved on will tap several of them before concluding the bot is
#: broken -- so the first click from each person gets a private line saying
#: where the game went (see RetroView._replaced_ack). Once each rather than
#: once per click, and only this many people, because a retired message can
#: sit in a busy channel for weeks and the set of who has been told is held
#: for as long as the view is.
MAX_REPLACED_NOTICES = 25


class Pending(typing.NamedTuple):
    """
    One press somebody has asked for that has not been emulated yet.

    Intent only: a button, a count of taps, and the deferred interaction the
    clip will eventually be edited onto. No emulator work is held here and
    none is done to build one.

    ``who`` is the author's display name, sanitised at the moment they
    clicked (see :func:`presser_name`), rather than the user object: a name
    has to keep reading correctly for somebody who has left the guild between
    clicking and being run, and re-deriving one from a member object that has
    since gone is exactly how that produces " pressed A.". The listing prints
    it -- ``QUEUE_ENTRY`` is ``"{who} {buttons}"``, so a run of presses reads
    ``Rob up up`` -- and it is taken here, at the click, because it cannot be
    recovered later. There was a spell when the listing printed only the
    buttons and this field was kept against the day it printed names again;
    that day came back with the per-person queue limit's removal, and this
    comment outlived it by claiming the opposite of what QUEUE_ENTRY says.

    ``epoch`` is the session's queue generation when this was accepted. Bumped
    by :meth:`RetroView.forget_queue`, so an entry that was already taken off
    the front when the game was reset or undone is still discarded rather
    than replayed into a state nobody queued it against.
    """

    interaction: typing.Any
    user_id: typing.Optional[int]
    who: str
    field: typing.Optional[str]
    repeat: int
    epoch: int


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
# view adds the three control buttons -- Wait, confirm x3, Undo -- to the
# last row if they fit and to a row of their own if they do not.
#
# There is deliberately no Reset button: rebooting somebody's game is
# destructive to their progress-in-flight, so it is `[p]retroreboot`, a
# command with the same permission check `[p]retrosleep` has, rather than one
# more thing a passer-by can click by mistake.
#
# -- Why no control is ever taken out of the view -----------------------------
#
# A message Discord has not re-rendered still shows exactly the buttons it
# was drawn with, and a click on one of them is routed by its custom_id
# alone. discord.py learns which custom_ids are routable by walking a view's
# children (ViewStore.add_view, on every add_view and on every edit that
# carries view=), and ViewStore.dispatch_view *drops* a click whose custom_id
# no view has: it returns without touching the interaction. Nothing
# acknowledges it, and three seconds later Discord shows the person who
# clicked its red "This interaction failed".
#
# That is the real price of taking an item out of a view, and this file used
# to claim it was free ("dropped silently rather than raising") -- true of
# the log and false of the player. The window is not theoretical either: a
# session laid out while hibernated counts its taps against DEFAULT_FPS (see
# RetroView.fps) and a real core's rate can disagree, so the repeat button
# can disappear on the very first press after a boot, with every message in
# the channel still showing it.
#
# So nothing is ever taken out. The one control that comes and goes -- the
# repeat button; see MIN_REPEAT_TAPS for why it goes rather than greying out
# -- stays a child of the view for the session's whole life and is left out
# of the *payload* instead (see RetroView.to_components). That is the only
# half of "removed" Discord needs: the button is not drawn, the component is
# not spent on the wire, Wait and Undo close up behind it -- and a click on a
# stale copy still arrives at _RepeatButton.callback, which answers it
# privately rather than leaving it to time out. Discord has no invisible
# component, so there is no way to have this both ways on the wire; keeping
# the item and dropping it from the payload is how it is had on ours.
#
# The alternative is to intercept the unknown-custom_id case, and there is no
# hook for it: dispatch_view's only escape is the dynamic-item pattern table,
# which routes clicks to a throwaway view rebuilt from the message rather
# than to the session, and which ViewStore.add_view un-registers again the
# moment a view stops carrying the item (_get_snapshot_diff). A button that
# never leaves the view needs no interception at all.
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
    """
    Tap the console's confirm button several times in one clip.

    The label counts the taps the clip length can actually fit rather than
    always claiming three: a short clip has no room for three 160ms presses
    250ms apart, and a button that says "A x3" while doing two is a lie. It
    is rewritten whenever the controls are redrawn, so changing
    `[p]retroset cliplength` mid-game corrects it (see
    :meth:`RetroView._update_repeat_label`). The custom_id never changes, so
    Discord keeps routing clicks to it, and the line the press puts on the
    message counts the same taps the label does.

    Below MIN_REPEAT_TAPS it is **hidden rather than removed**: it stays a
    child of the view, so Discord keeps routing clicks to it, and
    :meth:`RetroView.to_components` leaves it out of the payload, so it is
    never drawn saying "A x1". The two halves are what make a click on a
    stale copy of it -- one still sitting on a message Discord has not
    re-rendered -- reach :meth:`callback` and get an answer, instead of
    resolving to a custom_id no view has and leaving the player looking at
    "This interaction failed". See the note above _STYLES.

    :attr:`hidden` is therefore the one piece of state on this button, and
    it is set from the tap count both at build time and on every redraw; see
    :meth:`RetroView._update_repeat_label`.
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
        # Whether this clip length has anything for it to do. Set here as
        # well as on redraw so that a session *built* on a short clip starts
        # out with it undrawn and says nothing about it: there is no news in
        # a control that was never there. Only a button that goes away
        # mid-session is worth a line; see REPEAT_GONE_NOTE.
        self.hidden: bool = int(taps) < MIN_REPEAT_TAPS

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.hidden and not self.view.closed:
            # A stale button on a message that still shows it. Nothing is
            # emulated and nothing is edited -- see REPEAT_STALE_NOTE.
            #
            # Not on a closed view, though: "this message's game has been
            # replaced" is the bigger fact and the one that says where the
            # game went, so a retired controller answers with that whether or
            # not the button that was clicked is one it still draws. _press
            # settles it; see RetroView._replaced_ack.
            await self.view._stale_repeat_ack(interaction)
            return
        # REPEAT_TAPS is what is *asked* for; press_plan decides how many of
        # them fit, and the label above says which it was.
        await self.view._press(interaction, self.field, repeat=REPEAT_TAPS)


class _UndoButton(discord.ui.Button):
    """
    Step the game back to just before the last press.

    The one control that can put the game *back*, which is also why there is
    no Reset button beside it: see the note above _STYLES.

    **Always enabled, even with nothing to undo.** It used to be greyed out
    whenever the history was empty, which is not a rare case at all: the
    history is memory-only (see :attr:`RetroView.history`), so every message
    that has survived a bot restart has an empty one until somebody presses
    something. The result was a permanently dead control with no explanation
    -- which reads as "Undo is broken", exactly as the greyed-out x3 button
    read as "the repeat feature was removed" (see MIN_REPEAT_TAPS).

    Worse, the explanation was *unreachable*: a disabled Discord button
    cannot be clicked, so the private line :meth:`RetroView._undo` writes for
    an empty history could only ever be seen by somebody clicking a stale
    button on a message Discord had not re-rendered. Leaving it enabled makes
    that line the answer to the obvious question, and costs one interaction
    response and zero edits of the message.
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
            await defer(interaction)
            return
        if not self.alive:
            # This message has been superseded: either this very button has
            # already brought the game back, or it was started again some
            # other way. Either way there is a newer message for it, and
            # starting a second copy would have two of them fighting over one
            # save state. discord.py still routes clicks here, because
            # registering a new view for a message keeps the old custom_ids.
            await ephemeral(
                interaction,
                f"**{self.game_name}** has already been started again \N{EM DASH} "
                "look for its newer message in this channel.",
            )
            return
        async with self.lock:
            await self.cog.resume_retired(self, interaction)


class _SpacerButton(discord.ui.Button):
    """
    A disabled button that holds a column open in the controller grid.

    Discord has no empty grid cell, so the only way to indent a row is to put
    something inert in front of it. This is permanently disabled, so it is
    never clickable, and _disable() skips it for the same reason nothing else
    re-enables it: its disabled state is not about what the session is doing.
    It still carries an explicit custom_id, because a persistent view requires
    every child to have one (discord.ui.Item.is_persistent).
    """

    def __init__(self, label: str, row: int, column: int) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=True,
            custom_id=f"{CUSTOM_ID_PREFIX}:spacer:{row}:{column}",
        )


class RetroView(SessionMixin, discord.ui.View):
    """
    An interactive game controller, laid out for whichever console is running.

    The view *is* the session: it outlives the emulator. When the emulator is
    freed (idle timeout, `[p]retrosleep`, cog unload, bot restart) the session
    hibernates, the controls stay enabled, and the next press transparently
    boots the core again from the cached ROM plus the last save state.

    Which is why it is two classes. :class:`retro.session.SessionMixin`
    carries the half that *drives the emulator* -- ``capture_press`` and the
    rest of the worker-thread methods, and the undo history they push onto --
    and everything declared here is the half that talks to Discord. The line
    between them is a thread: nothing inherited from the mixin may be called
    on the event loop.

    Traffic the other way is deliberately narrow and deliberately dull.
    :meth:`forget_queue` and :meth:`forget_pacing` are called from the worker
    thread, by ``capture_undo`` and ``capture_reset``, and each of them says
    so where it is defined: between them they write five plain attributes and
    touch no asyncio primitive at all, which is exactly why
    :meth:`cancel_pacing` -- the same thing plus an :class:`asyncio.Event` --
    is a separate method that the mixin never calls.

    :class:`SessionMixin` is listed first among the bases so that
    ``super().__init__`` still reaches ``discord.ui.View``; it deliberately
    has no ``__init__`` of its own.

    Anyone in the channel can press the buttons (it's a social feature), and
    a press that arrives while somebody else's is being emulated is queued
    rather than dropped -- see the note above MAX_QUEUED_PRESSES. Only the
    person who started the game, moderators, and the bot owner can put the
    session to sleep (`[p]retrosleep`), reboot it (`[p]retroreboot`) or finish
    with it (`[p]retroend`).

    The message carries the clip and one line of text. There is no status
    card: the buttons say what they do, and the line says what just happened
    -- which button was pressed, or whatever had to be said instead (the game
    was asleep and has come back, a save state could not be restored, an
    emulator error). It is rewritten by every press, so it is never stale.
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
        clip_seconds: float = CLIP_SECONDS,
        hold_ms: int = DEFAULT_HOLD_MS,
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
        # There is deliberately no `timeout_minutes` here. A session used to
        # be handed the idle timeout at construction, and to be handed it
        # again by from_record on every restart, and nothing ever read it:
        # the sweep that hibernates idle sessions re-reads
        # `session_timeout_minutes` from Config on every pass (see
        # Retro._hibernate_idle), which is what makes `[p]retroset timeout` apply
        # to every session that is already running rather than only to the
        # next one started. A copy on the view could only ever have been the
        # stale answer.
        # Fractional, and clamped on the way in: the setting is a float now,
        # an installation that set it before it was has an int in Config, and
        # neither must be able to ask for a clip of no frames at all.
        self.clip_seconds: float = clamp_clip_seconds(clip_seconds)
        self.hold_ms: int = hold_ms
        self.screen_filename: str = self._screen_filename(game_name)

        # The live emulator, or None while hibernated.
        self.emulator: typing.Optional[RetroEmulator] = None

        # The machine states the last few presses started from, which is what
        # the Undo button pops. Set up by the mixin that owns it, because that
        # is where everything which reads or writes it now lives; see
        # SessionMixin._init_history for what it holds and why losing it is
        # cheap.
        self._init_history()
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
        # "fresh" (nothing to restore). Read by the cog after boot().
        self.boot_outcome: str = "fresh"

        self.message: typing.Optional[discord.Message] = None
        self.lock: asyncio.Lock = asyncio.Lock()

        # Presses that arrived while the lock was held, oldest first, at most
        # MAX_QUEUED_PRESSES of them -- and, deliberately, with no per-person
        # limit at all: one person may hold every slot, because pressing a
        # direction four times to walk four tiles is the commonest thing
        # anybody does with this controller and the per-person limit refused
        # three of those four clicks. This comment claimed "at most one per
        # person" for a while after that limit came off. See the "Queued
        # presses" note above MAX_QUEUED_PRESSES; nothing in here is emulator
        # work, and nothing in here survives a reset, an undo, a sleep or a
        # retirement.
        self.queue: typing.Deque[Pending] = collections.deque()
        # Bumped by forget_queue(), so an entry already taken off the front
        # cannot be run against a state it was not queued against.
        self._queue_epoch: int = 0
        # Whether a runner is working through the queue. Held across the gaps
        # where it lets the lock go (so `[p]retroreboot` can get in), and read
        # by _press to decide "queue this" rather than "run this now".
        self._draining: bool = False
        # How many entries the last discard threw away, so the next line the
        # session writes can say so once. See DROPPED_NOTE.
        self.queue_dropped: int = 0
        # Who has already been told that this controller's game was replaced,
        # so nobody is whispered at twice for tapping a dead button. Bounded
        # by MAX_REPLACED_NOTICES; see _replaced_ack.
        self._told_replaced: typing.Set[int] = set()

        # When the clip now on the message went out, on the monotonic clock,
        # and how long it plays for. A playback of 0 means "nothing is
        # playing", which is both the state before the first clip and what
        # teardown puts this back to. See the pacing note in retro/timing.py,
        # above MAX_PACE_SECONDS.
        self._posted_at: float = 0.0
        self._posted_playback: float = 0.0
        # The still the last posted clip left in the channel, as a
        # picture_hash and never as pixels: a SNES hi-res frame is 688 KiB
        # and this is per channel, for the life of the session. It is what
        # the next clip's opening pictures are compared against so that none
        # of the previous clip is shown again; see _encode and
        # clips.trim_repeated_opening. None means "nothing on screen is this
        # session's own to compare with", which is where it starts and what
        # forget_pacing puts it back to.
        self._last_picture: typing.Optional[bytes] = None
        # What the most recent _encode measured about the clip it produced,
        # waiting for the edit that posts it to make it true. See EncodedClip
        # and note_posted.
        self._encoded: typing.Optional[EncodedClip] = None
        # Set to release a pacing wait that is already in progress, and
        # cleared by the edit that posts the next clip. Only ever set on the
        # event loop; see cancel_pacing and forget_pacing.
        self._pace_release: asyncio.Event = asyncio.Event()
        #: How long the last edit was actually held back for, in seconds, and
        #: zero for an edit that went straight out. Read by the tests, and by
        #: nothing in the cog.
        self.last_pace_seconds: float = 0.0

        self._build_controls()
        self._update_repeat_label()

    # -- Layout -------------------------------------------------------------

    def _build_controls(self) -> None:
        """
        Lay this console's controller out; see the row plan in systems.py.

        The console's own grid comes first, spacers and all, and the control
        cluster -- Wait, confirm x3, Undo -- goes on the end of the last row
        if all of it fits there and on a row of its own if it does not. Three
        wide fits beside every console's bottom row here, including a
        two-button Start/Select.

        The room for the cluster is reserved with CONTROL_BUTTONS, i.e. for
        all three of them, whether or not the confirm x3 button is drawn (see
        MIN_REPEAT_TAPS): Wait and Undo must not move between one clip length
        and another, and a layout that only fits on a short clip would be a
        layout that breaks the day somebody lengthens the clip.

        All three are also *built* either way, at every clip length, and the
        repeat button is hidden from the payload rather than left out of the
        view when it has nothing to do -- which is what keeps a click on a
        stale copy of it routable. See the note above _STYLES.
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

        # Wait / confirm x3 / Undo. They share the last row when there is
        # space, which on every console here they do.
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
            # started on a short clip never shows a promise it cannot keep --
            # and built hidden on a clip too short for a repeat to mean
            # anything, so it is never *drawn* dead while still catching the
            # clicks a message drawn before then can still send. See
            # MIN_REPEAT_TAPS and _RepeatButton.hidden.
            self.add_item(_RepeatButton(confirm, control_row, self.repeat_taps))
        self.add_item(_UndoButton(control_row))
        if len(self.children) > MAX_COMPONENTS:
            raise ValueError(
                f"{self.system.name} needs {len(self.children)} components; "
                f"Discord allows {MAX_COMPONENTS}."
            )

    def to_components(self) -> typing.List[typing.Dict[str, typing.Any]]:
        """
        The rows Discord is sent, which are the children minus the hidden one.

        The one place "hidden" means anything. discord.py builds a message's
        ``components`` from this (see HTTPClient's payload assembly), and
        builds its *routing table* from ``children`` instead
        (ViewStore.add_view) -- so leaving an item out here takes it off the
        screen and leaves it reachable, which is exactly the split the repeat
        button needs. See the note above _STYLES.

        Filtering the payload the superclass built, rather than the children
        it builds it from, on purpose: what a row is and how a button becomes
        a dict stays discord.py's business, and this only has to drop one
        entry by custom_id and then drop a row that has nothing left in it
        (Discord refuses an empty action row). Nothing this cog lays out can
        empty a row -- the control cluster is three buttons and only the
        middle one hides -- but a future layout that could must not produce a
        payload Discord will 400.
        """
        rows = super().to_components()
        hidden = {
            child.custom_id
            for child in self.children
            if getattr(child, "hidden", False)
        }
        if not hidden:
            return rows
        drawn = []
        for row in rows:
            components = [
                component
                for component in row["components"]
                if component.get("custom_id") not in hidden
            ]
            if components:
                drawn.append({**row, "components": components})
        return drawn

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
        clip_seconds: float = CLIP_SECONDS,
        hold_ms: int = DEFAULT_HOLD_MS,
    ) -> "RetroView":
        """
        Rebuild a hibernated session from Config after a restart.

        The two settings that arrive here are the two a *layout* depends on:
        how long a clip is and how long a button is held decide how many taps
        the repeat button can do, and therefore whether it is drawn at all
        (see :meth:`_update_repeat_label`). The idle timeout used to be
        threaded through here as well and was never read by anything; see the
        note in :meth:`__init__`.
        """
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

        Anything still waiting in the queue goes with it. Those presses were
        aimed at a game this channel has moved on from, and replaying them
        into whatever is playing now would be worse than dropping them.
        """
        self.closed = True
        self.forget_queue()
        # And a press that is sitting out the clip on screen stops sitting:
        # there is nothing left for it to pace against. See cancel_pacing.
        self.cancel_pacing()
        self._disable()

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

    def _fps(self, emulator: typing.Optional[RetroEmulator] = None) -> float:
        """
        The rate to lay a clip out against: this core's, or the session's.

        ``emulator`` is the core the caller already has in hand and is about
        to drive, which is the honest rate for the clip it is about to record
        -- and is not always ``self.emulator``: a press captures with the core
        the cog handed it while the emulator lock held it still (see
        ``Retro.run_press``), and another channel is entitled to have cleared
        ``self.emulator`` by the time the arithmetic is re-done. Omitting it
        falls back to :attr:`fps`, which is the live core's rate or
        DEFAULT_FPS while hibernated.

        Two callers, and they have to agree: :meth:`clip_frames` decides how
        long the window is and :meth:`press_plan` decides where the taps go
        inside it, so a rate that differed between them would put a tap past
        the end of the clip it was scheduled into.
        """
        return self.fps if emulator is None else emulator.fps

    def clip_frames(self, emulator: typing.Optional[RetroEmulator] = None) -> int:
        """How many emulated frames one clip covers on this console."""
        return clip_frame_count(self._fps(emulator), self.clip_seconds)

    def clip_playback(self) -> float:
        """
        How long one of this session's clips plays for, in seconds.

        The sum of the durations the encoder is handed rather than the clip
        length that was asked for, so it is what the clip really does on
        screen: at the default second a Game Boy clip is 60 frames, emulates
        1.0046 seconds and plays for 1.005. See :func:`playback_seconds`.

        A property of the *window*, so it is answerable before anything has
        been captured, which is what a session that has not encoded a clip
        yet needs. A clip whose opening was trimmed as already-on-screen
        plays for less than this, and that is what :meth:`posted_playback`
        reads; the pacing gate uses that one. See the pacing note in
        retro/timing.py, above MAX_PACE_SECONDS.

        There is deliberately **no** ``emulator`` argument, unlike its two
        neighbours. There was one and nothing ever passed it: every caller is
        either :meth:`posted_playback` or a test, and all of them ask about
        the session rather than about a particular core. Its neighbours' ones
        are used and stay -- ``Retro.run_press`` hands the core it captured
        with to the arithmetic that has to match it.
        """
        return playback_seconds(self.fps, self.clip_frames())

    def press_plan(
        self, taps: int = 1, emulator: typing.Optional[RetroEmulator] = None
    ) -> typing.List[typing.Tuple[int, int]]:
        """This session's ``(start, hold)`` frames; see :func:`press_plan`."""
        return press_plan(self._fps(emulator), self.clip_seconds, self.hold_ms, taps)

    @property
    def repeat_taps(self) -> int:
        """
        How many taps the repeat button really does at this clip length.

        Three when there is room for three, fewer when there is not, and one
        -- meaning "no more than the confirm button itself" -- on a clip too
        short for even two, which is when the button is not drawn at all.
        """
        return len(self.press_plan(REPEAT_TAPS))

    # -- The clip on the message --------------------------------------------

    def _button_caption(self, field: str, repeat: int = 1) -> str:
        """
        What to call one button in a line of text: ``A``, ``\N{UPWARDS BLACK ARROW}``, ``A x3``.

        The console's own name for it, through :meth:`System.caption_for`, so
        nothing here can drift out of step with the label on the button that
        was clicked -- a Genesis press reads "C" rather than "A" because its C
        is RetroPad *a*, and the d-pad has no labels at all, so it names itself
        with its arrow.

        The tap count is the one :func:`press_plan` will really fit rather than
        the ``repeat`` that was asked for: a short clip fits two, and "x3" over
        a clip showing two taps is the same lie the button's own label refuses
        to tell (see :meth:`_update_repeat_label`).

        **This function is the invariant**, which is the reason it is one
        function. :meth:`press_note` writes what just happened and
        :meth:`queued_label` writes what is about to, on the same line, one
        after the other -- ``Rob pressed A x3. *Queued: Ada A x3*`` -- and two
        copies of these three lines are two chances for the same button to be
        named two ways in the same sentence.
        """
        caption = self.system.caption_for(field)
        taps = len(self.press_plan(repeat)) if repeat > 1 else 1
        return f"{caption} x{taps}" if taps > 1 else caption

    def press_note(
        self,
        field: typing.Optional[str],
        repeat: int = 1,
        user: typing.Any = None,
    ) -> str:
        """
        The one line that says who pressed which button.

        See ACTION_NOTES for the sentence and :meth:`_button_caption` for the
        button's name in it -- the console's own, taken from systems.py so
        nothing can drift out of step with what is drawn on the button that was
        clicked. ``field`` of None is the Wait button, which pressed nothing.

        ``user`` is whoever clicked -- ``interaction.user``, which is a
        Member in a guild and a plain User otherwise, and is still handed over
        for somebody who has left the guild since. Omitting it (or passing
        anybody :func:`presser_name` cannot name) gives the impersonal form of
        the same sentence rather than no line at all.

        The repeat button counts the taps that will really happen rather than
        the three that were asked for, exactly as its label does -- a short
        clip fits two, and a line saying "x3" over a clip showing two taps
        would be the same lie the label refuses to tell.
        """
        if field is None:
            return action_note("wait", user)
        return action_note("press", user, self._button_caption(field, repeat))

    @staticmethod
    def undo_note(user: typing.Any = None) -> str:
        """The line the Undo button puts on the message; see ACTION_NOTES."""
        return action_note("undo", user)

    @staticmethod
    def reset_note(user: typing.Any = None) -> str:
        """
        The line `[p]retroreboot` puts on the message; see ACTION_NOTES.

        A command rather than a button, so the author is ``ctx.author``
        rather than ``interaction.user`` -- but the same person, named the
        same way, in the same voice as every press above it.
        """
        return action_note("reset", user)

    def _repeat_button(self) -> typing.Optional["_RepeatButton"]:
        """
        The repeat button, which every console with a confirm button has.

        There either way, drawn or not: it is hidden from the payload on a
        clip too short to repeat in rather than taken out of the view. See
        :meth:`to_components` and the note above _STYLES.
        """
        return next(
            (child for child in self.children if isinstance(child, _RepeatButton)), None
        )

    def _update_repeat_label(self) -> None:
        """
        Draw the repeat button if it can do anything, and say how much.

        Three taps need about 1.4 seconds of clip; :func:`press_plan` squeezes
        the spacing to fit a shorter one and then drops taps, so the label has
        to follow it rather than stating REPEAT_TAPS for ever.

        At one tap the button does nothing the console's own confirm button
        does not, and it is **taken out of the payload** rather than greyed
        out. That is a change from how it used to behave, and the reason is
        that the greyed-out version was reported as the feature having been
        taken out of the cog: a dead control with no explanation looks
        broken. A missing control says "not at this clip length", which is
        the truth.

        Out of the payload and not out of the view, though, which is the
        other half of the same honesty: a message Discord has not re-rendered
        still shows the button, and a click on it has to land somewhere that
        answers rather than on a custom_id nothing is registered for. See
        :meth:`to_components`, :class:`_RepeatButton` and the note above
        _STYLES. Nothing here rebuilds the row any more, so the button also
        cannot come back in the wrong place: it never leaves its seat between
        Wait and Undo.

        A control that silently *disappears* reads as removed too, so going
        sets :attr:`notice` -- one line on the next edit the session makes,
        which is an edit that was happening anyway. Coming back says nothing:
        the button is right there saying what it does. Only a change is news;
        a session that was built this way says nothing at all, which is why
        :class:`_RepeatButton` decides its own starting state.

        Called by **every** edit that redraws the row (see :meth:`_edit`), so
        changing `[p]retroset cliplength` mid-game hides or shows the button
        both ways round -- and so does booting a core, since a session laid
        out while hibernated used DEFAULT_FPS and a 50 fps PAL core can fit a
        tap the fallback said would not fit (and vice versa).

        "Every edit" is newer than it looks, and it is the fix to a bug worth
        naming. :meth:`refresh` was the one edit path that sent ``view=self``
        without coming through here, so `[p]retroset cliplength`'s
        redraw-every-live-game -- whose entire purpose is to close the
        stale-button window *now* rather than on the next press -- was sending
        the previous clip length's visibility and label. See :meth:`_edit`,
        which is where there stopped being four edit paths to forget one of.
        """
        button = self._repeat_button()
        if button is None:
            # A console with no confirm button at all, which none of the ones
            # in systems.py is.
            return
        taps = self.repeat_taps
        hidden = taps < MIN_REPEAT_TAPS
        if hidden and not button.hidden:
            self.notice = REPEAT_GONE_NOTE.format(
                button=self.system.label_for(self.system.confirm)
            )
        button.hidden = hidden
        # Written even while hidden, so the label is already honest on the
        # redraw that brings the button back rather than one press later.
        button.label = f"{button.name} x{taps}"

    # -- The press queue ----------------------------------------------------

    @property
    def running(self) -> bool:
        """
        Whether the emulator is being driven for this session right now.

        Two things say so: the lock is held by a press that is emulating, or
        a runner is working through the queue and has merely let the lock go
        between two of them (see :meth:`_drain`). Either way nothing else may
        touch the core.
        """
        return bool(self.lock.locked() or self._draining)

    @property
    def busy(self) -> bool:
        """
        Whether a press must be queued rather than run now.

        :attr:`running`, or something is already waiting -- in which case
        running now would jump the line.

        The second half is not merely theoretical, and assuming it was is
        what made this the worst bug in the queue: entries are made whenever
        something holds :attr:`lock`, and an undo, a reboot or a sleep holds
        it without ever draining afterwards. One click landing in that window
        left a queue with no runner, which makes this permanently true --
        every later click is queued behind an entry nothing will ever take,
        and the controller is dead until the session hibernates. Both halves
        of that are fixed: the paths that hold the lock now deal with what
        arrived while they held it (see :meth:`_undo` and ``Retro``'s sleep
        and reboot commands), and :meth:`_press` starts a runner itself if it
        ever finds a stranded queue, so the state repairs itself on the next
        click rather than needing the session to go to sleep.
        """
        return bool(self.running or self.queue)

    def enqueue_press(
        self,
        interaction: typing.Any,
        field: typing.Optional[str],
        repeat: int = 1,
        user: typing.Any = None,
    ) -> bool:
        """
        Remember a press to emulate as soon as the session is free.

        Returns whether it was accepted. It is refused when

        * the session has been replaced or retired -- there is nothing left
          for a press to reach;
        * MAX_QUEUED_PRESSES are already waiting.

        That is the whole list. **One person may hold every slot**, which is
        a deliberate change: pressing a direction four times to walk four
        tiles is the commonest thing anybody does with this controller, and
        the per-person limit refused three of those four clicks. See the note
        above MAX_QUEUED_PRESSES.

        Never raises, and never touches the emulator: see the same note.

        The ``closed`` half of that guard is belt and braces: :meth:`_press`,
        its only caller, answers a closed view through :meth:`_replaced_ack`
        and never reaches here. It is kept anyway, and not merely because the
        two-line contract above would otherwise be a lie -- an accepted entry
        holds a Discord interaction object for the life of the queue, and a
        queue on a retired view is one nothing will ever drain, so the cheap
        boolean is the difference between "cannot happen" and "cannot happen
        *and* would be harmless".
        """
        if self.closed or len(self.queue) >= MAX_QUEUED_PRESSES:
            return False
        user = user if user is not None else getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        self.queue.append(
            Pending(
                interaction=interaction,
                user_id=user_id,
                who=presser_name(user),
                field=field,
                repeat=max(1, int(repeat)),
                epoch=self._queue_epoch,
            )
        )
        return True

    def forget_queue(self) -> None:
        """
        Throw every waiting press away, and remember how many that was.

        Called whenever the game stops being the thing those presses were
        aimed at: it is stopped, rebooted, undone, put to sleep, retired, or
        replaced by another game. Replaying a queued direction into a
        different game state is worse than dropping it -- Undo in particular
        would be undone again by the very presses it was correcting.

        How many went is left in :attr:`queue_dropped`, so the next line the
        session writes can say so once (see DROPPED_NOTE). That is the whole
        mechanism, and it is deliberately the *only* answer this gives: it used
        to return the count as well, which no production caller has ever read
        -- all five throw it away -- and a second way to ask the same question
        is a second thing to keep true. The accumulating attribute is the one
        that has to work, because the discard and the line that reports it are
        two different moments and often two different threads (``capture_undo``
        calls this from the worker).

        Bumping the epoch is what also discards an entry a runner has already
        taken off the front but not yet emulated.
        """
        dropped = len(self.queue)
        self.queue.clear()
        self._queue_epoch += 1
        if dropped:
            self.queue_dropped += dropped

    def queued_label(self, entry: Pending) -> str:
        """
        One waiting press's button, named the way the press line names it.

        The button and nothing else; whose it is comes from grouping in
        :meth:`queue_note`, because one person's run of presses reads as one
        thing rather than as several. See QUEUE_ENTRY.

        Named by :meth:`_button_caption`, which is the same three lines
        :meth:`press_note` used to keep its own copy of -- and the reason they
        are one function is that these two strings end up side by side on one
        line, so a drift between them would be visible in a single sentence.
        A queued Wait is the exception, because "wait" is a word rather than a
        button; see QUEUED_WAIT.
        """
        if entry.field is None:
            return QUEUED_WAIT
        return self._button_caption(entry.field, entry.repeat)

    def queued_runs(self) -> typing.List[typing.Tuple[str, typing.List[str]]]:
        """
        The queue as ``(who, [button, ...])`` runs, in the order it will run.

        Consecutive entries by the same person are one run, so four clicks
        from one person are ``("Rob", ["\N{UPWARDS BLACK ARROW}", ...])``
        rather than four separate things to read. A different person starts
        a new run; the order is never rearranged, because the order is what
        the queue *is*.

        Grouped on the presser's *id* rather than on their name, so two
        people who happen to render the same name are still two runs.
        """
        runs: typing.List[typing.Tuple[str, typing.List[str]]] = []
        last_id = object()
        for entry in self.queue:
            if entry.user_id is None or entry.user_id != last_id:
                runs.append((entry.who, []))
                last_id = entry.user_id if entry.user_id is not None else object()
            runs[-1][1].append(self.queued_label(entry))
        return runs

    def queue_note(self) -> str:
        """
        The suffix that says what is waiting, or ``""`` when nothing is.

        This is the whole acknowledgement a queued press gets, and it is
        deliberately the *only* one: it rides on the line the running press
        is already rewriting, so a queued click costs no edit of its own. An
        ephemeral "your press is queued" would be a second message per click
        (removed once already for being spam) and any edit of this message
        would re-render the attachment and visibly rewind the clip -- see
        :meth:`_ack_now`.
        """
        if not self.queue:
            return ""
        parts = []
        for who, buttons in self.queued_runs():
            # A Wait reads as a word rather than a glyph, so a run holding
            # one needs a space to stay readable: "Rob wait ⬆️", not
            # "Rob wait⬆️". A run of pure emoji is closed up, which is what
            # makes four presses read as one gesture.
            joiner = " " if any(" " in b or b.isalpha() for b in buttons) else ""
            drawn = joiner.join(buttons)
            parts.append(
                QUEUE_ENTRY.format(who=who, buttons=drawn)
                if who
                else QUEUE_ENTRY_ANONYMOUS.format(buttons=drawn)
            )
        return QUEUE_NOTE.format(queued=", ".join(parts))

    def dropped_note(self) -> str:
        """
        The suffix that says a discard happened, shown exactly once.

        Cleared as it is read, like :attr:`notice`, because "3 queued presses
        dropped" is news about one moment rather than a state.
        """
        dropped, self.queue_dropped = self.queue_dropped, 0
        if not dropped:
            return ""
        return DROPPED_NOTE.format(count=dropped, plural="" if dropped == 1 else "es")

    # -- Pacing the clips ---------------------------------------------------
    #
    # See the note above MAX_PACE_SECONDS in retro/timing.py for the
    # measurements and the rule.
    # Four small methods and one await, and none of them touches a lock.

    def posted_playback(self) -> float:
        """
        How long the clip that is about to go on the message plays for.

        :meth:`clip_playback` is the same question asked of the *window* --
        answerable at any time, and what a session that has not encoded
        anything yet has to fall back on. This is the figure read off the
        clip that was really encoded, which is shorter by exactly the
        durations :func:`trim_repeated_opening` dropped when a clip opened on
        the still already in the channel.

        Paired with :meth:`note_posted`, which is the only caller that
        matters: the edit reads this, posts, and then says it posted.
        """
        encoded = self._encoded
        return self.clip_playback() if encoded is None else encoded.playback

    def note_posted(self, playback: float) -> None:
        """
        Remember that a clip is now on screen, and for how long it plays.

        Called by every edit that puts a clip on the message -- the first one
        of a session, a press, an undo, a reboot -- immediately *after* the
        edit has gone through, because that is when the clip starts playing
        in somebody's client rather than when it was encoded.

        This is also where the still that clip will leave in the channel
        becomes the one the *next* clip's opening is compared against, for
        the same reason and at the same moment: an edit that Discord refused
        put nothing on anybody's screen, so its :class:`EncodedClip` is
        dropped here unpromoted and the picture from the edit before it stays
        the one on screen. See :meth:`_encode`.

        Also clears the release flag: a new clip is a fresh start, and the
        reason the last wait was cut short does not apply to this one.
        """
        encoded, self._encoded = self._encoded, None
        self._last_picture = None if encoded is None else encoded.last_picture
        self._posted_at = time.monotonic()
        self._posted_playback = max(0.0, float(playback))
        self._pace_release.clear()

    def forget_pacing(self) -> None:
        """
        Stop pacing against whatever is on screen: it no longer describes the
        game.

        Used by the paths that move the machine somewhere the clip on the
        message is not -- an undo, a reboot -- so that the clip *they* post
        goes out immediately rather than waiting behind the one they have
        just made wrong.

        The remembered still goes with it, and for the same reason: a picture
        this session is no longer entitled to call its own must not be
        allowed to trim the opening off the clip that replaces it. That is
        what stops a stale hash from cutting the first clip after a wake --
        every path that takes the game away (sleep, reboot, undo, retire,
        eviction, unload) comes through here or through
        :meth:`cancel_pacing`. The clip that has been *encoded* but not yet
        posted is deliberately left alone: it is not on screen, so it is not
        what this is about.

        Safe from a worker thread, which is why it is separate from
        :meth:`cancel_pacing`: it writes two plain attributes and nothing
        else. It cannot release a wait that is already in progress, and it
        does not have to -- both callers run under :attr:`lock`, which a
        waiting press is holding.
        """
        self._posted_playback = 0.0
        self._last_picture = None

    def cancel_pacing(self) -> None:
        """
        Stop pacing, and release a wait that is already in progress.

        **Every teardown path calls this before it takes anything.** Sleeping
        a session, ending it, rebooting it, evicting it for another channel
        and unloading the cog all reach for :attr:`lock` or free the core,
        and a press that is sitting out a clip's playing time is holding that
        lock; without this they would wait for up to MAX_PACE_SECONDS behind
        a purely cosmetic delay. Releasing the wait lets that press make its
        one edit and get out of the way immediately.

        Event loop only (:class:`asyncio.Event` is not thread-safe). A worker
        thread wants :meth:`forget_pacing`.

        Never raises, and safe to call on a session that is not pacing, which
        is almost always.
        """
        self.forget_pacing()
        self._pace_release.set()

    def pace_delay(self) -> float:
        """
        How long an edit has to wait before it may replace the clip on screen.

        Zero -- meaning "go now" -- whenever nothing is playing, the session
        is finished with, or the clip has already played through. Otherwise
        **all** of the time left on it, however long the clip is.

        MAX_PACE_SECONDS bounds the answer, but it is a second clear of the
        longest clip this cog can be asked to make, so it only ever bites on
        a ``_posted_playback`` no clip could have produced. It used to be
        1.25 seconds flat, which truncated every clip above that length and
        jumped the player forward over the footage it cut; see the pacing
        note in retro/timing.py for the measurements.
        """
        if self.closed or self._posted_playback <= 0.0:
            return 0.0
        left = (self._posted_at + self._posted_playback) - time.monotonic()
        return max(0.0, min(left, max(0.0, MAX_PACE_SECONDS)))

    async def pace(self) -> None:
        """
        Sit out the rest of the clip on screen, so the next one replaces it
        whole.

        Called by :meth:`_run_press` between the clip coming back from the
        emulator and the edit that posts it -- i.e. with the emulator lock
        already given back, so a wait here costs this channel some latency
        and costs every other channel nothing at all.

        Records what it waited in :attr:`last_pace_seconds`, and spends it in
        :func:`pace_wait`, which is the one place any of this takes time.
        """
        delay = self.pace_delay()
        if delay < MIN_PACE_SECONDS or self._pace_release.is_set():
            # Nothing playing, a clip that has already played through, a wait
            # too short to be worth a round trip through the event loop, or a
            # teardown that has already said not to bother.
            self.last_pace_seconds = 0.0
            return
        self.last_pace_seconds = delay
        log.debug(
            "Holding the next clip for %.3fs in channel %s so the one on "
            "screen plays through.",
            delay,
            self.channel_id,
        )
        await pace_wait(self._pace_release, delay)

    @staticmethod
    def _screen_filename(game_name: str) -> str:
        """A stable, Discord-safe attachment name for this session's clips."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game_name).strip("-")[:48]
        return f"{safe or 'screen'}{CLIP_EXTENSION}"

    def _disable(self) -> None:
        """
        Grey the controls out for good. Spacers were already inert.

        One direction only, and :meth:`retire` is the only caller: a press
        used to grey the controls out for the second it took to emulate, and
        that cost an extra edit of the message, which is what made the
        previous clip play again from the beginning (see :meth:`_ack_now`).

        This was ``_set_disabled(bool)``, and the ``False`` half has gone
        rather than been kept for symmetry. It had two jobs and only ever
        really did the second: re-enabling children that nothing disables any
        more (Undo stopped greying itself out on an empty history -- see
        :class:`_UndoButton`) and, on the way past, calling
        :meth:`_update_repeat_label`. The redraw was the whole live payload of
        it, and it now lives in :meth:`_edit`, where the redraw belongs
        because that is the one place a redraw is *sent*.

        Dropping the ``False`` half also closed a real hole rather than merely
        tidying one. ``Retro._retire`` takes only ``emulator_lock``, so
        :meth:`retire` can run while a press is sitting in :meth:`pace`
        holding this session's own lock -- and that press's edit then called
        ``_set_disabled(False)`` and put a retired controller's buttons back.
        Nothing puts them back now.
        """
        for child in self.children:
            if isinstance(child, _SpacerButton):
                continue
            if hasattr(child, "disabled"):
                child.disabled = True

    # -- Messages -----------------------------------------------------------

    @property
    def header(self) -> str:
        """
        What game this is, and on what, in the few characters it deserves.

        ``**µCity**``, and nothing else. It is the *stable* part of the one
        line the message carries, and it exists because the line used to be
        nothing but "Rob pressed A." -- and, on the first clip of a cold
        boot, nothing at all. Anybody scrolling into the channel saw an
        animation, a grid of unlabelled arrows and a name, with nothing
        anywhere saying what was being played.

        The console is deliberately no longer part of it; see HEADER.

        **Whether the session is asleep is deliberately not part of it
        either.** It used to append ``· asleep`` whenever no core was
        loaded, which was accurate and unhelpful: sleeping is an
        implementation detail of how this cog fits one emulator across every
        channel, the controls stay live through it, and the next press wakes
        the game with no more ceremony than any other press. Labelling a
        perfectly usable controller "asleep" invites somebody to think it is
        broken, or that they have to do something to it first. The wake
        press says RESUMED_NOTE, which is the one moment the delay is worth
        explaining.

        Deliberately not a status card: the card this cog used to have was
        removed, and this is one line rather than a second attempt at it.
        See HEADER.

        The game's name is escaped the same way a presser's is: it comes from
        a ROM filename, so it can perfectly well contain the underscores and
        hyphens Discord reads as markup. See :func:`escape_label`.
        """
        name = str(self.game_name or "Game")[:MAX_GAME_NAME]
        line = HEADER.format(game=escape_label(name))
        return line

    def _line(self, text: typing.Optional[str] = None) -> str:
        """
        The whole of the one line the message carries, assembled.

        ``**µCity** · Rob pressed A. *Queued: ⬅️*``: three pieces, in this
        order, and every one of them rides on an edit that was already being
        made:

        1. the :attr:`header` -- what game, and whether it is asleep. Always
           there;
        2. ``text`` -- what just happened. Which button was pressed and by
           whom, that the session woke up, that a save state could not be
           restored, that the emulator failed;
        3. the queue, and anything the queue has just dropped. See
           :meth:`queue_note` and :meth:`dropped_note`: this suffix is the
           entire acknowledgement a queued press gets, which is why it has to
           live on a line somebody else's press is rewriting anyway.

        Never returns None any more, and that is a real change: it used to,
        so that discord.py would send an explicit null and clear a stale
        line. With a header there is always something to say, so every edit
        rewrites the whole content and nothing can go stale either way.
        """
        parts = [self.header]
        if text:
            parts.append(text)
        line = HEADER_SEPARATOR.join(parts)
        for suffix in (self.dropped_note(), self.queue_note()):
            if suffix:
                line = f"{line} {suffix}"
        return line

    def _content(self, message: typing.Optional[str] = None) -> str:
        """
        The text for an edit that is *not* posting a new clip: one short line.

        The clip and the buttons are most of the interface, so the text is
        one line and no more: the :attr:`header`, then ``message`` from the
        caller -- the game went to sleep, the emulator failed -- or, when the
        caller has nothing to say, a pending one-off notice, cleared as it is
        shown so it appears exactly once.

        ``message`` deliberately wins here, which is the opposite of
        :meth:`_clip_content`: the callers with something to say are a
        hibernate and a failed press, and neither may have its news replaced
        by a notice about the boot. The notice is left pending instead, so
        the next clip carries it.

        Which is the whole of the difference between the two, and it only
        exists when there is something to choose between: with nothing to say,
        "the caller wins" and "the notice wins" are the same sentence, so this
        hands ``None`` straight over rather than keeping a second copy of the
        pop. **Both names stay** -- they are a real two-value precedence
        policy with two call sites each, and collapsing them into one method
        with a flag would move the decision out to five call sites that each
        have to get it right.
        """
        return self._line(message) if message is not None else self._clip_content()

    def _clip_content(self, note: typing.Optional[str] = None) -> str:
        """
        The line that goes out on the one edit a new clip is posted with.

        A pending :attr:`notice` beats ``note`` and is cleared as it is
        shown. That order is the point: ``note`` is a label for what was just
        done ("Rob pressed A.") and a notice is something the session
        *discovered* and has to report -- that the save state was rejected
        after a core update, that the repeat button had to go -- and only one
        line fits.

        Both paths that post a clip use this: :meth:`_show`, from a button
        click, and :meth:`show_clip`, from a command. They each used to
        decide it for themselves, and they disagreed -- ``show_clip`` went
        through :meth:`_content`, whose precedence is the other way round, so
        a reboot that shrank the repeat button deferred its notice and popped
        it up out of context on some later press.
        """
        notice, self.notice = self.notice, None
        return self._line(notice or note)

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

    async def _edit(
        self,
        target: typing.Any,
        content: typing.Callable[[], str],
        clip: typing.Optional[bytes] = None,
        what: str = "update the Libretro message",
    ) -> bool:
        """
        Redraw the controls and rewrite the line, with or without a new clip.

        **The one edit path.** There were four -- :meth:`refresh`,
        :meth:`show_clip`, :meth:`_show` and :meth:`_recover` -- and they were
        a clean two by two: a Message or an Interaction to edit, a new clip or
        the one already up there. Every cell repeated ``view=self``,
        ``allowed_mentions=NO_PINGS`` and ``except discord.HTTPException``, and
        the promises that made them the same thing were only true because four
        separate bodies happened to agree. One of them did not, which is why
        this exists rather than merely being tidier: ``refresh`` was the only
        one that never redrew the row, so `[p]retroset cliplength`'s
        edit-every-live-game sent the *previous* clip length's button
        visibility and label -- and the comment in ``Retro.py`` claiming that
        edit closed the stale-button window was wrong for as long as that
        redraw was missing. Four paths is four chances to forget one of three
        things; one path is none.

        ``target`` is a :class:`discord.Message` or a
        :class:`discord.Interaction`, told apart by which edit method it has:
        an interaction's ``edit_original_response`` targets the message the
        clicked component is on, which ``response.defer()`` acknowledged
        without touching. Both hand back the edited message, which is cached.

        ``clip`` decides three things at once, and that is the point rather
        than a coincidence. A clip means ``attachments=`` (omitting it is what
        makes Discord *keep* the clip already on the message), and it means
        :meth:`note_posted` afterwards -- so a clip is paced against and
        remembered exactly when there is a clip to pace against, and
        ``refresh``'s documented refusal to re-arm the gate falls out of
        passing no clip rather than out of remembering not to.

        ``content`` is a **callable**, called here rather than by the caller,
        for two reasons. It has to run after the redraw, because a redraw can
        discover news -- :meth:`_update_repeat_label` sets :attr:`notice` when
        the repeat button has to go -- and the line that announces it should be
        the line on the very edit that takes the button away. And it is the one
        place the state it consumes can be *put back*: assembling the line pops
        the one-off notice and zeroes the dropped-press count, so before this
        an edit Discord refused lost both for good and "shown exactly once"
        really meant "assembled exactly once". They are restored below.

        Returns whether Discord took it. Never raises: every caller has
        somewhere better to put a failure than up the stack -- a command has
        its own reply, a press has a private line to whoever clicked, and a
        hibernate's line on the message is not worth failing a hibernate over.
        """
        # The row first, so `content()` below can report anything the redraw
        # discovered on this very edit.
        self._update_repeat_label()
        notice, dropped = self.notice, self.queue_dropped
        kwargs: typing.Dict[str, typing.Any] = {
            "content": content(),
            "view": self,
            # The line this carries names whoever clicked or whoever ran the
            # command, and must never notify them or anybody else; see
            # NO_PINGS. One promise, made in one place, for all four callers.
            "allowed_mentions": NO_PINGS,
        }
        if clip is not None:
            kwargs["attachments"] = [self._clip_file(clip)]
        edit = getattr(target, "edit_original_response", None) or target.edit
        try:
            self.message = await edit(**kwargs)
        except discord.HTTPException as error:
            # The game itself is fine -- the message may simply have been
            # deleted, or the bot may have lost the channel, or Discord may
            # have refused the component payload. The HTTP status and Discord's
            # own error code go here, where somebody who can act on them will
            # look; what the player is told (if anything) is the caller's
            # business.
            log.warning(
                "Failed to %s in channel %s (HTTP %s, code %s).",
                what,
                self.channel_id,
                getattr(error, "status", "?"),
                getattr(error, "code", "?"),
                exc_info=True,
            )
            # Nothing reached anybody's screen, so nothing was "shown once".
            # Added rather than assigned, and only into an empty notice,
            # because the await above is a real suspension point: another
            # channel's teardown can have discarded this queue or set a newer
            # notice while Discord was thinking, and the newer news wins.
            self.queue_dropped += dropped
            if self.notice is None:
                self.notice = notice
            return False
        if clip is not None:
            # This clip is now the one playing, so it is the one the next edit
            # is paced against, and the still it leaves is what the next clip's
            # opening is compared against. After the edit, not before: what is
            # being timed is the picture on somebody's screen. See note_posted.
            self.note_posted(self.posted_playback())
        return True

    async def refresh(self, note: typing.Optional[str] = None) -> None:
        """
        Re-edit the message with the current controls, keeping the last clip.

        ``attachments`` is deliberately not passed, so Discord keeps the clip
        that is already on the message.

        A client re-renders on any edit, so the clip it keeps does start
        playing again -- and this deliberately does *not* re-arm the pacing
        gate for it (see :meth:`note_posted`). Passing no ``clip`` to
        :meth:`_edit` is the whole of how that is arranged, which is neater
        than it sounds: the refusal is now the same fact as "there is no new
        clip here", rather than a thing to remember not to do.

        One caller is a hibernate, which has just cancelled pacing on purpose,
        and whose next press is a wake: a core to load and a save state to
        restore, which takes far longer than any clip plays for. The other is
        ``Retro._refresh_live_controls``, behind `[p]retroset cliplength`,
        which wants the *row* rather than the line -- and which was getting a
        stale row until :meth:`_edit` took the redraw over. See
        :meth:`_update_repeat_label`.

        Not re-arming the gate also means not re-remembering the still: the
        clip kept here is one this session has stopped being able to reason
        about, and its picture must not be allowed to trim the opening off
        the first clip after the wake. That is the same ``cancel_pacing`` the
        hibernate already made, in the same order -- it happens before this
        edit is queued. See :meth:`forget_pacing`.
        """
        message = await self.resolve_message()
        if message is None:
            return
        await self._edit(
            message,
            lambda: self._content(note),
            what="refresh the Libretro message",
        )

    async def show_clip(self, clip: bytes, note: typing.Optional[str] = None) -> bool:
        """
        Put a clip on the session's message from outside an interaction.

        :meth:`_show` is the same edit made from a button click, where the
        interaction is what has to be edited; this is for a *command* that
        moved the game on and wants the game's own message to show it --
        `[p]retroreboot`. One edit, for the same reason a press makes one (see
        :meth:`_ack_now`), and the same precedence: a pending :attr:`notice`
        beats ``note``.

        Returns whether the message was edited. Never raises: the command has
        its own reply to fall back on, and a message that has been deleted is
        not a reason for the reset itself to look like it failed.
        """
        message = await self.resolve_message()
        if message is None:
            return False
        return await self._edit(
            message,
            lambda: self._clip_content(note),
            clip,
            what="put a new clip on the Libretro message",
        )

    # -- Starting -----------------------------------------------------------

    async def boot(
        self,
        starter_id: typing.Optional[int],
        emulator: RetroEmulator,
        progress: typing.Optional[Progress] = None,
        on_booted: typing.Optional[typing.Callable[["RetroView"], None]] = None,
    ) -> bytes:
        """
        Bring the core up and record the first clip. Returns the clip.

        **The half of starting a game that needs the core**, and therefore
        the half the cog holds its emulator lock across. :meth:`post` is the
        other half, and they are separate for exactly that reason: posting a
        message is a Discord round trip, and one made under the emulator lock
        stalls every other channel's presses for as long as Discord takes to
        answer. They used to be one method, so a rate-limited send froze
        gameplay bot-wide.

        ``progress`` is this channel's saved progress for this game, if it has
        played it before. Starting a game the channel already has a save for
        picks up where it left off rather than cold-booting over the top of
        it, through the same chain a hibernated session is woken with.

        ``on_booted`` is called once the core is up and :attr:`boot_outcome`
        says how much of the game came back with it, and *before* the message
        goes out -- which is what lets the cog put the sentence explaining the
        restore on the very message the first clip arrives on, rather than in
        a second one after it.

        ``starter_id`` is whoever is starting the game, and **None means
        "leave the starter alone"**. That is not a convenience: a Resume click
        brings back a game somebody else may have started, and the starter is
        what decides who may sleep, reboot or wipe it (see
        ``permissions.may_manage``). Overwriting it with the clicker would
        quietly hand those rights to whoever pressed a button. This used to
        take a ``ctx`` for the sole purpose of reading ``ctx.author.id``,
        which is why the resume path could not use it and hand-rolled all
        five of the steps below instead.
        """
        if starter_id is not None:
            self.starter_id = starter_id
        clip = await self.cog.run_in_emulator_thread(self._boot, emulator, progress)
        self.emulator = emulator
        # Now that there is a core, the repeat button's label can be written
        # from its real frame rate rather than DEFAULT_FPS -- and it is
        # written before the message goes out, so the first thing anybody
        # sees is already correct.
        self._update_repeat_label()
        self.touch()
        if on_booted is not None:
            on_booted(self)
        return clip

    async def post(self, ctx: commands.Context, clip: bytes) -> discord.Message:
        """
        Post the first clip with the controls under it. Returns the message.

        The half of starting a game that talks to Discord, run once the
        emulator lock has been given back; see :meth:`boot`. The core is
        already up and already this view's by the time this is called, so a
        Discord failure here is reported against a session that exists rather
        than one that is half-built -- which is what the cog's
        ``_abandon_session`` is for.
        """
        self.message = await ctx.send(
            self._content(),
            file=self._clip_file(clip),
            view=self,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        self.message_id = self.message.id
        # The first clip is playing from here, so the first press is paced
        # against it exactly as every later one is; see note_posted.
        self.note_posted(self.posted_playback())
        return self.message

    # There is deliberately no `start()` that does both halves. There was one,
    # kept on the grounds that it was "the obvious way to say 'start this
    # game'", and its docstring said the tests used it; nothing did. The cog
    # calls `boot()` under the emulator lock and `post()` once it has given the
    # lock back, which is the entire reason those are two methods (see
    # `boot`), so a convenience that puts them back together is a convenience
    # whose only possible caller would be reintroducing the bug the split
    # fixed: a rate-limited send freezing gameplay in every other channel.
    #
    # `Retro.py` still refers to it in a comment beside a `_update_repeat_label`
    # call, which is the other thing `start()` did between its two halves.

    # -- What this server will take -----------------------------------------
    #
    # The one question about a clip that is answered on *this* side of the
    # thread line, which is the whole reason it stayed behind when
    # SessionMixin took the encoding: the answer comes out of discord.py's
    # cache, and it is handed to the encode rather than looked up by it.

    def upload_limit(self) -> typing.Optional[int]:
        """
        The biggest attachment this server will take, or None if unknown.

        Read on the event loop and handed to
        :meth:`~retro.session.SessionMixin._encode`, which runs in a worker
        thread: ``Guild.filesize_limit`` is a cached attribute and reaching
        into discord.py's state from another thread is not something to do for
        a size check. None means "do not second-guess it", which is what a
        session with no guild in the cache gets.
        """
        try:
            guild = self.cog.bot.get_guild(self.guild_id) if self.guild_id else None
            limit = getattr(guild, "filesize_limit", None)
            return int(limit) if limit else None
        except Exception:
            log.debug("Could not read the upload limit.", exc_info=True)
            return None

    # -- Interactions -------------------------------------------------------

    async def _press(
        self,
        interaction: discord.Interaction,
        field: typing.Optional[str],
        repeat: int = 1,
    ) -> None:
        """
        Handle one click of a console button, the Wait button or x3.

        Three outcomes, and only the first of them does any emulating:

        * the session is free, so this press runs now and then the presses
          that arrived while it was running are worked through in order;
        * the session is :attr:`busy`, so the press is *queued* -- see the
          note above MAX_QUEUED_PRESSES. It used to be dropped here, which is
          why the controller felt dead in a busy channel;
        * the session has been replaced or retired, so there is nothing to
          press. Acknowledged and ignored.

        Every one of them acknowledges the click, so Discord never shows
        "interaction failed", and none of them edits the message more than
        once; see :meth:`_ack_now`.
        """
        if self.closed:
            # This message belongs to a session that has been replaced. The
            # click is acknowledged either way; whether anything is *said*
            # depends on whether this person has been told already. See
            # _replaced_ack, which is also why this is not a silent defer any
            # more: a controller that answers nothing at all reads as broken,
            # and the game is very probably still playable further down the
            # channel.
            await self._replaced_ack(interaction)
            return
        if self.busy:
            # Somebody else's press is still being emulated (or a runner is
            # between two of them). Take this one down and answer with a
            # plain defer: the acknowledgement is the suffix the running
            # press's own edit puts on the line, which costs no edit here.
            #
            # A press with no room left in the queue gets exactly the same
            # answer and is simply dropped. It is deliberately not explained:
            # the queue is already listed on the message (see queue_note), so
            # a full one is visible to anybody who looks, and a whisper per
            # refused click is a message per click at the very moment
            # somebody is clicking fastest.
            queued = self.enqueue_press(interaction, field, repeat)
            await self._ack_now(interaction)
            if not queued:
                return
            # A queue with nothing working through it strands every entry in
            # it and makes `busy` permanently true; see :attr:`busy`. It is
            # not supposed to happen, and it did -- so rather than trusting
            # that, a press that finds one starts the runner itself. Costs
            # nothing in the ordinary case (`running` is true, so this is
            # skipped) and turns a dead controller into one that repairs
            # itself on the next click.
            if not self.running:
                await self._drain()
            return
        async with self.lock:
            await self._run_press(interaction, field, repeat)
        await self._drain()

    async def _run_press(
        self,
        interaction: discord.Interaction,
        field: typing.Optional[str],
        repeat: int = 1,
    ) -> None:
        """
        Emulate one press and put its clip on the message. One edit, always.

        **Must be called with :attr:`lock` held**, which is the whole of the
        one-core-at-a-time discipline as far as this module is concerned: the
        queue holds intent, and this is the only place any of it is turned
        into emulator work.

        ``interaction`` is whichever click this press came from -- the one
        that arrived while the session was free, or a deferred one taken off
        the queue. A component defer is a DEFERRED_UPDATE_MESSAGE, so a
        queued click's ``edit_original_response`` is still available when its
        turn comes, and a queued press therefore makes exactly the same
        single edit an immediate one does.

        The one edit may be *held back* -- see :meth:`pace` -- until the clip
        it is replacing has had its playing time on screen. That is a delay
        to this one edit and to nothing else: the emulator is finished with
        by then, so no other channel waits, and the press still makes
        exactly one edit whether it waited or not.

        The envelope around the emulator call is shared with :meth:`_run_undo`;
        see :meth:`_emulate`. All this contributes is the cog call, the word
        in its log lines, and the line the clip goes out with.
        """
        # A hibernated session has a core to load and a save state to
        # restore before it can emulate anything, which is the one delay
        # worth explaining. It is said *with* the clip rather than before
        # it, because a press only gets one edit now; see _ack_now. Taken
        # before the work for the obvious reason: the work is the wake.
        resuming = not self.live
        # The press names itself, and whoever made it, on the message -- on the
        # very same edit that carries the clip (see :meth:`press_note`). The
        # resume line beats it when there is one, because "the game was asleep
        # and is back" is news and "Rob pressed A" is a label; a real notice
        # beats both, which _clip_content settles.
        await self._emulate(
            interaction,
            lambda: self.cog.run_press(self, field, repeat),
            RESUMED_NOTE
            if resuming
            else self.press_note(field, repeat, interaction.user),
            "Emulation",
        )

    async def _emulate(
        self,
        interaction: discord.Interaction,
        work: typing.Callable[[], typing.Awaitable[bytes]],
        note: str,
        what: str,
    ) -> None:
        """
        Ask the cog for a clip and put it on the message. One edit, always.

        **Must be called with :attr:`lock` held.** The whole of what a press
        and an undo have in common, which turned out to be everything except
        three values: acknowledge the click, do the work, turn either kind of
        failure into a line on the message instead of a traceback at the
        player, touch the session, wait out the clip on screen, and make the
        one edit. Both callers were twenty lines of that with a different cog
        method in the middle.

        ``note`` is computed by the caller **before** the work, and that is
        safe rather than merely convenient. The only part of a note that could
        go stale is :meth:`press_note`'s tap count, which depends on
        :attr:`fps`, and :attr:`fps` only changes when a hibernated session
        boots a core -- at which point the note shown is RESUMED_NOTE instead,
        because a press that had to wake the game says so. Computing it after
        the work would also mean computing it after :meth:`pace`, i.e. up to a
        clip's length later, for no gain.

        :meth:`pace` is in here rather than in the press alone, and that is
        free for an undo rather than a behaviour change: ``capture_undo`` calls
        ``forget_pacing()``, so :meth:`pace_delay` is already zero by the time
        this reaches it. An undo's clip is never held back, because the clip it
        is replacing is of a press that is about to stop having happened; see
        :meth:`forget_pacing`. The gate is asked either way now, and answers
        "go now" for the undo, which is the same thing said once instead of
        being arranged twice.

        Nothing here re-raises. A press that fails is a line on the message and
        a session that is still playable, which is the whole reason
        :meth:`_recover` exists.
        """
        await self._ack_now(interaction)
        try:
            clip = await work()
        except EmulatorError as error:
            # The core's own complaint, which is written for a player: "the
            # state would not load", "the emulator is not running".
            log.warning("%s failed in channel %s: %s", what, self.channel_id, error)
            await self._recover(interaction, str(error))
            return
        except Exception:
            log.exception(
                "Unexpected %s failure in channel %s", what.lower(), self.channel_id
            )
            await self._recover(interaction, "The emulator hit an unexpected error.")
            return
        self.touch()
        # The clip is made; the edit that shows it may still have to wait.
        # A clip takes a tenth of a second to produce and a second to
        # watch, so without this a queued press replaced a clip that had
        # played about a tenth of the way through and the picture lurched.
        # The emulator lock was given back by the cog before ``work()``
        # returned, so nothing else is held up by this -- see the note above
        # MAX_PACE_SECONDS in retro/timing.py.
        await self.pace()
        await self._show(interaction, clip, note)

    async def _drain(self) -> None:
        """
        Work through the presses that arrived while this one was running.

        Called by the press that was holding the lock, once, as it finishes.
        One runner at a time (:attr:`_draining` says so, and :attr:`busy`
        reads it), so two presses finishing back to back cannot both start
        draining and emulate the same entry twice.

        The lock is **let go between entries** on purpose. Holding it for the
        whole drain would be simpler, but `[p]retroreboot`, `[p]retrosleep`
        and `[p]retroend` all wait on that same lock, and in a channel where
        people are still clicking they would wait for as long as the clicking
        went on. asyncio.Lock hands itself to waiters in order, so releasing
        between entries lets a command that is already waiting win -- and it
        then discards the queue, which is what ends this loop.

        A press that arrives during one of those gaps still queues rather
        than running: :attr:`busy` is true while ``_draining`` is.

        Every entry is popped *under* the lock and checked against the queue
        epoch, so an entry that was taken off the front just as the game was
        reset or undone is dropped rather than emulated into a state nobody
        aimed it at.

        This is also where pacing earns its keep: a drain is the one place
        clips are produced with no human delay between them, so without it
        each one replaced the last after about a tenth of a second of a
        second-long animation. Each entry's *edit* now waits out the clip it
        is replacing (see :meth:`pace`), which is what turns a drain from a
        lurch into a run of clips that each play through. The lock is held
        across that wait, and every teardown path calls
        :meth:`cancel_pacing` before it reaches for the lock, so nothing
        waits on pacing but the picture.
        """
        if self._draining:
            return
        self._draining = True
        try:
            while not self.closed:
                async with self.lock:
                    if self.closed or not self.queue:
                        break
                    entry = self.queue.popleft()
                    if entry.epoch != self._queue_epoch:
                        # Discarded while it was at the front. See
                        # forget_queue(); the count is already recorded.
                        continue
                    await self._run_press(
                        entry.interaction, entry.field, entry.repeat
                    )
        finally:
            self._draining = False

    # There is no `_silent_ack` any more, and its callers are worth following
    # if you are looking for it. It was a staticmethod wrapping one `defer()`
    # in one `try`, which is now the module-level `defer` above -- reachable
    # from `RetiredView` too, which had been hand-rolling its own copy for
    # exactly the reason that this was a method on the other class.
    #
    # The two callers that were acknowledging a *click* with it -- a press that
    # had to be queued, and an undo that arrived mid-press -- now go through
    # `_ack_now`, which is the same defer plus two things neither of them
    # minds: a guard against responding twice, and caching the message the
    # interaction arrived on (which a session rebuilt from Config after a
    # restart has never seen). Its log line called every one of these "a
    # dropped Libretro press", which stopped being true when presses started
    # being queued instead of dropped, and was never true of the undo or of
    # the two fallbacks.

    async def _replaced_ack(self, interaction: discord.Interaction) -> None:
        """
        Answer a click on a controller whose game has been replaced, once.

        These buttons are dead -- the channel has moved on and this session
        has been closed -- but a dead control that answers nothing at all is
        exactly how "the bot stopped working" gets reported, and it is not
        even true: the game is still there, either further down the channel
        or behind the Resume button this message was given. So the first
        click from each person gets the same kind of private pointer
        :meth:`RetiredView._resume` already gives for the same situation.

        Once each, because the alternative is a whisper per tap on a
        controller somebody is poking precisely *because* it looks dead. The
        set is bounded for the same reason everything else here is: a closed
        view can sit in a channel for a long time before it is released.

        Everybody after the first click, and anybody there is no id to
        remember, gets the plain :func:`defer` -- which is also what
        :func:`ephemeral` falls back to if Discord will not take the line, so
        the click is answered on every one of the three ways out of here.
        """
        if (
            len(self._told_replaced) < MAX_REPLACED_NOTICES
            and getattr(getattr(interaction, "user", None), "id", None) is not None
            and interaction.user.id not in self._told_replaced
        ):
            self._told_replaced.add(interaction.user.id)
            await ephemeral(
                interaction,
                f"This message's game has been replaced, so its controls "
                f"no longer do anything. **{escape_label(self.game_name)}** "
                "itself was saved \N{EM DASH} look for the newest game "
                "message in this channel, or press **Resume** on this one "
                "if it has one.",
            )
            return
        await defer(interaction)

    async def _stale_repeat_ack(self, interaction: discord.Interaction) -> None:
        """
        Answer a click on a repeat button that is no longer drawn.

        The click can only have come from a message Discord has not
        re-rendered since the clip length last changed -- the button is on
        that message, the view has stopped putting it in the payload, and it
        stays in the view precisely so that this can be reached. Without it
        the interaction would never be acknowledged at all and the player
        would be shown "This interaction failed" three seconds later; see the
        note above _STYLES.

        Private, and one interaction response with no edit of the message --
        editing it would re-render the attachment and rewind the clip that is
        playing (see :meth:`_ack_now`), which is a real cost to everybody in
        the channel for one person's stale click. That, and the fallback to a
        plain defer because the one thing that must not happen is the click
        going unanswered, are :func:`ephemeral`: every answer this view gives
        to a click it cannot act on is shaped the same way, and is now shaped
        that way by the same four lines.
        """
        await ephemeral(
            interaction,
            REPEAT_STALE_NOTE.format(button=self.system.label_for(self.system.confirm)),
        )

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

        Overlapping clicks are unaffected: :meth:`_press` queues them and
        acknowledges each through here, so a second presser never sees
        "interaction failed" and never sees a message either. That is what
        this is, for a queued press: nothing but the defer, because the
        acknowledgement a queued press gets is the suffix the *running*
        press's edit puts on the line (see :meth:`queue_note`).

        Which is also why this has to tolerate an interaction that is
        *already* deferred: a queued press was acknowledged when it was
        taken down, and Discord (and discord.py) refuse a second response to
        the same interaction. The defer is skipped in that case and the one
        edit still happens, so a queued press and an immediate one behave
        identically -- one response, one edit.

        So this is :func:`defer` plus exactly two things, and it is worth
        knowing that both of them are why the three sites that used to call a
        bare defer for a *click* call this instead: the guard above, and
        catching the message an interaction is carrying for a session that was
        rebuilt from Config after a restart and has never seen its own.
        """
        if self.message is None and interaction.message is not None:
            # After a restart the view is rebuilt from Config and has never
            # seen its message; the interaction carries it.
            self.message = interaction.message
            self.message_id = interaction.message.id
        response = getattr(interaction, "response", None)
        if response is not None and response.is_done():
            return
        await defer(interaction)

    async def _show(
        self,
        interaction: discord.Interaction,
        clip: bytes,
        note: typing.Optional[str] = None,
    ) -> None:
        """
        Swap in the new clip and redraw the controls. The *only* edit a press
        makes; see :meth:`_ack_now` for why there is not a second one.

        ``note`` is a line the *click* wants said: which button was pressed
        (see :meth:`press_note`), that the game was asleep and has come back,
        or that the last press was undone. A pending :attr:`notice` beats it,
        because that is something the wake itself discovered and has to
        report -- that the save state was rejected after a core update, for
        instance -- and only one line can be shown.

        So the order of precedence, most important first, is: a notice, then
        the resumed line, then which button was pressed. It is never empty on
        a press any more, which is what stops a line going stale: the one
        edit a press makes always writes the whole ``content``, so the press
        before last cannot still be on screen.

        Whichever wins, it is assembled by :meth:`_line`, so the header and
        the queue suffix go out with it. That is the whole reason a queued
        press needs no message of its own.

        The edit itself is :meth:`_edit`, which is where the attachment, the
        redraw, NO_PINGS and the ``note_posted`` that follows a successful
        clip all live. All this adds is what to say to the person who clicked
        when Discord refuses it, which is the one thing only a press has --
        a command has its own reply, and a hibernate has nobody waiting.
        """
        if await self._edit(
            interaction,
            lambda: self._clip_content(note),
            clip,
            what="update the Libretro screen",
        ):
            return
        # The game itself is fine, so say so rather than leaving the controls
        # looking broken. The HTTP status, Discord's own error code and the
        # traceback went to the log in `_edit`, which is where somebody who can
        # act on them will look -- this is how an invalid button emoji shows up
        # in production. What the player gets is what they can act on: the game
        # is fine, press again. "HTTP 400" told them nothing and read like a
        # crash.
        #
        # `whisper` rather than a bare followup, because the defer this press
        # made is allowed to have failed: a followup on an interaction that was
        # never acknowledged is a 404, and the one path that most needs to say
        # something would say nothing. See `whisper`.
        await whisper(
            interaction,
            "Discord would not accept the new clip, so the picture above "
            "is the one before it. The game itself is fine and was saved "
            "\N{EM DASH} press a button to carry on.",
        )

    async def _recover(self, interaction: discord.Interaction, reason: str) -> None:
        """
        Say on the message that a press failed, and leave it playable.

        No clip -- there is no clip, that is what failed -- so this keeps
        whatever is on the message and rewrites the line under it. ``reason``
        goes through :meth:`_content`, whose precedence puts the caller's news
        ahead of a pending notice for exactly this case: "the emulator hit an
        unexpected error" must not be replaced by a line about the repeat
        button.

        Nothing is said to the player privately as well. The line is on the
        message they are already looking at, and they are the only person who
        is waiting for it.
        """
        await self._edit(
            interaction,
            lambda: self._content(reason),
            what="report a Libretro failure",
        )

    async def _undo(self, interaction: discord.Interaction) -> None:
        """
        Step the game back to just before the last press.

        One edit of the message, like a press: a plain ``defer`` that changes
        nothing on screen, then the single ``edit_original_response`` that
        swaps the clip in. See :meth:`_ack_now` for why it cannot be two.

        An empty history is the ordinary case rather than an error, because
        the history is memory-only: every message that has outlived a bot
        restart has nothing to undo until somebody presses something. The
        button stays clickable for exactly that case (see :class:`_UndoButton`)
        and the answer is private -- one interaction response, zero edits.

        An undo is deliberately **not** queueable. A click that arrives while
        a press is being emulated is acknowledged and dropped rather than
        taken down, because "one press back" queued three presses deep means
        undoing a press its author never saw. What an undo that *does* run
        does is throw the queue away -- in ``capture_undo``, before it touches
        the machine and while the session's lock still guarantees nothing can
        be added; see :meth:`retro.session.SessionMixin.capture_undo`. (This
        used to point at a ``SessionMixin.run_undo`` wrapper, which was never
        on the path an undo takes and no longer exists.)
        """
        if self.closed:
            await self._replaced_ack(interaction)
            return
        # :attr:`running` rather than :attr:`busy`: a queue with nobody
        # working through it is not a reason to refuse an undo, and an undo
        # that runs is exactly what should throw that queue away.
        if self.running:
            # Not queueable (see above), so this click is not going to happen.
            # It is acknowledged and nothing else: the clip that is already
            # being emulated lands a moment later and is the answer, so a
            # whisper explaining the refusal is one more thing to read for
            # something the next picture settles by itself.
            await self._ack_now(interaction)
            return
        if not self.history:
            # `ephemeral` rather than a bare send_message, which is how this
            # branch came by the defer fallback every other answer-and-do-
            # nothing branch in this file already had: without it, an ephemeral
            # Discord refused left the click unanswered and the person who
            # clicked Undo on a message with nothing to undo -- the case this
            # whole "leave the button enabled" decision exists for -- was shown
            # "This interaction failed" instead of the explanation.
            await ephemeral(
                interaction,
                "There is nothing to undo here yet: Undo steps back "
                f"through the last {UNDO_DEPTH} presses, and that history "
                "is kept in memory only, so a bot restart empties it. "
                "Press any button and Undo works again from there. The "
                "game itself is exactly where you left it.",
            )
            return
        async with self.lock:
            await self._run_undo(interaction)
        # Exactly as a press does, and for a reason that cost a bricked
        # controller to learn: clicks that landed while the lock was held are
        # sitting in the queue with nothing to run them. ``capture_undo`` threw
        # away the ones queued *before* it -- those were aimed at the state it
        # has just put back -- but it cannot throw away what arrives after
        # that, and a queue nobody drains makes :attr:`busy` true for ever.
        # Draining is the right answer rather than dropping, too: these are
        # ordinary next presses in a channel where somebody is still playing.
        await self._drain()

    async def _run_undo(self, interaction: discord.Interaction) -> None:
        """
        Step the game back and put the clip on the message. One edit, always.

        **Must be called with :attr:`lock` held.** The body of :meth:`_undo`,
        split out for the same reason :meth:`_run_press` is: the early
        returns on a failed undo used to be returns out of ``_undo`` itself,
        which is how a press queued during the undo ended up with nothing to
        drain it. Those early returns are now :meth:`_emulate`'s, one step
        further in again, and the caller's ``await self._drain()`` still runs
        whatever happened.

        The edit replaces the undone press's clip with this one, so what the
        channel is left looking at is where the game actually is. An undo line
        rather than a press line: nothing was pressed, and "Rob undid the last
        press." is one of the sentences in ACTION_NOTES that every line this
        session writes is written to match.
        """
        await self._emulate(
            interaction,
            lambda: self.cog.run_undo(self),
            self.undo_note(interaction.user),
            "Undo",
        )

    async def can_stop(self, user: typing.Union[discord.Member, discord.User]) -> bool:
        """Whether this user may sleep, reboot or end the session."""
        return await may_manage(self.cog.bot, user, self.starter_id)
