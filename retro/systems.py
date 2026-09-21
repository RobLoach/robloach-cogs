"""
The consoles this cog can emulate, the libretro core each one needs, and the
controller layout each one shows in Discord.

This module deliberately imports nothing but the standard library: the cog,
the view and the emulator all read from it, and it can be exercised on its
own without discord.py or Red installed.

Every core listed here is BIOS-free (it boots a game with nothing but the ROM)
and is published for linux/x86_64, linux/aarch64, macOS and Windows on the
libretro buildbot.

Two further conditions a new console has to meet, both learned the hard way:

* **It must not ask for a screen rotation.** libretro.py's software video
  driver rotates a frame by 90 degrees from the wrong starting offset --
  ``(width - 4) * height`` where ``(width - 1) * height`` is meant -- so a
  core that calls ``RETRO_ENVIRONMENT_SET_ROTATION`` with 90 degrees gets a
  picture whose rows are cyclically shifted and partly overwritten, on both
  libretro.py 0.6.0 and 0.11.1. There is no fast path and no slow path that
  survives it, so the WonderSwan (``mednafen_wswan``), which rotates for a
  good half of its library, is deliberately absent. An emulator test asserts
  that no core here reports a rotation after boot.
* **Its controls must fit one d-pad and the grid below.** The Virtual Boy
  (``mednafen_vb``) is out because its defining control scheme is *two*
  d-pads -- its own input descriptors are "Left D-Pad Up" and "Right D-Pad
  Up" and so on -- which this layout cannot express.
"""

import sys
import typing
import unicodedata

__all__ = [
    "Button",
    "System",
    "SYSTEMS",
    "CORES",
    "EXTENSIONS",
    "core_suffix",
    "core_filename",
    "system_for_core",
    "system_for_extension",
    "system_by_key",
    "core_name_from_filename",
    "extensions_for_core",
    "DPAD",
    "SPACER",
    "SPACER_LABEL",
    "is_spacer",
    "WAIT_EMOJI",
    "REPLAY_EMOJI",
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
# command rather than rendering a blank button. This happened for real: the
# Replay button briefly used U+21BB CLOCKWISE OPEN CIRCLE ARROW, which is an
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
    0x23E9: False,  # BLACK RIGHT-POINTING DOUBLE TRIANGLE
    0x25B6: True,   # BLACK RIGHT-POINTING TRIANGLE
    0x1F501: False,  # CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS
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
WAIT_EMOJI = "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE}"
REPLAY_EMOJI = "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
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
    found = {WAIT_EMOJI, REPLAY_EMOJI, RESUME_EMOJI, UNDO_EMOJI}
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

# Kept in the historical Up/Down/Left/Right order; RetroView reads it for the
# set of fields that count as a direction.
DPAD: typing.Tuple[Button, ...] = (UP, DOWN, LEFT, RIGHT)


def _face(label: str, field: str) -> Button:
    return Button(label, field, style="primary")


# -- The controller layouts ---------------------------------------------------
#
# Discord gives a message five action rows of five components each, 25 in
# total. Each console's ``rows`` below is its whole controller as a grid; the
# view appends the control cluster -- Wait / confirm x3 / Replay / Undo -- to
# the last row if all four of them fit, and to a row of their own if they do
# not (see RetroView._build_controls).
#
# Every layout is built from the same shape, so a Genesis pad and a Game Boy
# pad are recognisably the same thing:
#
#     row 0:  ·  ⬆   <shoulders or the upper face row>
#     row 1:  ⬅ ⬇ ➡  <the face buttons that fit beside the d-pad>
#     row 2:  ·  ·    <the lower face row, on consoles that need one>
#     next :  Start / Select / Mode / ...
#     last :  Wait  confirm x3  Replay  Undo   (sharing the row above when it fits)
#
# "·" is SPACER: a disabled button that holds the column above "down" open so
# the d-pad reads as a cross instead of a line. The spacers on a lower face
# row are there to keep that row aligned under the row above it.
#
# In full, per console (⬆⬇⬅➡ are the d-pad, · a spacer, and the four
# controls the view appends are shown in brackets):
#
#   Game Boy / NES        ·  ⬆                       13 components, 4 rows
#                         ⬅  ⬇  ➡  B  A
#                         Start  Select
#                         [Wait  A x3  Replay  Undo]
#
#   Game Boy Advance      ·  ⬆  L  R                 15 components, 4 rows
#                         ⬅  ⬇  ➡  B  A
#                         Start  Select
#                         [Wait  A x3  Replay  Undo]
#
#   Super Nintendo        ·  ⬆  L  R                 20 components, 5 rows
#                         ⬅  ⬇  ➡  Y  X
#                         ·  ·  ·  B  A
#                         Start  Select
#                         [Wait  A x3  Replay  Undo]
#
#   Sega Genesis          ·  ⬆  X  Y  Z              19 components, 5 rows
#                         ⬅  ⬇  ➡
#                         ·  ·  A  B  C
#                         Mode  Start
#                         [Wait  B x3  Replay  Undo]
#
#   PC Engine             ·  ⬆  IV  V  VI            19 components, 5 rows
#                         ⬅  ⬇  ➡
#                         ·  ·  III  II  I
#                         Select  Run
#                         [Wait  I x3  Replay  Undo]
#
#   Master System         ·  ⬆                       12 components, 3 rows
#                         ⬅  ⬇  ➡  1  2
#                         Pause  [Wait  1 x3  Replay  Undo]
#
#   Neo Geo Pocket        ·  ⬆                       12 components, 3 rows
#                         ⬅  ⬇  ➡  A  B
#                         Option  [Wait  A x3  Replay  Undo]
#
# Undo is in the control cluster rather than in any console's grid because
# that is what it is: a control, like Wait and Replay, and not a button the
# console has. The cost of adding it was that the cluster no longer fits
# beside Start/Select on a two-button bottom row, so on six of the eight
# consoles the controls moved to a row of their own -- which is why the Super
# Nintendo, the Genesis and the PC Engine now use all five action rows. The
# Master System and the Neo Geo Pocket have a one-button bottom row and still
# share it. Nothing was put next to the d-pad on purpose: the cell left of
# "up" is the one free column every console has, and a labelled button there
# would both shift ⬆ out of line (Discord sizes a button to its content) and
# sit exactly where a thumb aims for a direction.
#
# The worst case is the Super Nintendo at 20 components over five rows, so
# there are five components of headroom and no spare row: a ninth console
# with a four-row grid and a bottom row of three or more is the first thing
# that would not fit. MAX_LAYOUT_ROWS and MAX_BUTTONS_PER_ROW enforce the
# budget, validate_layouts() below checks it at import time, and the row plan
# above is asserted on in CI.
#
# Discord's own limits, repeated here rather than imported from discord.py so
# that this module keeps working with nothing but the standard library.
MAX_ACTION_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_COMPONENTS = 25
# Wait, confirm x3, Replay and Undo, which RetroView appends to every layout.
CONTROL_BUTTONS = 4
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


# Extensions we never claim. ".fds" needs Nintendo's disksys.rom BIOS, and
# the disc-image formats all need a CD image plus (usually) a console BIOS.
#
# ".bin" used to be excluded because three of the cores here claimed it
# (stella2014, mednafen_vb and genesis_plus_gx); with the first two gone only
# genesis_plus_gx claims it, so it is no longer ambiguous *within this set*.
# It stays out anyway, because the ambiguity was never really about our core
# list: ".bin" is the one extension that carries no console information at
# all. An Atari 2600 cartridge, a Mega Drive ROM, a Virtual Boy ROM, a raw CD
# track and a BIOS dump are all ".bin" in the wild, so accepting it would
# mean silently loading somebody's PlayStation disc track as a Mega Drive
# game. Genesis ROMs should be named ".md" instead.
AMBIGUOUS_EXTENSIONS = frozenset(
    {"bin", "cue", "iso", "chd", "toc", "m3u", "ccd", "img", "fds"}
)


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
        extensions=("smc", "sfc", "swc", "fig", "bs", "st"),
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
        extensions=("sms", "gg", "sg"),
        rows=(
            (SPACER, UP),
            (LEFT, DOWN, RIGHT, _face("1", "b"), _face("2", "a")),
            (Button("Pause", "start"),),
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

# Every extension `[p]retro` will accept, with the leading dot, sorted so the
# help text reads the same every time.
EXTENSIONS: typing.Tuple[str, ...] = tuple(sorted(f".{e}" for e in _BY_EXTENSION))


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


def extensions_for_core(core: str) -> typing.Tuple[str, ...]:
    found: typing.List[str] = []
    for system in SYSTEMS:
        if system.core == core:
            found.extend(f".{e}" for e in system.extensions)
    return tuple(sorted(found))


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
