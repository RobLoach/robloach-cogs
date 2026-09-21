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

    # One second, not four: a turn is press -> watch -> press, and three of
    # those four seconds were the game sitting still after the press.
    assert CLIP_SECONDS == 1.0
    assert (MIN_CLIP_SECONDS, MAX_CLIP_SECONDS) == (0.2, 15.0)
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
    assert clamp_clip_seconds(9999) == 15.0
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
    # The Replay button's figure: a buffer of clips is an approximation, and
    # round() used to turn two 0.2s clips into "Replay 0s".
    assert format_seconds(0.4018, 1) == "0.4"
    assert format_seconds(3.0135, 1) == "3"
    assert describe_seconds(1.0) == "1 second"
    assert describe_seconds(0.8) == "0.8 seconds"
    assert describe_seconds(4) == "4 seconds"


def test_directions_are_no_longer_held_longer_than_anything_else():
    # A Game Boy walk cycle is 16 frames (~270ms), so a longer hold walks two
    # tiles for one press; see test_emulator.py's Pokemon tile count.
    assert not hasattr(viewmod, "DPAD_HOLD_MULTIPLIER")
    assert round(59.727 * viewmod.DEFAULT_HOLD_MS / 1000) < 16


# -- Fitting a press into a clip ----------------------------------------------
#
# Gambatte's real frame rate, so this is the arithmetic that runs in
# production rather than a tidy 60. No core is needed: press_plan and friends
# are plain functions of a frame rate.

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

    plan = viewmod.press_plan(GB_FPS, seconds, viewmod.DEFAULT_HOLD_MS, viewmod.REPEAT_TAPS)
    assert plan == expected
    budget = E.input_budget(GB_FPS, E.clip_frame_count(GB_FPS, seconds))
    assert max(start + hold for start, hold in plan) <= budget
    gaps = [b[0] - (a[0] + a[1]) for a, b in zip(plan, plan[1:], strict=False)]
    assert all(gap >= E.frame_count(GB_FPS, viewmod.MIN_REPEAT_GAP_MS / 1000) for gap in gaps)


@pytest.mark.parametrize("seconds", [0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.0, 4.0, 15.0])
@pytest.mark.parametrize("hold_ms", [50, 160, 250, 400, 2000])
@pytest.mark.parametrize("taps", [1, 3])
def test_no_schedule_ever_runs_past_the_end_of_its_clip(seconds, hold_ms, taps):
    """The invariant the whole thing exists for, over every legal setting."""
    from retro import emulator as E

    frames = E.clip_frame_count(GB_FPS, seconds)
    budget = E.input_budget(GB_FPS, frames)
    plan = viewmod.press_plan(GB_FPS, seconds, hold_ms, taps)

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
            fallback = viewmod.press_plan(E.DEFAULT_FPS, seconds, 160, viewmod.REPEAT_TAPS)
            real = viewmod.press_plan(fps, seconds, 160, viewmod.REPEAT_TAPS)
            assert len(fallback) == len(real), (fps, seconds)

    pal = [s for s in lengths
           if len(viewmod.press_plan(E.DEFAULT_FPS, s, 160, viewmod.REPEAT_TAPS))
           != len(viewmod.press_plan(50.0, s, 160, viewmod.REPEAT_TAPS))]
    assert pal, "a PAL core used to disagree; if it no longer can, say so here"
    # A 50 fps core fits *more* taps around 0.45s, because a 160ms hold is
    # eight of its frames rather than ten and leaves proportionally more of
    # the clip free. Either way the fallback is a guess and the core is not.
    assert len(viewmod.press_plan(50.0, pal[0], 160, viewmod.REPEAT_TAPS)) == 2
    assert len(viewmod.press_plan(E.DEFAULT_FPS, pal[0], 160, viewmod.REPEAT_TAPS)) == 1


def test_a_press_is_only_ever_cut_short_by_a_clip_that_cannot_show_it():
    from retro import emulator as E

    wanted = E.frame_count(GB_FPS, 0.4)
    # A four second clip honours a 400ms hold to the frame...
    assert viewmod.press_plan(GB_FPS, 4.0, 400, 1) == [(0, wanted)]
    # ...and a fifth of a second holds for the eight frames it can show.
    assert viewmod.press_plan(GB_FPS, 0.2, 400, 1) == [(0, 8)]
    assert 8 < wanted


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
