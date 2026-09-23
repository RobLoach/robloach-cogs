"""Every console's controls, as the payload discord.py would really send.

This is the check that would have caught the 400 a control button's emoji
once caused: a real RetroView is built for every console and the dict
discord.py hands to the HTTP layer is inspected, rather than the tables being
trusted. Needs discord.py; the tables themselves are covered, with no
dependencies at all, in test_systems.py.
"""

import asyncio
import re
import types

import pytest

discord = pytest.importorskip("discord", reason="the view tests need discord.py")

from retro import RetroView as viewmod  # noqa: E402
from retro import systems as S  # noqa: E402
from retro import text as textmod  # noqa: E402
from retro import timing as timingmod  # noqa: E402

SYSTEM_KEYS = [system.key for system in S.SYSTEMS]


@pytest.fixture(params=SYSTEM_KEYS)
def system(request):
    return S.system_by_key(request.param)


@pytest.fixture
def view(retro, system):
    return viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename=f"test.{system.extensions[0]}",
        channel_id=1,
        system=system,
    )


def game_buttons(view):
    return [c for c in view.children if isinstance(c, viewmod._GameButton)]


def spacers(view):
    return [c for c in view.children if isinstance(c, viewmod._SpacerButton)]


# -- The wire payload ---------------------------------------------------------


def test_the_payload_fits_discord_s_grid(view, system):
    rows = view.to_components()
    assert len(rows) <= S.MAX_ACTION_ROWS
    buttons = []
    for row in rows:
        assert row["type"] == 1, row
        assert 1 <= len(row["components"]) <= S.MAX_BUTTONS_PER_ROW, row
        buttons.extend(row["components"])
    assert len(buttons) <= S.MAX_COMPONENTS, len(buttons)


def test_every_component_is_a_button_discord_will_accept(view, system):
    for row in view.to_components():
        for button in row["components"]:
            assert button["type"] == 2, button
            label, emoji = button.get("label"), button.get("emoji")
            # Discord refuses a non-link button with neither, and refuses an
            # empty or whitespace-only label outright.
            assert label or emoji, button
            if label is not None:
                assert isinstance(label, str) and 1 <= len(label) <= 80, repr(label)
                assert label.strip(), repr(label)
            if emoji is not None:
                assert S.emoji_problem(emoji["name"]) is None, emoji
            assert button["style"] in (1, 2, 3, 4), button["style"]


def test_custom_ids_are_unique_and_routable(view, system):
    ids = [
        button["custom_id"]
        for row in view.to_components()
        for button in row["components"]
    ]
    assert len(set(ids)) == len(ids), ids
    for custom_id in ids:
        assert 1 <= len(custom_id) <= 100, custom_id
        # Live messages route clicks by these exact strings, so the prefix is
        # not cosmetic: renaming it orphans every game in every channel.
        assert custom_id.startswith(f"{viewmod.CUSTOM_ID_PREFIX}:"), custom_id
    for button in system.buttons:
        assert f"{viewmod.CUSTOM_ID_PREFIX}:press:{button.field}" in ids, button


def test_the_prefix_is_still_libretro():
    assert viewmod.CUSTOM_ID_PREFIX == "libretro"


def test_the_view_is_persistent_even_with_spacers_in_it(view):
    assert view.is_persistent()


def test_a_spacer_is_inert_on_the_wire(view):
    for row in view.to_components():
        for button in row["components"]:
            if ":spacer:" in (button.get("custom_id") or ""):
                assert button["disabled"], button


def test_there_is_no_stop_button_any_more(view):
    ids = [c.custom_id for c in view.children]
    assert not any(i.endswith(":stop") for i in ids), ids


# -- The controls, as objects -------------------------------------------------


def test_only_this_console_s_own_buttons_are_offered(view, system):
    assert {c.field for c in game_buttons(view)} == set(system.fields)


def test_labels_map_to_the_right_retropad_fields(view, system):
    labels = {c.label: c.field for c in game_buttons(view) if c.label}
    assert labels == {b.label: b.field for b in system.buttons if b.label}


def test_the_dpad_is_four_emoji_only_buttons(view):
    arrows = [c for c in game_buttons(view) if c.field in ("up", "down", "left", "right")]
    assert len(arrows) == 4
    assert all(not c.label and c.emoji for c in arrows), [(c.field, c.label) for c in arrows]


def test_the_dpad_reads_as_a_cross(view):
    row0 = [c for c in view.children if c.row == 0]
    row1 = [c for c in view.children if c.row == 1]
    assert isinstance(row0[0], viewmod._SpacerButton)
    assert getattr(row0[1], "field", None) == "up"
    assert [getattr(c, "field", None) for c in row1[:3]] == ["left", "down", "right"]


def test_every_spacer_is_disabled_labelled_and_not_a_game_button(view):
    for spacer in spacers(view):
        assert spacer.disabled and spacer.label
        assert not isinstance(spacer, viewmod._GameButton)


def test_the_controls_share_the_last_row(view):
    last = max(c.row for c in view.children)
    for name in ("wait", "repeat", "undo"):
        button = next(
            c for c in view.children if c.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:{name}"
        )
        assert button.row == last, (name, button.row, last)


#: console key -> (components, action rows, the last row's labels) **at the
#: default one second clip**, which is where the repeat button is drawn.
#: test_the_repeat_button_is_drawn_only_when_it_can_do_something below walks
#: the same table across every clip length; the rows and the labels either
#: side of the repeat button are the same at all of them.
#:
#: The whole point of the table: removing Replay took the control cluster back
#: to three wide, which fits beside every console's bottom row -- so all nine
#: consoles' controls share that row again, the widest layouts are back to
#: four of Discord's five action rows, and every console is one component and
#: (on six of the nine -- the ones whose bottom row is two wide) one row
#: smaller than it was. Anything that changes this has changed what every
#: player sees.
LAYOUTS = {
    "gb": (12, 3, ["Start", "Select", "Wait", "A x3", "Undo"]),
    "gba": (14, 3, ["Start", "Select", "Wait", "A x3", "Undo"]),
    "nes": (12, 3, ["Start", "Select", "Wait", "A x3", "Undo"]),
    "snes": (19, 4, ["Start", "Select", "Wait", "A x3", "Undo"]),
    "genesis": (18, 4, ["Mode", "Start", "Wait", "B x3", "Undo"]),
    "pce": (18, 4, ["Select", "Run", "Wait", "I x3", "Undo"]),
    "sms": (11, 3, ["Pause", "Wait", "1 x3", "Undo"]),
    # The Game Gear's pad is the Master System's, but the button beside its
    # screen is START -- Pause is a button on the SMS console deck, which a
    # handheld does not have.
    "gg": (11, 3, ["Start", "Wait", "1 x3", "Undo"]),
    "ngp": (11, 3, ["Option", "Wait", "A x3", "Undo"]),
}


def test_every_console_s_layout(view, system):
    components, rows, last_row = LAYOUTS[system.key]
    payload = view.to_components()
    assert len(view.children) == components, len(view.children)
    assert len(payload) == rows, len(payload)
    assert sum(len(row["components"]) for row in payload) == components
    labels = [
        button.get("label") or button["custom_id"]
        for button in payload[-1]["components"]
    ]
    assert labels == last_row, labels
    # Whatever else moved, it still fits: five rows of five, 25 components.
    assert rows <= S.MAX_ACTION_ROWS
    assert all(len(row["components"]) <= S.MAX_BUTTONS_PER_ROW for row in payload)
    assert components <= S.MAX_COMPONENTS


def test_the_undo_button_is_one_of_the_controls_and_has_its_own_id(view):
    undo = next(c for c in view.children if isinstance(c, viewmod._UndoButton))
    assert undo.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:undo"
    assert undo.label == "Undo" and undo.emoji is not None
    assert str(undo.emoji) == S.UNDO_EMOJI
    # Nothing was taken away to make room for it, and nothing was renamed:
    # live messages route clicks by these exact strings.
    ids = {c.custom_id for c in view.children}
    for name in ("wait", "repeat", "undo"):
        assert f"{viewmod.CUSTOM_ID_PREFIX}:{name}" in ids, name
    # It starts *enabled*, even though a fresh session has nothing to undo.
    # It used to be greyed out for that case, which made the explanation
    # unreachable: a disabled Discord button cannot be clicked, so the
    # private "there is nothing to undo yet, and here is why" line could
    # never be got at -- and a permanently dead control reads as a broken
    # one. See _UndoButton and RetroView._undo.
    assert not undo.disabled
    assert not view.history, "and there really is nothing to undo yet"


def test_the_repeat_button_taps_this_console_s_confirm_button(view, system):
    repeat = next(
        c for c in view.children if c.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:repeat"
    )
    assert repeat.field == system.confirm
    assert repeat.label == f"{system.label_for(system.confirm)} x{viewmod.REPEAT_TAPS}"


# -- When the repeat button exists ---------------------------------------------
#
# It is *drawn only when it can do something*: below two taps it does no more
# than the console's own confirm button one row over, and it used to be drawn
# and greyed out for that case -- which was read as the feature having been
# removed from the cog. Now it is simply not there, and it comes back on the
# next press at a longer clip.
#
# "Not there" is on the wire only. The button stays a child of the view at
# every clip length, because that is what discord.py builds its routing table
# from: a message Discord has not re-rendered still shows the button, and a
# click on a custom_id no view has is dropped without ever being
# acknowledged, which is what puts "This interaction failed" in front of the
# player. So the assertions below are about `to_components()` -- the payload
# -- and the object is asserted to still be there and to know it is hidden.
#
# The table is every clip length that changes the answer, against every
# console, because this is what every player's controller looks like. The
# boundaries (measured at DEFAULT_FPS with the default 160ms hold) are 0.48s
# for the second tap and 0.68s for the third.

#: clip seconds -> (taps, whether the button is drawn, the "xN" it says)
REPEAT_BY_LENGTH = [
    (0.2, 1, False, None),   # the settings floor
    (0.3, 1, False, None),
    (0.4, 1, False, None),
    (0.47, 1, False, None),  # the last length with only one tap
    (0.48, 2, True, "x2"),   # ...and the first with two
    (0.5, 2, True, "x2"),
    (0.67, 2, True, "x2"),
    (0.68, 3, True, "x3"),   # the first with all three
    (0.8, 3, True, "x3"),
    (1.0, 3, True, "x3"),    # the default
    (4.0, 3, True, "x3"),
    (5.0, 3, True, "x3"),    # the settings ceiling
]


@pytest.mark.parametrize(
    "seconds, taps, drawn, suffix", REPEAT_BY_LENGTH, ids=[str(r[0]) for r in REPEAT_BY_LENGTH]
)
def test_the_repeat_button_is_drawn_only_when_it_can_do_something(
    retro, system, seconds, taps, drawn, suffix
):
    view = viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename=f"test.{system.extensions[0]}",
        channel_id=1,
        system=system,
        clip_seconds=seconds,
    )
    button = next(
        (c for c in view.children if isinstance(c, viewmod._RepeatButton)), None
    )
    assert view.repeat_taps == taps, (system.key, seconds)
    assert (view.repeat_taps >= viewmod.MIN_REPEAT_TAPS) is drawn
    # The object is there at every clip length, so a click on a stale copy of
    # the button has somewhere to land; only the payload changes.
    assert button is not None, (system.key, seconds)
    assert button.hidden is not drawn, (system.key, seconds)
    assert f"{viewmod.CUSTOM_ID_PREFIX}:repeat" in {c.custom_id for c in view.children}
    assert button.label.endswith(f" x{taps}"), button.label
    assert not button.disabled, "hidden or not, it is never a greyed-out button"
    if drawn:
        assert button.label.endswith(f" {suffix}"), button.label

    # Whatever the clip length, the layout is still one Discord will take,
    # and Wait and Undo have not moved: the row reserves space for all three
    # controls whether or not the third is drawn.
    payload = view.to_components()
    expected_components, expected_rows, full_row = LAYOUTS[system.key]
    assert len(payload) == expected_rows, (system.key, seconds)
    assert all(len(row["components"]) <= S.MAX_BUTTONS_PER_ROW for row in payload)
    assert len(view.children) <= S.MAX_COMPONENTS
    assert len(view.children) == expected_components
    on_the_wire = sum(len(row["components"]) for row in payload)
    assert on_the_wire == expected_components - (0 if drawn else 1)
    wire_ids = {b["custom_id"] for row in payload for b in row["components"]}
    assert (f"{viewmod.CUSTOM_ID_PREFIX}:repeat" in wire_ids) is drawn
    confirm = system.label_for(system.confirm)
    wanted = []
    for label in full_row:
        if label.startswith(f"{confirm} x"):
            if drawn:
                wanted.append(f"{confirm} {suffix}")
            continue
        wanted.append(label)
    labels = [b.get("label") or b["custom_id"] for b in payload[-1]["components"]]
    assert labels == wanted, (system.key, seconds, labels)
    # Undo is last either way, and the repeat button sits between Wait and
    # Undo when it is there at all.
    assert labels[-1] == "Undo"
    assert labels[-2] == (f"{confirm} {suffix}" if drawn else "Wait")


def test_changing_the_clip_length_adds_and_removes_the_button_in_place(view, system):
    """Both directions, and the button lands back in its proper place.

    A Discord action row is ordered by insertion, so a button that really
    left the row and came back would land after Undo and read "Wait Undo
    A x3". It never leaves it: only the payload changes, so its seat between
    Wait and Undo is kept for it. See RetroView._update_repeat_label.
    """
    confirm = system.label_for(system.confirm)
    full = [b.get("label") for b in view.to_components()[-1]["components"]]
    assert f"{confirm} x3" in full

    view.clip_seconds = 0.2
    view._update_repeat_label()
    short = [b.get("label") for b in view.to_components()[-1]["components"]]
    assert short == [label for label in full if label != f"{confirm} x3"]

    view.clip_seconds = 0.5
    view._update_repeat_label()
    two = [b.get("label") for b in view.to_components()[-1]["components"]]
    assert two == [f"{confirm} x2" if label == f"{confirm} x3" else label for label in full]

    view.clip_seconds = 1.0
    view._update_repeat_label()
    assert [b.get("label") for b in view.to_components()[-1]["components"]] == full
    # And it is still a view Discord will register for a message.
    assert view.is_persistent()
    ids = [c.custom_id for c in view.children]
    assert len(set(ids)) == len(ids), ids


# -- Saying which button was pressed ------------------------------------------
#
# Every press names itself on the message it edits, and the name comes from
# the console's own Button table in systems.py rather than from a second one
# in RetroView -- which is the whole point, because the RetroPad field a
# button maps to is frequently *not* what the console calls it. The table
# below is the per-console answer, written out in full: an entry that changes
# has changed what a player reads after every press.


#: console key -> {RetroPad field: the line a press of it puts on the message}
#: for every button that console has, the four d-pad arrows included.
PRESS_LINES = {
    "gb": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "b": "Pressed B.", "a": "Pressed A.",
        "start": "Pressed Start.", "select": "Pressed Select.",
    },
    "gba": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "l": "Pressed L.", "r": "Pressed R.",
        "b": "Pressed B.", "a": "Pressed A.",
        "start": "Pressed Start.", "select": "Pressed Select.",
    },
    "nes": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "b": "Pressed B.", "a": "Pressed A.",
        "start": "Pressed Start.", "select": "Pressed Select.",
    },
    "snes": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "l": "Pressed L.", "r": "Pressed R.",
        "y": "Pressed Y.", "x": "Pressed X.",
        "b": "Pressed B.", "a": "Pressed A.",
        "start": "Pressed Start.", "select": "Pressed Select.",
    },
    # The Genesis pad's A/B/C are RetroPad y/b/a and its X/Y/Z are x/l/r, so
    # every one of these would read wrongly if the line were built from the
    # RetroPad's own names: a press of the Genesis C would say "Pressed A."
    "genesis": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "x": "Pressed X.", "l": "Pressed Y.", "r": "Pressed Z.",
        "y": "Pressed A.", "b": "Pressed B.", "a": "Pressed C.",
        "start": "Pressed Start.", "select": "Pressed Mode.",
    },
    "sms": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "b": "Pressed 1.", "a": "Pressed 2.",
        "start": "Pressed Pause.",
    },
    # Same two buttons as the Master System above, and the same core -- but
    # its own console, so `start` is its own Start rather than the SMS's
    # console-deck Pause.
    "gg": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "b": "Pressed 1.", "a": "Pressed 2.",
        "start": "Pressed Start.",
    },
    "pce": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "x": "Pressed IV.", "l": "Pressed V.", "r": "Pressed VI.",
        "y": "Pressed III.", "a": "Pressed II.", "b": "Pressed I.",
        "start": "Pressed Run.", "select": "Pressed Select.",
    },
    # A and B the right way round, i.e. swapped from the RetroPad convention.
    "ngp": {
        "up": "Pressed ⬆️.", "down": "Pressed ⬇️.",
        "left": "Pressed ⬅️.", "right": "Pressed ➡️.",
        "b": "Pressed A.", "a": "Pressed B.",
        "start": "Pressed Option.",
    },
}


def test_every_button_of_every_console_names_itself(view, system):
    expected = PRESS_LINES[system.key]
    # The table covers this console exactly: no button missing, none invented.
    assert set(expected) == set(system.fields), (
        sorted(set(expected) ^ set(system.fields))
    )
    for field, line in expected.items():
        assert view.press_note(field) == line, field


def test_the_press_line_is_the_label_the_button_itself_carries(view, system):
    """The anti-drift assertion: one source, which is systems.py.

    Not a restatement of the table above -- this one says the line and the
    component are built from the same Button, so a console renamed in
    systems.py cannot end up with controls saying one thing and the message
    saying another.
    """
    for button in game_buttons(view):
        caption = button.label or str(button.emoji)
        assert view.press_note(button.field) == f"Pressed {caption}."


def test_the_dpad_names_itself_with_an_emoji_discord_accepts(view, system):
    """The class of bug that caused a production 400, held off here too.

    The d-pad carries no label, so its line is its emoji -- which means a
    bare codepoint with no U+FE0F would be posted as message *text* rather
    than on a button. It cannot be: these are the same strings the buttons
    carry, and systems.validate_emoji() has already refused that at import.
    """
    for field in ("up", "down", "left", "right"):
        line = view.press_note(field)
        emoji = line[len("Pressed "):-1]
        assert S.emoji_problem(emoji) is None, (field, emoji)
        assert emoji.endswith(S.VARIATION_SELECTOR_16), (field, emoji)


def test_wait_and_the_repeat_button_say_what_they_do(view, system):
    assert view.press_note(None) == "Waited."
    # The repeat button counts the taps that will really happen, exactly as
    # its own label does, so the line and the button cannot disagree.
    repeat = next(
        c for c in view.children if c.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:repeat"
    )
    confirm = system.caption_for(system.confirm)
    assert view.press_note(system.confirm, viewmod.REPEAT_TAPS) == (
        f"Pressed {confirm} x{view.repeat_taps}."
    )
    assert repeat.label.endswith(f"x{view.repeat_taps}")

    # A clip too short for two taps: the button is not drawn at all, and a
    # click on a stale one still on an un-redrawn message drops the count
    # rather than claiming a repeat that did not happen.
    view.clip_seconds = 0.2
    view._update_repeat_label()
    assert view.repeat_taps == 1
    assert view._repeat_button().hidden, "hidden from the payload..."
    assert view._repeat_button() in view.children, "...but still routable"
    assert view.press_note(system.confirm, viewmod.REPEAT_TAPS) == f"Pressed {confirm}."


def test_a_field_this_console_does_not_have_still_reads_as_something(view):
    """A stale click, i.e. a message drawn for a different game."""
    assert view.press_note("nonexistent") == "Pressed NONEXISTENT."


def test_the_press_line_matches_the_tone_of_the_other_notes():
    # One short sentence, capitalised, full stop, no markdown: the same shape
    # as the line Undo has always shown.
    for _named, plain in viewmod.ACTION_NOTES.values():
        note = plain.format(button="A")
        assert note[0].isupper() and note.endswith("."), note
        assert "*" not in note and "`" not in note, note
        assert len(note) <= 40, note


# -- Who did it ----------------------------------------------------------------
#
# Every action names its author as well as itself, in one voice for all four
# of them, and the author's name is a string somebody else chose -- so the
# two things checked here are the wording (a table over ACTION_NOTES) and the
# sanitising (a table of hostile display names). The third property, that a
# line can never notify anybody, is checked against real edits in
# test_cog_session.py; what is checked here is the half of it that is a
# property of the string: no mention syntax is ever emitted.
#
# The names themselves live in retro/text.py now -- every line this cog
# writes and the sanitising that goes into one -- so that is what this
# section calls. RetroView re-exports all of it, which
# test_every_name_that_moved_is_still_reachable_from_the_view holds.


class Named:
    """The least a presser can be: something with a display_name."""

    def __init__(self, **attributes):
        for name, value in attributes.items():
            setattr(self, name, value)


#: action -> (the line with a name, the line without one). Written out rather
#: than read from ACTION_NOTES, because "the wording is what it is" is the
#: assertion: a change here is a change to what a player reads after every
#: single press.
ACTION_LINES = {
    "press": ("Rob pressed A.", "Pressed A."),
    "wait": ("Rob waited.", "Waited."),
    "undo": ("Rob undid the last press.", "Undid the last press."),
    "reset": ("Rob reset the game.", "Reset the game."),
}


@pytest.mark.parametrize("action", sorted(ACTION_LINES))
def test_every_action_reads_in_one_voice(action):
    named, plain = ACTION_LINES[action]
    assert textmod.action_note(action, Named(display_name="Rob"), "A") == named
    # Nobody to name: the impersonal form of the very same sentence, never an
    # empty line and never a stray leading space.
    assert textmod.action_note(action, None, "A") == plain
    assert set(ACTION_LINES) == set(textmod.ACTION_NOTES), "an action was added"
    for line in (named, plain):
        assert line[0].isupper() and line.endswith("."), line
        assert "  " not in line and line == line.strip(), repr(line)


def test_the_four_actions_are_the_four_the_view_can_perform():
    """One table, so the lines a session can show cannot drift apart."""
    for named, plain in textmod.ACTION_NOTES.values():
        assert "{who}" in named and "{who}" not in plain
        # The verb is lower case in the named form ("Rob pressed A.") and
        # capitalised in the impersonal one ("Pressed A."), which is the only
        # difference between them.
        assert named.split()[1][0].islower(), named


#: display name -> what goes on the line. Everything a name can contain that
#: would otherwise change the shape of the message.
SANITISED = {
    "Rob": "Rob",
    # Markdown of every kind, escaped unconditionally rather than "as
    # needed", so no pairing trick gets through.
    "**Rob**": "\\*\\*Rob\\*\\*",
    "`Rob`": "\\`Rob\\`",
    "~~Rob~~": "\\~\\~Rob\\~\\~",
    "||Rob||": "\\|\\|Rob\\|\\|",
    "R\\o*b": "R\\\\o\\*b",
    "[Rob](http://x)": "\\[Rob\\]\\(http://x\\)",
    # A URL in a name is not a link to be left alone, which is what
    # discord.utils.escape_markdown would have done with it.
    "http://x/__a__": "http://x/\\_\\_a\\_\\_",
    # Markdown that is only markdown at the start of a line, which is exactly
    # where the name sits.
    "# Rob": "\\# Rob",
    "- Rob": "\\- Rob",
    "> Rob": "\\> Rob",
    "+ Rob": "\\+ Rob",
    "1. Rob": "1\\. Rob",
    "Jean-Luc": "Jean\\-Luc",
    # Mass mentions and a real user mention: no pingable syntax survives.
    "@everyone": "@\u200beveryone",
    "@here": "@\u200bhere",
    "<@1234567890123456789>": "\\<@\u200b1234567890123456789\\>",
    "<@&1234567890123456789>": "\\<@\u200b&1234567890123456789\\>",
    # Angle brackets in general: a custom emoji and a timestamp would both
    # have rendered.
    "<:evil:1234567890123456789>": "\\<:evil:1234567890123456789\\>",
    "<t:0:R>": "\\<t:0:R\\>",
    # Invisible characters: dropped, because a name made of them is not a
    # name.
    "Ro\u200bb": "Rob",
    "Rob\ufeff": "Rob",
    "Ro\u00adb": "Rob",
    "\u202eRob": "Rob",
    "\u200b\u200b\u200b": "",
    # Whitespace: collapsed to single spaces, so one line stays one line.
    "Rob\nLoach": "Rob Loach",
    "  Rob  Loach  ": "Rob Loach",
    "Rob\tLoach": "Rob Loach",
    " ": "",
    "": "",
}


@pytest.mark.parametrize("raw", sorted(SANITISED))
def test_a_display_name_is_made_safe_before_it_goes_on_the_line(raw):
    assert textmod.presser_name(Named(display_name=raw)) == SANITISED[raw]


def test_a_very_long_display_name_cannot_own_the_line():
    long = textmod.presser_name(Named(display_name="R" * 200))
    assert len(long) == textmod.MAX_PRESSER_NAME
    assert long.endswith("\N{HORIZONTAL ELLIPSIS}")
    # 32 is Discord's own ceiling for a nickname, so a real name is never
    # cut; the whole line stays short either way.
    assert textmod.MAX_PRESSER_NAME == 32
    line = textmod.action_note("undo", Named(display_name="R" * 200))
    assert len(line) <= textmod.MAX_PRESSER_NAME + 30, line


#: Names crafted to notify somebody, or to break the line, or both.
HOSTILE_NAMES = sorted(SANITISED) + [
    "@everyone @here <@1234567890123456789>",
    "\u200b@everyone\u200b",
    "**@everyone**",
    "#\u00ad @everyone",
    "<@!1234567890123456789>",
    "@" + "1" * 19,
    "`@everyone`",
    "[@everyone](http://x)",
    "- @here\n- @here",
    "R" * 200 + "@everyone",
]


@pytest.mark.parametrize("raw", HOSTILE_NAMES)
def test_no_line_can_ever_emit_mention_syntax(view, raw):
    """The half of "a press never pings" that is a property of the string.

    Nothing Discord resolves into a notification survives: ``@everyone`` and
    ``@here`` come back with a zero-width space wedged into them, and every
    ``<`` is backslash-escaped so no ``<@id>`` can form. The other half --
    that the edit also carries an AllowedMentions suppressing everything --
    is checked against real edits in test_cog_session.py.
    """
    user = Named(display_name=raw)
    lines = [
        view.press_note("a", 1, user),
        view.press_note(None, 1, user),
        view.press_note("a", viewmod.REPEAT_TAPS, user),
        view.undo_note(user),
        view.reset_note(user),
    ]
    for line in lines:
        assert "@everyone" not in line, line
        assert "@here" not in line, line
        # No un-escaped `<`, so none of the `<@id>`/`<@&id>`/`<#id>` family
        # can be parsed out of it.
        assert not re.search(r"(?<!\\)<", line), line
        # Still one line, still one sentence.
        assert "\n" not in line and line.endswith("."), line


def test_anybody_the_cog_can_be_handed_is_named_or_gracefully_not():
    """The awkward callers, which are all real.

    A Member has a per-guild nickname; a plain User (a DM, or somebody who
    has left the guild between clicking and being looked up) has a global
    display name or just a username; and a stripped-down object with none of
    them at all must still produce a sentence rather than a traceback or a
    line beginning with a space.
    """
    # A Member: display_name is the nickname, and is preferred.
    member = Named(display_name="Robbo", global_name="Rob Loach", name="robloach")
    assert textmod.presser_name(member) == "Robbo"
    # A User with no nickname anywhere: discord.py's User.display_name
    # already falls back to global_name, but an object that only carries one
    # of the two is still named.
    assert textmod.presser_name(Named(global_name="Rob Loach")) == "Rob Loach"
    assert textmod.presser_name(Named(name="robloach")) == "robloach"
    # Somebody who has left the guild: discord.py still hands over a Member
    # or User object, with no guild_permissions on the User case -- which is
    # not something naming them depends on.
    left = Named(display_name="Gone", id=7)
    assert not hasattr(left, "guild_permissions")
    assert textmod.presser_name(left) == "Gone"
    # Nothing nameable at all, in every shape it can arrive in.
    for nobody in (None, Named(), Named(display_name=""), Named(display_name=None),
                   Named(display_name="\u200b"), object()):
        assert textmod.presser_name(nobody) == ""
        assert textmod.action_note("press", nobody, "A") == "Pressed A."


def test_a_named_press_uses_the_console_s_own_button_name(view, system):
    """The attribution does not disturb the half that comes from systems.py."""
    user = Named(display_name="Rob")
    for field, line in PRESS_LINES[system.key].items():
        # "Pressed X." -> "Rob pressed X."
        expected = f"Rob pressed {line[len('Pressed '):]}"
        assert view.press_note(field, 1, user) == expected, field


# -- The consoles whose buttons are not what the RetroPad calls them -----------


def test_the_genesis_a_is_not_the_retropad_a(retro):
    view = viewmod.RetroView(
        retro.cog,
        game_name="X",
        slug="x",
        rom_filename="x.md",
        channel_id=1,
        system=S.system_by_key("genesis"),
    )
    labels = {c.label: c.field for c in game_buttons(view)}
    assert labels["A"] == "y"
    assert labels["C"] == "a"


def test_the_neo_geo_pocket_a_is_the_retropad_b(retro):
    view = viewmod.RetroView(
        retro.cog,
        game_name="X",
        slug="x",
        rom_filename="x.ngp",
        channel_id=1,
        system=S.system_by_key("ngp"),
    )
    assert {c.label: c.field for c in game_buttons(view)}["A"] == "b"


# -- Layouts the view itself refuses ------------------------------------------


def test_a_layout_with_no_room_for_the_controls_is_refused(retro):
    bad = S.System(
        key="bad",
        name="Bad",
        core="gambatte",
        extensions=("zz",),
        rows=tuple(((S.Button("Q", "a"),),) * 5),
        confirm="a",
    )
    with pytest.raises(ValueError):
        viewmod.RetroView(
            retro.cog, game_name="X", slug="x", rom_filename="x.zz", channel_id=1, system=bad
        )


# -- Clicks on buttons that no longer exist -----------------------------------


def test_a_click_on_a_removed_button_is_dropped_silently():
    # An old message still has the Stop button drawn on it. discord.py's
    # ViewStore resolves the click to a custom_id no view has any more and
    # must drop it rather than raise.
    store = discord.ui.view.ViewStore(None)

    class FakeMessage:
        id = 12345

    class FakeInteraction:
        message = FakeMessage()
        data = {}

    store.dispatch_view(2, f"{viewmod.CUSTOM_ID_PREFIX}:stop", FakeInteraction())


def test_a_hidden_repeat_button_is_still_registered_for_routing(retro, system):
    """The half of hiding that a removal could never have.

    discord.py builds its routing table by walking a view's *children*
    (ViewStore.add_view) and builds the message from ``to_components()``, so
    a button that is a child and not in the payload is reachable and
    invisible at the same time -- which is exactly what a stale message
    needs. A button that had been taken out of the view would resolve to a
    custom_id nothing is registered for, and ViewStore.dispatch_view returns
    without acknowledging the interaction at all: three seconds later the
    player is shown Discord's "This interaction failed".
    """
    view = viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename=f"test.{system.extensions[0]}",
        channel_id=1,
        system=system,
        clip_seconds=0.2,
        message_id=4343,
    )
    assert view.repeat_taps == 1 and view._repeat_button().hidden

    store = discord.ui.view.ViewStore(None)
    store.add_view(view, 4343)
    key = (2, f"{viewmod.CUSTOM_ID_PREFIX}:repeat")
    assert key in store._views.get(4343, {}), sorted(store._views.get(4343, {}))
    # ...and it is not on the wire, so nobody is shown a dead "A x1".
    drawn = {b["custom_id"] for row in view.to_components() for b in row["components"]}
    assert f"{viewmod.CUSTOM_ID_PREFIX}:repeat" not in drawn
    # Registering the view again after it has been hidden must not unregister
    # it: discord.py pops the custom_ids a view has *stopped* carrying
    # (ViewStore.add_view's snapshot diff), which is precisely what taking
    # the item out of the view would have done on the next message edit.
    store.add_view(view, 4343)
    assert key in store._views.get(4343, {})
    assert view.is_persistent()


async def test_a_click_on_a_hidden_repeat_button_is_answered_privately(retro, system):
    """A stale click costs one ephemeral line and no edit of the message.

    Not an edit, because any edit re-renders the message and rewinds the clip
    that is playing on it for everybody in the channel (see
    RetroView._ack_now), and not a press either: pressing the confirm button
    on somebody's behalf is doing something they did not ask for.
    """
    view = viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename=f"test.{system.extensions[0]}",
        channel_id=1,
        system=system,
        clip_seconds=0.2,
    )
    interaction = retro.interaction(view)
    await view._repeat_button().callback(interaction)

    assert interaction.kinds() == ["response.send_message"]
    said = interaction.log[-1][1]
    assert said["ephemeral"] is True
    confirm = system.label_for(system.confirm)
    assert f"**{confirm}**" in said["content"], said["content"]
    assert "cliplength" in said["content"]
    # No count is claimed: the button on their screen says x2 or x3 depending
    # on when that message was last drawn, and the view cannot know which.
    assert f"{confirm} x" not in said["content"], said["content"]


def test_a_real_button_still_routes_to_its_view(retro):
    view = viewmod.RetroView(
        retro.cog,
        game_name="X",
        slug="x",
        rom_filename="x.gb",
        channel_id=1,
        system=S.system_by_key("gb"),
        message_id=4242,
    )
    store = discord.ui.view.ViewStore(None)
    store.add_view(view, 4242)
    routed = store._views.get(4242, {})
    assert (2, f"{viewmod.CUSTOM_ID_PREFIX}:press:a") in routed, sorted(routed)
    # The spacers are registered too, and are inert.
    assert any(key[1].startswith(f"{viewmod.CUSTOM_ID_PREFIX}:spacer:") for key in routed)


# -- Defaults -----------------------------------------------------------------


def test_the_hold_and_clip_defaults():
    from retro.emulator import CLIP_SECONDS, MAX_CLIP_SECONDS, MIN_CLIP_SECONDS

    # One second, not four: a turn is press -> watch -> press, and three of
    # those four seconds were the game sitting still after the press.
    assert CLIP_SECONDS == 1.0
    assert (MIN_CLIP_SECONDS, MAX_CLIP_SECONDS) == (0.2, 5.0)
    assert isinstance(CLIP_SECONDS, float), "the clip length is fractional now"
    assert viewmod.DEFAULT_HOLD_MS == 160
    assert (viewmod.MIN_HOLD_MS, viewmod.MAX_HOLD_MS) == (50, 2000)


def test_a_clip_length_is_clamped_and_may_be_fractional():
    from retro.emulator import CLIP_SECONDS, clamp_clip_seconds

    assert clamp_clip_seconds(0.8) == 0.8
    assert clamp_clip_seconds(0.25) == 0.25
    # An installation that set an integer before the setting was a float.
    assert clamp_clip_seconds(4) == 4.0 and isinstance(clamp_clip_seconds(4), float)
    assert clamp_clip_seconds(0) == 0.2, "no clip of no frames"
    assert clamp_clip_seconds(-5) == 0.2
    assert clamp_clip_seconds(9999) == 5.0
    # Hundredths, so the number can be printed straight back out.
    assert clamp_clip_seconds(0.8004) == 0.8
    # Nonsense in Config must not take a session down with it.
    for junk in (None, "", "soon", float("nan"), float("inf")):
        assert clamp_clip_seconds(junk) == CLIP_SECONDS


def test_a_clip_length_is_printed_without_a_pointless_decimal():
    from retro.emulator import describe_seconds, format_seconds

    assert format_seconds(1.0) == "1"
    assert format_seconds(0.8) == "0.8"
    assert format_seconds(0.25) == "0.25"
    assert format_seconds(15.0) == "15"
    # An approximate figure rather than a setting, where round() to whole
    # seconds used to turn 0.4 seconds of footage into "0s".
    assert format_seconds(0.4018, 1) == "0.4"
    assert format_seconds(3.0135, 1) == "3"
    assert describe_seconds(1.0) == "1 second"
    assert describe_seconds(0.8) == "0.8 seconds"
    assert describe_seconds(4) == "4 seconds"


def test_the_default_hold_is_under_a_game_boy_walk_cycle():
    # 16 frames (~270ms), so a longer hold walks two tiles for one press;
    # see test_emulator.py's tile count.
    assert round(59.727 * viewmod.DEFAULT_HOLD_MS / 1000) < 16


# -- Fitting a press into a clip ----------------------------------------------
#
# Gambatte's real frame rate, so this is the arithmetic that runs in
# production rather than a tidy 60. No core is needed: press_plan and friends
# are plain functions of a frame rate -- which is exactly why they live in
# retro/timing.py rather than on the view, and why this section asks that
# module directly.

GB_FPS = 59.727

#: clip seconds -> (emulated frames, last frame input may be released on)
CLIP_SHAPES = {0.2: (12, 8), 0.5: (30, 28), 0.8: (48, 44), 1.0: (60, 56), 4.0: (239, 236)}


@pytest.mark.parametrize("seconds", sorted(CLIP_SHAPES))
def test_a_clip_is_this_many_frames_and_leaves_a_picture_for_the_aftermath(seconds):
    from retro import emulator as E

    frames, budget = CLIP_SHAPES[seconds]
    assert E.clip_frame_count(GB_FPS, seconds) == frames
    assert E.input_budget(GB_FPS, frames) == budget
    step = E.capture_step(GB_FPS)
    assert step == 4, "15fps against a 59.73fps core is every fourth frame"
    # The budget is a *captured* frame, which is the point of it: releasing a
    # button on frame 59 of a 60 frame clip would never be photographed,
    # because the last picture was taken on frame 56.
    assert budget % step == 0 and budget <= frames - 1
    assert frames - budget <= step, "no more of the clip is reserved than has to be"


def test_a_clip_is_never_fewer_frames_than_an_animation_needs():
    from retro import emulator as E

    step = E.capture_step(60.0)
    pictures = -(-E.MIN_CLIP_FRAMES // step)
    assert pictures >= 2, "a one-picture 'animation' is a screenshot"
    assert E.input_budget(60.0, E.MIN_CLIP_FRAMES) >= 1, "and room for a press"
    assert E.clip_frame_count(60.0, 0.001) == E.MIN_CLIP_FRAMES
    assert E.clip_frame_count(1.0, 0.2) == E.MIN_CLIP_FRAMES, "a nonsense fps"
    # ...and the floor never bites at a length the settings can reach, on any
    # console here (50fps PAL through 60.10fps NES).
    for fps in (50.0, 59.727, 60.0, 60.0988):
        assert E.clip_frame_count(fps, E.MIN_CLIP_SECONDS) > E.MIN_CLIP_FRAMES
        assert E.input_budget(fps, E.clip_frame_count(fps, E.MIN_CLIP_SECONDS)) >= 8


@pytest.mark.parametrize(
    "seconds, expected",
    [
        # Four seconds: exactly what it always did, three taps 250ms apart.
        (4.0, [(0, 10), (25, 10), (50, 10)]),
        # One second: 3 x 160ms + 2 x 250ms is 1.4s of schedule, so the
        # spacing is squeezed to 13 frames (218ms) and all three taps stay.
        (1.0, [(0, 10), (23, 10), (46, 10)]),
        (0.8, [(0, 10), (17, 10), (34, 10)]),
        # Half a second cannot fit three, even touching, so it does two.
        (0.5, [(0, 10), (18, 10)]),
        # A fifth of a second fits one tap, and the hold itself is cut from
        # ten frames to the eight the clip can show being released.
        (0.2, [(0, 8)]),
    ],
)
def test_three_taps_are_squeezed_then_dropped_to_fit_the_clip(seconds, expected):
    from retro import emulator as E

    plan = timingmod.press_plan(GB_FPS, seconds, timingmod.DEFAULT_HOLD_MS, timingmod.REPEAT_TAPS)
    assert plan == expected
    budget = E.input_budget(GB_FPS, E.clip_frame_count(GB_FPS, seconds))
    assert max(start + hold for start, hold in plan) <= budget
    gaps = [b[0] - (a[0] + a[1]) for a, b in zip(plan, plan[1:], strict=False)]
    assert all(gap >= E.frame_count(GB_FPS, timingmod.MIN_REPEAT_GAP_MS / 1000) for gap in gaps)


@pytest.mark.parametrize("seconds", [0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.0, 4.0, 5.0])
@pytest.mark.parametrize("hold_ms", [50, 160, 250, 400, 2000])
@pytest.mark.parametrize("taps", [1, 3])
def test_no_schedule_ever_runs_past_the_end_of_its_clip(seconds, hold_ms, taps):
    """The invariant the whole thing exists for, over every legal setting."""
    from retro import emulator as E

    frames = E.clip_frame_count(GB_FPS, seconds)
    budget = E.input_budget(GB_FPS, frames)
    plan = timingmod.press_plan(GB_FPS, seconds, hold_ms, taps)

    assert 1 <= len(plan) <= taps
    assert plan[0][0] == 0, "the first press is down before the first frame"
    assert all(hold >= 1 for _, hold in plan)
    assert [start for start, _ in plan] == sorted({start for start, _ in plan})
    assert max(start + hold for start, hold in plan) <= budget < frames
    # A press is never longer than asked for, only shorter.
    assert plan[0][1] <= E.frame_count(GB_FPS, hold_ms / 1000)


def test_the_tap_count_can_depend_on_the_core_so_the_label_is_rewritten():
    """Why the repeat button's label is written again once a core is up.

    While a session is hibernated there is no core to ask, so the label falls
    back to DEFAULT_FPS. Every NTSC console here agrees with that fallback
    about how many taps fit, at every clip length the settings allow -- but a
    50 fps PAL core does not, so the label is rewritten from the core's own
    rate on boot, on wake and on every redraw.
    """
    from retro import emulator as E

    lengths = [tenths / 100 for tenths in range(20, 1501)]
    for fps in (59.727, 60.0988):
        for seconds in lengths:
            fallback = timingmod.press_plan(E.DEFAULT_FPS, seconds, 160, timingmod.REPEAT_TAPS)
            real = timingmod.press_plan(fps, seconds, 160, timingmod.REPEAT_TAPS)
            assert len(fallback) == len(real), (fps, seconds)

    pal = [s for s in lengths
           if len(timingmod.press_plan(E.DEFAULT_FPS, s, 160, timingmod.REPEAT_TAPS))
           != len(timingmod.press_plan(50.0, s, 160, timingmod.REPEAT_TAPS))]
    assert pal, "a PAL core used to disagree; if it no longer can, say so here"
    # A 50 fps core fits *more* taps around 0.45s, because a 160ms hold is
    # eight of its frames rather than ten and leaves proportionally more of
    # the clip free. Either way the fallback is a guess and the core is not.
    assert len(timingmod.press_plan(50.0, pal[0], 160, timingmod.REPEAT_TAPS)) == 2
    assert len(timingmod.press_plan(E.DEFAULT_FPS, pal[0], 160, timingmod.REPEAT_TAPS)) == 1


def test_a_press_is_only_ever_cut_short_by_a_clip_that_cannot_show_it():
    from retro import emulator as E

    wanted = E.frame_count(GB_FPS, 0.4)
    # A four second clip honours a 400ms hold to the frame...
    assert timingmod.press_plan(GB_FPS, 4.0, 400, 1) == [(0, wanted)]
    # ...and a fifth of a second holds for the eight frames it can show.
    assert timingmod.press_plan(GB_FPS, 0.2, 400, 1) == [(0, 8)]
    assert 8 < wanted


@pytest.mark.parametrize("seconds", [0.2, 0.3, 0.4, 0.5, 0.8, 1.0, 2.0, 4.0, 5.0])
@pytest.mark.parametrize("hold_ms", [50, 160, 250, 400, 2000])
def test_every_tap_of_the_repeat_button_is_inside_the_clip_that_records_it(
    seconds, hold_ms
):
    """The repeat button and the recording, over every legal setting.

    A clip photographs frames 0..frames-1 of its own window and nothing else
    (see the seam block in retro/clips.py), so every tap the button promises
    has to be scheduled inside that window and be up before the last picture
    worth a whole step of playback, or the player is shown fewer presses than
    they asked for.

    This used to be a rule about the *pre-roll*, which ran the schedule out
    unphotographed and so could have swallowed a tap. There is no pre-roll
    any more -- the clip starts one emulated frame after the previous one
    ended, always -- so what is left is the schedule's own arithmetic.
    """
    from retro import emulator as E

    frames = E.clip_frame_count(GB_FPS, seconds)
    plan = timingmod.press_plan(GB_FPS, seconds, hold_ms, timingmod.REPEAT_TAPS)

    # The first press is always at frame 0, which is the frame the clip's
    # opening picture is taken after: the press is landing in it.
    assert plan[0][0] == 0
    budget = E.input_budget(GB_FPS, frames)
    for start, hold in plan:
        assert 0 <= start < frames, (start, frames)
        assert start + hold <= budget <= frames - 1, (plan, budget, frames)


def test_the_clip_is_a_webp_attachment(retro):
    view = viewmod.RetroView(
        retro.cog, game_name="My Game!", slug="my-game", rom_filename="x.gb", channel_id=1
    )
    assert view.screen_filename == "My-Game.webp"


@pytest.mark.parametrize(
    "name, expected",
    [("ucity", "ucity.webp"), ("", "screen.webp"), ("!!!", "screen.webp"), ("a" * 80, "a" * 48 + ".webp")],
)
def test_the_attachment_name_is_always_something_discord_accepts(name, expected):
    assert viewmod.RetroView._screen_filename(name) == expected


# -- What moved out of the view, and what still answers to its old name -------
#
# RetroView.py was 2,700 lines with about a third of them not about the view
# at all: when a button goes down, what the message says, what a channel has
# saved, and who is allowed to end somebody else's game. Four modules took
# that third.
#
# It was a *move*: RetroView re-exports every name that went, so
# `retro.RetroView.press_plan` is still `retro.timing.press_plan` and no
# import site outside the package had to be touched for it. The table below
# is that promise, written down -- a name dropped from a re-export is a name
# an older import site stops finding, which is a breakage no test of the new
# module would notice.

MOVED = {
    "timing": [
        "BOOT_SECONDS", "DEFAULT_HOLD_MS", "MAX_HOLD_MS", "MAX_PACE_SECONDS",
        "MIN_HOLD_MS", "MIN_PACE_SECONDS", "MIN_REPEAT_GAP_MS",
        "MIN_REPEAT_TAPS", "REPEAT_GAP_MS", "REPEAT_TAPS", "pace_wait",
        "press_plan",
    ],
    "text": [
        "ACTION_NOTES", "ASLEEP_MARK", "DROPPED_NOTE", "HEADER",
        "HEADER_SEPARATOR", "INVISIBLE_CATEGORIES", "LEADING_ORDINAL",
        "MARKDOWN_ESCAPES", "MAX_GAME_NAME", "MAX_PRESSER_NAME", "NO_PINGS",
        "QUEUED_WAIT", "QUEUE_ENTRY", "QUEUE_NOTE", "REPEAT_GONE_NOTE",
        "REPEAT_STALE_NOTE", "RESUMED_NOTE", "action_note", "escape_label",
        "presser_name",
    ],
    "restore": [
        "BackupSource", "Progress", "backup_offered", "read_backup",
        "restore_into",
    ],
    "permissions": ["may_manage"],
}


@pytest.mark.parametrize("module", sorted(MOVED))
def test_every_name_that_moved_is_still_reachable_from_the_view(module):
    import importlib

    home = importlib.import_module(f"retro.{module}")
    for name in MOVED[module]:
        assert hasattr(home, name), f"retro.{module}.{name} has gone"
        # The same object, not merely a name of the same spelling: a copy
        # would drift, and a test that patches one would not reach the other.
        assert getattr(viewmod, name) is getattr(home, name), name


def test_nothing_that_moved_is_still_defined_in_the_view():
    """The move was a move, not a copy; see MOVED.

    Two definitions of `presser_name` -- one imported, one left behind -- is
    how the restore chain came to have two copies in the first place, which
    is the duplication test_restore.py exists to stop coming back.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "retro" / "RetroView.py").read_text()
    defined = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    for names in MOVED.values():
        assert not (defined & set(names)), sorted(defined & set(names))


def test_the_view_still_owns_the_things_a_view_owns():
    """...and the split stopped where it should have.

    The custom_id prefix is baked into every message this cog has ever
    posted, the queue and the undo history are the session's own state, and
    the emulation drivers (capture_press and friends) are methods on the
    session: they are the half of a press that needs the core, and they read
    and write the view's own history, queue and pacing. They stayed.
    """
    for name in (
        "CUSTOM_ID_PREFIX", "DEFAULT_TIMEOUT_MINUTES", "MAX_QUEUED_PRESSES",
        "MAX_REPLACED_NOTICES", "SAVE_STATE_EVERY_PRESSES", "UNDO_DEPTH",
        "MAX_UNDO_BYTES", "UNDO_COMPRESSION_LEVEL", "Pending",
    ):
        assert hasattr(viewmod, name), name
    for name in (
        "capture_press", "capture_undo", "capture_reset", "run_press",
        "run_undo", "run_reset", "remember_state", "pace", "to_components",
    ):
        assert callable(getattr(viewmod.RetroView, name)), name


async def test_a_hidden_repeat_click_on_a_replaced_game_says_where_it_went(retro):
    """Two true things, and the useful one wins.

    A retired controller's buttons do nothing at all, which is a bigger fact
    about that message than which of them it still draws -- and the reply
    says where the game went. See RetroView._replaced_ack.
    """
    view = viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename="test.gb",
        channel_id=1,
        clip_seconds=0.2,
    )
    view.retire()
    interaction = retro.interaction(view)
    await view._repeat_button().callback(interaction)

    assert interaction.kinds() == ["response.send_message"]
    said = interaction.log[-1][1]["content"]
    assert "has been replaced" in said, said
    assert "cliplength" not in said, said


async def test_a_stale_click_really_reaches_the_view_through_discord_s_own_store(
    retro, system
):
    """The end of the chain, walked rather than assumed.

    Everything else here asserts a piece: the item is registered, the payload
    leaves it out, the callback answers. This one hands a click to the real
    ``ViewStore.dispatch_view`` -- the function that *drops* a click whose
    custom_id no view has, which is what leaves the player looking at "This
    interaction failed" -- and watches it come out the other end at a button
    Discord is no longer being sent.
    """
    view = viewmod.RetroView(
        retro.cog,
        game_name="Test",
        slug="test",
        rom_filename=f"test.{system.extensions[0]}",
        channel_id=1,
        system=system,
        clip_seconds=0.2,
        message_id=4747,
    )
    store = discord.ui.view.ViewStore(None)
    store.add_view(view, 4747)

    interaction = retro.interaction(view, message=types.SimpleNamespace(id=4747))
    # discord.py reads this off the interaction before it calls the callback.
    interaction.data = {"custom_id": f"{viewmod.CUSTOM_ID_PREFIX}:repeat", "component_type": 2}
    store.dispatch_view(2, f"{viewmod.CUSTOM_ID_PREFIX}:repeat", interaction)
    # dispatch_view schedules a task rather than awaiting one.
    for _ in range(10):
        await asyncio.sleep(0)
        if interaction.log:
            break

    assert interaction.kinds() == ["response.send_message"], interaction.log
    assert "hidden while clips are this short" in interaction.log[-1][1]["content"]
