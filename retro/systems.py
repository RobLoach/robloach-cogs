"""
The consoles this cog can emulate, the libretro core each one needs, and the
controller layout each one shows in Discord.

This module deliberately imports nothing but the standard library: the cog,
the view and the emulator all read from it, and it can be exercised on its
own without discord.py or Red installed.

Every core listed here is BIOS-free (it boots a game with nothing but the ROM)
and is published for linux/x86_64, linux/aarch64, macOS and Windows on the
libretro buildbot.

Two further conditions a new console has to meet, both learned the hard way,
and both the reason a console somebody expects to find here is not:

* **It must not ask for a screen rotation.** libretro.py rotates a frame by
  90 degrees from the wrong offset and corrupts it (see FAST_ROTATIONS in
  retro/clips.py); there is no path, fast or slow, that survives it. The
  WonderSwan (``mednafen_wswan``) rotates for half its library and is
  therefore absent. An emulator test asserts no core here reports a rotation.
* **Its controls must fit one d-pad and the grid below.** The Virtual Boy
  (``mednafen_vb``) is out because its defining control scheme is *two*
  d-pads, which this layout cannot express.
"""

import sys
import typing
import unicodedata

__all__ = [
    "Button",
    "System",
    "SYSTEMS",
    "CORES",
    "core_suffix",
    "core_filename",
    "system_for_core",
    "system_for_extension",
    "AMBIGUOUS_EXTENSIONS",
    "AMBIGUOUS_REASONS",
    "ambiguous_reason",
    "system_by_key",
    "core_name_from_filename",
    "DPAD",
    "SPACER",
    "SPACER_LABEL",
    "is_spacer",
    "WAIT_EMOJI",
    "RESUME_EMOJI",
    "UNDO_EMOJI",
    "EMOJI_CODEPOINTS",
    "all_button_emoji",
    "emoji_problem",
    "validate_emoji",
    "validate_layouts",
    "MAX_ACTION_ROWS",
    "MAX_BUTTONS_PER_ROW",
    "MAX_COMPONENTS",
    "MAX_LAYOUT_ROWS",
    "CONTROL_BUTTONS",
]


class Button(typing.NamedTuple):
    """One controller button as the player sees it.

    ``label`` is the console's own name for the button (Genesis calls its
    face buttons A/B/C, the PC Engine calls them I/II, the Master System
    numbers them 1 and 2); ``field`` is the RetroPad field the libretro core
    actually reads, which is frequently something else entirely.

    ``label`` may be empty, in which case the button shows nothing but its
    ``emoji``: the d-pad is four arrows and nothing else. Discord accepts a
    component with an emoji and no label, and discord.py's
    ``Button.to_dict()`` simply omits a falsy label from the payload.

    A ``field`` of ``""`` marks the layout spacer; see :data:`SPACER`.
    """

    label: str
    field: str
    # "primary" renders blurple, "secondary" grey. Face buttons are blurple
    # so the things you press constantly stand out from Start/Select.
    style: str = "secondary"
    emoji: typing.Optional[str] = None


# -- The layout spacer --------------------------------------------------------
#
# The controller layouts below need to leave a hole in a row -- the column to
# the left of "up", so the d-pad reads as a cross rather than a line. Discord
# has no such thing as an empty grid cell: a row is a list of components, left
# aligned, so the only way to hold a column open is to put a component in it
# and make it do nothing. That is a disabled button.
#
# A disabled button still has to carry *something*, because Discord rejects a
# button with neither a label nor an emoji. The usual trick is a zero-width
# space (U+200B) label, and discord.py 2.7.1 would pass it through untouched:
# ui.Button stores the label verbatim (button.py:225 does no stripping) and
# components.Button.to_dict() writes `if self.label: payload['label'] = ...`,
# so a ZWSP -- truthy in Python -- reaches the wire intact.
#
# What cannot be checked from here is the *server* side, and that is the side
# that once took this cog out with `400 ... Invalid emoji` over a character
# that looked fine locally. A visible, ordinary character cannot fail that
# way, so the spacer is a single MIDDLE DOT on a greyed-out button: it reads
# as a blank key on a controller, and there is nothing about it for Discord to
# object to. If ZWSP is ever confirmed against the live API, only this one
# constant has to change.
SPACER_LABEL = "\N{MIDDLE DOT}"

SPACER = Button(SPACER_LABEL, "", style="secondary")


def is_spacer(button: Button) -> bool:
    """Whether this cell is a layout spacer rather than a real button."""
    return not button.field


# -- Button emoji -------------------------------------------------------------
#
# Discord validates every component emoji server-side. A character that is not
# a real Unicode emoji is rejected with
#
#   400 Bad Request (error code: 50035): Invalid Form Body
#   In components.2.components.1.emoji.name: Invalid emoji
#
# and, because that is a 400 on the *send*, one bad character breaks the whole
# command rather than rendering a blank button. This happened for real: a
# control button briefly used U+21BB CLOCKWISE OPEN CIRCLE ARROW, which is an
# ordinary symbol with no emoji form at all.
#
# So every emoji the cog can put on a button is listed here, deliberately, and
# validate_emoji() checks the set. Python's unicodedata carries no emoji
# properties whatsoever (there is no Emoji or Emoji_Presentation lookup in the
# standard library), so the one property that matters -- whether the character
# already renders as an emoji or needs a U+FE0F VARIATION SELECTOR-16 to be
# coerced into one -- is recorded here by hand from Unicode's emoji-data.txt.
#
#   False = Emoji_Presentation=Yes, it is an emoji on its own.
#   True  = Emoji=Yes but Emoji_Presentation=No, so it MUST carry U+FE0F.
#
# A character that is in neither category (U+21BB, for instance) simply is not
# an emoji and must never appear here.
EMOJI_CODEPOINTS: typing.Dict[int, bool] = {
    0x2B06: True,   # UPWARDS BLACK ARROW
    0x2B07: True,   # DOWNWARDS BLACK ARROW
    0x2B05: True,   # LEFTWARDS BLACK ARROW
    0x27A1: True,   # BLACK RIGHTWARDS ARROW
    # HOURGLASS WITH FLOWING SAND, which is what the Wait button does: time
    # passes and nothing is pressed. It replaced U+23E9 BLACK RIGHT-POINTING
    # DOUBLE TRIANGLE (fast-forward), which was simply wrong -- the game runs
    # at its own speed either way, and a fast-forward symbol promises a
    # feature this cog does not have. Emoji_Presentation=Yes, so no U+FE0F.
    0x23F3: False,
    0x25B6: True,   # BLACK RIGHT-POINTING TRIANGLE
    # LEFTWARDS ARROW WITH HOOK, which Unicode's emoji-data.txt calls "right
    # arrow curving left" and every client draws as the undo/reply arrow. It
    # is Emoji=Yes, Emoji_Presentation=No, so it MUST carry U+FE0F -- the
    # Undo button below is exactly the kind of place the U+21BB mistake was
    # made, so this one was checked against emoji-data.txt before being added.
    0x21A9: True,
}

VARIATION_SELECTOR_16 = "\N{VARIATION SELECTOR-16}"

# The buttons that are not part of any console's controller: the ones every
# console's controls end with, and the single one left on a message whose game
# has been replaced. They live here rather than in RetroView so that
# validate_emoji() sees every emoji the cog can render without importing
# discord.py.
# The Wait button lets a clip's worth of time pass with no input at all, so
# it is an hourglass rather than the fast-forward triangles it used to be:
# nothing is sped up, and nothing is skipped. See EMOJI_CODEPOINTS.
WAIT_EMOJI = "\N{HOURGLASS WITH FLOWING SAND}"
RESUME_EMOJI = "\N{BLACK RIGHT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16}"
UNDO_EMOJI = "\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}"


def emoji_problem(emoji: str) -> typing.Optional[str]:
    """
    Explain why ``emoji`` would not survive a Discord component, or None.

    Only single-codepoint emoji (optionally with a variation selector) are
    allowed: the cog has no use for flags, skin tones or ZWJ sequences, and
    refusing them keeps this check exact instead of approximate.
    """
    if not isinstance(emoji, str) or not emoji:
        return "not a non-empty string"
    characters = list(emoji)
    selector = False
    if len(characters) == 2 and characters[1] == VARIATION_SELECTOR_16:
        selector = True
        characters.pop()
    if len(characters) != 1:
        return (
            "expected one character, optionally followed by U+FE0F; got "
            + " ".join(f"U+{ord(c):04X}" for c in emoji)
        )
    codepoint = ord(characters[0])
    if codepoint not in EMOJI_CODEPOINTS:
        name = unicodedata.name(characters[0], "an unnamed character")
        return (
            f"U+{codepoint:04X} ({name}) is not in EMOJI_CODEPOINTS; add it "
            "there only after checking it really is an emoji Discord accepts"
        )
    needs_selector = EMOJI_CODEPOINTS[codepoint]
    if needs_selector and not selector:
        return (
            f"U+{codepoint:04X} has text presentation by default and must be "
            "followed by U+FE0F VARIATION SELECTOR-16"
        )
    if selector and not needs_selector:
        return (
            f"U+{codepoint:04X} is already an emoji, so the trailing U+FE0F "
            "is redundant"
        )
    return None


def all_button_emoji() -> typing.Tuple[str, ...]:
    """Every emoji the cog can put on a button, deduplicated and sorted."""
    found = {WAIT_EMOJI, RESUME_EMOJI, UNDO_EMOJI}
    for button in DPAD:
        if button.emoji:
            found.add(button.emoji)
    for system in SYSTEMS:
        for button in system.buttons:
            if button.emoji:
                found.add(button.emoji)
    return tuple(sorted(found))


def validate_emoji() -> typing.Tuple[str, ...]:
    """
    Check every renderable emoji, raising ValueError on the first bad one.

    Returns the emoji that were checked. Called at import time so a bad
    character can never reach Discord, and asserted on in CI.
    """
    checked = all_button_emoji()
    for emoji in checked:
        problem = emoji_problem(emoji)
        if problem is not None:
            raise ValueError(f"Unusable button emoji {emoji!r}: {problem}")
    unused = set(EMOJI_CODEPOINTS) - {
        ord(e[0]) for e in checked
    }
    if unused:
        raise ValueError(
            "EMOJI_CODEPOINTS lists codepoints no button uses: "
            + ", ".join(f"U+{c:04X}" for c in sorted(unused))
        )
    return checked


# The d-pad is identical on every console here. The four buttons carry no
# label at all: an arrow is not improved by the word "Up" beside it, and four
# emoji-only buttons are narrow enough to line up into a cross. The custom_ids
# are built from ``field``, so dropping the labels does not disturb any
# message that is already in a channel.
UP = Button("", "up", emoji="\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}")
DOWN = Button("", "down", emoji="\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}")
LEFT = Button("", "left", emoji="\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}")
RIGHT = Button("", "right", emoji="\N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}")

#: The four directions, for :func:`all_button_emoji` -- which is what holds
#: every emoji this cog can put on a button to validate_emoji() at import.
DPAD: typing.Tuple[Button, ...] = (UP, DOWN, LEFT, RIGHT)


def _face(label: str, field: str) -> Button:
    return Button(label, field, style="primary")


# -- The controller layouts ---------------------------------------------------
#
# Discord gives a message five action rows of five components each, 25 in
# total. Each console's ``rows`` below is its whole controller as a grid; the
# view appends the control cluster -- Wait / confirm x3 / Undo -- to the last
# row if all three of them fit, and to a row of their own if they do not (see
# RetroView._build_controls).
#
# Every layout is built from the same shape, so a Genesis pad and a Game Boy
# pad are recognisably the same thing:
#
#     row 0:  ·  ⬆   <shoulders or the upper face row>
#     row 1:  ⬅ ⬇ ➡  <the face buttons that fit beside the d-pad>
#     row 2:  ·  ·    <the lower face row, on consoles that need one>
#     next :  Start / Select / Mode / ...
#     last :  Wait  confirm x3  Undo   (sharing the row above when it fits)
#
# "·" is SPACER: a disabled button that holds the column above "down" open so
# the d-pad reads as a cross instead of a line. The spacers on a lower face
# row are there to keep that row aligned under the row above it.
#
# In full, per console (⬆⬇⬅➡ are the d-pad, · a spacer, and the three
# controls the view appends are shown in brackets).
#
# The middle one of the three -- the "confirm x3" repeat button -- is drawn
# only on a clip long enough to fit more than one tap, which at the default
# 160ms hold means 0.48 seconds and up (the default clip is 1 second, so it
# is normally there). Below that the row is exactly the one shown here with
# that button left out: Wait and Undo do not move, because the space is
# reserved with CONTROL_BUTTONS for all three either way. See MIN_REPEAT_TAPS
# in retro/RetroView.py.
#
#   Game Boy / NES        ·  ⬆                       12 components, 3 rows
#                         ⬅  ⬇  ➡  B  A
#                         Start  Select  [Wait  A x3  Undo]
#
#   Game Boy Advance      ·  ⬆  L  R                 14 components, 3 rows
#                         ⬅  ⬇  ➡  B  A
#                         Start  Select  [Wait  A x3  Undo]
#
#   Super Nintendo        ·  ⬆  L  R                 19 components, 4 rows
#                         ⬅  ⬇  ➡  Y  X
#                         ·  ·  ·  B  A
#                         Start  Select  [Wait  A x3  Undo]
#
#   Sega Genesis          ·  ⬆  X  Y  Z              18 components, 4 rows
#                         ⬅  ⬇  ➡
#                         ·  ·  A  B  C
#                         Mode  Start  [Wait  B x3  Undo]
#
#   PC Engine             ·  ⬆  IV  V  VI            18 components, 4 rows
#                         ⬅  ⬇  ➡
#                         ·  ·  III  II  I
#                         Select  Run  [Wait  I x3  Undo]
#
#   Master System         ·  ⬆                       11 components, 3 rows
#                         ⬅  ⬇  ➡  1  2
#                         Pause  [Wait  1 x3  Undo]
#
#   Game Gear             ·  ⬆                       11 components, 3 rows
#                         ⬅  ⬇  ➡  1  2
#                         Start  [Wait  1 x3  Undo]
#
#   Neo Geo Pocket        ·  ⬆                       11 components, 3 rows
#                         ⬅  ⬇  ➡  A  B
#                         Option  [Wait  A x3  Undo]
#
# Undo is in the control cluster rather than in any console's grid because
# that is what it is: a control, like Wait, and not a button the console has.
# The cluster was four wide while Replay existed, which is one too many to sit
# beside a two-button Start/Select row; with Replay gone it is three again and
# every console's controls share the bottom row, so the widest layouts are
# back to four action rows and the whole set is one row and one component per
# console smaller than it was. Nothing was put next to the d-pad on purpose:
# the cell left of "up" is the one free column every console has, and a
# labelled button there would both shift ⬆ out of line (Discord sizes a
# button to its content) and sit exactly where a thumb aims for a direction.
#
# The worst case is the Super Nintendo at 19 components over four rows, so
# there are six components of headroom and a spare row. MAX_LAYOUT_ROWS and
# MAX_BUTTONS_PER_ROW enforce the budget, validate_layouts() below checks it
# at import time, and the row plan above is asserted on in CI.
#
# Discord's own limits, repeated here rather than imported from discord.py so
# that this module keeps working with nothing but the standard library.
MAX_ACTION_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_COMPONENTS = 25
# Wait, confirm x3 and Undo, which RetroView appends to every layout.
#
# Always three, even though the middle one is not drawn on a clip too short
# for two taps: this is the *space reserved* for the cluster, and reserving
# only two on a short clip would move Wait and Undo between one clip length
# and the next, and would validate a layout that broke the moment somebody
# lengthened the clip.
CONTROL_BUTTONS = 3
# How many rows a console's own grid may use, leaving room for the controls.
MAX_LAYOUT_ROWS = 4


class System(typing.NamedTuple):
    """A console: which core runs it, what it accepts, and how it is played."""

    key: str
    name: str
    core: str
    # Lowercase, without the leading dot. Ambiguous or BIOS-dependent
    # extensions are deliberately absent; see AMBIGUOUS_EXTENSIONS below.
    extensions: typing.Tuple[str, ...]
    # The whole controller as a grid of rows, d-pad and spacers included. See
    # the row plan above; at most MAX_LAYOUT_ROWS rows of MAX_BUTTONS_PER_ROW.
    rows: typing.Tuple[typing.Tuple[Button, ...], ...]
    # The button that means "yes, go on" on this console. It is what the
    # repeat button taps, so menu-heavy games need fewer round trips.
    confirm: str

    @property
    def buttons(self) -> typing.Tuple[Button, ...]:
        """Every real game button this console offers, spacers excluded."""
        return tuple(b for row in self.rows for b in row if not is_spacer(b))

    @property
    def fields(self) -> typing.Tuple[str, ...]:
        return tuple(b.field for b in self.buttons)

    def button(self, field: str) -> typing.Optional[Button]:
        for candidate in self.buttons:
            if candidate.field == field:
                return candidate
        return None

    def label_for(self, field: str) -> str:
        """A human name for a field, for prose and the repeat button."""
        found = self.button(field)
        if found is None or not found.label:
            return field.upper()
        return found.label

    def caption_for(self, field: str) -> str:
        """
        How a press of this button is named in the message above the clip.

        The console's own name for it when it has one -- A, Start, Mode, I
        through VI, Option, Pause -- and the button's emoji when it has no
        label at all, which is the d-pad: an arrow says which way somebody
        went far better than the word "UP" does.

        Read from the very same :class:`Button` the view renders rather than
        from a second table beside it, so a console that names its buttons
        unusually (the Genesis A is RetroPad *y*; the Neo Geo Pocket's A and B
        are swapped from the RetroPad convention) is described the way its own
        players read it, and nothing can drift out of step with the controls.
        Every emoji here has already been through :func:`validate_emoji` at
        import time, so nothing this returns can be a character Discord
        refuses.

        Falls back to :meth:`label_for` for a field this console does not
        have, which is only reachable from a stale click on a message drawn
        for a different game.
        """
        found = self.button(field)
        if found is None:
            return self.label_for(field)
        return found.label or found.emoji or field.upper()


# Extensions we never claim. ".fds" needs Nintendo's disksys.rom BIOS, and
# the disc-image formats all need a CD image plus (usually) a console BIOS.
#
# ".bs" (Satellaview) and ".st" (Sufami Turbo) are here for the same reason
# as ".fds", and were in the Super Nintendo's extension list until somebody
# read this module's own header: both formats are *slot* cartridges, and
# snes9x cannot start one without the base cartridge they plug into --
# BS-X.bin for a Satellaview download, STBIOS.bin for a Sufami Turbo game.
# Every core here is BIOS-free, which is a condition of being here at all, so
# claiming these two meant a session that started, showed nothing, and looked
# like a broken cog rather than an unsupported file.
#
# ".bin" is refused even though exactly one core here claims it
# (genesis_plus_gx), which makes it the one entry in this set that is not
# about a collision at all. It is about the extension itself: ".bin" carries
# no console information whatsoever. An Atari 2600 cartridge, a Mega Drive
# ROM, a Virtual Boy ROM, a raw CD track and a BIOS dump are all ".bin" in
# the wild, so claiming it would mean silently loading somebody's PlayStation
# disc track as a Mega Drive game and showing them the result. Genesis ROMs
# should be named ".md" instead, which is what the unsupported-extension
# reply tells people.
AMBIGUOUS_EXTENSIONS = frozenset(
    {"bin", "cue", "iso", "chd", "toc", "m3u", "ccd", "img", "fds", "bs", "st"}
)

#: Why each of those is refused, in a sentence the person who uploaded it can
#: act on -- and, where there is one, what to do instead.
#:
#: The reasoning was written out at length above and then only ever existed
#: as a comment, so somebody uploading the commonest ROM extension in the
#: wild got "`.bin` isn't a console this bot knows" and no way to find out
#: why. Every entry here ends in something actionable, because a refusal
#: that cannot be acted on is just a wall.
#:
#: Keyed without the dot, as AMBIGUOUS_EXTENSIONS is. An extension absent
#: from here falls back to the plain "not a console this bot knows" reply,
#: which is the right answer for a `.txt`.
AMBIGUOUS_REASONS: typing.Dict[str, str] = {
    "bin": (
        "`.bin` does not say which console a file is for \N{EM DASH} an Atari "
        "cartridge, a Mega Drive ROM, a Virtual Boy ROM, a raw CD track and a "
        "BIOS dump are all `.bin`. Accepting it would mean guessing, and "
        "guessing wrong means loading a disc track as a Mega Drive game. "
        "**If this is a Genesis or Mega Drive ROM, rename it to `.md`** and "
        "it will start."
    ),
    "fds": (
        "`.fds` is a Famicom Disk System image, which cannot boot without "
        "Nintendo's `disksys.rom` BIOS. Every console this bot plays works "
        "without a BIOS, so it does not claim this one."
    ),
    "bs": (
        "`.bs` is a Satellaview download, which only boots inside the BS-X "
        "base cartridge it plugged into. Without that it starts and shows "
        "nothing, so it is refused rather than left looking broken."
    ),
    "st": (
        "`.st` is a Sufami Turbo game, which only boots inside the Sufami "
        "Turbo base cartridge. Without that it starts and shows nothing, so "
        "it is refused rather than left looking broken."
    ),
}
# The disc-image formats all fail the same way and for the same reason, so
# they share a sentence rather than repeating four near-identical ones.
for _disc in ("cue", "iso", "chd", "toc", "m3u", "ccd", "img"):
    AMBIGUOUS_REASONS[_disc] = (
        f"`.{_disc}` is a CD image. The consoles this bot plays are all "
        "cartridge machines, and disc systems need both the disc and a "
        "console BIOS, so none of them is supported."
    )


def ambiguous_reason(extension: str) -> typing.Optional[str]:
    """
    Why this extension is deliberately not claimed, if it is one of those.

    ``extension`` may be given with or without its dot and in any case.
    Returns None for anything that is simply unknown rather than
    deliberately refused, which is what keeps the caller's fallback honest.
    """
    key = str(extension or "").lstrip(".").lower()
    return AMBIGUOUS_REASONS.get(key)


# Ordered roughly by how likely someone is to want them. Extensions come from
# each core's own valid_extensions string, minus AMBIGUOUS_EXTENSIONS.
SYSTEMS: typing.Tuple[System, ...] = (
    System(
        key="gb",
        name="Game Boy",
        core="gambatte",
        extensions=("gb", "gbc", "dmg"),
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("B", "b"), _face("A", "a")),
            (Button("Start", "start"), Button("Select", "select")),
        ),
        confirm="a",
    ),
    System(
        key="gba",
        name="Game Boy Advance",
        core="mgba",
        # mGBA runs the GBA BIOS in high-level emulation, so no BIOS file is
        # needed. It also accepts .gb/.gbc, but Gambatte owns those above.
        extensions=("gba",),
        # L and R ride on the top row, where the shoulder buttons really are.
        rows=(
            (SPACER, UP, _face("L", "l"), _face("R", "r")),
            (LEFT, DOWN, RIGHT, _face("B", "b"), _face("A", "a")),
            (Button("Start", "start"), Button("Select", "select")),
        ),
        confirm="a",
    ),
    System(
        key="nes",
        name="Nintendo Entertainment System",
        core="fceumm",
        extensions=("nes", "unf", "unif"),
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("B", "b"), _face("A", "a")),
            (Button("Start", "start"), Button("Select", "select")),
        ),
        confirm="a",
    ),
    System(
        key="snes",
        name="Super Nintendo",
        core="snes9x",
        # No ".bs" or ".st": both need a base cartridge BIOS, which no core
        # here does. See AMBIGUOUS_EXTENSIONS.
        extensions=("smc", "sfc", "swc", "fig"),
        # The SNES diamond (X on top, Y left, A right, B below) flattened into
        # the 2x2 block everyone draws it as: Y X over B A.
        rows=(
            (SPACER, UP, _face("L", "l"), _face("R", "r")),
            (LEFT, DOWN, RIGHT, _face("Y", "y"), _face("X", "x")),
            (SPACER, SPACER, SPACER, _face("B", "b"), _face("A", "a")),
            (Button("Start", "start"), Button("Select", "select")),
        ),
        confirm="a",
    ),
    System(
        key="genesis",
        name="Sega Genesis",
        core="genesis_plus_gx",
        extensions=("md", "mdx", "smd", "gen", "68k", "sgd"),
        # The Genesis pad's own A/B/C are RetroPad y/b/a, and its six-button
        # X/Y/Z are RetroPad x/l/r. Labelling them by the RetroPad name would
        # put "A" on the wrong button for every Genesis game ever made.
        #
        # Six face buttons do not fit beside a d-pad in five columns, so they
        # keep their real two-by-three shape: X Y Z above A B C, exactly as
        # they sit on the six-button pad.
        rows=(
            (SPACER, UP, _face("X", "x"), _face("Y", "l"), _face("Z", "r")),
            (LEFT, DOWN, RIGHT),
            (SPACER, SPACER, _face("A", "y"), _face("B", "b"), _face("C", "a")),
            (Button("Mode", "select"), Button("Start", "start")),
        ),
        confirm="b",
    ),
    System(
        key="sms",
        name="Sega Master System",
        core="genesis_plus_gx",
        # ".sg" is an SG-1000 cartridge, which genesis_plus_gx runs as part
        # of its Master System support (the SG-1000 is the same 8-bit line,
        # and its pad is the same two buttons). It stays here rather than
        # becoming a system of its own: the controls and the console name a
        # player sees would both be right either way, and a third entry on
        # this core would buy nothing but a longer list.
        extensions=("sms", "sg"),
        # "Pause" really is the Master System's own name for this: the
        # console has a Pause button on the deck, not on the pad, and
        # start/pause is the RetroPad field the core reads for it.
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("1", "b"), _face("2", "a")),
            (Button("Pause", "start"),),
        ),
        confirm="b",
    ),
    System(
        key="gg",
        name="Sega Game Gear",
        core="genesis_plus_gx",
        # Its own console, not a Master System: the Game Gear was folded in
        # with the SMS because genesis_plus_gx runs both, which announced a
        # ".gg" game as a "Sega Master System" one and put "Pause" on its
        # Start button. The handheld has no console-deck Pause -- the button
        # beside the screen is START, and every Game Gear manual calls it
        # that -- so the label was wrong for every Game Gear game.
        extensions=("gg",),
        # Same pad as the Master System otherwise: a d-pad and two buttons,
        # numbered 1 and 2 on the shell.
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("1", "b"), _face("2", "a")),
            (Button("Start", "start"),),
        ),
        confirm="b",
    ),
    System(
        key="pce",
        name="PC Engine",
        core="mednafen_pce_fast",
        # HuCard games only: a CD-ROM² game needs a disc image and the System
        # Card BIOS, neither of which this cog handles.
        extensions=("pce",),
        # Same two-by-three block as the Genesis: the Avenue Pad 6's extra
        # buttons sit above the original I and II, and I stays on the right
        # where the thumb expects it.
        rows=(
            (SPACER, UP, _face("IV", "x"), _face("V", "l"), _face("VI", "r")),
            (LEFT, DOWN, RIGHT),
            (SPACER, SPACER, _face("III", "y"), _face("II", "a"), _face("I", "b")),
            (Button("Select", "select"), Button("Run", "start")),
        ),
        confirm="b",
    ),
    System(
        key="ngp",
        name="Neo Geo Pocket",
        core="mednafen_ngp",
        extensions=("ngp", "ngc", "ngpc", "npc"),
        # The Neo Geo Pocket's A is RetroPad b and its B is RetroPad a, i.e.
        # swapped from the Nintendo convention the RetroPad is named for.
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("A", "b"), _face("B", "a")),
            (Button("Option", "start"),),
        ),
        confirm="b",
    ),
)


# core name -> a short description of what it plays, for `[p]retroset` output.
# A core that runs more than one console (genesis_plus_gx) lists them all.
CORES: typing.Dict[str, str] = {}
for _system in SYSTEMS:
    _names = CORES.setdefault(_system.core, [])
    if _system.name not in _names:
        _names.append(_system.name)
CORES = {_core: " / ".join(_names) for _core, _names in CORES.items()}
del _system, _names


_BY_EXTENSION: typing.Dict[str, System] = {}
for _system in SYSTEMS:
    for _extension in _system.extensions:
        assert _extension not in AMBIGUOUS_EXTENSIONS, _extension
        assert _extension not in _BY_EXTENSION, _extension
        _BY_EXTENSION[_extension] = _system
del _system, _extension


def system_for_extension(extension: str) -> typing.Optional[System]:
    """Pick the console for a file extension (with or without the dot)."""
    return _BY_EXTENSION.get(str(extension).lower().lstrip("."))


def system_by_key(key: str) -> typing.Optional[System]:
    for system in SYSTEMS:
        if system.key == key:
            return system
    return None


def system_for_core(core: str) -> typing.Optional[System]:
    """The first console a core runs; used when only a core name is known."""
    for system in SYSTEMS:
        if system.core == core:
            return system
    return None


def core_suffix() -> str:
    """The shared-library suffix libretro cores use on this platform."""
    if sys.platform == "darwin":
        return "_libretro.dylib"
    if sys.platform in ("win32", "cygwin"):
        return "_libretro.dll"
    return "_libretro.so"


def core_filename(core: str) -> str:
    """e.g. ``gambatte`` -> ``gambatte_libretro.so`` on Linux."""
    return f"{core}{core_suffix()}"


def core_name_from_filename(filename: str) -> typing.Optional[str]:
    """``/cores/snes9x_libretro.so`` -> ``snes9x``, if we know that core."""
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    for marker in ("_libretro.so", "_libretro.dylib", "_libretro.dll"):
        if name.endswith(marker):
            name = name[: -len(marker)]
            break
    else:
        name = name.rsplit(".", 1)[0]
    return name if name in CORES else None


def validate_layouts() -> typing.Tuple[str, ...]:
    """
    Check every console's grid against Discord's component limits.

    Raises ValueError on the first layout that would not survive a send.
    Returns the console keys that were checked. Called at import time and
    asserted on in CI, for the same reason validate_emoji() is: a layout
    Discord refuses is a 400 on the send, which takes out the whole command.
    """
    checked = []
    for system in SYSTEMS:
        rows = system.rows
        if not rows:
            raise ValueError(f"{system.key} declares no controller rows")
        # The three control buttons the view appends need a row of their own
        # when they do not fit on the last one.
        used = len(rows) + (0 if len(rows[-1]) + CONTROL_BUTTONS <= MAX_BUTTONS_PER_ROW else 1)
        if len(rows) > MAX_LAYOUT_ROWS or used > MAX_ACTION_ROWS:
            raise ValueError(
                f"{system.key} needs {used} action rows; Discord allows "
                f"{MAX_ACTION_ROWS}."
            )
        total = CONTROL_BUTTONS
        for index, row in enumerate(rows):
            if not row:
                raise ValueError(f"{system.key} row {index} is empty")
            if len(row) > MAX_BUTTONS_PER_ROW:
                raise ValueError(
                    f"{system.key} row {index} has {len(row)} components; "
                    f"Discord allows {MAX_BUTTONS_PER_ROW}."
                )
            total += len(row)
            for button in row:
                if is_spacer(button):
                    if not button.label:
                        raise ValueError(
                            f"{system.key} row {index} has a spacer with no "
                            "label; Discord refuses a button with neither a "
                            "label nor an emoji."
                        )
                    continue
                if not button.label and not button.emoji:
                    raise ValueError(
                        f"{system.key}'s {button.field!r} button has neither a "
                        "label nor an emoji, which Discord refuses."
                    )
        if total > MAX_COMPONENTS:
            raise ValueError(
                f"{system.key} needs {total} components; Discord allows "
                f"{MAX_COMPONENTS}."
            )
        fields = system.fields
        if len(set(fields)) != len(fields):
            raise ValueError(f"{system.key} uses a RetroPad field twice: {fields}")
        if system.confirm not in fields:
            raise ValueError(
                f"{system.key}'s confirm button {system.confirm!r} is not one "
                "of its buttons."
            )
        directions = {b.field for b in DPAD}
        if not directions <= set(fields):
            raise ValueError(
                f"{system.key} is missing part of the d-pad: "
                f"{sorted(directions - set(fields))}"
            )
        checked.append(system.key)
    return tuple(checked)


# Fail loudly at import time rather than with a 400 from Discord halfway
# through somebody's game. This costs a few microseconds once per process.
validate_emoji()
validate_layouts()
