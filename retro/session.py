"""
Driving the emulator for one session: the boot, the press, the undo, the
reboot, and the clip each of them leaves behind.

Pulled out of retro/RetroView.py, which had grown to 2,700 lines with about a
third of them not about the view at all. Four modules took most of that third
-- :mod:`retro.timing`, :mod:`retro.text`, :mod:`retro.restore` and
:mod:`retro.permissions`, all plain functions -- and this group was
deliberately left behind at the time, because it is not plain functions: it
reads and writes the session's own state (the emulator, the undo history, the
press queue, the pacing) and so it moves as a *mixin* or not at all. This is
that follow-up. :class:`SessionMixin` is inherited by :class:`RetroView`, its
methods are still methods on a session, and every one of them is still
reached as ``view.capture_press(...)`` exactly as before.

What is left in RetroView.py is the other half of a session: the grid of
buttons, the one edit each click makes, the queue of intent behind it and the
pacing that decides when an edit may go out. Nothing here draws a component
or touches Discord at all.

-- The rule that makes this group dangerous -----------------------------------

**Everything here blocks, and everything here must be called in the cog's
single emulator worker thread -- never on the event loop -- with the cog's
``emulator_lock`` held.** ``_encode`` and ``_trim_opening`` are the one
deliberate exception, and they are exempt from the second and third clauses
only: they still block, and they still must not be called on the event loop.
See below, where that is the whole point of them.

That contract was, until this module existed, visible only as a sentence
repeated across eight docstrings ("Runs in a worker thread", "Worker thread
only"), which is exactly the kind of rule that is true until somebody adds a
ninth method. It is three separate requirements, they fail in three different
ways, and none of the three failures looks like a threading bug from the
outside:

* **blocking.** A press emulates a clip's worth of frames and then compresses
  a save state: tens of milliseconds of C, with the GIL held for most of it.
  On the event loop that is tens of milliseconds in which the bot answers
  nothing at all, per press, in every channel.
* **that one thread, not any thread.** Every call into a libretro core goes
  through ``Retro.run_in_emulator_thread``, which owns a single worker for
  the life of the cog. ``asyncio.to_thread`` would serialize just as well and
  is still wrong: a core is a C shared object that may keep state in
  thread-local storage or assert it is being called from the thread that
  initialised it, and is entitled to segfault the bot over a second thread
  even though no two calls ever overlap. See
  ``Retro.run_in_emulator_thread``, which says the same thing from the other
  end.
* **under the lock.** One core is loaded at a time for the whole bot, so the
  emulator these methods are handed can be *freed* by another channel the
  moment the lock is free: an eviction sets ``view.emulator`` to None and
  unloads the core out from under anything still using it.

Nothing here awaits, and nothing here may grow an await: a coroutine in this
module would be a coroutine somebody eventually calls from the wrong side.
The event-loop half of a press -- acknowledging the click, pacing the edit,
making it -- is in RetroView.py, and the two halves meet in
``Retro.run_press``.

-- ...and the half that deliberately runs with the lock given back ------------

:meth:`SessionMixin._encode` (and :meth:`SessionMixin._trim_opening`, which it
calls) **needs no core**, and the cog runs it with the emulator lock
*released*, on an ordinary ``asyncio.to_thread`` worker rather than on the
core's own thread. That split is the point of ``capture_press`` and
``run_press`` being two methods rather than one: encoding a clip to WebP costs
more CPU than emulating it does, and doing it under the lock made every press
in every other channel wait for this channel's Pillow. See ``Retro.run_press``
for the shape of it -- capture under the lock, take ``view.emulator`` while
the lock still holds it still, give the lock back, encode.

Which is also why ``_encode`` takes the emulator it should use as an argument
instead of reading ``self.emulator``: by the time it runs, another channel is
entitled to have set that to None. Do not undo either half of this.

-- What this assumes the view brings -----------------------------------------

A mixin's requirements are otherwise only visible as a ``self.`` two files
away, so they are written down here. From :class:`RetroView` this needs
``emulator``, ``slug``, ``channel_id``, ``boot_outcome``, the queue's
``forget_queue()``, the pacing's ``forget_pacing()`` and ``_last_picture``
and ``_encoded``, and the clip arithmetic -- ``clip_frames()`` and
``press_plan()`` -- which stayed with the view because the button layout
reads it too. ``history`` and ``_history_bytes`` are this module's own, set
up by :meth:`SessionMixin._init_history`. A test holds that list to the
truth; see tests/test_view.py.

RetroView re-exports every module-level name in here, so
``retro.RetroView.UNDO_DEPTH`` still resolves; see the note at the top of that
file.
"""

import collections
import logging
import typing
import zlib

from .clips import (
    captured_playback,
    picture_hash,
    shrink_clip,
    trim_repeated_opening,
)
from .emulator import EmulatorError, RetroEmulator
from .restore import Progress, restore_into
from .timing import BOOT_SECONDS

log = logging.getLogger("red.robloach.retro")


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
# it never touches the event loop; see SessionMixin.remember_state, and the
# module docstring for why that is the rule for everything in here.
UNDO_COMPRESSION_LEVEL = 1

# How many presses back Undo can reach. Eight is enough to walk out of a
# corridor you should never have gone down, and small enough that the memory
# is not worth thinking about.
UNDO_DEPTH = 8

# ...and the same bound in bytes, because the count alone is not one: the
# numbers above are what today's consoles cost, and a core update or a bigger
# machine can change them without anybody editing this file. At the measured
# sizes two megabytes is 100 times more than UNDO_DEPTH states ever need; it
# only bites if a state compresses to a quarter of a megabyte, and then it
# keeps fewer of them instead of growing. One entry is always kept, even if
# it is over the cap on its own: an Undo button that cannot undo the press
# somebody has just made would be worse than the memory.
#
# This is the *only* bound on anything a session holds: the undo history is
# the only thing a session keeps at all. It holds no footage -- a clip is
# built, uploaded and dropped inside one press.
MAX_UNDO_BYTES = 2 * 1024 * 1024


class EncodedClip(typing.NamedTuple):
    """
    The two facts about a clip that only the encode knows, carried forward.

    :meth:`SessionMixin._encode` runs in a worker thread and hands back
    nothing but bytes (``Retro.run_press`` is the caller, and that is its
    contract),
    so the two numbers the *session* needs from it ride here instead until
    the edit either goes through or does not. Both are about the clip that is
    really going out rather than the window it was captured from, which is
    the distinction :func:`trim_repeated_opening` introduces.

    ``playback`` is what the next edit is paced against; ``last_picture`` is
    :func:`picture_hash` of the still this clip will leave in the channel,
    which is what the next clip's opening is compared against.

    Promoted by :meth:`RetroView.note_posted`, i.e. only once the edit has
    succeeded, and dropped on the floor when it has not: a clip Discord
    refused is not on anybody's screen, and the picture that is still there
    is the one before it.
    """

    playback: float
    last_picture: typing.Optional[bytes]


class SessionMixin:
    """
    The half of a session that drives the emulator, mixed into the view.

    Inherited by :class:`RetroView`, which is the whole session: these
    methods call its ``clip_frames``, its ``forget_queue`` and its
    ``forget_pacing``, and the docstrings below say ``:meth:`` of both halves
    without distinguishing them, because from a caller's side there is only
    ever one object. Deliberately not a :class:`discord.ui.View` subclass and
    deliberately without an ``__init__``: the view's own ``super().__init__``
    has to reach discord.py's, so the one piece of state this owns is set up
    by :meth:`_init_history` instead.

    Every method here is subject to the module docstring's rule above.
    """

    # -- The undo history ---------------------------------------------------

    def _init_history(self) -> None:
        """
        Give a fresh session its (empty) undo history.

        Called from ``RetroView.__init__``, which is where these two used to
        be set inline. A method rather than an ``__init__`` here because the
        view is a discord.py View and its constructor has to reach
        ``discord.ui.View.__init__`` through the MRO; a mixin that joined in
        would be one more thing between a session and the thing that actually
        builds its children.

        The deque holds the machine states the last few presses started from,
        oldest first, each one zlib-compressed. This is what the Undo button
        pops. Memory only, and *cheap* to lose, because the authoritative save
        state is on disk either way. A restart therefore empties it and Undo
        has nothing to put back until the next press (which is an ordinary
        case, not an error; see ``RetroView._undo``).
        """
        self.history: typing.Deque[bytes] = collections.deque()
        self._history_bytes: int = 0

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

    # -- Booting ------------------------------------------------------------

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

    # -- Making a clip ------------------------------------------------------

    def _schedule(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int
    ) -> typing.List[tuple]:
        """
        Work out when, and for how long, to hold a button during a clip.

        Every button, direction or not, is held for the same ``hold_ms``
        (see DEFAULT_HOLD_MS), and :func:`press_plan` is what makes the
        schedule fit inside the clip it will be recorded into. ``field`` of
        None is the Wait button: no input at all.
        """
        if field is None:
            return []
        return [
            (field, start, hold) for start, hold in self.press_plan(repeat, emulator)
        ]

    def _capture(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int = 1
    ):
        """
        Emulate the clip and hand back its frames, *unencoded*.

        The half of making a clip that needs the core. Turning the frames
        into WebP is the expensive half and needs no core at all, so it is
        left to the caller: see :meth:`RetroEmulator.record_frames` and
        :func:`clips.encode_clip`, and ``Retro.run_press``, which captures
        with the emulator lock held and encodes once it has given it back.
        """
        return emulator.record_frames(
            self.clip_frames(emulator),
            presses=self._schedule(emulator, field, repeat),
        )

    def _record(
        self, emulator: RetroEmulator, field: typing.Optional[str], repeat: int = 1
    ) -> bytes:
        """Capture and encode in one call, for a caller with one emulator.

        Goes through :meth:`_encode` rather than straight to
        ``encode_captured`` so that the clip it produces is measured and
        remembered like any other. That matters for the one caller that is
        not a press: :meth:`_boot` makes the first clip of a session, and
        without this the still it leaves in the channel would not be
        remembered, so the *first press* of every game would be the one press
        whose opening could not be trimmed.
        """
        return self._encode(self._capture(emulator, field, repeat), emulator=emulator)

    # -- What the cog calls -------------------------------------------------

    def capture_press(self, field: typing.Optional[str], repeat: int = 1):
        """
        Emulate one press and return its captured frames. Worker thread only.

        **This is what the cog calls.** The returned frames still have to be
        encoded (``emulator.encode_captured``), which the cog does with the
        emulator lock released -- encoding a clip costs more CPU than
        emulating it does, and doing it under the lock made every press in
        every other channel wait for it.
        """
        # The press happens inside the recording, so the clip shows the game
        # reacting to it. A field of None is the "Wait" button: a clip's worth
        # of gameplay with no input at all.
        if self.emulator is None:
            raise EmulatorError("The emulator is not running.")
        # Before anything is emulated: this is the moment Undo puts back.
        # Wait counts as a press here -- it moves the game on, so it is
        # something to step back from.
        self.remember_state(self.emulator)
        return self._capture(self.emulator, field, repeat)

    def capture_undo(self):
        """
        Put the last press back and capture a clip of where it landed.

        Returns the clip's frames rather than the encoded clip; the cog
        encodes them with the emulator lock released. See
        :meth:`capture_press`.

        Runs in a worker thread, called from ``Retro.run_undo``. Two things
        happen, in this order:

        1. the newest undo point is popped and loaded, so the machine is
           back where the undone press found it;
        2. a fresh clip is recorded with no input at all, so the channel can
           see where the game ended up. That clip replaces the undone
           press's on the message, which is all there is to put back: the
           message is the only place a clip exists.

        That second step means an undo costs one clip's worth of emulated
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

        Anything waiting in the press queue is thrown away, and this is the
        case that most needs it: those presses were queued against the state
        the undo has just put *back*, so running them would undo the undo one
        button at a time. See :meth:`forget_queue`, which leaves a count for
        the line this clip goes out on.

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
        # Only ever reached with the session's lock held, so no runner is
        # working through the queue and nothing can be added between the
        # discard and the clip.
        self.forget_queue()
        # An undo's own clip is never paced: the clip on the message is of a
        # press that is about to stop having happened, so holding the
        # correction back to let it finish playing would be showing somebody
        # the thing they just asked to take away. See forget_pacing -- this
        # runs in a worker thread, which is why it is not cancel_pacing.
        self.forget_pacing()
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
        return self._capture(emulator, None)

    def capture_reset(self):
        """
        Reboot the machine and capture a clip of it coming back up.

        Returns the clip's frames rather than the encoded clip; the cog
        encodes them with the emulator lock released. See
        :meth:`capture_press`.

        Runs in a worker thread, called from ``Retro.run_reset``, which is
        what `[p]retroreboot` goes through. There is deliberately no button
        for this: see the note above _STYLES.

        Three things happen, in this order:

        1. the state the reset is about to throw away is pushed onto the undo
           history, so one click of **Undo** puts the player back where they
           were. A reset is the most destructive thing this cog can do to
           progress-in-flight, and absorbing exactly that class of mistake is
           what the history is for; it costs a sub-millisecond save_state()
           and some tens of kilobytes. The rest of the history is left alone:
           every entry in it came from this same core and this same ROM, so
           it is still loadable, and a second click of Undo means what it
           always means -- the machine one press further back;
        2. ``retro_reset``, i.e. the power switch. The cartridge's battery
           memory survives it (see :meth:`RetroEmulator.reset`), so the
           player's own in-game save is not touched;
        3. BOOT_SECONDS of emulation and then a clip, exactly as a cold boot
           does, so the channel sees the game at its title screen rather than
           one second of a blank screen with the logo still coming up.

        Nothing is written to disk here, and that is the point of doing it
        this way: the save state on disk still holds the moment before the
        reset until the game saves again of its own accord (every
        SAVE_STATE_EVERY_PRESSES presses, or when it next sleeps). See
        ``Retro.retroreboot``, which says so in the reply.

        Anything waiting in the press queue is thrown away as well: it was
        aimed at a game that was mid-play, and this is the title screen.

        Raises EmulatorError if the core is not running or will not reset.
        """
        emulator = self.emulator
        if emulator is None:
            raise EmulatorError("The emulator is not running.")
        self.forget_queue()
        # A reboot's clip is not paced either, for the same reason an undo's
        # is not: the clip on the message is of a game that no longer exists.
        # See forget_pacing; this runs in a worker thread.
        self.forget_pacing()
        # Before anything is thrown away: this is the moment Undo puts back.
        self.remember_state(emulator)
        emulator.reset()
        emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
        return self._capture(emulator, None)

    def run_press(self, field: typing.Optional[str], repeat: int = 1) -> bytes:
        """
        :meth:`capture_press` and encode it, in one call. Worker thread only.

        The convenient form, for a caller with no lock to give back. The cog
        deliberately does not use it; see :meth:`capture_press`.
        """
        return self._encode(self.capture_press(field, repeat))

    def run_undo(self) -> bytes:
        """:meth:`capture_undo` and encode it; see :meth:`run_press`."""
        return self._encode(self.capture_undo())

    def run_reset(self) -> bytes:
        """:meth:`capture_reset` and encode it; see :meth:`run_press`."""
        return self._encode(self.capture_reset())

    # -- Encoding, with the emulator lock given back ------------------------

    def _encode(
        self,
        captured,
        limit: typing.Optional[int] = None,
        emulator: typing.Optional[RetroEmulator] = None,
    ) -> bytes:
        """
        Turn captured frames into the clip's bytes. No core is touched.

        Which is the whole reason the capture and the encode are separate
        calls: this is the expensive half of making a clip and it needs
        nothing but Pillow, so the cog runs it with the emulator lock given
        back -- and on an ordinary ``asyncio.to_thread`` worker rather than on
        the core's own thread, since there is no core here to be particular
        about which thread it is called from. It is **the one exception to
        this module's rule**, together with the :meth:`_trim_opening` it calls;
        see the module docstring, which says so at length because this is
        exactly the property a later edit would helpfully tidy away.

        It is a method on the session rather than a function beside
        :func:`clips.encode_clip` because of what it writes rather than what
        it reads: the clip's measured playback and the still it will leave on
        screen are session state (see :meth:`_trim_opening`), and the slug and
        channel id below are what make the log lines about this channel's
        clip.

        ``emulator`` is the one the frames were captured with, and passing it
        is what makes running outside the lock safe. Nothing here *uses* a
        core -- ``encode_captured`` is a staticmethod -- but reading it off
        ``self`` would be reading state another channel is entitled to change
        the moment the lock is free: an eviction sets ``self.emulator`` to
        None, and a press whose frames were already captured would then fail
        to encode a clip that was sitting right there. Falls back to the
        session's own for a caller that has no particular one in mind.

        ``limit`` is what this server will accept as an attachment, when the
        caller knows it. A clip over it is re-encoded smaller rather than
        posted and refused: Discord's rejection costs the whole round trip
        (emulate, encode, upload), spends the press, and answers with advice
        aimed at the bot owner rather than at the person who pressed the
        button. See :func:`clips.shrink_clip` for what is given up, in order.

        **This is also where a clip's opening is trimmed of the picture that
        is already on screen**, because this is the one place the captured
        clip is in hand and the core is not needed: it costs no emulation and
        no core time, and it runs on the worker thread that was going to
        spend tens of milliseconds encoding anyway. See :meth:`_trim_opening`
        for the rules and :func:`clips.trim_repeated_opening` for why there
        are any.
        """
        emulator = emulator if emulator is not None else self.emulator
        if emulator is None:
            raise EmulatorError("The emulator is not running.")
        captured = self._trim_opening(captured)
        clip = emulator.encode_captured(captured)
        if limit and len(clip) > limit:
            log.info(
                "A clip for %s came out at %s bytes, over this server's %s "
                "byte limit; re-encoding it smaller.",
                self.slug,
                len(clip),
                limit,
            )
            # `clip` is handed over as the baseline: lossy is not reliably
            # smaller on console art, so shrink_clip compares against what
            # we already have and never returns anything bigger.
            smaller = shrink_clip(captured, limit, clip)
            if smaller is not None and len(smaller) < len(clip):
                clip = smaller
        return clip

    def _trim_opening(self, captured):
        """
        Cut the opening the viewer is already looking at, and measure what is
        left.

        **The owner's requirement is that none of the previous clip is shown
        in the new one**, and pacing alone does not get there: the shutter
        falls ``step`` frames into the window (four on a Game Boy at
        CLIP_FPS), and a core slower than that to react -- mgba takes eleven
        frames -- opens its clip on a picture byte-identical to the still
        sitting in the channel. Those pictures are dropped here, with their
        durations, so the clip opens on something new. The final picture is
        never dropped and a frozen screen is left whole;
        :func:`clips.trim_repeated_opening` carries the rules and the reasons.

        Whatever comes back is then *measured* rather than assumed: the clip
        no longer necessarily plays for as long as its window emulated, so
        the pacing deadline has to come from the durations that are really
        being encoded. Both numbers are parked in :attr:`_encoded` for
        :meth:`note_posted` to promote once the edit has actually landed.

        Runs in a worker thread (see :meth:`_encode`), and writes exactly one
        attribute of the session, which is the same discipline
        :meth:`forget_pacing` follows. The session's own lock is held by the
        press this belongs to throughout, so nothing else is producing a clip
        for this view at the same time.
        """
        trimmed = trim_repeated_opening(captured, self._last_picture)
        images = getattr(trimmed, "images", None)
        if not images:
            # A stand-in emulator that carries finished bytes rather than
            # pictures; there is nothing to measure and nothing to remember,
            # so the window's own arithmetic stands. See posted_playback.
            self._encoded = None
            return trimmed
        self._encoded = EncodedClip(
            captured_playback(trimmed), picture_hash(images[-1])
        )
        dropped = len(captured.images) - len(images)
        if dropped:
            log.debug(
                "Dropped %s opening picture(s) of a clip in channel %s: the "
                "game had not answered the button yet, so they were the "
                "still already on the message.",
                dropped,
                self.channel_id,
            )
        return trimmed
