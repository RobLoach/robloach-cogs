"""
When a button goes down, how long it stays down, and when a clip may be
replaced.

Pulled out of retro/RetroView.py, which had grown to 2,700 lines with a third
of them not about the view at all. Everything here is a plain function of a
frame rate and a clip length -- no view, no emulator instance, no Discord --
which is exactly what made it worth separating: the numbers below are the
measured heart of how a press feels, and they were buried in a file about
laying buttons out in a grid.

Nothing here imports discord.py. RetroView re-exports every name in it, so
`retro.RetroView.press_plan` still resolves; see the note at the top of that
file.
"""

import asyncio
import typing

from .emulator import clip_frame_count, frame_count, input_budget

# How long a button is held down at the start of a clip, in milliseconds, for
# every button including the directions.
#
# Measured on a commercial Game Boy RPG under Gambatte (59.73 fps), with the
# player already facing the way they were pushed and two clear tiles ahead:
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

# The fewest taps worth having a button for. One tap is not a repeat at all --
# it is precisely what the console's own confirm button does, one row over --
# so at that point the button is not drawn.
#
# It used to be drawn and greyed out, which was worse than useless: a control
# that is present, dead and unexplained reads as broken, and it was reported
# as the repeat button having been *removed from the cog*. A control that
# cannot do anything is clearer gone, and going frees the component on the
# wire. It comes back by itself the moment the clip is long enough, because
# the row is drawn again on every redraw (see
# RetroView._update_repeat_label), so changing `[p]retroset cliplength`
# corrects it on the next press.
#
# "Gone" means gone from the *payload* and nothing else: the button object
# stays a child of the view for the session's whole life, so a click on a
# stale copy of it still routes somewhere that answers. See the note above
# _STYLES, which is where that costs something if it is got wrong.
#
# Measured against press_plan at DEFAULT_FPS with the default 160ms hold --
# the clip lengths where it appears at all:
#
#     clip    taps    the button
#     0.2s      1     not drawn
#     0.4s      1     not drawn
#     0.5s      2     "A x2"
#     0.8s      3     "A x3"
#     1s        3     "A x3"   <- the default
#     4s        3     "A x3"
#
# CONTROL_BUTTONS still reserves room for the whole cluster either way, so
# Wait and Undo do not move when it comes and goes.
MIN_REPEAT_TAPS = 2

# Seconds of emulation to run before the first clip, so the console's boot
# logo is out of the way. Converted to frames with the core's real frame rate.
#
# Deliberately not tied to the clip length: how long a Game Boy takes to get
# past its logo has nothing to do with how much of the game a press shows, so
# a one second clip still gets three seconds of boot. It is skipped entirely
# when a save state comes back, since that is already past the title screen.
BOOT_SECONDS = 3

# -- Pacing: a clip is not replaced before it has been watched ----------------
#
# A clip costs far less to make than it does to watch. Measured on the real
# thing -- gambatte running Libbet, one second clips, libretro.py 0.6.0, on a
# Raspberry Pi 5 -- three presses back to back:
#
#     clip   emulated + encoded in   plays for
#       1              92 ms          1005 ms
#       2              68 ms          1005 ms
#       3              42 ms          (one picture, static screen)
#
# So producing a clip is 11-24x faster than playing it. Nothing noticed that
# while a press needed a human to decide on it: by the time somebody had
# looked at the picture and clicked again, the clip had long since played
# through and was holding its last frame (clips are encoded with ``loop=1``;
# see encode_animation). The press *queue* removed the human from the gap --
# queued presses drain one after another with nothing between them but the
# lock -- so each new clip replaced the previous one after about a tenth of
# it had played. What that looks like is the picture lurching: the animation
# never reaches the frame the next clip carries on from, so the game appears
# to jump back a little on every press. The seam itself is exact (see
# capture_plan and the seam block above it in retro/clips.py, which between
# them make the last picture of one clip the emulated frame before the first
# picture of the next one), and the pacing below is
# what makes it exact *on screen* rather than only in the emulation.
#
# The rule: **an edit that replaces a clip waits until that clip has had its
# playing time on screen.** The playing time is known exactly before the edit
# is made -- it is the sum of the frame durations the encoder was handed; see
# clips.playback_seconds, which is the same arithmetic RetroEmulator.record
# writes into the clip's ANMF chunks -- so it is a deadline rather than a
# guess, and the time already spent emulating, encoding and uploading the new
# clip counts against it.
#
# Four things bound the cost of that, and all four matter:
#
# * **nothing playing, no wait.** The deadline is in the past for any press
#   that arrives more than a clip after the last one, which is every ordinary
#   single press. It is the common case and it is untouched.
# * **the emulator is never held waiting.** The wait happens after
#   ``Retro.run_press`` has returned, so the cog's emulator lock -- the one
#   core, shared by every channel -- is free throughout, and another
#   channel can start a game or press a button during it. Only the *edit*
#   waits.
# * **MAX_PACE_SECONDS caps a single wait**, so a long clip cannot turn a
#   queue into a minute of staring.
# * **teardown never waits at all.** Sleeping, ending, rebooting, undoing,
#   eviction and cog unload all call :meth:`RetroView.cancel_pacing` before
#   they take anything, which releases a wait already in progress and stops
#   the next one from starting. No timer is left behind: the wait is an
#   ``await`` inside the press it belongs to, not a scheduled callback.
#
# What the cap is worth, at the three clip lengths that matter. A full queue
# is MAX_QUEUED_PRESSES waiting behind the one running, so the pacing a whole
# drain can add is at most MAX_QUEUED_PRESSES * MAX_PACE_SECONDS:
#
#   cliplength   clip plays for   wait per edit      full queue adds
#     0.2s          0.201s          0.2s (in full)      0.6s
#     1s (default)  1.005s          1.005s (in full)    3.0s
#     5s (max)       5.008s          1.25s (capped)      3.75s
#
# 1.25 seconds is therefore the smallest cap that still paces a default clip
# *in full* (a one second clip plays for 1.005s, so a cap of 1.0 would clip
# the last 5ms off every single one of them) while keeping a full queue
# inside the four seconds the queue was already designed around -- see
# MAX_QUEUED_PRESSES, which sizes itself on "about four seconds of latency,
# which is the most that is still recognisably 'I pressed that'".
#
# Capping each wait rather than budgeting the drain as a whole is deliberate:
# a budget spent on the first edit would give the head of the queue its full
# second and let the tail lurch exactly as it does today, and the lurch is
# the complaint. A cap gives every clip in a drain the same time on screen.
MAX_PACE_SECONDS = 1.25

#: A wait shorter than this is not worth taking. One picture of a clip is
#: 67ms at CLIP_FPS, so 50ms cannot cost a visible frame, and a Discord edit
#: takes longer than that anyway -- the wait would be spent before the
#: request it is delaying had been sent.
MIN_PACE_SECONDS = 0.05


async def pace_wait(release: asyncio.Event, delay: float) -> None:
    """
    Wait ``delay`` seconds, or until ``release`` says there is no point.

    ``release`` is the session's :attr:`RetroView._pace_release`, set by
    :meth:`RetroView.cancel_pacing` when the game is being torn down or moved
    somewhere the clip on screen no longer describes. It is checked by
    waiting on it rather than by polling, so teardown is immediate.

    A module-level function, and the only place pacing actually spends time,
    so that a test can watch the wait -- or act during it, which is how "the
    emulator lock is not held while this is happening" is proved -- instead
    of paying for it. Nothing in the cog rebinds it.

    :meth:`RetroView.pace` calls it by bare name out of *its* module's
    globals, where it arrives by re-export, so a test that swaps
    ``retro.RetroView.pace_wait`` still intercepts every wait -- which is how
    the suite avoids spending a real second per press. Moving this function
    here did not move that binding, and must not.
    """
    try:
        await asyncio.wait_for(release.wait(), delay)
    except (asyncio.TimeoutError, TimeoutError):
        # The ordinary ending: the clip finished playing and nothing
        # interrupted us.
        pass


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
      button, and the view does not draw a button for it at all -- see
      MIN_REPEAT_TAPS and :meth:`RetroView._update_repeat_label`. Measured at
      DEFAULT_FPS with the default 160ms hold, the boundaries are 0.48s for
      the second tap and 0.68s for the third.

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

