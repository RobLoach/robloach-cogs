"""Every console's controls, as the payload discord.py would really send.

This is the check that would have caught the 400 the Replay button's emoji
once caused: a real RetroView is built for every console and the dict
discord.py hands to the HTTP layer is inspected, rather than the tables being
trusted. Needs discord.py; the tables themselves are covered, with no
dependencies at all, in test_systems.py.
"""

import pytest

discord = pytest.importorskip("discord", reason="the view tests need discord.py")

from retro import RetroView as viewmod  # noqa: E402
from retro import systems as S  # noqa: E402

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


def test_wait_and_replay_share_the_last_row(view):
    last = max(c.row for c in view.children)
    for name in ("wait", "replay", "repeat"):
        button = next(
            c for c in view.children if c.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:{name}"
        )
        assert button.row == last, (name, button.row, last)


def test_the_repeat_button_taps_this_console_s_confirm_button(view, system):
    repeat = next(
        c for c in view.children if c.custom_id == f"{viewmod.CUSTOM_ID_PREFIX}:repeat"
    )
    assert repeat.field == system.confirm
    assert repeat.label == f"{system.label_for(system.confirm)} x{viewmod.REPEAT_TAPS}"


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

    assert CLIP_SECONDS == 4
    assert (MIN_CLIP_SECONDS, MAX_CLIP_SECONDS) == (1, 15)
    assert viewmod.DEFAULT_HOLD_MS == 160
    assert (viewmod.MIN_HOLD_MS, viewmod.MAX_HOLD_MS) == (50, 2000)


def test_directions_are_no_longer_held_longer_than_anything_else():
    # A Game Boy walk cycle is 16 frames (~270ms), so a longer hold walks two
    # tiles for one press; see test_emulator.py's Pokemon tile count.
    assert not hasattr(viewmod, "DPAD_HOLD_MULTIPLIER")
    assert round(59.727 * viewmod.DEFAULT_HOLD_MS / 1000) < 16


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
