import asyncio
import collections
import io
import logging
import re
import time
import typing
import unicodedata
import zlib
from pathlib import Path

import discord
from redbot.core import commands

from .emulator import (
    CLIP_SECONDS,
    DEFAULT_CLIP_FORMAT,
    DEFAULT_FPS,
    EmulatorError,
    RetroEmulator,
    clamp_clip_seconds,
    clip_extension,
    clip_frame_count,
    frame_count,
    input_budget,
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

# The fewest taps worth having a button for. One tap is not a repeat at all --
# it is precisely what the console's own confirm button does, one row over --
# so at that point the button is not drawn.
#
# It used to be drawn and greyed out, which was worse than useless: a control
# that is present, dead and unexplained reads as broken, and it was reported
# as the repeat button having been *removed from the cog*. A control that
# cannot do anything is clearer gone, and going frees a component. It comes
# back by itself the moment the clip is long enough, because the row is drawn
# again on every redraw (see RetroView._update_repeat_label), so changing
# `[p]retroset cliplength` corrects it on the next press.
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

#: Said once, on the next line the session writes, when the repeat button has
#: had to go. A control that vanishes with no explanation reads as removed
#: just as surely as a greyed-out one does -- that is precisely how the
#: greyed-out version was reported -- so the moment it goes is the moment to
#: say why, and where it went. It rides on an edit that was happening anyway.
REPEAT_GONE_NOTE = (
    "The **{button} x3** button is hidden while clips are this short: only "
    "one tap fits, which is what **{button}** already does. A longer "
    "`cliplength` brings it back."
)

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
#
# It is only half of saying "this session is asleep", and it is the half that
# arrives *late*: a wake is the longest wait in the cog (a core to dlopen, a
# save state to load) and this line is written at the end of it. The other
# half is ASLEEP_MARK, which the edit that *puts* the session to sleep leaves
# on the header, so the message reads "asleep" for the whole time it is --
# and the wake's one edit takes the mark off and adds this. Two states, two
# edits that were happening anyway, no third one.
RESUMED_NOTE = "Woke up where you left off."

#: Appended to the header (see :meth:`RetroView.header`) whenever there is no
#: core loaded for this session, which is exactly "asleep": stopped, evicted
#: by another channel, idle-timed-out, or waiting out a bot restart. The next
#: press wakes it, and the edit that press makes drops the mark again.
ASLEEP_MARK = "asleep"

#: How the game and the console are named on the line above the clip. A
#: stable prefix rather than a status card: the first clip of a cold boot used
#: to go out with *no* text at all, and after that the only text was the press
#: line, so somebody scrolling past saw an animation, a grid of buttons and
#: "Rob pressed A." with nothing anywhere saying what game it was.
#:
#: It rides on the content that is already rewritten by every press, so it
#: costs nothing: no card, no embed, no extra edit. One line, always.
HEADER = "**{game}** \N{MIDDLE DOT} {console}"

#: What separates the header from whatever just happened.
HEADER_SEPARATOR = " \N{EM DASH} "

# How much of a game's name goes in the header. Game names come from a ROM
# filename that has already been through Retro._sanitize_filename (which caps
# it at 64 characters), so this is a second, independent bound for a name that
# arrives from a stored session record written by an older version.
MAX_GAME_NAME = 48

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
# * **at most MAX_QUEUED_PRESSES waiting.** Each one costs a second, and a
#   queued press is emulated against a game state its author has not seen
#   yet. Three waiting plus the one running is about four seconds of latency,
#   which is the most that is still recognisably "I pressed that".
# * **one waiting press per person.** Round-robin rather than
#   first-come-first-served, and it falls out of the rule rather than needing
#   a scheduler: a fast clicker cannot fill the queue on their own, so a
#   roomful of people take it in turns without anybody arranging it. A second
#   click from somebody who already has one waiting is refused and the first
#   one stands -- the message has already told them their press is queued (see
#   RetroView.queue_note), and quietly swapping it for something else would
#   make that acknowledgement a lie for a second.
# * **every waiting press is visible.** An input nobody can see is an input
#   that feels lost, which is the whole complaint. The queue is listed as a
#   suffix on the very line the running press is already rewriting, so it
#   costs no extra edit -- see RetroView.queue_note and _ack_now.
# * **the queue is intent, never work.** A pending entry is a button name and
#   a deferred interaction; nothing touches the emulator until the runner
#   takes the lock again for it. The one-core-at-a-time discipline is
#   untouched.
#
# A queued press makes its own single edit when it runs, through its own
# deferred interaction -- a component defer is a DEFERRED_UPDATE_MESSAGE and
# leaves edit_original_response available for the next fifteen minutes -- so
# "one press, one edit" still holds exactly.
MAX_QUEUED_PRESSES = 3

#: What a queued Wait is called in the listing. The other entries name the
#: console's own button (or the d-pad's arrow) through System.caption_for,
#: exactly as the press line does.
QUEUED_WAIT = "wait"

#: The suffix that shows what is waiting. Italic and parenthetical on purpose:
#: the sentence in front of it is what just happened, and this is a footnote
#: to it rather than a second announcement.
QUEUE_NOTE = "*Queued: {queued}*"

#: One entry in that listing, with and without a name to use.
QUEUE_ENTRY = "{who} {button}"
QUEUE_ENTRY_ANONYMOUS = "{button}"

#: Said once, on the next line the session writes, when something threw the
#: queue away: a reset, an undo, a stop. Without it the presses simply
#: vanish, which is the bug this whole mechanism exists to fix -- so the one
#: case where dropping them is *right* has to say so out loud.
DROPPED_NOTE = "*{count} queued press{plural} dropped*"


class Pending(typing.NamedTuple):
    """
    One press somebody has asked for that has not been emulated yet.

    Intent only: a button, a count of taps, and the deferred interaction the
    clip will eventually be edited onto. No emulator work is held here and
    none is done to build one.

    ``who`` is the author's display name, sanitised at the moment they
    clicked (see :func:`presser_name`), rather than the user object: the
    listing has to keep reading correctly for somebody who has left the guild
    between clicking and being run, and re-deriving a name from a member
    object that has since gone is exactly how that produces " pressed A.".

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

# -- Saying who did what ------------------------------------------------------
#
# Every action names itself, and its author, on the message it edits: one
# short line above the clip, in one voice for all five of them --
#
#     Rob pressed A.
#     Rob pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}.
#     Rob pressed A x3.
#     Rob waited.
#     Rob undid the last press.
#     Rob reset the game.
#
# -- and the impersonal form of the same sentence ("Pressed A.", "Waited.")
# whenever there is no name to use, which is the one thing that must never
# fail: a click always gets a line, even from a user object that turns out to
# have no usable name at all.
#
# It rides on the one edit the action already makes (see RetroView._show), so
# it costs nothing: no extra request, no extra rate-limit budget, and nothing
# that could re-render the message a second time and rewind the clip.
#
# The name of a *button* comes from the console's own Button in systems.py,
# via System.caption_for -- never from a second table here. That is what makes
# a Genesis press read "pressed C." rather than "pressed A." (its C is
# RetroPad *a*) and what keeps the line in step with the label on the button
# that was clicked. The d-pad has no labels at all, so it names itself with
# its arrow emoji, which has already been through systems.validate_emoji() at
# import time; a bare codepoint with no U+FE0F is what caused a 400 in
# production once, and it cannot get in here without failing that check first.
#
#: action -> (the line when the author is known, the line when it is not).
#: Table-driven on purpose: five actions in one voice is a property of this
#: dict rather than of five string literals scattered through the file, and
#: the tests read it back.
ACTION_NOTES: typing.Dict[str, typing.Tuple[str, str]] = {
    "press": ("{who} pressed {button}.", "Pressed {button}."),
    "wait": ("{who} waited.", "Waited."),
    "undo": ("{who} undid the last press.", "Undid the last press."),
    "reset": ("{who} reset the game.", "Reset the game."),
}

#: The impersonal forms, kept as names because the cog and the tests refer to
#: them and because they are what a line falls back to.
PRESSED_NOTE = ACTION_NOTES["press"][1]
WAITED_NOTE = ACTION_NOTES["wait"][1]
UNDONE_NOTE = ACTION_NOTES["undo"][1]
RESET_NOTE = ACTION_NOTES["reset"][1]

# How much of a display name goes on the line. 32 is Discord's own ceiling for
# both a nickname and a global display name, so no real name is ever cut; the
# cap is here for a name that arrives from somewhere else (a cached member
# object, a future API, a test) and to make "one presser cannot own the line"
# a property of this module rather than a hope about Discord's limits. The
# worst case is therefore a 32 character name plus " undid the last press.",
# which is 54 characters -- still one short line above the clip.
MAX_PRESSER_NAME = 32

# Unicode general categories that take up no space on screen: control
# characters (Cc, which includes the newlines that would turn one line into
# three), format characters (Cf, which is the zero-width space, the
# zero-width joiner, the soft hyphen, the byte-order mark and the
# right-to-left override), lone surrogates, private use, unassigned, and the
# line/paragraph separators.
#
# Whitespace is handled before this, so a name written with newlines or tabs
# in it still reads as separate words rather than having them run together.
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Every character Discord reads as markup, escaped unconditionally with a
# backslash -- which Discord renders as the plain character, so "Jean\-Luc"
# reads "Jean-Luc" and the only cost is a backslash nobody sees.
#
# Deliberately a table here rather than discord.utils.escape_markdown, for
# two reasons that both matter for a *name*:
#
# * that helper defaults to ignore_links=True and leaves markdown inside
#   anything URL-shaped alone, so a display name of
#   "http://example.com/__x__" would come back unescaped. A display name is
#   not prose with links in it;
# * it also leaves the start-of-line syntax alone (`#` headers, `-`/`+`
#   lists, `>` quotes), which is exactly where a presser's name sits. A
#   display name of "# hello" would render the whole line as a header --
#   precisely the "one user dominates the line" problem the length cap is
#   for.
#
# (On Python 3.13 it additionally emits a DeprecationWarning from inside
# discord.py, once per call, which a cog should not be minting on every
# button press.)
#
# `<` is in here for the family of things angle brackets open: `<@123>` (a
# mention), `<#123>` (a channel), `<:name:123>` (a custom emoji) and `<t:0>`
# (a timestamp). Brackets and parentheses are for `[text](url)` masked links.
# The backslash itself is first, and a single str.translate pass maps every
# character from the *input*, so escaping it cannot cascade into the
# backslashes this adds.
#
# @everyone, @here and <@id> are still handed to discord.py's own
# escape_mentions afterwards: it is the maintained answer for those, it is
# warning-free, and a second opinion on the one class of markup that can
# actually notify somebody is worth having.
MARKDOWN_ESCAPES = str.maketrans(
    {character: f"\\{character}" for character in "\\*_~`|#-+<>[]()"}
)

#: A leading "1." starts an ordered list, so the stop after a leading run of
#: digits is escaped too. The digits themselves are fine, and "1)" is already
#: covered by the bracket in MARKDOWN_ESCAPES.
LEADING_ORDINAL = re.compile(r"^(\d+)(\.)")

# Nothing this cog writes may ping anybody, and the press line is why: a
# notification on every button press, from everybody in the channel, would be
# intolerable in a way that no amount of "well, it is only one line" fixes.
#
# Two independent guards, because one of them is a promise about a string and
# the other is a promise to Discord:
#
# * presser_name() emits *no mention syntax at all*. The name is plain text
#   with every mention-shaped thing escaped, so there is nothing for Discord
#   to resolve into a ping in the first place;
# * every edit that can carry a name also carries this, so even a line that
#   somehow contained a live mention could not deliver one.
#
# Belt and braces on purpose: the first guard is the one that matters and the
# second is the one that cannot be got wrong by a future edit to the wording.
NO_PINGS = discord.AllowedMentions.none()


def presser_name(user: typing.Any) -> str:
    """
    A person's display name, safe to drop into the middle of a sentence.

    Returns ``""`` for anybody who cannot be named, which is the caller's
    signal to use the impersonal form of the line. Never raises: a click must
    never fail because of whatever somebody called themselves.

    ``display_name`` is asked for first and works for every kind of author
    this cog sees -- a :class:`discord.Member` (their per-guild nickname), a
    plain :class:`discord.User` with no guild at all (their global display
    name, or their username), and a member object left over from somebody who
    has since left the guild (the nickname Discord last told us about). The
    two fallbacks after it are for an object that has only one of the others.

    What comes back is then made safe, in this order:

    1. **whitespace is collapsed**, so a name with a newline in it cannot
       turn one line above the clip into three;
    2. **invisible characters are dropped** (see INVISIBLE_CATEGORIES): a
       name made entirely of zero-width spaces is indistinguishable from
       having no name, and is treated as such rather than producing
       " pressed A.";
    3. **the length is capped** at MAX_PRESSER_NAME, before escaping, so a
       trailing backslash can never be cut off from the character it escapes;
    4. **markdown and mentions are escaped** -- MARKDOWN_ESCAPES for every
       character Discord reads as markup, LEADING_ORDINAL for the one piece
       of it that is positional, and discord.py's ``escape_mentions`` for
       ``@everyone``/``@here``/``<@id>``. The result contains no mention
       syntax at all, which is the first of the two guarantees described
       above NO_PINGS.
    """
    raw = ""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(user, attribute, None)
        if isinstance(value, str) and value.strip():
            raw = value
            break
    if not raw:
        return ""

    characters = []
    for character in raw:
        if character.isspace():
            characters.append(" ")
            continue
        try:
            category = unicodedata.category(character)
        except (TypeError, ValueError):  # not reachable from a str, but free
            continue
        if category in INVISIBLE_CATEGORIES:
            continue
        characters.append(character)
    name = " ".join("".join(characters).split())
    if not name:
        return ""
    if len(name) > MAX_PRESSER_NAME:
        name = name[: MAX_PRESSER_NAME - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"

    return escape_label(name)


def escape_label(name: str) -> str:
    """
    Make a name safe to drop into a sentence Discord will render.

    MARKDOWN_ESCAPES for every character Discord reads as markup,
    LEADING_ORDINAL for the one piece of it that is positional, and
    discord.py's ``escape_mentions`` for ``@everyone``/``@here``/``<@id>``.
    The result contains no mention syntax at all.

    Shared by :func:`presser_name` and :meth:`RetroView.header`, because the
    two things that go on that one line -- who clicked and what game it is --
    have exactly the same problem: a game called ``__x__`` or a nickname of
    ``# hello`` would otherwise reformat the line it sits on.
    """
    escaped = LEADING_ORDINAL.sub(r"\1\\\2", str(name).translate(MARKDOWN_ESCAPES))
    return discord.utils.escape_mentions(escaped)


def action_note(action: str, user: typing.Any = None, button: str = "") -> str:
    """
    The one line an action puts on the message, with its author's name in it.

    ``action`` is a key of ACTION_NOTES. The impersonal form is used whenever
    :func:`presser_name` cannot name the author, so this always returns a
    sentence.
    """
    named, plain = ACTION_NOTES[action]
    who = presser_name(user)
    return (named if who else plain).format(who=who, button=button)

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
# machine can change them without anybody editing this file. At the measured
# sizes two megabytes is 100 times more than UNDO_DEPTH states ever need; it
# only bites if a state compresses to a quarter of a megabyte, and then it
# keeps fewer of them instead of growing. One entry is always kept, even if
# it is over the cap on its own: an Undo button that cannot undo the press
# somebody has just made would be worse than the memory.
#
# This is now the *only* bound on anything a session holds: the undo history
# is the only thing left that a session keeps at all. It used to be a quarter
# of the 8 MiB replay buffer beside it, and then sat next to the single clip
# that was on the message (`last_clip`, which nothing read and which is gone).
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
# view adds the three control buttons -- Wait, confirm x3, Undo -- to the
# last row if they fit and to a row of their own if they do not.
#
# Two buttons that used to sit here are gone, and neither is coming back:
# Stop (`[p]retrosleep` puts a game to sleep and `[p]retroend` finishes with
# it) and Replay. There is no Reset button either, deliberately: rebooting
# somebody's game is destructive to their progress-in-flight, so it is
# `[p]retroreboot`, a command with the same permission check `[p]retrosleep`
# has, rather than one more thing a passer-by can click by mistake.
#
# A message posted before a button was removed still has it drawn on it until
# its next press redraws the row, and a click on that stale button resolves to
# a custom_id this view no longer has -- which discord.py's
# ViewStore.dispatch_view drops silently rather than raising.
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
    Discord keeps routing clicks to it, and the line the press puts on the
    message counts the same taps the label does.

    Below MIN_REPEAT_TAPS this button is not built at all, so it is never
    drawn saying "A x1". A click on a stale one still sitting on a message
    Discord has not re-rendered resolves to a custom_id the view no longer
    has, which discord.py's ViewStore.dispatch_view drops silently.
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
    freed (idle timeout, `[p]retrosleep`, cog unload, bot restart) the session
    hibernates, the controls stay enabled, and the next press transparently
    boots the core again from the cached ROM plus the last save state.

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

        # No attribute here holds a clip, and that is deliberate. A session
        # used to keep its last fifteen seconds of footage -- up to
        # MAX_REPLAY_BYTES, i.e. 8 MiB -- so the Replay button could stitch it
        # back together, and after that button went it still kept the single
        # clip that was on the message (`last_clip`, with a `remember_clip()`
        # to set it). Nothing read either: `_show` is handed the clip it is
        # about to post, as an argument. So a clip is now built, uploaded and
        # dropped inside one press, and the picture the channel is looking at
        # is read back off the message -- which is where it actually lives,
        # and what a player sees.

        # The machine states the last few presses started from, oldest first,
        # each one zlib-compressed. This is what the Undo button pops. Memory
        # only, and *cheap* to lose, because the authoritative save state is
        # on disk either way. A restart therefore empties it and Undo greys
        # itself out until the next press.
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

        # Presses that arrived while the lock was held, oldest first, at most
        # MAX_QUEUED_PRESSES of them and at most one per person. See the
        # "Queued presses" note above MAX_QUEUED_PRESSES; nothing in here is
        # emulator work, and nothing in here survives a reset, an undo, a
        # sleep or a retirement.
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
        two-button Start/Select; it was four while Replay existed, which was
        one too many for all but the Master System and the Neo Geo Pocket.

        The room for the cluster is reserved with CONTROL_BUTTONS, i.e. for
        all three of them, whether or not the confirm x3 button is drawn (see
        MIN_REPEAT_TAPS): Wait and Undo must not move between one clip length
        and another, and a layout that only fits on a short clip would be a
        layout that breaks the day somebody lengthens the clip.
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
        taps = self.repeat_taps
        if confirm is not None and taps >= MIN_REPEAT_TAPS:
            # Built with the taps this clip length can fit, so a session
            # started on a short clip never shows a promise it cannot keep --
            # and not built at all on a clip too short for a repeat to mean
            # anything, so it is never drawn dead. See MIN_REPEAT_TAPS.
            self.add_item(_RepeatButton(confirm, control_row, taps))
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

        Anything still waiting in the queue goes with it. Those presses were
        aimed at a game this channel has moved on from, and replaying them
        into whatever is playing now would be worse than dropping them.
        """
        self.closed = True
        self.forget_queue()
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
        short for even two, which is when the button is not drawn at all.
        """
        return len(self.press_plan(REPEAT_TAPS))

    @property
    def has_repeat_button(self) -> bool:
        """Whether this clip length is long enough to draw the x3 button."""
        return self.repeat_taps >= MIN_REPEAT_TAPS

    # -- The clip on the message --------------------------------------------

    def press_note(
        self,
        field: typing.Optional[str],
        repeat: int = 1,
        user: typing.Any = None,
    ) -> str:
        """
        The one line that says who pressed which button.

        See ACTION_NOTES: the console's own name for the button, or the
        d-pad's arrow, taken from systems.py so nothing can drift out of step
        with what is drawn on the button that was clicked. ``field`` of None
        is the Wait button, which pressed nothing.

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
        button = self.system.caption_for(field)
        taps = len(self.press_plan(repeat)) if repeat > 1 else 1
        if taps > 1:
            button = f"{button} x{taps}"
        return action_note("press", user, button)

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
        """The repeat button, or None on a clip too short to draw one."""
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
        does not, and it is **removed from the row** rather than greyed out.
        That is a change from how it used to behave, and the reason is that
        the greyed-out version was reported as the feature having been taken
        out of the cog: a dead control with no explanation looks broken. A
        missing control says "not at this clip length", which is the truth.
        It also frees a component.

        A control that silently *disappears* reads as removed too, though, so
        going sets :attr:`notice` -- one line on the next edit the session
        makes, which is an edit that was happening anyway. Coming back says
        nothing: the button is right there saying what it does.

        Called on every redraw (see :meth:`_set_disabled`), so changing
        `[p]retroset cliplength` mid-game adds or removes the button on the
        next press, both ways round -- and so does booting a core, since a
        session laid out while hibernated used DEFAULT_FPS and a 50 fps PAL
        core can fit a tap the fallback said would not fit (and vice versa).

        Coming or going means rebuilding the whole row, because a Discord
        action row is ordered by insertion: adding the button back would
        otherwise land it after Undo and read "Wait Undo A x3". The rebuild
        is a couple of dozen Button objects and only happens when the answer
        actually changes, which is on a settings change and once per boot.
        """
        taps = self.repeat_taps
        button = self._repeat_button()
        wanted = taps >= MIN_REPEAT_TAPS
        if wanted != (button is not None):
            if not wanted:
                self.notice = REPEAT_GONE_NOTE.format(
                    button=self.system.label_for(self.system.confirm)
                )
            self._rebuild_controls()
            return
        if button is not None:
            button.label = f"{button.name} x{taps}"

    def _rebuild_controls(self) -> None:
        """
        Draw the whole controller again, in row order.

        Only :meth:`_update_repeat_label` needs this, and only when the
        repeat button has to appear or disappear. The custom_ids are fixed
        strings, so the freshly built buttons route exactly as the old ones
        did and the view stays persistent.

        Every caller edits the message with ``view=self`` immediately
        afterwards, and both ``edit_original_response`` and ``Message.edit``
        re-register the view's children as they go, so Discord is routing
        clicks to the new buttons from that edit on. A click that lands in
        the moment between the rebuild and the edit resolves to one of the
        old, now detached items, which discord.py's ViewStore drops with a
        log line rather than raising (it checks ``item.view is None``) -- the
        same treatment a click on any button this cog has removed gets.
        """
        self.clear_items()
        self._build_controls()

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
        running now would jump the line. A non-empty queue with no runner
        cannot normally happen (entries are only ever made while one is
        working), so the second half is a guard against the ordering bug
        rather than a state anybody reaches.
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
        * MAX_QUEUED_PRESSES are already waiting;
        * **this person already has one waiting.** One slot each is what
          makes a group take turns without a scheduler, and the first click
          is the one that stands: the message has already said it is queued.

        Never raises, and never touches the emulator: see the note above
        MAX_QUEUED_PRESSES.
        """
        if self.closed or len(self.queue) >= MAX_QUEUED_PRESSES:
            return False
        user = user if user is not None else getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        if user_id is not None and any(
            entry.user_id == user_id for entry in self.queue
        ):
            return False
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

    def forget_queue(self) -> int:
        """
        Throw every waiting press away, and remember how many that was.

        Called whenever the game stops being the thing those presses were
        aimed at: it is stopped, rebooted, undone, put to sleep, retired, or
        replaced by another game. Replaying a queued direction into a
        different game state is worse than dropping it -- Undo in particular
        would be undone again by the very presses it was correcting.

        Returns the number dropped, and leaves it in :attr:`queue_dropped` so
        the next line the session writes can say so once (see DROPPED_NOTE).
        Bumping the epoch is what also discards an entry a runner has already
        taken off the front but not yet emulated.
        """
        dropped = len(self.queue)
        self.queue.clear()
        self._queue_epoch += 1
        if dropped:
            self.queue_dropped += dropped
        return dropped

    def queued_label(self, entry: Pending) -> str:
        """One waiting press, named the way the press line names a button."""
        if entry.field is None:
            button = QUEUED_WAIT
        else:
            button = self.system.caption_for(entry.field)
            taps = len(self.press_plan(entry.repeat)) if entry.repeat > 1 else 1
            if taps > 1:
                button = f"{button} x{taps}"
        template = QUEUE_ENTRY if entry.who else QUEUE_ENTRY_ANONYMOUS
        return template.format(who=entry.who, button=button)

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
        return QUEUE_NOTE.format(
            queued=", ".join(self.queued_label(entry) for entry in self.queue)
        )

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

    def run_undo(self) -> bytes:
        """
        Put the last press back and record a clip of where it landed.

        Runs in a worker thread, called from ``Retro.run_undo``. Two things
        happen, in this order:

        1. the newest undo point is popped and loaded, so the machine is
           back where the undone press found it;
        2. a fresh clip is recorded with no input at all, so the channel can
           see where the game ended up. That clip replaces the undone
           press's on the message, which is all there is to put back now:
           there used to be a buffer of recent clips here that had to be
           rewound in step with the game so a stitched replay could not show
           somebody walking into a room they were not in, and both the
           buffer and the replay are gone.

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
        return self._record(emulator, None)

    def run_reset(self) -> bytes:
        """
        Reboot the machine and record a clip of it coming back up.

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
        # Before anything is thrown away: this is the moment Undo puts back.
        self.remember_state(emulator)
        emulator.reset()
        emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
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
        redraw, because that is also where the repeat button is made to say
        what it will really do.
        """
        for child in self.children:
            if isinstance(child, _SpacerButton):
                continue
            if hasattr(child, "disabled"):
                child.disabled = disabled
        if not disabled:
            # The repeat button is the one control that can have nothing to
            # do, and the answer is not to draw it at all rather than to grey
            # it out; see MIN_REPEAT_TAPS and _update_repeat_label. Undo used
            # to be greyed out beside it whenever the history was empty and
            # is not any more -- see _UndoButton, which explains why.
            self._update_repeat_label()

    # -- Messages -----------------------------------------------------------

    @property
    def header(self) -> str:
        """
        What game this is, and on what, in the few characters it deserves.

        ``**µCity** · Game Boy``, plus ``· asleep`` while there is no core
        loaded. It is the *stable* part of the one line the message carries,
        and it exists because the line used to be nothing but "Rob pressed
        A." -- and, on the first clip of a cold boot, nothing at all. Anybody
        scrolling into the channel saw an animation, a grid of unlabelled
        arrows and a name, with nothing anywhere saying what was being
        played.

        Deliberately not a status card: the card this cog used to have was
        removed, and this is one line rather than a second attempt at it. See
        HEADER and ASLEEP_MARK.

        The game's name is escaped the same way a presser's is: it comes from
        a ROM filename, so it can perfectly well contain the underscores and
        hyphens Discord reads as markup. See :func:`escape_label`.
        """
        name = str(self.game_name or "Game")[:MAX_GAME_NAME]
        line = HEADER.format(game=escape_label(name), console=self.system.name)
        if not self.live:
            line = f"{line} \N{MIDDLE DOT} {ASLEEP_MARK}"
        return line

    def _line(self, text: typing.Optional[str] = None) -> str:
        """
        The whole of the one line the message carries, assembled.

        Three pieces, in this order, and every one of them rides on an edit
        that was already being made:

        1. the :attr:`header` -- what game, what console, and whether it is
           asleep. Always there;
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
        The text to put on the message with the clip: one short line.

        The clip and the buttons are most of the interface, so the text is
        one line and no more: the :attr:`header`, then ``message`` from the
        caller (which button was pressed, the game went to sleep, the
        emulator failed), or a pending one-off notice, which beats it and is
        cleared as it is shown so it appears exactly once.
        """
        if message is not None:
            return self._line(message)
        notice, self.notice = self.notice, None
        return self._line(notice)

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
            await message.edit(
                content=self._content(note), view=self, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            # The message may have been deleted, or the bot may have lost
            # access to the channel; the session state is still correct.
            log.warning("Failed to refresh the Libretro message.", exc_info=True)

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
        self._set_disabled(False)
        try:
            self.message = await message.edit(
                content=self._content(note),
                attachments=[self._clip_file(clip)],
                view=self,
                # The line this carries names whoever ran the command; see
                # NO_PINGS.
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            log.warning(
                "Failed to put a new clip on the Libretro message in channel %s.",
                self.channel_id,
                exc_info=True,
            )
            return False
        return True

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
            # This message belongs to a session that has been replaced.
            # Acknowledge the click so Discord never shows "interaction
            # failed", but say nothing: nagging everyone who taps a button is
            # just spam.
            await self._silent_ack(interaction)
            return
        if self.busy:
            # Somebody else's press is still being emulated (or a runner is
            # between two of them). Take this one down and answer with a
            # plain defer: the acknowledgement is the suffix the running
            # press's own edit puts on the line, which costs no edit here.
            self.enqueue_press(interaction, field, repeat)
            await self._silent_ack(interaction)
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
        """
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
        self.touch()
        # The press names itself, and whoever made it, on the message --
        # on the very same edit that carries the clip (see
        # :meth:`press_note`). The resume line beats it when there is
        # one, because "the game was asleep and is back" is news and
        # "Rob pressed A" is a label; a real notice beats both, which
        # _show settles.
        await self._show(
            interaction,
            clip,
            RESUMED_NOTE
            if resuming
            else self.press_note(field, repeat, interaction.user),
        )

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

        Overlapping clicks are unaffected: :meth:`_press` queues them and
        acknowledges each with :meth:`_silent_ack`, which is this same defer,
        so a second presser never sees "interaction failed" and never sees a
        message either.

        Which is also why this has to tolerate an interaction that is
        *already* deferred: a queued press was acknowledged when it was
        taken down, and Discord (and discord.py) refuse a second response to
        the same interaction. The defer is skipped in that case and the one
        edit still happens, so a queued press and an immediate one behave
        identically -- one response, one edit.
        """
        if self.message is None and interaction.message is not None:
            # After a restart the view is rebuilt from Config and has never
            # seen its message; the interaction carries it.
            self.message = interaction.message
            self.message_id = interaction.message.id
        response = getattr(interaction, "response", None)
        try:
            if response is not None and response.is_done():
                return
        except Exception:  # pragma: no cover - is_done() cannot fail
            log.debug("Could not read an interaction's response state.", exc_info=True)
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
        """
        self._set_disabled(False)
        notice, self.notice = self.notice, None
        content = self._line(notice or note)
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
                # The line above the clip names whoever clicked, and must
                # never notify them or anybody else; see NO_PINGS.
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException as error:
            # The game itself is fine, so say so rather than leaving the
            # controls looking broken. The traceback goes to the log: this is
            # how an invalid button emoji shows up in production.
            # The HTTP status and Discord's own error code go to the log,
            # which is where somebody who can act on them will look. What the
            # player gets is what they can act on: the game is fine, press
            # again. "HTTP 400" told them nothing and read like a crash.
            log.exception(
                "Failed to update the Libretro screen in channel %s "
                "(HTTP %s, code %s).",
                self.channel_id,
                getattr(error, "status", "?"),
                getattr(error, "code", "?"),
            )
            await self._whisper(
                interaction,
                "Discord would not accept the new clip, so the picture above "
                "is the one before it. The game itself is fine and was saved "
                "\N{EM DASH} press a button to carry on.",
            )

    async def _recover(self, interaction: discord.Interaction, reason: str) -> None:
        """Put the controls back after a failed press and explain why."""
        self._set_disabled(False)
        try:
            await interaction.edit_original_response(
                content=self._content(reason), view=self, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            log.warning("Failed to report a Libretro failure.", exc_info=True)

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
        does is throw the queue away; see :meth:`run_undo`.
        """
        # :attr:`running` rather than :attr:`busy`: a queue with nobody
        # working through it is not a reason to refuse an undo, and an undo
        # that runs is exactly what should throw that queue away.
        if self.closed or self.running:
            await self._silent_ack(interaction)
            return
        if not self.history:
            try:
                await interaction.response.send_message(
                    "There is nothing to undo here yet: Undo steps back "
                    f"through the last {UNDO_DEPTH} presses, and that history "
                    "is kept in memory only, so a bot restart empties it. "
                    "Press any button and Undo works again from there. The "
                    "game itself is exactly where you left it.",
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
            self.touch()
            # The edit below replaces the undone press's clip with this one,
            # so what the channel is left looking at is where the game
            # actually is.
            # An undo line rather than a press line: nothing was pressed, and
            # "Rob undid the last press." is one of the five sentences in
            # ACTION_NOTES that every line here is written to match.
            await self._show(interaction, clip, self.undo_note(interaction.user))

    async def can_stop(self, user: typing.Union[discord.Member, discord.User]) -> bool:
        """
        Whether this user may stop or reset the session.

        Anyone in the channel can play; ending someone else's game
        (`[p]retrosleep`), rebooting it (`[p]retroreboot`) and finishing with
        it (`[p]retroend`) are the three things that are not open to
        everybody, because all of them cost the whole channel its
        progress-in-flight. One check for all three, rather than three that
        could drift.

        ``guild_permissions`` is duck-typed rather than gated on
        ``isinstance(user, discord.Member)``, exactly as
        ``SavesMixin._may_manage_saves`` does it: a Member has the attribute
        and a plain User does not, which *is* the question being asked, and
        an isinstance check on a library class only makes the branch
        impossible to exercise in a test.
        """
        if user.id == self.starter_id:
            return True
        if await self.cog.bot.is_owner(user):
            return True
        permissions = getattr(user, "guild_permissions", None)
        return bool(permissions is not None and getattr(permissions, "manage_messages", False))
