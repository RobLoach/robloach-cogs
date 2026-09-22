"""A session's whole life: start, press, sleep, wake, restart, stop.

The real Retro and the real RetroView, driven against the fakes in
tests/fakes.py. Nothing here needs a libretro core.
"""

import asyncio
import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

import types  # noqa: E402

import discord  # noqa: E402

from .fakes import (  # noqa: E402
    NES_BYTES,
    ROM_BYTES,
    FakeEmulator,
    FakeUser,
    footage_bytes,
)

# -- Defaults -----------------------------------------------------------------


async def test_the_clip_and_hold_defaults_reach_a_new_install(retro):
    from retro.emulator import CLIP_SECONDS

    assert CLIP_SECONDS == 1.0
    assert await retro.cog.config.clip_seconds() == 1.0
    assert await retro.cog.config.hold_ms() == retro.viewmod.DEFAULT_HOLD_MS == 160


async def test_an_already_configured_value_survives_a_new_default(retro):
    await retro.cog.config.clip_seconds.set(9)
    await retro.cog.config.hold_ms.set(420)
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8050, "defaults")
    assert view.clip_seconds == 9
    assert view.hold_ms == 420


async def test_an_integer_clip_length_in_config_still_loads_as_a_float(retro):
    """Nobody who set `[p]retroset cliplength 4` before it was a float."""
    await retro.cog.config.clip_seconds.set(7)  # an int, as it was written
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8049, "legacy")
    assert view.clip_seconds == 7.0 and isinstance(view.clip_seconds, float)
    assert view.clip_frames(view.emulator) == view.emulator.frames_for_seconds(7)


async def test_a_nonsense_clip_length_in_config_cannot_make_an_empty_clip(retro):
    from retro.emulator import CLIP_SECONDS

    await retro.cog.config.clip_seconds.set(0)
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8048, "zeroed")
    assert view.clip_seconds == 0.2, "clamped on the way into the session"
    assert view.clip_frames(view.emulator) >= retro.emumod.MIN_CLIP_FRAMES
    assert view.press_plan(1)[0][1] >= 1, "and there is still a press in it"

    view.clip_seconds = retro.emumod.clamp_clip_seconds("nonsense")
    assert view.clip_seconds == CLIP_SECONDS


async def test_retroset_cliplength_reports_and_clamps(retro):
    channel = retro.channel(8051)
    ctx = retro.context(channel)
    cliplength = retro.cogmod.Retro.retroset_cliplength.callback

    await cliplength(retro.cog, ctx, 4)
    assert await retro.cog.config.clip_seconds() == 4
    assert "4 seconds" in ctx.sent[-1]

    # Fractions are the point of the change, and the reply says the number
    # back the way it was typed rather than as "0.8000000000000001 seconds".
    await cliplength(retro.cog, ctx, 0.8)
    assert await retro.cog.config.clip_seconds() == 0.8
    assert "0.8 seconds" in ctx.sent[-1]

    # ...and the default does not read "1.0 seconds".
    await cliplength(retro.cog, ctx, 1)
    assert await retro.cog.config.clip_seconds() == 1.0
    assert "1 second of play" in ctx.sent[-1], ctx.sent[-1]

    await cliplength(retro.cog, ctx, 0)
    assert await retro.cog.config.clip_seconds() == retro.emumod.MIN_CLIP_SECONDS == 0.2
    await cliplength(retro.cog, ctx, 9999)
    assert await retro.cog.config.clip_seconds() == retro.emumod.MAX_CLIP_SECONDS == 15.0


async def test_retroset_cliplength_says_what_a_short_clip_does_to_a_press(retro):
    """The knock-ons are surfaced rather than left to be discovered."""
    ctx = retro.context(retro.channel(8053))
    cliplength = retro.cogmod.Retro.retroset_cliplength.callback

    await cliplength(retro.cog, ctx, 1)
    assert "held for about" not in ctx.sent[-1], "a 160ms hold fits in a second"
    assert "repeat button" not in ctx.sent[-1], "and so do three taps"

    await cliplength(retro.cog, ctx, 0.5)
    assert "repeat button taps 2 times" in ctx.sent[-1], ctx.sent[-1]

    await cliplength(retro.cog, ctx, 0.2)
    assert "held for about 133ms" in ctx.sent[-1], ctx.sent[-1]
    # Not "greyed out" any more: at one tap the button is not drawn at all,
    # and the reply says so and says it comes back. See MIN_REPEAT_TAPS.
    assert "is not shown at this length" in ctx.sent[-1], ctx.sent[-1]
    assert "comes back" in ctx.sent[-1], ctx.sent[-1]
    assert "greyed out" not in ctx.sent[-1], ctx.sent[-1]


async def test_retroset_cliplength_reaches_live_sessions_and_their_buttons(retro):
    """Changing the clip length re-draws the row on the next press.

    Both ways round, which is the whole point of the button being hidden
    rather than greyed out: at 0.2s only one tap fits, so the x3 button goes
    away entirely, and at 4s it comes back -- in its proper place in the row,
    between Wait and Undo, rather than tacked on the end.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(8054, "relength")
    cliplength = retro.cogmod.Retro.retroset_cliplength.callback
    assert retro.control(view, "repeat").label == "A x3"
    assert not retro.control(view, "repeat").disabled

    await cliplength(retro.cog, ctx, 0.2)
    assert view.clip_seconds == 0.2
    # The next press redraws the controls, and the repeat button -- which can
    # now do no more than the console's own A button -- is gone.
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.repeat_taps == 1 and not view.has_repeat_button
    assert retro.control(view, "repeat") is None
    assert view.clip_frames(view.emulator) == 12
    # Wait and Undo have not moved, because the row still reserves space for
    # all three controls whether or not the third is drawn.
    assert [c.label for c in view.children if c.row == 2] == [
        "Start", "Select", "Wait", "Undo"
    ]

    await cliplength(retro.cog, ctx, 4)
    await view._press(retro.interaction(view, message=view.message), "a")
    back = retro.control(view, "repeat")
    assert back is not None and back.label == "A x3" and not back.disabled
    assert [c.label for c in view.children if c.row == 2] == [
        "Start", "Select", "Wait", "A x3", "Undo"
    ]


async def test_nothing_of_the_stop_button_is_left_in_the_view(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8052, "stopless")
    assert not hasattr(retro.viewmod, "_StopButton")
    assert not hasattr(view, "stop_item")
    assert not hasattr(view, "_stop")
    assert not hasattr(view, "_sync_children")


async def test_can_stop_is_kept_for_retrostop_and_retroreset(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8053, "whosegame")
    assert await view.can_stop(FakeUser(uid=view.starter_id))
    assert not await view.can_stop(FakeUser(uid=4242))
    # The bot owner may always stop a game; FakeBot says owner is user 1.
    assert await view.can_stop(FakeUser(uid=1))
    # A moderator, i.e. Manage Messages. Read off `guild_permissions` rather
    # than from isinstance(user, discord.Member), which is what makes this
    # branch checkable at all; see can_stop.
    assert await view.can_stop(FakeUser(uid=4243, manage_messages=True))
    assert not await view.can_stop(FakeUser(uid=4244, manage_messages=False))


async def test_a_started_session_is_registered_as_a_persistent_view(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8054, "persistent")
    assert view.is_persistent()
    retro.bot.add_view(view, message_id=view.message_id)
    assert any(registered is view for registered, _ in retro.bot.added_views)


# -- Core selection by file extension -----------------------------------------


async def test_with_no_cores_at_all_the_user_is_told_to_download_them(retro):
    channel = retro.channel(8100)
    ctx = retro.context(channel)
    await retro.cog.config.cores.set({})
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game="https://example.com/x.gb")
    assert "retroset download" in ctx.said()


async def test_a_missing_core_is_named_by_console_and_download_command(retro):
    channel = retro.channel(8101)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte")
    retro.serve("nestest.nes", NES_BYTES)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/nestest.nes"
    )
    said = ctx.said()
    assert "Nintendo Entertainment System" in said
    assert "retroset download fceumm" in said
    assert retro.cog.sessions.get(channel.id) is None


async def test_an_unknown_extension_lists_what_is_supported(retro):
    channel = retro.channel(8102)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte")
    retro.serve("movie.mp4", b"x" * 4096)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/movie.mp4"
    )
    said = ctx.said()
    assert ".mp4" in said
    assert "Game Boy" in said and "Super Nintendo" in said


async def test_a_nes_rom_starts_the_nes_on_the_fceumm_core(retro):
    channel = retro.channel(8103)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte", "fceumm")
    retro.serve("nestest.nes", NES_BYTES)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/nestest.nes"
    )
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.system.key == "nes"
    assert view.core == "fceumm"
    assert view.emulator.core_path.name == "fceumm_libretro.so"
    assert {c.field for c in view.children if isinstance(c, retro.viewmod._GameButton)} == set(
        retro.sysmod.system_by_key("nes").fields
    )
    assert retro.cog.config.channels[channel.id]["session"]["system"] == "nes"
    assert view.screen_filename.endswith(".webp")


async def test_a_restored_record_keeps_its_console_and_core(retro):
    channel = retro.channel(8104)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte", "fceumm")
    retro.serve("nestest.nes", NES_BYTES)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/nestest.nes"
    )
    record = retro.cog.config.channels[channel.id]["session"]
    rebuilt = retro.viewmod.RetroView.from_record(retro.cog, record)
    assert rebuilt.system.key == "nes"
    assert rebuilt.core == "fceumm"


async def test_a_record_from_before_multi_console_support_falls_back(retro):
    channel = retro.channel(8105)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte")
    view = await retro.start_game(ctx, "legacy")
    legacy = dict(view.to_record())
    legacy.pop("system"), legacy.pop("core")
    legacy["rom_filename"] = "1-x.sfc"
    assert retro.viewmod.RetroView.from_record(retro.cog, legacy).system.key == "snes"
    legacy["rom_filename"] = "1-x.unknown"
    assert retro.viewmod.RetroView.from_record(retro.cog, legacy).system.key == "gb"


# -- One core at a time -------------------------------------------------------


async def test_starting_a_second_game_sleeps_and_saves_the_first(retro):
    await retro.install_cores("gambatte", "fceumm")
    first = retro.channel(8200, name="arcade")
    second = retro.channel(8201, name="lounge")
    ctx1, ctx2 = retro.context(first), retro.context(second)

    view1 = await retro.start_game(ctx1, "gameone")
    emulator1 = view1.emulator
    state_path = retro.cog._state_path(first.id, view1.slug)
    assert view1.live
    assert not state_path.is_file()

    view2 = await retro.start_game(ctx2, "gametwo")
    assert view2.live
    assert not view1.live
    assert not emulator1.started
    assert state_path.is_file(), "the first game must be SAVED before being freed"
    assert sum(1 for v in retro.cog.sessions.values() if v.live) == 1
    assert sum(1 for v in retro.cog.sessions.values() if v.live) <= retro.cogmod.MAX_LIVE_EMULATORS


async def test_the_new_channel_is_told_what_got_paused(retro):
    await retro.install_cores("gambatte")
    first = retro.channel(8210, name="arcade")
    second = retro.channel(8211, name="lounge")
    view1 = await retro.start_game(retro.context(first), "gameone")
    ctx2 = retro.context(second)
    await retro.start_game(ctx2, "gametwo")

    notices = [s for s in ctx2.sent if isinstance(s, str) and "Paused" in s]
    assert notices, ctx2.sent
    assert "gameone" in notices[-1] and f"<#{first.id}>" in notices[-1]
    edits = view1.message.edits
    assert edits and "went to sleep" in (edits[-1]["content"] or "")
    assert "embed" not in edits[-1], "the notice is plain text"


async def test_a_channel_the_author_cannot_see_stays_vague(retro):
    await retro.install_cores("gambatte")
    playing = retro.channel(8220, name="lounge")
    view = await retro.start_game(retro.context(playing), "gametwo")
    asking = retro.channel(8221, name="third")

    ctx = retro.context(asking)
    notice = retro.cog._eviction_notice(ctx, [view])
    assert notice and f"<#{playing.id}>" in notice and "gametwo" in notice

    playing.visible = False
    assert "another channel" in (retro.cog._eviction_notice(ctx, [view]) or "")

    playing.visible = True
    other_guild = retro.context(asking, guild_id=999)
    assert "another channel" in (retro.cog._eviction_notice(other_guild, [view]) or "")

    assert retro.cog._eviction_notice(ctx, []) is None


async def test_waking_a_sleeping_game_evicts_and_saves_the_other(retro):
    await retro.install_cores("gambatte")
    first = retro.channel(8230)
    second = retro.channel(8231)
    view1 = await retro.start_game(retro.context(first), "gameone")
    view2 = await retro.start_game(retro.context(second), "gametwo")
    state2 = retro.cog._state_path(second.id, view2.slug)

    await view1._press(retro.interaction(view1, message=view1.message), "a")
    assert view1.live and not view2.live
    assert state2.is_file()


# -- Hold length and repeat ---------------------------------------------------


@pytest.fixture
async def held(retro):
    """A live Game Boy session, for the hold/press tests."""
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(8300, "holdme")
    return view, ctx, channel


async def test_every_button_is_held_for_the_configured_time(retro, held):
    view, _, _ = held
    emulator = view.emulator
    assert retro.viewmod.DEFAULT_HOLD_MS == 160
    assert view.hold_ms == retro.viewmod.DEFAULT_HOLD_MS

    a_hold = view._schedule(emulator, "a", 1)[0][2]
    up_hold = view._schedule(emulator, "up", 1)[0][2]
    assert a_hold == emulator.frames_for_ms(160)
    # The fix for "pressing right in Pokemon walks two tiles": a direction is
    # held for exactly as long as a face button now.
    assert up_hold == a_hold
    assert not hasattr(retro.viewmod, "DPAD_HOLD_MULTIPLIER")
    for field in ("up", "down", "left", "right", "a", "b", "start", "select"):
        if field in set(view.system.fields):
            assert view._schedule(emulator, field, 1)[0][2] == emulator.frames_for_ms(view.hold_ms)


async def test_a_hold_is_under_a_game_boy_walk_cycle_and_uses_the_real_fps(retro, held):
    view, _, _ = held
    emulator = view.emulator
    hold = view._schedule(emulator, "a", 1)[0][2]
    assert hold < 16, f"{hold} frames @ {emulator.fps:.4f}fps"
    assert hold == round(emulator.fps * 0.16)
    assert emulator.fps != 60, "hold frames must come from the core, not a hardcoded 60"


async def test_wait_presses_nothing(retro, held):
    view, _, _ = held
    assert view._schedule(view.emulator, None, 1) == []


async def test_repeat_schedules_three_spread_out_taps_inside_the_clip(retro, held):
    view, _, _ = held
    emulator = view.emulator
    taps = view._schedule(emulator, "a", retro.viewmod.REPEAT_TAPS)
    assert len(taps) == retro.viewmod.REPEAT_TAPS
    assert [t[1] for t in taps] == sorted({t[1] for t in taps})
    # All of it is released by the last frame the clip actually photographs,
    # so the final picture shows where the taps got you. At a one second clip
    # this is the bound that bites: three 160ms taps 250ms apart is 1.4s.
    frames = view.clip_frames(emulator)
    assert max(t[1] + t[2] for t in taps) <= emulator.input_budget(frames) < frames


async def test_a_schedule_never_outlasts_a_short_clip(retro, held):
    view, _, _ = held
    emulator = view.emulator
    for seconds in (0.2, 0.25, 0.5, 0.8, 1.0, 4.0, 15.0):
        view.clip_seconds = seconds
        frames = view.clip_frames(emulator)
        budget = emulator.input_budget(frames)
        for repeat in (1, retro.viewmod.REPEAT_TAPS):
            taps = view._schedule(emulator, "a", repeat)
            assert taps, f"{seconds}s left no press at all"
            assert max(t[1] + t[2] for t in taps) <= budget, (seconds, repeat, taps)
            # ...and the emulator is handed a schedule it does not have to
            # clamp, which is what used to shove a tap onto the last frame.
            view.run_press("a", repeat)
            assert emulator.last_presses == taps


async def test_a_press_reaches_the_emulator_as_a_webp_clip_schedule(retro, held):
    view, _, _ = held
    emulator = view.emulator
    view.run_press("a")
    assert emulator.last_presses == view._schedule(emulator, "a", 1)
    assert emulator.last_format == "WEBP"


async def test_retroset_hold_stores_clamps_and_reaches_live_sessions(retro, held):
    view, ctx, _ = held
    hold_cmd = retro.cogmod.Retro.retroset_hold.callback

    await hold_cmd(retro.cog, ctx, 450)
    assert await retro.cog.config.hold_ms() == 450
    assert view.hold_ms == 450
    assert view._schedule(view.emulator, "b", 1)[0][2] == view.emulator.frames_for_ms(450)

    await hold_cmd(retro.cog, ctx, 5)
    assert await retro.cog.config.hold_ms() == retro.viewmod.MIN_HOLD_MS
    await hold_cmd(retro.cog, ctx, 999999)
    assert await retro.cog.config.hold_ms() == retro.viewmod.MAX_HOLD_MS

    await hold_cmd(retro.cog, ctx, retro.viewmod.DEFAULT_HOLD_MS)
    assert "160ms" in ctx.sent[-1] and "320ms" not in ctx.sent[-1]
    assert "direction" in ctx.sent[-1], "it must say the directions are included"


async def test_the_repeat_button_taps_the_confirm_field_three_times(retro, held):
    view, _, _ = held
    repeat = retro.control(view, "repeat")
    interaction = retro.interaction(view, message=view.message)
    await repeat.callback(interaction)
    assert any(snap.get("has_attachments") for _, snap in interaction.log)
    presses = view.emulator.last_presses
    assert len(presses) == 3 and all(tap[0] == "a" for tap in presses)


# -- The message the clip lands on --------------------------------------------


async def test_the_first_post_is_a_bare_clip_with_no_embed_or_caption(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(8400, "layout")
    posted = list(channel.messages.values())[-1]
    assert posted.kwargs.get("embed") is None
    assert posted.kwargs.get("content") is None
    assert getattr(posted.kwargs.get("file"), "filename", "").endswith(".webp")
    assert view._content() is None


async def test_a_one_off_notice_is_shown_once_and_then_forgotten(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8401, "notice")
    view.notice = "something happened"
    assert view._content() == "something happened"
    assert view._content() is None


async def test_a_press_edit_carries_one_clip_and_no_embed(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8402, "pressedit")
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    for _, snap in interaction.log:
        assert not snap.get("has_embed"), snap
    final = interaction.log[-1][1]
    assert final["n_attachments"] == 1
    assert final["filenames"][0].endswith(".webp")
    # One line of text -- which button it was -- and nothing else. See the
    # "Which button was pressed" section below.
    assert final["content"] == "Tester pressed A."
    assert final["spacers_disabled"], "the spacers were never re-enabled"


@pytest.mark.redbot
async def test_playing_needs_no_embed_links_permission(retro):
    # The clip is a plain attachment and the controls are buttons, so the
    # play loop must not ask for Embed Links. One owner-only command does.
    play = {name for name, on in retro.cogmod.Retro.retro.requires.bot_perms if on}
    assert play == {"attach_files"}
    settings = {
        name for name, on in retro.cogmod.Retro.retroset_settings.requires.bot_perms if on
    }
    assert settings == {"embed_links"}


async def test_the_permission_error_names_attach_files(retro):
    forbidden = retro.cog._http_error_message(
        discord.Forbidden(types.SimpleNamespace(status=403, reason="f"), "no")
    )
    assert "Attach Files" in forbidden and "Embed Links" not in forbidden


# -- Pressing a button --------------------------------------------------------


async def test_a_press_makes_exactly_one_edit_to_the_message(retro):
    """The fix for "the clip rewinds when I press a button".

    A press used to edit the message twice: once immediately, to grey the
    controls out, and once again with the new clip. The first edit changed no
    attachment, but a Discord client re-renders a message on *any* edit, and
    re-rendering restarts the clip that is already attached -- which, since
    clips play through once and hold their last frame, played the previous
    clip again from frame zero before the new one arrived. So the press is
    acknowledged with a plain defer (which shows nothing) and makes one edit.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9002, "ucity")
    assert view.live

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    edits = [snap for kind, snap in interaction.log if kind != "response.defer"]
    assert len(edits) == 1, interaction.kinds()
    only = edits[0]
    assert only["has_attachments"] and only["n_attachments"] == 1
    assert only["filenames"][0].endswith(".webp")
    assert not only["any_disabled"], "the buttons end up enabled"
    assert only["spacers_disabled"], "except the inert layout spacers"


async def test_nothing_is_edited_while_the_press_is_being_emulated(retro):
    """No intermediate edit, not even a content-only one.

    The whole bug was an edit that did not touch the attachment and still
    restarted it, so "the clip is only uploaded once" is not the assertion
    that matters; "the message is only touched once" is.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9005, "midpress")
    interaction = retro.interaction(view, message=view.message)

    original = view.run_press
    seen = []

    def watched(field, repeat=1):
        # What the message had been told by the time the emulator was asked.
        seen.append(list(interaction.kinds()))
        return original(field, repeat)

    view.run_press = watched
    try:
        await view._press(interaction, "a")
    finally:
        view.run_press = original

    assert seen == [["response.defer"]], seen
    assert not any(
        kind.endswith("edit_message") for kind in interaction.kinds()
    ), interaction.kinds()


async def test_a_press_no_longer_greys_the_controls_out(retro):
    """The trade this cost, pinned so it cannot be reintroduced by accident.

    Instant "your click landed" feedback and a clip that does not rewind
    cannot both be had: any component response that shows something either
    edits this message (the rewind) or posts a second one (an ephemeral
    notice, removed earlier for being spam). The controls therefore stay
    enabled throughout, and _set_disabled(True) is left for retirement only.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9006, "nogrey")
    interaction = retro.interaction(view, message=view.message)

    original = view.run_press
    disabled_midway = []

    def watched(field, repeat=1):
        disabled_midway.append(
            [c.custom_id for c in retro.pressable(view) if c.disabled]
        )
        return original(field, repeat)

    view.run_press = watched
    try:
        await view._press(interaction, "a")
    finally:
        view.run_press = original

    assert disabled_midway == [[]], disabled_midway
    assert not any(c.disabled for c in retro.pressable(view))
    # ...and the one control that *starts* greyed out came back, because the
    # press it was waiting for has happened. See fakes.CONDITIONAL_CONTROLS.
    assert not retro.control(view, "undo").disabled


async def test_the_clip_put_on_the_message_is_a_real_animation(retro):
    """Read off the edit, which is the only place the clip exists.

    The session used to keep a copy in ``last_clip``; nothing in the cog
    read it (``_show`` is handed the clip as an argument), so it is gone and
    the message is the statement.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9003, "cached")
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    clip = interaction.clip()
    assert isinstance(clip, bytes) and clip
    assert clip[:4] == b"RIFF" and clip[8:12] == b"WEBP"
    assert not hasattr(view, "last_clip"), "the session kept a copy of the clip"
    assert not hasattr(view, "remember_clip")


async def test_a_save_state_is_written_every_third_press(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9004, "autosave")
    state_path = retro.cog._state_path(channel.id, view.slug)
    assert retro.viewmod.SAVE_STATE_EVERY_PRESSES == 3

    await view._press(retro.interaction(view, message=view.message), "a")
    assert not state_path.is_file()
    await view._press(retro.interaction(view, message=view.message), "b")
    assert not state_path.is_file()
    await view._press(retro.interaction(view, message=view.message), "start")
    assert state_path.is_file()
    assert retro.cog._rom_path(view.rom_filename).is_file()


# -- Overlapping presses ------------------------------------------------------


async def test_a_press_while_the_session_is_busy_only_defers(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9010, "busy")
    async with view.lock:
        held = retro.interaction(view, message=view.message)
        await view._press(held, "b")
        repeat = retro.interaction(view, message=view.message)
        await retro.control(view, "repeat").callback(repeat)
        waiting = retro.interaction(view, message=view.message)
        await retro.control(view, "wait").callback(waiting)
    # Deferred and nothing else: no edit, and no line naming a press that
    # never happened.
    assert held.kinds() == ["response.defer"]
    assert repeat.kinds() == ["response.defer"]
    assert waiting.kinds() == ["response.defer"]


async def test_two_simultaneous_presses_produce_exactly_one_clip(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9011, "race")
    original = view.run_press

    def slow(field, repeat=1):
        time.sleep(0.4)
        return original(field, repeat)

    view.run_press = slow
    first = retro.interaction(view, message=view.message)
    second = retro.interaction(view, message=view.message)
    try:
        await asyncio.gather(view._press(first, "a"), view._press(second, "b"))
    finally:
        view.run_press = original

    # The loser is deferred and says nothing at all; the winner defers too
    # and then makes the one edit. Neither ever shows "interaction failed".
    kinds = sorted([tuple(first.kinds()), tuple(second.kinds())])
    assert kinds == [
        ("response.defer",),
        ("response.defer", "edit_original_response"),
    ]
    clips = sum(
        1 for i in (first, second) for _, snap in i.log if snap.get("has_attachments")
    )
    assert clips == 1
    edits = sum(
        1 for i in (first, second) for kind, _ in i.log if kind != "response.defer"
    )
    assert edits == 1, "two presses, one visible change"


# -- Which button was pressed -------------------------------------------------
#
# Every press names itself in the one line of content the message carries,
# above the clip and on the very same edit as the clip. The per-console
# wording is a table in test_view.py (PRESS_LINES); what is checked here is
# that the line really reaches the message, that it beats nothing important,
# and that it cannot go stale.


async def test_a_press_says_which_button_it_was(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9020, "named")

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    # Still one edit: the line rides on the edit that carries the clip.
    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    landed = interaction.log[-1][1]
    assert landed["content"] == "Tester pressed A."
    assert landed["n_attachments"] == 1, "and the clip came with it"


@pytest.mark.parametrize(
    "field, expected",
    [
        ("up", "Tester pressed \N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}."),
        ("down", "Tester pressed \N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}."),
        ("left", "Tester pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}."),
        ("right", "Tester pressed \N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}."),
        ("b", "Tester pressed B."),
        ("start", "Tester pressed Start."),
        ("select", "Tester pressed Select."),
        (None, "Tester waited."),
    ],
)
async def test_every_control_puts_its_own_name_on_the_message(retro, field, expected):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9021, "eachname")
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, field)
    assert interaction.log[-1][1]["content"] == expected


async def test_the_repeat_button_says_how_many_taps_it_really_did(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9022, "taps")
    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "repeat").callback(interaction)
    assert interaction.log[-1][1]["content"] == "Tester pressed A x3."

    # A clip too short to fit three: the line follows press_plan down, like
    # the label on the button does, rather than claiming a tap that did not
    # happen.
    view.clip_seconds = 0.5
    again = retro.interaction(view, message=view.message)
    await retro.control(view, "repeat").callback(again)
    assert again.log[-1][1]["content"] == "Tester pressed A x2."


async def test_the_press_line_is_replaced_by_the_next_press_rather_than_kept(retro):
    """It must not go stale: yesterday's press over today's clip is a lie."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9023, "notstale")

    said = []
    for field in ("a", "b", "left", None, "start"):
        interaction = retro.interaction(view, message=view.message)
        await view._press(interaction, field)
        said.append(interaction.log[-1][1]["content"])
    assert said == [
        "Tester pressed A.",
        "Tester pressed B.",
        "Tester pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}.",
        "Tester waited.",
        "Tester pressed Start.",
    ]
    # Every one of those was a whole `content`, so there is never a press
    # line left over from an earlier press: the edit always writes one.
    assert all(line is not None for line in said)


async def test_a_real_notice_still_beats_the_press_line(retro):
    """Only one line fits, and "your save state was rejected" is the news."""
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9024, "noticewins")
    await retro.cog.hibernate(view, None)
    # A state the fake core will refuse, i.e. what a core update looks like.
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"not a state")

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    content = interaction.log[-1][1]["content"] or ""
    assert "save state could not be used" in content, content
    assert "pressed" not in content
    # ...and the press after it, with nothing to report, names itself again.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == "Tester pressed A."


async def test_the_resume_line_beats_the_press_line(retro):
    """"The game was asleep and is back" is news; "you pressed A" is a label."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9025, "resumewins")
    await retro.cog.hibernate(view, None)

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert interaction.log[-1][1]["content"] == retro.viewmod.RESUMED_NOTE
    # And it is said once: the next press names itself instead.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == "Tester pressed A."


async def test_a_failed_press_explains_itself_rather_than_naming_a_button(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9026, "pressfails")

    def boom(field, repeat=1):
        raise retro.emumod.EmulatorError("the core fell over")

    view.run_press = boom
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    snap = interaction.log[-1][1]
    assert "the core fell over" in (snap["content"] or "")
    assert "pressed" not in (snap["content"] or "")
    assert not snap["any_disabled"], "and the controls still work"


# -- Who did it ---------------------------------------------------------------
#
# The line also names whoever did it, in one voice for all five actions, and
# it must never notify them: a ping on every button press, from everybody in
# the channel, would make the cog unusable in any channel anybody is in.
#
# Two guarantees, checked separately here because they fail separately:
#
# * the *string* contains no mention syntax, so there is nothing for Discord
#   to resolve (the sanitising table is in test_view.py);
# * the *edit* carries an AllowedMentions that suppresses everything, so even
#   a line that somehow did could not deliver a notification.
#
# And all of it still rides on the one edit the action already makes.


#: A press, the Wait button, the repeat button, Undo and `[p]retroreset`, each
#: as "how to do it" and "what the message must then say". Everything a
#: session can put on that line, in one table, so the voice cannot drift.
#:
#: Each of these takes the person doing it, because that is the thing under
#: test: a button click carries ``interaction.user`` and `[p]retroreset` is a
#: command carrying ``ctx.author``, and both have to come out the same way.
async def do_press(retro, view, ctx, who, field="a"):
    interaction = retro.interaction(view, user=who, message=view.message)
    await view._press(interaction, field)
    return interaction.log[-1][1]


async def do_wait(retro, view, ctx, who):
    return await do_press(retro, view, ctx, who, None)


async def do_repeat(retro, view, ctx, who):
    interaction = retro.interaction(view, user=who, message=view.message)
    await retro.control(view, "repeat").callback(interaction)
    return interaction.log[-1][1]


async def do_undo(retro, view, ctx, who):
    # Something to step back from first, by somebody else, so the line that
    # is checked is the undoer's rather than the presser's.
    await view._press(retro.interaction(view, message=view.message), "a")
    interaction = retro.interaction(view, user=who, message=view.message)
    await retro.control(view, "undo").callback(interaction)
    return interaction.log[-1][1]


async def do_reset(retro, view, ctx, who):
    ctx.author = who
    view.starter_id = who.id
    await retro.cogmod.Retro.retroreset.callback(retro.cog, ctx)
    # A command, so the edit is to the message rather than to an interaction.
    return retro.message_edit(view)


ATTRIBUTED = [
    ("press", do_press, "pressed A."),
    ("wait", do_wait, "waited."),
    ("repeat", do_repeat, "pressed A x3."),
    ("undo", do_undo, "undid the last press."),
    ("reset", do_reset, "reset the game."),
]


@pytest.mark.parametrize(
    "name, act, tail", ATTRIBUTED, ids=[row[0] for row in ATTRIBUTED]
)
async def test_every_action_says_who_did_it(retro, name, act, tail):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9030 + len(name), f"who{name}")
    rob = FakeUser(uid=4242, name="Rob", manage_messages=True)

    landed = await act(retro, view, ctx, rob)

    assert landed["content"] == f"Rob {tail}"
    # One short line, above a clip, and never anything that could notify.
    assert "\n" not in landed["content"]
    assert landed["pings_nobody"], landed["allowed_mentions"]


@pytest.mark.parametrize(
    "name, act, tail", ATTRIBUTED, ids=[row[0] for row in ATTRIBUTED]
)
async def test_no_action_can_notify_anybody(retro, name, act, tail):
    """A hostile display name changes neither of the two guarantees.

    ``@everyone`` in somebody's nickname is the case worth being explicit
    about: it comes out with a zero-width space in it *and* the edit carries
    an AllowedMentions that would have refused it anyway.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9060 + len(name), f"ping{name}")
    nasty = FakeUser(
        uid=4243,
        name="@everyone **<@111111111111111111>**",
        manage_messages=True,
    )

    landed = await act(retro, view, ctx, nasty)

    content = landed["content"] or ""
    assert content.endswith(tail), content
    assert "@everyone" not in content, content
    assert "@here" not in content, content
    assert not re.search(r"(?<!\\)<", content), content
    # ...and the edit itself forbids every kind of mention, so the string
    # guarantee is not the only thing standing between a press and a ping.
    allowed = landed["allowed_mentions"]
    assert allowed is not None, landed
    for kind in ("everyone", "users", "roles", "replied_user"):
        assert getattr(allowed, kind) is False, (kind, allowed)
    assert landed["pings_nobody"]


async def test_the_attribution_still_rides_on_the_one_edit(retro):
    """The rule that must not be paid for: one press, one visible change."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9050, "oneeditnamed")

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    landed = interaction.log[-1][1]
    assert landed["content"] == "Tester pressed A."
    assert landed["n_attachments"] == 1, "the clip came on the same edit"


async def test_a_presser_with_no_usable_name_still_gets_a_line(retro):
    """A User rather than a Member, and an object with nothing on it at all.

    Both are real: a plain ``discord.User`` arrives for somebody who has left
    the guild, and neither the naming nor the press depends on the
    ``guild_permissions`` a Member has and a User does not.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9051, "nameless")

    # A plain User: no guild_permissions, but a global display name.
    user = types.SimpleNamespace(id=99, display_name="Ex Member")
    assert not hasattr(user, "guild_permissions")
    interaction = retro.interaction(view, user=user, message=view.message)
    await view._press(interaction, "a")
    assert interaction.log[-1][1]["content"] == "Ex Member pressed A."

    # Nothing nameable: the impersonal form of the same sentence, not a
    # traceback and not a line starting with a space.
    anonymous = types.SimpleNamespace(id=100)
    interaction = retro.interaction(view, user=anonymous, message=view.message)
    await view._press(interaction, "b")
    assert interaction.log[-1][1]["content"] == "Pressed B."


async def test_two_people_pressing_are_told_apart(retro):
    """The whole point of the feature, on one message."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9052, "twopeople")

    said = []
    for uid, who, field in ((11, "Rob", "a"), (12, "Ada", "b"), (11, "Rob", None)):
        interaction = retro.interaction(
            view, user=FakeUser(uid=uid, name=who), message=view.message
        )
        await view._press(interaction, field)
        said.append(interaction.log[-1][1]["content"])
    assert said == ["Rob pressed A.", "Ada pressed B.", "Rob waited."]


# -- Nothing of the Replay button is left --------------------------------------


def test_the_replay_button_and_its_buffer_are_gone(retro):
    """Removed in full, not merely hidden: it saved 8 MiB a session.

    A session used to keep its recent clips so Replay could stitch the last
    fifteen seconds back together. All of it went -- the button, its
    custom_id, its emoji, the buffer and the stitching -- so this is a list
    of names that must not come back rather than a behaviour check.
    """
    for name in (
        "_ReplayButton",
        "REPLAY_SECONDS",
        "MAX_REPLAY_BYTES",
        "MAX_REPLAY_CLIPS",
        "MAX_REPLAY_FRAMES",
        "concatenate_clips",
    ):
        assert not hasattr(retro.viewmod, name), name
    for name in (
        "REPLAY_SECONDS",
        "MAX_REPLAY_BYTES",
        "MAX_REPLAY_CLIPS",
        "MAX_REPLAY_FRAMES",
        "concatenate_clips",
        "decode_clip",
    ):
        assert not hasattr(retro.clipsmod, name), name
        assert not hasattr(retro.emumod, name), name
        assert name not in retro.emumod.__all__, name
    assert not hasattr(retro.sysmod, "REPLAY_EMOJI")
    # The single clip that replaced the buffer is gone too. Nothing read it:
    # `_show` is handed the clip it is about to post, so keeping a copy on
    # the session was bookkeeping and a test hook and nothing else.
    assert not hasattr(retro.viewmod.RetroView, "remember_clip")
    for module in (retro.viewmod, retro.cogmod):
        source = Path(module.__file__).read_text()
        # The comments may say it was removed and why; nothing may set it.
        assert "self.last_clip =" not in source, module.__name__
        assert "view.last_clip =" not in source, module.__name__


async def test_a_session_holds_no_footage_at_all(retro):
    """Not a buffer of clips, and not one clip either.

    The replay buffer went with the Replay button, and the single
    ``last_clip`` that replaced it went too: nothing read it. So a press
    builds a clip, uploads it and drops it, and twenty presses leave the
    session holding no bytes of picture whatsoever.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9027, "oneclip")
    for gone in ("clips", "buffered_seconds", "_trim_clips", "last_clip", "remember_clip"):
        assert not hasattr(view, gone), gone

    posted = []
    for _ in range(20):
        interaction = retro.interaction(view, message=view.message)
        await view._press(interaction, "a")
        clip = interaction.clip()
        assert isinstance(clip, bytes) and clip
        posted.append(clip)
    assert len(set(posted)) == 20, "the fake produced the same clip twice"
    # Twenty clips posted, and the session is not holding one of them: no
    # attribute on it is anything like a clip's worth of bytes.
    assert footage_bytes(view) == 0, vars(view).keys()


# -- Undo ---------------------------------------------------------------------
#
# A misclick is the dominant frustration in turn-based play by button: you
# press a direction, wait a second for the clip, and find you walked into
# the wrong room. So every press pushes the state it is about to change onto
# a bounded in-memory stack first and Undo pops it back. The real core does
# the same round trip in test_saves_roundtrip.py; FakeEmulator's save state
# carries its frame counter, which is what makes "back one press" checkable
# here without a core.


async def undo(retro, view):
    """Click Undo, and hand back the interaction it was clicked with."""
    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "undo").callback(interaction)
    return interaction


async def test_undo_puts_the_game_back_where_the_last_press_found_it(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9200, "undome")
    before = view.emulator.frame

    await view._press(retro.interaction(view, message=view.message), "right")
    assert view.emulator.frame > before, "the press moved the game on"
    assert len(view.history) == 1, "and left something to step back to"

    await undo(retro, view)
    # The state that went back in is the one the press started from, to the
    # frame: FakeEmulator records what it was asked to load.
    assert view.emulator.loaded_from == before
    assert not view.history, "the undo point was consumed, not reused"


async def test_undo_makes_exactly_one_edit_to_the_message(retro):
    """The same rule a press lives by; see _ack_now."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9201, "oneedit")
    await view._press(retro.interaction(view, message=view.message), "a")

    interaction = await undo(retro, view)

    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    edits = [snap for kind, snap in interaction.log if kind != "response.defer"]
    assert len(edits) == 1, interaction.kinds()
    only = edits[0]
    assert only["has_attachments"] and only["n_attachments"] == 1
    assert only["filenames"][0].endswith(".webp")
    assert only["content"] == "Tester undid the last press."
    assert not only["any_disabled"], "the controls come back enabled"
    assert only["spacers_disabled"]


async def test_nothing_is_edited_while_the_undo_is_being_emulated(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9202, "midundo")
    await view._press(retro.interaction(view, message=view.message), "a")

    original = view.run_undo
    seen = []

    def watched():
        seen.append(list(interaction.kinds()))
        return original()

    view.run_undo = watched
    interaction = retro.interaction(view, message=view.message)
    try:
        await retro.control(view, "undo").callback(interaction)
    finally:
        view.run_undo = original
    assert seen == [["response.defer"]], seen


async def test_the_undo_button_is_dead_until_there_is_a_press_to_undo(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9203, "deadundo")
    button = retro.control(view, "undo")
    assert button.disabled, "a game that has only just booted"

    await view._press(retro.interaction(view, message=view.message), "a")
    assert not button.disabled

    await undo(retro, view)
    assert button.disabled, "and dead again once the history is spent"


async def test_an_undo_with_nothing_to_undo_says_so_and_touches_nothing(retro):
    """The fresh-restart path: the history is memory only, like the clips."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9204, "emptyundo")
    view.forget_history()

    interaction = await undo(retro, view)

    assert interaction.kinds() == ["response.send_message"], interaction.kinds()
    snap = interaction.log[0][1]
    assert snap["ephemeral"] is True
    assert "nothing to undo" in (snap["content"] or "")
    assert "memory only" in (snap["content"] or "")
    assert not view.message.edits, "the message itself was never touched"


async def test_a_session_restored_after_a_restart_has_nothing_to_undo(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9205, "afterrestart")
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.history

    cog2, bot2 = retro.make_cog()
    bot2.channels[channel.id] = channel
    await cog2._restore_sessions()
    restored = cog2.sessions[channel.id]

    assert not restored.history and not restored.can_undo
    assert restored.history_bytes == 0
    button = next(
        c for c in restored.children
        if c.custom_id == f"{retro.viewmod.CUSTOM_ID_PREFIX}:undo"
    )
    assert button.disabled, "a restart cannot leave a button that lies"
    # ...and clicking it anyway is answered, never an error.
    interaction = retro.interaction(restored, message=view.message)
    await button.callback(interaction)
    assert interaction.kinds() == ["response.send_message"]


async def test_undo_reaches_back_through_a_sleep(retro):
    """The history outlives a hibernate, because the same view does.

    A state is loadable by any instance of the same core build (that is how
    waking a session works at all), so an undo point taken before the game
    went to sleep is still good afterwards. Waking restores the *disk* state
    first, which is where the game was when it slept; the undo then steps
    back from there.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9206, "sleepundo")
    before = view.emulator.frame
    await view._press(retro.interaction(view, message=view.message), "a")
    await retro.cog.hibernate(view, None)
    assert not view.live and view.history

    await undo(retro, view)

    assert view.live, "the undo woke the session up first"
    assert view.emulator.loaded_from == before
    assert not view.history


async def test_two_undos_step_back_two_presses(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9207, "twiceundo")
    frames = [view.emulator.frame]
    for field in ("right", "right"):
        await view._press(retro.interaction(view, message=view.message), field)
        frames.append(view.emulator.frame)
    assert len(view.history) == 2

    await undo(retro, view)
    assert view.emulator.loaded_from == frames[1]
    await undo(retro, view)
    assert view.emulator.loaded_from == frames[0]
    assert not view.history
    # A third click has nothing left and says so rather than failing.
    interaction = await undo(retro, view)
    assert interaction.kinds() == ["response.send_message"]


async def test_the_history_is_bounded_by_its_depth(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9208, "deep")
    assert retro.viewmod.UNDO_DEPTH == 8

    for _ in range(retro.viewmod.UNDO_DEPTH + 5):
        await view._press(retro.interaction(view, message=view.message), "a")

    assert len(view.history) == retro.viewmod.UNDO_DEPTH
    assert view.history_bytes == sum(len(blob) for blob in view.history)


async def test_the_history_is_bounded_by_bytes_as_well_as_by_count(
    retro, monkeypatch
):
    """The count alone bounds nothing: a bigger console has bigger states."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9209, "fat")
    for _ in range(4):
        await view._press(retro.interaction(view, message=view.message), "a")
    one = max(len(blob) for blob in view.history)

    # A cap that fits two and a half of these, so the count (8) is no longer
    # the bound that bites.
    monkeypatch.setattr(retro.viewmod, "MAX_UNDO_BYTES", one * 2 + 1)
    for _ in range(6):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert len(view.history) < retro.viewmod.UNDO_DEPTH
    assert view.history_bytes <= one * 2 + 1
    assert view.history_bytes == sum(len(blob) for blob in view.history)

    # A single state larger than the whole cap keeps exactly one entry: an
    # Undo that cannot undo the press somebody just made is worse than the
    # memory. Same compromise the replay buffer makes.
    monkeypatch.setattr(retro.viewmod, "MAX_UNDO_BYTES", 1)
    for _ in range(3):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert len(view.history) == 1
    await undo(retro, view)
    assert not view.history


async def test_the_undo_s_own_clip_replaces_the_undone_press_s_on_the_message(retro):
    """What the channel is left looking at is where the game actually is.

    This is what is left of a coupling that used to be much larger: Undo had
    to drop the undone press's footage from the replay buffer as well, so a
    stitched replay could not show somebody walking into a room they were not
    in. The buffer is gone, so the whole of it is "the clip on the message is
    the one the undo just recorded" -- and the assertion is on the edits
    themselves, because the message is where the clip lives and what a
    player sees. There is no attribute left to read it off.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9210, "undoclip")
    await view._press(retro.interaction(view, message=view.message), "a")
    pressed = retro.interaction(view, message=view.message)
    await view._press(pressed, "right")
    pressed_clip = pressed.clip()
    assert isinstance(pressed_clip, bytes) and pressed_clip

    undoing = await undo(retro, view)

    assert undoing.clip() != pressed_clip, "a fresh clip was recorded"
    assert isinstance(undoing.clip(), bytes) and undoing.clip()
    # And it is the clip that went out: one edit, one attachment, the undo
    # line above it.
    assert undoing.kinds() == ["response.defer", "edit_original_response"]
    snap = undoing.log[-1][1]
    assert snap["n_attachments"] == 1
    assert snap["content"] == "Tester undid the last press."


async def test_undo_writes_the_state_through_rather_than_waiting(retro):
    """Or a restart straight after an undo would bring the press back.

    The autosave runs every SAVE_STATE_EVERY_PRESSES presses, so the state on
    disk can easily be *newer* than the one Undo has just restored.
    """
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9211, "through")
    state_path = retro.cog._state_path(channel.id, view.slug)
    frames = []
    for field in ("a", "b", "start"):
        await view._press(retro.interaction(view, message=view.message), field)
        frames.append(view.emulator.frame)
    assert state_path.is_file(), "the third press autosaved"
    pressed = state_path.read_bytes()
    assert int(pressed.rstrip(b"\0").split(b":")[1]) == frames[-1]

    await undo(retro, view)

    # What is on disk is the game as the undo left it, not as the press left
    # it -- so a restart, a hibernate or a `[p]retro` now brings back the
    # undone timeline rather than quietly putting the press back.
    assert state_path.read_bytes() != pressed, "the undo was written through"
    assert state_path.read_bytes() == view.emulator.save_state()
    assert view.emulator.loaded_from == frames[-2], "and it is the right moment"
    assert view.press_count == 3, "an undo is not a press and does not count as one"


async def test_a_state_the_core_will_not_take_back_is_reported_once(retro):
    """A core updated mid-session invalidates every state it ever wrote."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9212, "badstate")
    for _ in range(3):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert len(view.history) == 3

    def refuse(data):
        raise retro.emumod.EmulatorError("the core refused to load the save state")

    view.emulator.load_state = refuse
    interaction = await undo(retro, view)

    # One edit, a sentence, and the controls still usable.
    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    snap = interaction.log[-1][1]
    assert "could not be undone" in (snap["content"] or "")
    assert not snap["any_disabled"]
    # The whole history went, rather than being retried press after press:
    # every entry in it came from the same core.
    assert not view.history and view.history_bytes == 0
    assert retro.control(view, "undo").disabled


async def test_an_undo_while_the_session_is_busy_only_defers(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9213, "busyundo")
    await view._press(retro.interaction(view, message=view.message), "a")
    async with view.lock:
        interaction = await undo(retro, view)
    assert interaction.kinds() == ["response.defer"]
    assert view.history, "and the history was not touched"


async def test_a_retired_session_lets_go_of_its_undo_history(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9214, "retiredundo")
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.history

    await retro.cog._retire(view, "Replaced.")

    assert not view.history and view.history_bytes == 0
    assert footage_bytes(view) == 0, "and it is holding no picture either"


async def test_a_core_that_cannot_save_states_costs_undo_and_nothing_else(retro):
    """Pressing buttons matters more than being able to take one back."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9215, "nostates")

    def refuse():
        raise retro.emumod.EmulatorError("This core does not support save states.")

    view.emulator.save_state = refuse
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    assert interaction.log[-1][1]["n_attachments"] == 1, "the press still worked"
    assert not view.history
    assert retro.control(view, "undo").disabled


async def test_the_undo_history_compresses_the_states_it_keeps(retro):
    import zlib

    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9216, "squeezed")
    await view._press(retro.interaction(view, message=view.message), "a")

    blob = view.history[-1]
    assert zlib.decompress(blob).startswith(b"STATE:")
    assert retro.viewmod.UNDO_COMPRESSION_LEVEL == 1
    assert view.history_bytes == len(blob)


# -- Hibernate and resume -----------------------------------------------------


async def test_the_idle_task_saves_and_frees_a_sleeping_session(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9030, "sleepy")
    emulator = view.emulator
    live_ids = [c.custom_id for c in view.children]
    state_path = retro.cog._state_path(channel.id, view.slug)

    await retro.cog.config.session_timeout_minutes.set(10)
    view.last_active = time.time() - 11 * 60
    await retro.cog._hibernate_idle()

    assert view.emulator is None
    assert not emulator.started
    assert state_path.is_file()
    # Sleeping changes nothing about the controls: the next press wakes it.
    assert [c.custom_id for c in view.children] == live_ids
    assert not any(c.disabled for c in retro.pressable(view))
    assert all(c.disabled for c in view.children if isinstance(c, retro.viewmod._SpacerButton))
    assert retro.cog.config.channels[channel.id]["session"]["slug"] == view.slug


async def test_a_press_resumes_a_sleeping_session_and_says_so_once(retro):
    """The resume line rides on the clip now, and is cleared by the next press.

    It used to be shown by the immediate "controls greyed out" edit and
    cleared by the edit that brought the clip -- but that first edit is what
    made the previous clip rewind, so it is gone and the line moved onto the
    one edit a press makes. Past tense, because by the time it is readable
    the game really is back.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9031, "wakeup")
    await retro.cog.hibernate(view, None)
    assert not view.live

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert view.live
    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    landed = interaction.log[-1][1]
    assert landed["content"] == retro.viewmod.RESUMED_NOTE
    assert "Resumed" in landed["content"]
    assert not landed["has_embed"]
    assert landed["n_attachments"] == 1, "and the clip came with it"

    # Said once: the next press replaces it with its own line rather than
    # repeating it.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == "Tester pressed A."


async def test_a_wake_that_has_something_to_report_beats_the_resume_line(retro):
    """Only one line fits, and the important one wins.

    A save state rejected after a core update is something the player has to
    be told; "resumed where you left off" is a courtesy. The wake sets
    view.notice and that is what the single edit carries.
    """
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9032, "wakenotice")
    await retro.cog.hibernate(view, None)
    # A state the fake core will refuse, which is what a core update looks
    # like from load_state()'s point of view.
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"not a state")

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    content = interaction.log[-1][1]["content"] or ""
    assert "save state could not be used" in content, content
    assert retro.viewmod.RESUMED_NOTE not in content
    assert view.notice is None, "and it is not said twice"


# -- Surviving a restart ------------------------------------------------------


async def test_a_session_is_restored_from_config_after_a_restart(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9040, "restarted")
    await retro.cog.hibernate(view, "bye")

    cog2, bot2 = retro.make_cog()
    cog2.config.globals.update(dict(retro.cog.config.globals))
    cog2.config.channels.update({k: dict(v) for k, v in retro.cog.config.channels.items()})
    bot2.channels[channel.id] = channel
    await cog2._restore_sessions()

    restored = cog2.sessions.get(channel.id)
    assert restored is not None
    assert not restored.live, "a restored session starts hibernated"
    assert any(mid == view.message_id for _, mid in bot2.added_views)
    assert restored.slug == view.slug and restored.system.key == view.system.key
    assert restored.hold_ms == await cog2.config.hold_ms()
    # Nothing about the picture survives: a restored session has no clip of
    # its own (it never did, and now no session has one at all) and an empty
    # undo history, so Undo comes back greyed out.
    assert footage_bytes(restored) == 0
    assert not restored.can_undo and retro.control(restored, "undo").disabled

    restored.message = view.message
    await restored._press(retro.interaction(restored, message=view.message), "a")
    assert restored.live


# -- Graceful failure ---------------------------------------------------------


async def test_a_pruned_rom_leaves_the_controls_usable_and_explains(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9050, "pruned")
    await retro.cog.hibernate(view, None)
    retro.cog._rom_path(view.rom_filename).unlink()
    retro.cog._state_path(channel.id, view.slug).unlink(missing_ok=True)

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert not any(getattr(c, "disabled", False) for c in retro.pressable(view))
    note = interaction.log[-1][1]["content"] or ""
    assert "Start the game again" in note
    assert not any(snap.get("has_attachments") for _, snap in interaction.log)


async def test_a_session_whose_core_was_removed_says_which_one(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9051, "coreless")
    await retro.cog.hibernate(view, None)
    await retro.cog.config.cores.set({})

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert "gambatte" in (interaction.log[-1][1]["content"] or "")


# -- Presets and switching games ----------------------------------------------


async def test_saved_games_can_be_added_listed_and_removed(retro):
    channel = retro.channel(9060)
    ctx = retro.context(channel)
    add = retro.cogmod.Retro.retroset_game_add.callback
    remove = retro.cogmod.Retro.retroset_game_remove.callback
    listing = retro.cogmod.Retro.retroset_game_list.callback

    await listing(retro.cog, ctx)
    assert "No games are saved" in ctx.sent[-1]
    await add(retro.cog, ctx, "Tobu Tobu", "https://example.com/tobu.gb")
    assert await retro.cog.config.games() == {"tobu-tobu": "https://example.com/tobu.gb"}
    await add(retro.cog, ctx, "bad", "ftp://example.com/x.gb")
    assert "must start with" in ctx.sent[-1]
    await remove(retro.cog, ctx, "Tobu Tobu")
    assert await retro.cog.config.games() == {}


async def test_a_preset_name_resolves_to_its_url_and_then_resumes(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9061)
    ctx = retro.context(channel)
    await retro.cogmod.Retro.retroset_game_add.callback(
        retro.cog, ctx, "ucity", "https://example.com/ucity.gbc"
    )

    seen = []

    async def fetch(ctx_, url):
        seen.append(url)
        return "ucity.gbc", ROM_BYTES

    retro.cog._fetch_rom = fetch
    play = retro.cogmod.Retro.retro.callback

    await play(retro.cog, ctx, game="ucity")
    assert seen == ["https://example.com/ucity.gbc"]
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.live

    seen.clear()
    await play(retro.cog, ctx, game="ucity")
    assert seen == [], "the same preset resumes rather than downloading again"

    await play(retro.cog, ctx, game=None)
    assert "controls to play" in ctx.sent[-1]

    await play(retro.cog, ctx, game="nope")
    assert "no saved game called" in ctx.sent[-1]


async def test_a_different_game_replaces_the_session_and_retires_the_old_one(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9062)
    ctx = retro.context(channel)
    old = await retro.start_game(ctx, "firstgame")
    old_slug = old.slug

    retro.serve("otherga.gbc", ROM_BYTES)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/otherga.gbc"
    )
    new = retro.cog.sessions.get(channel.id)
    assert new is not None and new is not old and new.slug != old_slug
    assert retro.cog._state_path(channel.id, old_slug).is_file()

    # The old message keeps exactly one working button: Resume.
    retired = retro.cog.retired[old.message_id]
    assert [child.custom_id for child in retired.children] == [
        f"{retro.viewmod.CUSTOM_ID_PREFIX}:resume"
    ]
    assert retired.record["slug"] == old_slug
    edit = old.message.edits[-1]
    assert edit["view"] is retired
    assert "Resume" in edit["content"]

    stale = retro.interaction(old, message=old.message)
    await old._press(stale, "a")
    assert stale.kinds() == ["response.defer"], "a click on the replaced message is a no-op"
    assert sum(1 for v in retro.cog.sessions.values() if v.live) <= retro.cogmod.MAX_LIVE_EMULATORS


# -- The bare command ---------------------------------------------------------


async def test_the_no_argument_help_mentions_presets_and_every_console(retro):
    channel = retro.channel(9070)
    ctx = retro.context(channel)
    await retro.install_cores("gambatte")
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game=None)
    said = ctx.said()
    assert "by name" in said or "retroset game" in said
    assert "Game Boy" in said
    assert ".gb" in said and "`.gb` or `.gbc`" not in said, "it is not Game Boy only any more"


# -- retrostop ----------------------------------------------------------------


async def test_retrostop_saves_frees_and_keeps_the_session(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9080, "stopme")
    emulator = view.emulator

    await retro.cogmod.Retro.retrostop.callback(retro.cog, ctx)
    assert view.emulator is None and not emulator.started
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    assert retro.cog.sessions.get(channel.id) is view
    assert not any(c.disabled for c in retro.pressable(view))
    assert "carry on" in ctx.sent[-1]
    stopped = view.message.edits[-1] if view.message.edits else {}
    assert "Stopped by Tester." in (stopped.get("content") or "")
    assert "embed" not in stopped


async def test_the_names_the_stop_and_reset_replies_carry_are_sanitised_too(retro):
    """Both are prose rather than the press line, and both name somebody.

    The reset one matters most: it is a plain channel message rather than an
    edit, so escaping the name is the only thing standing between a nickname
    of "@everyone" and a notification.
    """
    await retro.install_cores("gambatte")
    nasty = FakeUser(uid=77, name="@everyone **<@111111111111111111>** # big")

    view, ctx, _ = await retro.posted_game(9082, "nastystop")
    ctx.author = nasty
    view.starter_id = nasty.id
    await retro.cogmod.Retro.retrostop.callback(retro.cog, ctx)
    stopped = (view.message.edits[-1].get("content") or "")
    assert "Stopped by " in stopped, stopped

    view, ctx, _ = await retro.posted_game(9083, "nastyreset")
    ctx.author = nasty
    view.starter_id = nasty.id
    await retro.cogmod.Retro.retroreset.callback(retro.cog, ctx)
    # The reply itself, not ctx.said() -- that joins in the repr of the
    # game's own post, angle brackets and all.
    reply = ctx.sent[-1]
    assert "has been reset by " in reply, reply

    for text in (stopped, reply):
        assert "@everyone" not in text, text
        assert not re.search(r"(?<!\\)<", text), text
        assert "\\*\\*" in text, "the markdown in the name was escaped"

    # And somebody with no usable name leaves a sentence that still reads.
    view, ctx, _ = await retro.posted_game(9084, "namelessstop")
    ctx.author = types.SimpleNamespace(id=78)
    view.starter_id = 78
    await retro.cogmod.Retro.retrostop.callback(retro.cog, ctx)
    assert "Stopped. Press a button" in (view.message.edits[-1].get("content") or "")


async def test_retrostop_saves_the_game_even_if_the_message_explodes(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9081, "explodes")
    emulator = view.emulator

    async def boom(*args, **kwargs):
        raise RuntimeError("message is gone")

    view.refresh = boom
    state = retro.cog._state_path(channel.id, view.slug)
    state.unlink(missing_ok=True)
    await retro.cogmod.Retro.retrostop.callback(retro.cog, ctx)
    assert not emulator.started
    assert state.is_file()


# -- retroreset ---------------------------------------------------------------
#
# Rebooting the running game, which the cog's author asked for as a *command*
# and not a button: it throws away everybody's progress-in-flight, so it has
# the permission check `[p]retrostop` has rather than being one more thing a
# passer-by can click. `[p]retrosaves reset` is a different command entirely
# (it deletes a save state file on disk), and the help text of each says so.


def reset_command(retro):
    return retro.cogmod.Retro.retroreset.callback


async def test_retroreset_reboots_the_running_game(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9090, "resetme")
    emulator = view.emulator
    for _ in range(3):
        await view._press(retro.interaction(view, message=view.message), "a")
    played = emulator.frame
    assert played > 0

    await reset_command(retro)(retro.cog, ctx)

    # The same core, power-cycled: FakeEmulator's frame counter *is* its
    # machine state, so being back near zero is "the game booted again".
    assert view.emulator is emulator, "the core was not swapped out"
    assert emulator.resets == 1
    boot = emulator.frames_for_seconds(retro.viewmod.BOOT_SECONDS)
    expected = 1 + boot + view.clip_frames(emulator)
    assert emulator.frame == expected, (emulator.frame, expected)
    assert emulator.frame < played, "the game was not rebooted"
    assert view.live, "and it is still playable"


async def test_retroreset_puts_the_boot_clip_on_the_game_s_message(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9091, "resetclip")
    await view._press(retro.interaction(view, message=view.message), "a")
    before = retro.shown_clip(view)

    await reset_command(retro)(retro.cog, ctx)

    assert retro.shown_clip(view) != before, "a fresh clip was recorded"
    # One edit of the game's own message, carrying the clip and the line that
    # says what happened -- the same shape a press makes.
    edit = view.message.edits[-1]
    assert edit["content"] == "Tester reset the game."
    assert len(edit["attachments"]) == 1
    assert edit["attachments"][0].filename.endswith(".webp")
    assert not any(c.disabled for c in retro.pressable(view))
    # ...and the reply in the channel says what happened and how to get back.
    said = ctx.said()
    assert "has been reset" in said
    assert "Undo" in said
    assert "retrosaves reset" in said, "the other reset is named, so the two cannot be confused"


async def test_retroreset_is_an_undo_point_rather_than_a_dead_end(retro):
    """A reset is the most destructive thing here, so Undo absorbs it.

    The state the reset threw away is pushed onto the history first, so one
    click of Undo puts the player back where they were -- and the entries
    from before it are left alone, because they came from the same core and
    the same ROM and still mean "the machine N presses ago".
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9092, "resetundo")
    frames = []
    for field in ("a", "right"):
        await view._press(retro.interaction(view, message=view.message), field)
        frames.append(view.emulator.frame)
    assert len(view.history) == 2

    await reset_command(retro)(retro.cog, ctx)
    assert len(view.history) == 3, "the pre-reset moment went onto the history"
    assert not retro.control(view, "undo").disabled

    # One click and the game is back at the moment before the reset.
    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "undo").callback(interaction)
    assert view.emulator.loaded_from == frames[-1]
    assert len(view.history) == 2, "and the older undo points are still there"


async def test_retroreset_does_not_overwrite_the_save_state_on_disk(retro):
    """The decision: a reset must not silently replace a good save.

    Nothing is written when the reset happens, so the ``.state`` file still
    holds the moment before it -- which is what makes an accidental reset
    recoverable even across a restart. It is replaced when the game saves of
    its own accord, i.e. a few presses later or when it next sleeps.
    """
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9093, "resetdisk")
    state_path = retro.cog._state_path(channel.id, view.slug)
    for field in ("a", "b", "start"):
        await view._press(retro.interaction(view, message=view.message), field)
    assert state_path.is_file(), "the third press autosaved"
    before = state_path.read_bytes()

    await reset_command(retro)(retro.cog, ctx)

    assert state_path.read_bytes() == before, "the reset overwrote the save state"
    assert int(before.rstrip(b"\0").split(b":")[1]) > view.emulator.frame

    # ...and it is the game carrying on that replaces it, not the reset.
    for field in ("a", "b", "start"):
        await view._press(retro.interaction(view, message=view.message), field)
    assert state_path.read_bytes() != before


async def test_retroreset_leaves_the_in_game_battery_save_alone(retro):
    """The player's own save, which a real console's reset never wiped."""
    from .fakes import FakeEmulator

    FakeEmulator.sram_bytes = 2048
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9094, "resetsram")
    saved = bytes((i * 7) % 251 for i in range(2048))
    assert view.emulator.load_sram(saved) is True

    await reset_command(retro)(retro.cog, ctx)

    assert view.emulator.save_sram() == saved, "the reset wiped the in-game save"


async def test_retroreset_wakes_a_sleeping_session_first(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9095, "resetasleep")
    await view._press(retro.interaction(view, message=view.message), "a")
    await retro.cog.hibernate(view, None)
    assert not view.live

    await reset_command(retro)(retro.cog, ctx)

    assert view.live, "the reset woke the session up"
    assert view.emulator.resets == 1
    assert "has been reset" in ctx.said()


async def test_retroreset_with_no_game_running_says_so(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9096)
    ctx = retro.context(channel)

    await reset_command(retro)(retro.cog, ctx)

    said = ctx.said()
    assert "No game is running in this channel." in said
    assert "retro <name or url>" in said


async def test_only_the_starter_a_moderator_or_the_owner_may_reset(retro):
    """The same gate `[p]retrostop` has, and for a stronger reason."""
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9097, "resetperms")
    starter = ctx.author
    assert view.starter_id == starter.id

    stranger = retro.context(channel, author=FakeUser(uid=99))
    await reset_command(retro)(retro.cog, stranger)
    assert "can reset it" in stranger.said()
    assert view.emulator.resets == 0, "a stranger rebooted somebody's game"

    for author in (
        starter,
        FakeUser(uid=98, manage_messages=True),  # a moderator
        FakeUser(uid=1),                         # the bot owner
    ):
        allowed = retro.context(channel, author=author)
        await reset_command(retro)(retro.cog, allowed)
        assert "has been reset" in allowed.said(), author.id
    assert view.emulator.resets == 3

    # And it is exactly the check retrostop uses, rather than a second copy.
    assert await view.can_stop(starter) is True
    assert await view.can_stop(FakeUser(uid=99)) is False


async def test_a_core_that_will_not_reset_leaves_the_game_alone(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9098, "resetfails")
    await view._press(retro.interaction(view, message=view.message), "a")
    frame = view.emulator.frame

    def refuse():
        raise retro.emumod.EmulatorError("the core could not be reset")

    view.emulator.reset = refuse
    await reset_command(retro)(retro.cog, ctx)

    assert "could not be reset" in ctx.said()
    assert view.emulator.frame == frame, "the game moved even though the reset failed"
    assert view.live


def test_the_two_reset_commands_each_say_they_are_not_the_other(retro):
    """The one thing somebody about to run either of them has to understand.

    `[p]retroreset` reboots the game that is playing; `[p]retrosaves reset`
    deletes a save state file on disk for any game the channel has played.
    Both help texts name the other and say what the difference is, because
    the names are one word apart and the consequences are not.
    """
    # The callback's docstring, which is what Red turns into the help text:
    # `Command.__doc__` is the *class*'s docstring under the real Red.
    reboot = retro.cogmod.Retro.retroreset.callback.__doc__
    drop = retro.cogmod.Retro.retrosaves_reset.callback.__doc__

    assert "[p]retrosaves reset" in reboot
    assert "This one reboots" in reboot and "playing right now" in reboot
    assert "Nothing on disk is deleted or overwritten" in reboot

    assert "[p]retroreset" in drop
    assert "touches no file at all" in drop
    assert "deletes a save state *file* on disk" in drop


async def test_there_is_no_reset_button_under_the_screen(retro):
    """The author's instruction: a command, and deliberately not a button."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9099, "nobutton")
    ids = [c.custom_id for c in view.children]
    assert not any("reset" in i for i in ids), ids
    labels = [(c.label or "") for c in view.children]
    assert not any("Reset" in label for label in labels), labels


# -- Unloading ----------------------------------------------------------------


async def test_cog_unload_saves_every_live_game_and_frees_the_cores(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9090, "unloadme")
    emulator = view.emulator

    await retro.cog.cog_unload()
    assert not emulator.started
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    assert retro.cog._idle_task is None
    assert retro.cog.sessions == {}


async def test_a_cancelled_save_still_frees_the_core_it_was_saving(retro):
    """The teardown case ``try`` used to miss entirely.

    ``_hibernate_locked`` takes the emulator out of the view into a local,
    writes the save state, and only then stops the core. A CancelledError
    from that write -- a bot shutdown, or `[p]unload retro` landing on it --
    is not an Exception, so it went straight past everything and left the
    last reference to a *loaded* core on a dead stack frame. Nothing could
    ever stop it again, and MAX_LIVE_EMULATORS is 1.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9091, "cancelmidsave")
    emulator = view.emulator
    assert emulator.started

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    retro.cog._write_state = cancelled

    with pytest.raises(asyncio.CancelledError):
        await retro.cog.hibernate(view, "going away")

    assert not emulator.started, "the core was abandoned while still loaded"
    assert view.emulator is None
    # ...and the lock it was holding is free, so the cog is not wedged.
    assert not retro.cog.emulator_lock.locked()


async def test_one_cancelled_hibernate_does_not_abandon_the_other_sessions(retro):
    """`except Exception` never caught this, so the loop ended at the first.

    Every session after the cancelled one kept its core and never wrote its
    progress. There may be several: only one is *live* at a time, but the
    sleeping ones still have to be released, and the live one is whichever
    channel pressed a button last.
    """
    await retro.install_cores("gambatte")
    views = []
    for index in range(3):
        view, _, _ = await retro.posted_game(9092 + index, f"unloadmany{index}")
        views.append(view)
    # Starting a game evicts whatever was playing, so all three have held a
    # core and only the newest still has one. That is the shape the unload
    # loop actually walks.
    for view in views[:-1]:
        assert not view.live
    assert views[-1].live

    real_hibernate = retro.cogmod.Retro.hibernate
    calls = []

    async def cancel_the_first(self, view, reason=None):
        calls.append(view)
        if len(calls) == 1:
            raise asyncio.CancelledError()
        return await real_hibernate(self, view, reason)

    retro.cogmod.Retro.hibernate = cancel_the_first
    try:
        with pytest.raises(asyncio.CancelledError):
            await retro.cog.cog_unload()
    finally:
        retro.cogmod.Retro.hibernate = real_hibernate

    # Every session was dealt with, not just the ones before the cancellation.
    assert len(calls) == 1, "the loop kept awaiting after it had been cancelled"
    assert retro.cog.sessions == {}
    for view in views:
        assert view.closed and view.is_finished(), "a view was left registered"
        assert view.emulator is None, "a core was abandoned by the unload"
    assert not any(e.started for e in FakeEmulator.instances), (
        "a libretro core survived the unload"
    )
    # And the live one's progress was written on the way out rather than lost.
    assert retro.cog._state_path(views[-1].channel_id, views[-1].slug).is_file()


# -- Cache pruning ------------------------------------------------------------


async def test_pruning_keeps_the_newest_few_and_the_current_game(retro):
    channel_id = 9100
    roms = retro.cog._roms_dir()
    made = []
    for index in range(retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL + 3):
        path = roms / f"{channel_id}-game{index}.gb"
        path.write_bytes(b"x" * 0x800)
        retro.cog._state_path(channel_id, f"game{index}").write_bytes(b"s")
        os.utime(path, (1000 + index, 1000 + index))
        made.append(path)
    neighbour = roms / f"{channel_id}0-neighbour.gb"
    neighbour.write_bytes(b"y" * 0x800)

    retro.cog._prune_cached_games(channel_id, keep_slug="game7")

    alive = [p for p in made if p.is_file()]
    assert len(alive) == retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL, [p.name for p in alive]
    assert (roms / f"{channel_id}-game7.gb").is_file(), "the current game is always kept"
    assert not (roms / f"{channel_id}-game0.gb").is_file(), "the oldest goes"
    assert neighbour.is_file(), "another channel's cache is untouched"
