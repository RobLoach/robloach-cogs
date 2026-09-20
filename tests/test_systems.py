"""The console/core/button tables: layouts, extensions, cores and emoji.

Pure standard library, so this runs on a bare checkout with nothing
installed. These are the checks that stop a message Discord would refuse
(a layout over budget, a button with no label, an emoji that is not an
emoji) from ever being sent.
"""

import unicodedata

import pytest

from .loader import load_standalone

S = load_standalone("retro_systems_standalone", "systems.py")

# The sixteen fields a libretro RetroPad actually has. A console's buttons
# are labelled for that console but must map onto one of these.
RETROPAD = frozenset(
    {
        "b", "y", "select", "start", "up", "down", "left", "right",
        "a", "x", "l", "r", "l2", "r2", "l3", "r3",
    }
)

DIRECTIONS = ("up", "down", "left", "right")

#: Every console, by key, for parametrising. Failures name the console.
SYSTEM_KEYS = [system.key for system in S.SYSTEMS]


@pytest.fixture(params=SYSTEM_KEYS)
def system(request):
    return S.system_by_key(request.param)


@pytest.fixture
def emoji_table():
    """Restore EMOJI_CODEPOINTS and SYSTEMS after a test edits them."""
    saved_codepoints = dict(S.EMOJI_CODEPOINTS)
    saved_systems = S.SYSTEMS
    yield S
    S.EMOJI_CODEPOINTS.clear()
    S.EMOJI_CODEPOINTS.update(saved_codepoints)
    S.SYSTEMS = saved_systems


# -- 1. Each console's buttons mean what that console calls them --------------
#
# The label is what the player sees and the field is what the core reads, and
# they are frequently not the same: a Genesis "A" is RetroPad y, a Neo Geo
# Pocket "A" is RetroPad b. Getting this wrong puts the wrong button under
# every game ever made for the console, and nothing else in the suite would
# notice.
EXPECTED_BUTTONS = {
    "gb": ("gambatte", {"A": "a", "B": "b", "Start": "start", "Select": "select"}),
    "gba": (
        "mgba",
        {"A": "a", "B": "b", "L": "l", "R": "r", "Start": "start", "Select": "select"},
    ),
    "nes": ("fceumm", {"A": "a", "B": "b", "Start": "start", "Select": "select"}),
    "snes": (
        "snes9x",
        {
            "A": "a", "B": "b", "X": "x", "Y": "y", "L": "l", "R": "r",
            "Start": "start", "Select": "select",
        },
    ),
    # y/b/a -> Genesis A/B/C; x/l/r -> X/Y/Z; select -> Mode.
    "genesis": (
        "genesis_plus_gx",
        {
            "A": "y", "B": "b", "C": "a", "X": "x", "Y": "l", "Z": "r",
            "Mode": "select", "Start": "start",
        },
    ),
    "sms": ("genesis_plus_gx", {"1": "b", "2": "a", "Pause": "start"}),
    "atari2600": ("stella2014", {"Fire": "b", "Select": "select", "Reset": "start"}),
    "pce": (
        "mednafen_pce_fast",
        {
            "I": "b", "II": "a", "III": "y", "IV": "x", "V": "l", "VI": "r",
            "Select": "select", "Run": "start",
        },
    ),
    "wswan": ("mednafen_wswan", {"A": "a", "B": "b", "Start": "start", "Rotate": "select"}),
    "ngp": ("mednafen_ngp", {"A": "b", "B": "a", "Option": "start"}),
    "vb": (
        "mednafen_vb",
        {"A": "a", "B": "b", "L": "l", "R": "r", "Start": "start", "Select": "select"},
    ),
}


def test_every_console_is_covered_by_the_button_table():
    assert sorted(EXPECTED_BUTTONS) == sorted(SYSTEM_KEYS)


@pytest.mark.parametrize("key", sorted(EXPECTED_BUTTONS))
def test_console_buttons_map_to_the_right_retropad_fields(key):
    core, mapping = EXPECTED_BUTTONS[key]
    system = S.system_by_key(key)
    assert system is not None, key
    assert system.core == core
    got = {b.label: b.field for b in system.buttons if b.field not in DIRECTIONS}
    assert got == mapping


def test_the_dpad_is_four_emoji_only_buttons(system):
    dpad = {b.field: b for b in system.buttons if b.field in DIRECTIONS}
    assert set(dpad) == set(DIRECTIONS)
    assert all(b.label == "" for b in dpad.values()), {f: b.label for f, b in dpad.items()}
    assert all(b.emoji for b in dpad.values())


def test_the_shared_dpad_carries_arrows_and_no_words():
    for button in S.DPAD:
        assert button.emoji and not button.label, button


# -- 2. Every field is a real RetroPad field ----------------------------------


def test_fields_are_real_retropad_fields(system):
    assert set(system.fields) <= RETROPAD, set(system.fields) - RETROPAD


def test_no_field_is_used_twice(system):
    assert len(set(system.fields)) == len(system.fields), system.fields


def test_the_confirm_button_is_one_of_the_console_s_own(system):
    assert system.confirm in system.fields


def test_label_for_falls_back_to_the_field_name(system):
    assert system.label_for("nonexistent") == "NONEXISTENT"
    assert system.label_for("up") == "UP"  # the d-pad has no labels


# -- 3. Discord's 5x5x25 component grid ---------------------------------------
#
# systems.System.rows is the whole controller -- d-pad, face buttons and
# layout spacers -- and RetroView appends Wait/repeat/Replay to it.


def action_rows(system):
    """How many action rows this console needs once the controls are added."""
    rows = system.rows
    return len(rows) + (0 if len(rows[-1]) + S.CONTROL_BUTTONS <= 5 else 1)


def test_validate_layouts_covers_every_console():
    assert S.validate_layouts() == tuple(x.key for x in S.SYSTEMS)


def test_layout_fits_discord_s_budget(system):
    assert action_rows(system) <= S.MAX_ACTION_ROWS
    assert len(system.rows) <= S.MAX_LAYOUT_ROWS
    assert all(len(row) <= S.MAX_BUTTONS_PER_ROW for row in system.rows)
    total = sum(len(row) for row in system.rows) + S.CONTROL_BUTTONS
    assert total <= S.MAX_COMPONENTS, total


def test_the_dpad_reads_as_a_cross(system):
    # A spacer, then Up; then Left Down Right underneath it.
    assert system.rows[0][0] is S.SPACER
    assert system.rows[0][1] is S.UP
    assert system.rows[1][:3] == (S.LEFT, S.DOWN, S.RIGHT)


def test_every_cell_has_something_on_it(system):
    # Discord refuses the whole message if a component has neither a label
    # nor an emoji, spacers included.
    for row in system.rows:
        for cell in row:
            assert cell.label or cell.emoji, cell


def test_spacers_are_labelled_and_are_not_real_buttons(system):
    spacers = [b for row in system.rows for b in row if S.is_spacer(b)]
    assert all(b.label for b in spacers)
    assert all(b.field == "" for b in spacers)


def test_the_worst_console_still_leaves_headroom():
    worst = max(sum(len(r) for r in x.rows) + S.CONTROL_BUTTONS for x in S.SYSTEMS)
    assert worst <= S.MAX_COMPONENTS
    # The Super Nintendo is the biggest at 19; a much larger number means a
    # console was added without thinking about the budget.
    assert worst == 19, worst


@pytest.mark.parametrize(
    "label, rows, why",
    [
        (
            "a six-wide row",
            ((S.SPACER, S.UP, S.LEFT, S.DOWN, S.RIGHT, S.SPACER),),
            "row 0",
        ),
        (
            "a spacer with no label",
            ((S.Button("", ""), S.UP), (S.LEFT, S.DOWN, S.RIGHT)),
            "spacer",
        ),
        (
            "a button with neither label nor emoji",
            ((S.SPACER, S.UP), (S.LEFT, S.DOWN, S.RIGHT, S.Button("", "a"))),
            "neither",
        ),
    ],
)
def test_a_layout_discord_would_refuse_does_not_get_past_import(
    emoji_table, label, rows, why
):
    bad = S.System(
        key="bad", name="Bad", core="gambatte", extensions=("zz",), rows=rows, confirm="up"
    )
    S.SYSTEMS = (bad,)
    with pytest.raises(ValueError) as error:
        S.validate_layouts()
    assert why in str(error.value), (label, str(error.value))


def test_a_layout_missing_part_of_the_dpad_is_refused(emoji_table):
    bad = S.System(
        key="bad",
        name="Bad",
        core="gambatte",
        extensions=("zz",),
        rows=((S.SPACER, S.UP), (S.LEFT, S.DOWN)),
        confirm="up",
    )
    S.SYSTEMS = (bad,)
    with pytest.raises(ValueError, match="missing part of the d-pad"):
        S.validate_layouts()


# -- 4. Extensions ------------------------------------------------------------


def test_no_extension_is_ambiguous(system):
    for extension in system.extensions:
        assert extension not in S.AMBIGUOUS_EXTENSIONS


def test_every_extension_is_claimed_by_exactly_one_console():
    seen = {}
    for system in S.SYSTEMS:
        for extension in system.extensions:
            assert extension not in seen, f".{extension}: {seen[extension]} vs {system.key}"
            seen[extension] = system.key
    assert len(seen) == len(S.EXTENSIONS)


@pytest.mark.parametrize(
    "extension, key",
    [
        ("gb", "gb"), ("gbc", "gb"), ("nes", "nes"), ("sfc", "snes"), ("smc", "snes"),
        ("md", "genesis"), ("sms", "sms"), ("gg", "sms"), ("a26", "atari2600"),
        ("pce", "pce"), ("ws", "wswan"), ("ngp", "ngp"), ("vb", "vb"), ("gba", "gba"),
    ],
)
def test_extension_picks_the_right_console(extension, key):
    found = S.system_for_extension(extension)
    assert found is not None and found.key == key


@pytest.mark.parametrize("extension", ["bin", "cue", "iso", "chd", "fds", "m3u", "zip", "txt"])
def test_an_unsupported_extension_resolves_to_nothing(extension):
    assert S.system_for_extension(extension) is None


def test_extension_lookup_ignores_case_and_a_leading_dot():
    assert S.system_for_extension(".GBC").key == "gb"
    assert S.system_for_extension("NES").core == "fceumm"
    assert S.system_for_extension("nes").core == "fceumm"


# -- 5. Cores -----------------------------------------------------------------

WANT_CORES = {
    "gambatte", "mgba", "fceumm", "snes9x", "genesis_plus_gx", "stella2014",
    "mednafen_wswan", "mednafen_ngp", "mednafen_pce_fast", "mednafen_vb",
}


def test_exactly_the_recommended_core_set_is_shipped():
    assert set(S.CORES) == WANT_CORES


@pytest.mark.parametrize("core", ["nestopia", "mesen", "sameboy"])
def test_a_core_we_deliberately_do_not_ship_is_absent(core):
    assert core not in S.CORES


def test_a_core_that_runs_two_consoles_says_so():
    described = S.CORES["genesis_plus_gx"]
    assert "Genesis" in described and "Master System" in described


def test_core_filename_and_name_round_trip():
    assert S.core_filename("snes9x") == "snes9x_libretro.so"
    assert S.core_name_from_filename("/opt/cores/genesis_plus_gx_libretro.so") == "genesis_plus_gx"
    assert S.core_name_from_filename("/x/snes9x_libretro.so") == "snes9x"
    assert S.core_name_from_filename("mgba_libretro.dll") == "mgba"
    assert S.core_name_from_filename("fceumm") == "fceumm"
    assert S.core_name_from_filename("nestopia_libretro.so") is None


def test_extensions_for_core_spans_every_console_it_runs():
    assert set(S.extensions_for_core("genesis_plus_gx")) == {
        ".md", ".mdx", ".smd", ".gen", ".68k", ".sgd", ".sms", ".gg", ".sg"
    }


def test_system_for_core_finds_the_first_console():
    assert S.system_for_core("gambatte").key == "gb"
    assert S.system_for_core("nope") is None


# -- 6. Button emoji ----------------------------------------------------------
#
# Regression cover for the 400/50035 "Invalid emoji" that took out a whole
# command: the Replay button briefly used U+21BB, which is not an emoji at
# all. Python's unicodedata has no emoji properties, so systems.py carries a
# reviewed table and these checks hold it to it.


def test_validate_emoji_returns_every_renderable_emoji():
    assert S.validate_emoji() == S.all_button_emoji()


def test_the_emoji_set_is_exactly_the_six_the_cog_renders():
    # Four arrows, Wait and Replay. The Stop button, and its U+23F9, are gone.
    assert len(S.all_button_emoji()) == 6, S.all_button_emoji()


def test_no_stop_emoji_survives_anywhere():
    assert not hasattr(S, "STOP_EMOJI")
    assert 0x23F9 not in S.EMOJI_CODEPOINTS


@pytest.mark.parametrize("emoji", S.all_button_emoji())
def test_a_rendered_emoji_is_one_discord_will_accept(emoji):
    assert S.emoji_problem(emoji) is None, S.emoji_problem(emoji)
    base = emoji[0]
    assert unicodedata.category(base) == "So", unicodedata.category(base)
    assert unicodedata.name(base, ""), f"{emoji!r} has no Unicode name"
    # The table says whether this character needs a variation selector to
    # render as an emoji; the string must agree with it.
    needs = S.EMOJI_CODEPOINTS[ord(base)]
    assert needs == emoji.endswith(S.VARIATION_SELECTOR_16), repr(emoji)


@pytest.mark.parametrize(
    "value, why",
    [
        ("↻", "U+21BB is not an emoji at all -- the 50035 culprit"),
        ("⬆", "a bare U+2B06 needs its U+FE0F"),
        ("⏩️", "a redundant variation selector"),
        ("\U0001f468‍\U0001f4bb", "a ZWJ sequence"),
        ("", "an empty string"),
        (None, "not a string at all"),
        ("x", "plain text"),
        ("ab", "two characters"),
    ],
)
def test_an_unusable_emoji_is_refused(value, why):
    assert S.emoji_problem(value) is not None, why


def test_an_unreviewed_emoji_fails_validate_emoji(emoji_table):
    bad = S.Button("Bad", "a", emoji="↻")
    broken = S.System(
        key="bad",
        name="Bad",
        core="gambatte",
        extensions=("zz",),
        rows=((S.SPACER, S.UP), (S.LEFT, S.DOWN, S.RIGHT, bad)),
        confirm="a",
    )
    S.SYSTEMS = S.SYSTEMS + (broken,)
    with pytest.raises(ValueError, match="U\\+21BB"):
        S.validate_emoji()


def test_an_unused_entry_in_the_table_is_a_stale_allowlist(emoji_table):
    S.EMOJI_CODEPOINTS[0x1F600] = False
    with pytest.raises(ValueError, match="U\\+1F600"):
        S.validate_emoji()


def test_the_table_is_intact_after_the_tests_that_edit_it():
    assert S.validate_emoji() == S.all_button_emoji()
    assert S.validate_layouts() == tuple(x.key for x in S.SYSTEMS)
