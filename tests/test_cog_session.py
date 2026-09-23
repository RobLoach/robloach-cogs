"""A session's whole life: start, press, sleep, wake, restart, stop.

The real Retro and the real RetroView, driven against the fakes in
tests/fakes.py. Nothing here needs a libretro core.
"""

import asyncio
import os
import re
import threading
import time

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
    history_is_consistent,
)

# -- Defaults -----------------------------------------------------------------


async def test_the_clip_and_hold_defaults_reach_a_new_install(retro):
    from retro.emulator import CLIP_SECONDS

    assert CLIP_SECONDS == 1.0
    assert await retro.cog.config.clip_seconds() == 1.0
    assert await retro.cog.config.hold_ms() == retro.viewmod.DEFAULT_HOLD_MS == 160


async def test_an_already_configured_value_survives_a_new_default(retro):
    await retro.cog.config.clip_seconds.set(4)
    await retro.cog.config.hold_ms.set(420)
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8050, "defaults")
    assert view.clip_seconds == 4
    assert view.hold_ms == 420


async def test_an_integer_clip_length_in_config_still_loads_as_a_float(retro):
    """Nobody who set `[p]retroset cliplength 4` before it was a float."""
    await retro.cog.config.clip_seconds.set(4)  # an int, as it was written
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8049, "legacy")
    assert view.clip_seconds == 4.0 and isinstance(view.clip_seconds, float)
    assert view.clip_frames(view.emulator) == view.emulator.frames_for_seconds(4)


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
    assert await retro.cog.config.clip_seconds() == retro.emumod.MAX_CLIP_SECONDS == 5.0


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
    # The *default* hold fits even at the floor now: a 12 frame clip's input
    # budget is frame 11 (see clips.input_budget, whose cadence moved when
    # capture_plan started photographing the end of each span), which is more
    # than the ten frames 160ms asks for. It used to be frame 8, i.e. ~133ms.
    assert "held for about" not in ctx.sent[-1], ctx.sent[-1]
    # Not "greyed out" any more: at one tap the button is not drawn at all,
    # and the reply says so and says it comes back. See MIN_REPEAT_TAPS.
    assert "is not shown at this length" in ctx.sent[-1], ctx.sent[-1]
    assert "comes back" in ctx.sent[-1], ctx.sent[-1]
    assert "greyed out" not in ctx.sent[-1], ctx.sent[-1]

    # A hold the floor really cannot honour still says so, which is what the
    # note is for: 400ms is 24 frames and the clip can only spare 11.
    await retro.cog.config.hold_ms.set(400)
    await cliplength(retro.cog, ctx, 0.2)
    assert "held for about 183ms rather than the 400ms" in ctx.sent[-1], ctx.sent[-1]


async def test_retroset_cliplength_reaches_live_sessions_and_their_buttons(retro):
    """Changing the clip length re-draws the row on the next press.

    Both ways round, which is the whole point of the button being hidden
    rather than greyed out: at 0.2s only one tap fits, so the x3 button goes
    off the message entirely, and at 4s it comes back -- in its proper place
    in the row, between Wait and Undo, rather than tacked on the end.

    Off the *message*: the button object stays in the view either way, so a
    click on a message Discord has not re-rendered still reaches a callback
    that answers it. See RetroView.to_components.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(8054, "relength")
    cliplength = retro.cogmod.Retro.retroset_cliplength.callback
    assert retro.control(view, "repeat").label == "A x3"
    assert not retro.control(view, "repeat").disabled
    assert retro.drawn(view, "repeat")["label"] == "A x3"

    await cliplength(retro.cog, ctx, 0.2)
    assert view.clip_seconds == 0.2
    # The next press redraws the controls, and the repeat button -- which can
    # now do no more than the console's own A button -- is gone from them.
    vanishing = retro.interaction(view, message=view.message)
    await view._press(vanishing, "a")
    assert view.repeat_taps == 1
    assert retro.drawn(view, "repeat") is None
    assert retro.control(view, "repeat").hidden, "hidden, not removed"
    # ...and that press says where it went, once. A control that silently
    # disappears reads as removed just as surely as a greyed-out one does,
    # which is exactly how the greyed-out version was reported. It rides on
    # the edit the press was making anyway, so it costs no extra edit.
    said = vanishing.log[-1][1]["content"]
    assert vanishing.kinds() == ["response.defer", "edit_original_response"]
    assert "**A x3** button is hidden" in said, said
    assert "cliplength" in said
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert "hidden" not in again.log[-1][1]["content"], "said once"
    assert view.clip_frames(view.emulator) == 12
    # Wait and Undo have not moved, because the row still reserves space for
    # all three controls whether or not the third is drawn.
    assert retro.drawn_row(view) == ["Start", "Select", "Wait", "Undo"]

    coming_back = retro.interaction(view, message=view.message)
    await cliplength(retro.cog, ctx, 4)
    await view._press(coming_back, "a")
    back = retro.drawn(view, "repeat")
    assert back is not None and back["label"] == "A x3" and not back["disabled"]
    # Coming back says nothing: the button is right there saying what it does.
    assert "hidden" not in coming_back.log[-1][1]["content"]
    assert retro.drawn_row(view) == ["Start", "Select", "Wait", "A x3", "Undo"]


async def test_can_stop_is_kept_for_retrosleep_reboot_and_end(retro):
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
    # The fix for "pressing right walks two tiles": a direction is
    # held for exactly as long as a face button now.
    assert up_hold == a_hold
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
    clip = view.run_press("a")
    assert emulator.last_presses == view._schedule(emulator, "a", 1)
    # Animated WebP is the only format a clip is ever encoded in; see
    # CLIP_EXTENSION in retro/clips.py for why GIF is not worth having back.
    assert clip[:4] == b"RIFF" and clip[8:12] == b"WEBP"
    assert view.screen_filename.endswith(".webp")


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


async def test_the_first_post_is_a_clip_with_one_line_and_no_embed(retro):
    """No card, no embed -- and no longer *nothing* either.

    The first clip of a cold boot used to go out with ``content=None``, so
    the whole message was an animation and a grid of arrows: somebody
    scrolling into the channel had no way of knowing what was being played.
    It now carries the one stable line the header provides, which is the
    same line every press rewrites.
    """
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(8400, "layout")
    posted = list(channel.messages.values())[-1]
    assert posted.kwargs.get("embed") is None
    assert getattr(posted.kwargs.get("file"), "filename", "").endswith(".webp")
    assert posted.kwargs.get("content") == "**layout**"
    assert posted.kwargs["content"] == retro.line(view)
    assert view._content() == retro.line(view)


async def test_the_line_names_the_game_before_anything_else(retro):
    """The whole line, spelled out, against a known game.

    The console used to sit between the two -- ``**ucity** \N{MIDDLE DOT} Game
    Boy \N{EM DASH} Tester pressed A.`` -- and is gone: the picture, the boot
    logo and the controller underneath all say which console it is, and the
    game's name is the thing none of them say. See HEADER.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8410, "ucity")
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert (
        interaction.log[-1][1]["content"]
        == "**ucity** \N{MIDDLE DOT} Tester pressed A."
    )


async def test_the_header_says_when_the_session_is_asleep(retro):
    """Legible while asleep, and legible again the moment it wakes.

    Both halves ride on edits that were happening anyway: the one that puts
    the session to sleep, and the one the waking press makes. See
    ASLEEP_MARK and RESUMED_NOTE.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8411, "sleepy")
    assert "asleep" not in view.header

    await retro.cog.hibernate(view, "Put to sleep.")
    assert not view.live
    assert view.header == "**sleepy** \N{MIDDLE DOT} asleep"
    # ...and that header is on the message the sleep edited, unasked.
    said = retro.message_edit(view)["content"]
    assert said.startswith(view.header) and "Put to sleep." in said

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    woken = interaction.log[-1][1]["content"]
    assert "asleep" not in woken
    assert retro.viewmod.RESUMED_NOTE in woken


async def test_a_one_off_notice_is_shown_once_and_then_forgotten(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8401, "notice")
    view.notice = "something happened"
    assert view._content() == retro.line(view, "something happened")
    assert view._content() == retro.line(view)


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
    # One line of text -- what game it is, then which button it was -- and
    # nothing else. See the "Which button was pressed" section below.
    assert final["content"] == retro.line(view, "Tester pressed A.")
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

    original = view.capture_press
    seen = []

    def watched(field, repeat=1):
        # What the message had been told by the time the emulator was asked.
        seen.append(list(interaction.kinds()))
        return original(field, repeat)

    view.capture_press = watched
    try:
        await view._press(interaction, "a")
    finally:
        view.capture_press = original

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

    original = view.capture_press
    disabled_midway = []

    def watched(field, repeat=1):
        disabled_midway.append(
            [c.custom_id for c in retro.pressable(view) if c.disabled]
        )
        return original(field, repeat)

    view.capture_press = watched
    try:
        await view._press(interaction, "a")
    finally:
        view.capture_press = original

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


# -- Overlapping presses are queued, not dropped ------------------------------
#
# A press holds the session's lock for about a second, and a click that
# arrived during it used to be *dropped*: deferred so Discord never said
# "interaction failed", and then forgotten. In a channel with two or three
# people playing, most clicks land in that second, so the controller felt
# intermittently dead -- which is exactly how it was reported.
#
# It is queued now, under four rules, and each of them is a test below:
# bounded depth, one waiting press per person, in order, and *visible* -- the
# acknowledgement is a suffix on the line the running press is already
# rewriting, so a queued click still costs no edit of its own.


def queued_names(view):
    """What is waiting, as the listing spells it: button names only.

    The presser used to be named in front of each one and is not any more;
    see QUEUE_ENTRY, and ``Pending.who`` -- which is still captured -- for
    what putting it back would use.
    """
    return [view.queued_label(entry) for entry in view.queue]


async def test_a_press_that_lands_mid_emulation_is_queued_rather_than_dropped(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9010, "busy")
    async with view.lock:
        held = retro.interaction(view, message=view.message)
        await view._press(held, "b")
        repeat = retro.interaction(view, message=view.message)
        await retro.control(view, "repeat").callback(repeat)
        waiting = retro.interaction(view, message=view.message)
        await retro.control(view, "wait").callback(waiting)

        # Every click is acknowledged with a plain defer and nothing else:
        # no edit, and no line naming a press that has not happened yet.
        # Their acknowledgement is the queue suffix on the running press's
        # own line.
        for click in (held, repeat, waiting):
            assert click.kinds() == ["response.defer"]
        # All three are the same person and all three are kept: one person
        # may hold every slot. See MAX_QUEUED_PRESSES.
        assert len(view.queue) == 3
    assert queued_names(view) == ["B", "A x3", "wait"]


async def test_a_full_queue_says_so_rather_than_swallowing_the_click(retro):
    """
    The other refusal, and the reason both are worth a sentence.

    A press that cannot be queued is a press that will never happen, and the
    two reasons it can be refused are genuinely different things to be told:
    "wait your turn" and "you are already in the queue". Both used to be
    answered with a defer that changes nothing on screen -- i.e. with
    nothing.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9011, "full")
    async with view.lock:
        # Fill every slot with somebody different, so the refusal below is
        # about the queue being full rather than about one person's slot.
        for index in range(retro.viewmod.MAX_QUEUED_PRESSES):
            clicker = FakeUser(uid=600 + index, name=f"P{index}")
            await view._press(
                retro.interaction(view, user=clicker, message=view.message), "a"
            )
        assert len(view.queue) == retro.viewmod.MAX_QUEUED_PRESSES

        latecomer = retro.interaction(
            view, user=FakeUser(uid=699, name="Late"), message=view.message
        )
        await view._press(latecomer, "b")

    assert latecomer.kinds() == ["response.send_message"]
    (_, said), = latecomer.log
    assert said["ephemeral"] is True
    assert str(retro.viewmod.MAX_QUEUED_PRESSES) in said["content"]
    assert len(view.queue) == retro.viewmod.MAX_QUEUED_PRESSES, "and nothing jumped in"


async def test_the_queue_is_bounded_and_says_how_deep(retro):
    """Each waiting press is a second of latency against a state nobody saw."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9012, "deep")
    assert 3 <= retro.viewmod.MAX_QUEUED_PRESSES <= 5

    async with view.lock:
        accepted = []
        # One more clicker than there is room for, each a different person.
        for index in range(retro.viewmod.MAX_QUEUED_PRESSES + 1):
            clicker = FakeUser(uid=500 + index, name=f"P{index}")
            interaction = retro.interaction(view, user=clicker, message=view.message)
            accepted.append(view.enqueue_press(interaction, "a"))
        assert accepted == [True] * retro.viewmod.MAX_QUEUED_PRESSES + [False]
        assert len(view.queue) == retro.viewmod.MAX_QUEUED_PRESSES


async def test_one_person_may_hold_every_waiting_slot(retro):
    """Walking four tiles is four clicks in a row, and all of them count.

    There used to be a one-waiting-press-per-person rule, on the theory that
    it made a roomful of people take turns. What it did in practice was
    refuse the second, third and fourth click of the commonest thing anybody
    does with a d-pad, so the controller felt dead for exactly the person
    using it most. The depth cap is what bounds the queue now; see
    MAX_QUEUED_PRESSES.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9013, "fair")
    rob = FakeUser(uid=11, name="Rob")

    depth = retro.viewmod.MAX_QUEUED_PRESSES
    async with view.lock:
        # A run as long as the queue is deep, all from one person. Derived
        # from the constant rather than written out, so raising the depth
        # cannot leave this test quietly checking less than it says.
        for index in range(depth):
            field = "up" if index % 2 == 0 else "down"
            assert view.enqueue_press(retro.interaction(view, user=rob), field)
        # The cap still bites, and it bites on depth rather than on who.
        assert not view.enqueue_press(retro.interaction(view, user=rob), "down")
        assert len(view.queue) == depth
        assert [entry.who for entry in view.queue] == ["Rob"] * depth


async def test_one_person_s_run_reads_as_one_run(retro):
    """`Rob ⬆️⬆️⬇️` rather than three unrelated-looking entries."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9015, "runs")
    rob = FakeUser(uid=11, name="Rob")
    ada = FakeUser(uid=12, name="Ada")
    up = "\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"
    down = "\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"
    left = "\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"

    async with view.lock:
        assert view.enqueue_press(retro.interaction(view, user=rob), "up")
        assert view.enqueue_press(retro.interaction(view, user=rob), "down")
        assert view.queue_note() == retro.queued(("Rob", up, down))

        # A different person starts a new run, and the order is never
        # rearranged: the queue order is what the queue *is*.
        assert view.enqueue_press(retro.interaction(view, user=ada), "left")
        assert view.queue_note() == retro.queued(("Rob", up, down), ("Ada", left))
        assert view.queued_runs() == [("Rob", [up, down]), ("Ada", [left])]


async def test_the_running_press_says_what_is_queued_behind_it(retro):
    """The whole acknowledgement, and it costs no extra edit.

    "Rob pressed A. *Queued: ⬅️*" -- one line, on the one edit Rob's
    press was making anyway. An ephemeral "your press is queued" would be a
    second message per click, and any edit of this message would re-render
    the attachment and visibly rewind the clip.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9014, "ackqueue")
    ada = FakeUser(uid=12, name="Ada")

    rob = retro.interaction(
        view, user=FakeUser(uid=11, name="Rob"), message=view.message
    )
    original = view.capture_press
    waiting = []

    def slow(field, repeat=1):
        # Ada clicks while Rob's press is being emulated, which is the case
        # that used to lose the input entirely.
        if not waiting:
            interaction = retro.interaction(view, user=ada, message=view.message)
            waiting.append(interaction)
            assert view.enqueue_press(interaction, "left")
        return original(field, repeat)

    view.capture_press = slow
    try:
        await view._press(rob, "a")
    finally:
        view.capture_press = original

    robs_edit = next(snap for kind, snap in rob.log if kind == "edit_original_response")
    assert robs_edit["content"] == retro.line(
        view,
        "Rob pressed A.",
        suffixes=[
            retro.queued(
                ("Ada", "\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}")
            )
        ],
    )
    # One edit for Rob's press, exactly as before the queue existed.
    assert rob.kinds() == ["response.defer", "edit_original_response"]
    # And Ada's press then ran on her own interaction, with its own single
    # edit and no mention of a queue, because hers is empty now.
    ada_interaction = waiting[0]
    assert ada_interaction.kinds() == ["response.defer", "edit_original_response"]
    assert ada_interaction.log[-1][1]["content"] == retro.line(
        view, "Ada pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}."
    )


async def test_two_simultaneous_presses_both_happen_one_edit_each(retro):
    """Neither is lost, and neither costs a second edit.

    This used to assert that the loser produced *nothing at all*. That was
    the bug: two people pressing meant one press.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9011, "race")
    original = view.capture_press
    original_enqueue = view.enqueue_press
    # The first press's worker thread is held until the second click has
    # actually been taken down, which is what makes this a race rather than
    # two presses in sequence -- and is exactly what four tenths of a second
    # of `time.sleep` was buying, less reliably and 400ms more slowly.
    queued = threading.Event()

    def slow(field, repeat=1):
        assert queued.wait(10), "the second press never arrived"
        return original(field, repeat)

    def enqueue(*args, **kwargs):
        accepted = original_enqueue(*args, **kwargs)
        queued.set()
        return accepted

    view.capture_press = slow
    view.enqueue_press = enqueue
    first = retro.interaction(
        view, user=FakeUser(uid=21, name="Rob"), message=view.message
    )
    second = retro.interaction(
        view, user=FakeUser(uid=22, name="Ada"), message=view.message
    )
    try:
        await asyncio.gather(view._press(first, "a"), view._press(second, "b"))
    finally:
        view.capture_press = original
        view.enqueue_press = original_enqueue

    # One defer and one edit each: "one press, one visible change" holds for
    # both of them rather than for whichever won the race.
    assert first.kinds() == ["response.defer", "edit_original_response"]
    assert second.kinds() == ["response.defer", "edit_original_response"]
    clips = sum(
        1 for i in (first, second) for _, snap in i.log if snap.get("has_attachments")
    )
    assert clips == 2
    assert not view.queue and not view._draining


async def test_a_queued_press_runs_against_the_state_it_was_queued_behind(retro):
    """Order, and that the queue really is intent rather than work.

    FakeEmulator's machine state is its frame counter, so "which state did
    this press see" is answerable exactly: the queued press must have been
    emulated after the one it was queued behind, never in parallel with it
    and never against the state from before it.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9015, "ordered")
    emulator = view.emulator
    original = view.capture_press
    seen = []
    queued = []

    def watched(field, repeat=1):
        seen.append((field, emulator.frame))
        if not queued:
            # Two more people click while the first press is emulating.
            for uid, who, button in ((31, "Ada", "b"), (32, "Sam", "start")):
                interaction = retro.interaction(
                    view, user=FakeUser(uid=uid, name=who), message=view.message
                )
                queued.append(interaction)
                assert view.enqueue_press(interaction, button)
        return original(field, repeat)

    view.capture_press = watched
    try:
        await view._press(retro.interaction(view, message=view.message), "a")
    finally:
        view.capture_press = original

    # In the order they were clicked, each one starting from a strictly later
    # machine state than the press before it.
    assert [field for field, _ in seen] == ["a", "b", "start"]
    frames = [frame for _, frame in seen]
    assert frames == sorted(frames) and len(set(frames)) == 3, seen
    # ...and each of the two queued presses made exactly one edit, on its own
    # interaction.
    for interaction in queued:
        assert interaction.kinds() == ["response.defer", "edit_original_response"]
    assert not view.queue


@pytest.mark.parametrize(
    "discard",
    [
        pytest.param("hibernate", id="hibernate"),
        pytest.param("retire", id="retire"),
        pytest.param("reset", id="reboot"),
        pytest.param("undo", id="undo"),
        pytest.param("release", id="replaced"),
    ],
)
async def test_the_queue_is_discarded_when_the_game_moves_somewhere_else(
    retro, discard
):
    """Replaying a stale input is worse than dropping it.

    Every one of these leaves the game somewhere the waiting presses were not
    aimed at: asleep (so the next press is a *wake*), retired, back at the
    title screen, or one press further back. Undo is the sharpest case --
    those presses were queued against the state the undo has just put back,
    so running them would undo the undo one button at a time.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9016, f"drop{discard}")
    # Something to undo, for the undo case.
    await view._press(retro.interaction(view, message=view.message), "a")

    for index in range(2):
        clicker = FakeUser(uid=600 + index, name=f"Q{index}")
        assert view.enqueue_press(
            retro.interaction(view, user=clicker, message=view.message), "left"
        )
    assert len(view.queue) == 2

    if discard == "hibernate":
        await retro.cog.hibernate(view, None)
    elif discard == "retire":
        view.retire()
    elif discard == "reset":
        await retro.cog.run_reset(view)
    elif discard == "undo":
        await retro.cog.run_undo(view)
    else:
        retro.cog._release_view(view)

    assert not view.queue, discard
    assert view.queue_dropped == 2, "and it remembers how many, to say so once"


async def test_a_discard_says_so_once_on_the_next_line(retro):
    """Presses that vanish silently are the bug; this is the one right case."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9017, "queuedrop")
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.enqueue_press(
        retro.interaction(view, user=FakeUser(uid=77, name="Ada"), message=view.message),
        "left",
    )

    undoing = retro.interaction(view, message=view.message)
    await retro.control(view, "undo").callback(undoing)

    said = undoing.log[-1][1]["content"]
    assert "1 queued press dropped" in said, said
    assert "Tester undid the last press." in said
    # Once, and not on the press after it.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == retro.line(view, "Tester pressed A.")


async def test_an_entry_queued_before_a_discard_is_not_run_after_it(retro):
    """The queue epoch, which is the gap `forget_queue()` alone cannot close.

    `RetroView.run_undo` and `run_reset` discard the queue from the worker
    thread that is emulating, so a click arriving on the event loop at that
    exact moment can be appended on either side of the clear. An entry
    carrying the old generation is dropped by the drain rather than emulated
    into a state nobody aimed it at.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9023, "stalequeue")
    ada = retro.interaction(
        view, user=FakeUser(uid=97, name="Ada"), message=view.message
    )
    assert view.enqueue_press(ada, "left")
    stale = view.queue[0]

    # The discard happens, and the entry is then put back by hand -- which is
    # what an append racing the clear looks like from the drain's side.
    assert view.forget_queue() == 1
    view.queue.append(stale)
    assert stale.epoch != view._queue_epoch

    await view._drain()

    assert not view.queue
    assert ada.kinds() == [], "it was never run, and never edited anything"


async def test_only_one_press_is_ever_inside_the_emulator(retro):
    """The queue holds *intent*; the lock still holds the core.

    A recent bug loaded a libretro core twice because a lock was let go too
    early, and MAX_LIVE_EMULATORS is one -- so the queue must not be a second
    way into the emulator. Checked by counting overlap from inside the thing
    the lock is protecting.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9021, "onlyone")
    original = view.capture_press
    inside = 0
    overlap = 0
    queued = []

    def watched(field, repeat=1):
        nonlocal inside, overlap
        inside += 1
        overlap = max(overlap, inside)
        try:
            if len(queued) < 3:
                for uid in (81, 82, 83):
                    if any(entry.user_id == uid for entry in view.queue):
                        continue
                    interaction = retro.interaction(
                        view, user=FakeUser(uid=uid, name=f"U{uid}"), message=view.message
                    )
                    if view.enqueue_press(interaction, "a"):
                        queued.append(interaction)
            return original(field, repeat)
        finally:
            inside -= 1

    view.capture_press = watched
    try:
        await view._press(retro.interaction(view, message=view.message), "a")
    finally:
        view.capture_press = original

    assert overlap == 1, overlap
    assert queued, "the queue was exercised at all"
    assert not view.queue and not view._draining and not view.lock.locked()


async def test_a_queued_press_from_somebody_unnameable_still_reads(retro):
    """A plain User, or somebody who has left the guild since clicking.

    The listing names people again (see QUEUE_ENTRY), so the run belonging
    to somebody with no usable name has to be the buttons on their own --
    never a leading space where the name would have gone.

    The name is sanitised *at the moment of the click* and kept on the entry,
    because it cannot be recovered afterwards: re-deriving one from a member
    object that has since gone is exactly how a listing ends up reading
    " A".
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9022, "goneaway")
    ex_member = types.SimpleNamespace(id=95, display_name="Ex Member")
    anonymous = types.SimpleNamespace(id=96)

    async with view.lock:
        assert view.enqueue_press(retro.interaction(view, user=ex_member), "a")
        assert view.enqueue_press(retro.interaction(view, user=anonymous), "b")
        assert queued_names(view) == ["A", "B"]
        # Two people, so two runs -- and the nameless one is its buttons
        # alone rather than " B".
        assert view.queue_note() == retro.queued(("Ex Member", "A"), "B")
        assert not view.queue_note().startswith("*Queued:  ")
        assert [entry.who for entry in view.queue] == ["Ex Member", ""]
        # The name on the entry is a snapshot: losing the user object later
        # cannot take it back off.
        view.queue[0].interaction.user = None
        assert view.queue[0].who == "Ex Member"


async def test_a_press_on_a_retired_message_is_neither_run_nor_queued(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9018, "retiredpress")
    view.retire()

    stale = retro.interaction(view, message=view.message)
    await view._press(stale, "a")
    # Nothing is run and nothing is queued -- but the click is answered.
    # These buttons are dead and the game is not: it was saved, and it is
    # either further down the channel or behind this message's own Resume
    # button. A controller that says nothing at all is how "the bot stopped
    # working" gets reported.
    assert stale.kinds() == ["response.send_message"]
    (_, said), = stale.log
    assert said["ephemeral"] is True, "only the person who clicked is told"
    assert "retiredpress" in said["content"]
    assert not view.queue, "a retired session has nothing for a press to reach"
    assert not view.enqueue_press(retro.interaction(view), "a")


async def test_a_replaced_controller_explains_itself_once_per_person(retro):
    """
    Once each, not once per click.

    Somebody who has not noticed the channel moved on will tap several
    buttons before concluding the bot is broken, and a whisper per tap is
    the spam an ephemeral acknowledgement was removed for being.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9020, "toldonce")
    view.retire()

    rob = FakeUser(uid=71, name="Rob")
    first = retro.interaction(view, user=rob, message=view.message)
    await view._press(first, "a")
    assert first.kinds() == ["response.send_message"]

    second = retro.interaction(view, user=rob, message=view.message)
    await view._press(second, "b")
    assert second.kinds() == ["response.defer"], "told once, then left alone"

    # Somebody else gets their own explanation: they have not been told.
    ada = retro.interaction(
        view, user=FakeUser(uid=72, name="Ada"), message=view.message
    )
    await view._press(ada, "a")
    assert ada.kinds() == ["response.send_message"]


async def test_a_queued_press_on_a_sleeping_session_wakes_it_first(retro):
    """The one delay worth explaining, and the queue does not skip it."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9019, "queuedwake")
    await retro.cog.hibernate(view, None)
    assert not view.live

    ada = retro.interaction(
        view, user=FakeUser(uid=41, name="Ada"), message=view.message
    )
    original = view.capture_press
    once = []

    def slow(field, repeat=1):
        if not once:
            once.append(True)
            assert view.enqueue_press(ada, "b")
        return original(field, repeat)

    view.capture_press = slow
    try:
        await view._press(retro.interaction(view, message=view.message), "a")
    finally:
        view.capture_press = original

    # The waking press says it woke up; the queued one behind it is an
    # ordinary press on a session that is now awake.
    assert view.live
    assert ada.log[-1][1]["content"] == retro.line(view, "Ada pressed B.")


# -- A clip is not replaced before it has been watched -------------------------
#
# A clip costs 42-92ms to make and 1005ms to watch, so presses that arrive
# back to back -- which is every queue drain, since queued presses run with no
# human delay between them -- each replaced the previous clip after about a
# tenth of it had played. The seam is exact (capture_plan sees to that: one
# clip carries on exactly one emulated frame after the last picture of the
# one before it) but nobody ever saw it, so the picture appeared to lurch.
#
# So the *edit* waits until the clip it is replacing has had its playing time
# on screen -- all of it, at every clip length. See the note above
# MAX_PACE_SECONDS in retro/timing.py for the rule and the numbers behind it.
# The wait was capped at 1.25 seconds once, which meant every clip longer
# than that was replaced part-played and the player was jumped forward over
# the difference (2.75 seconds of a 4 second clip). That is the stutter these
# tests now pin shut at 1.5, 2, 4 and 5 seconds as well as at the default.
#
# The gate spends its time in exactly one place -- the module-level
# `pace_wait` -- and `RetroEnv` swaps that for a recorder, so these tests read
# the delay that was *asked for* rather than paying for it. `retro.pace_waits`
# is every delay any view asked for, and `view.last_pace_seconds` is the last
# one. The three tests that are about the waiting itself put the real wait
# back with `retro.real_pacing()`.


def playing_now(view):
    """Pretend the clip on the message has only just gone out."""
    view.note_posted(view.clip_playback())


def played_out(view):
    """Pretend it went out long enough ago to have finished playing."""
    view.note_posted(view.clip_playback())
    view._posted_at -= view.clip_playback() + 1.0


async def fill_the_queue(retro, view, waiting):
    """Run one press with ``waiting`` more queued behind it, and drain it all.

    The presses have to be taken down from *inside* the running one, exactly
    as real clicks arrive: a press only queues while the session is busy, and
    a queue with nobody working through it is not a state anybody reaches.

    Returns the running press's interaction and the queued ones, in order.
    """
    people = [FakeUser(uid=600 + index, name=f"P{index}") for index in range(waiting)]
    queued = [
        retro.interaction(view, user=person, message=view.message)
        for person in people
    ]
    original = view.capture_press
    once = []

    def slow(field, repeat=1):
        if not once:
            once.append(True)
            for interaction in queued:
                assert view.enqueue_press(interaction, "a")
        return original(field, repeat)

    running = retro.interaction(view, message=view.message)
    view.capture_press = slow
    try:
        await view._press(running, "b")
    finally:
        view.capture_press = original
    return running, queued


async def test_a_press_with_nothing_playing_is_not_held_back(retro):
    """The common case -- one person, one press at a time -- is untouched."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9200, "unpaced")
    played_out(view)

    await view._press(retro.interaction(view, message=view.message), "a")

    assert view.last_pace_seconds == 0.0
    assert retro.pace_waits == [], retro.pace_waits
    assert view.pace_delay() > 0.0, "but its own clip is now the one playing"


async def test_a_clip_is_not_replaced_until_the_one_on_screen_has_played(retro):
    """The whole fix, in one press.

    A second press landing a moment after the first used to replace a clip
    that had played a fraction of the way through. Now the emulation happens
    straight away and only the edit waits, for what is left of the clip.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9201, "paced")
    playback = view.clip_playback()
    assert playback == pytest.approx(1.005, abs=0.001), playback
    playing_now(view)

    await view._press(retro.interaction(view, message=view.message), "a")

    # Everything the press spent producing the clip counts against the wait,
    # so it is a shade under the whole playing time and never more than it.
    assert 0.0 < view.last_pace_seconds <= playback
    assert view.last_pace_seconds == pytest.approx(playback, abs=0.05)
    assert retro.pace_waits == [view.last_pace_seconds]


async def test_a_clip_that_has_already_played_through_is_replaced_at_once(retro):
    """Pacing is a deadline, not a delay: the time already spent counts."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9202, "halfway")
    playback = view.clip_playback()

    playing_now(view)
    view._posted_at -= playback * 0.75
    assert view.pace_delay() == pytest.approx(playback * 0.25, abs=0.02)

    view._posted_at -= playback * 0.25
    assert view.pace_delay() == 0.0


async def test_a_wait_too_short_to_see_is_not_taken_at_all(retro):
    """Under one picture of the clip, so nothing visible is lost by going."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9203, "sliver")
    playing_now(view)
    view._posted_at -= view.clip_playback() - (retro.viewmod.MIN_PACE_SECONDS / 2)

    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.last_pace_seconds == 0.0
    assert retro.pace_waits == []


@pytest.mark.parametrize(
    "seconds", [0.2, 0.8, 1.0, 1.5, 2.0, 4.0, 5.0]
)
async def test_every_clip_length_is_paced_for_its_whole_playing_time(
    retro, seconds
):
    """The fix, across the whole of `[p]retroset cliplength`.

    The wait used to be capped at a flat 1.25 seconds, so everything from 1.5
    upwards was replaced part-played and the player was jumped forward over
    the rest: 0.26s of a 1.5s clip, 0.74s of a 2s one, 2.75s of a 4s one.
    That was the reported stutter, and it got worse the longer the clip,
    which is exactly how it was reported.

    So the deadline is now the clip's own playing time at every length the
    setting can reach, and MAX_PACE_SECONDS only bounds a figure no clip
    could produce. 4.0 is in the list because it is the length the report
    came from.
    """
    assert retro.emumod.MAX_CLIP_SECONDS == 5.0, "the list above stops at the ceiling"
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9204, "long")
    view.clip_seconds = seconds
    playback = view.clip_playback()
    playing_now(view)

    assert view.pace_delay() == pytest.approx(playback, abs=0.01)
    assert playback <= retro.viewmod.MAX_PACE_SECONDS, (
        "no clip length the setting can reach may be cut short by the ceiling"
    )


async def test_the_ceiling_only_bounds_a_playback_no_clip_could_produce(retro):
    """MAX_PACE_SECONDS as what it now is: a guard, not a policy.

    It is a whole second clear of MAX_CLIP_SECONDS, so the only way to reach
    it is to put a playing time into the session that no recording could have
    made -- a corrupt figure, or a clip length from a future where the
    ceiling moved. The margin is for the millisecond rounding, which only
    ever goes up: a 5 second clip plays for 5.008 on a Game Boy and up to
    5.04 on the slowest frame rate a core reports, so a bound of exactly
    MAX_CLIP_SECONDS would quietly shave the end off the longest clip the
    setting can ask for. The wait is bounded at all, rather than left open,
    because a session must not be able to hold an edit for an hour.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9213, "ceiling")
    cap = retro.viewmod.MAX_PACE_SECONDS
    assert cap == retro.emumod.MAX_CLIP_SECONDS + 1.0

    view.clip_seconds = retro.emumod.MAX_CLIP_SECONDS
    assert view.clip_playback() == 5.008 < cap, "and 5.04 on a slow core"

    view.note_posted(cap * 20)
    assert view.pace_delay() == pytest.approx(cap, abs=0.01)


async def test_a_full_queue_costs_its_clips_and_says_so(retro):
    """What a whole drain costs, stated as the arithmetic it is.

    The queue is bounded (MAX_QUEUED_PRESSES) and each edit now waits out the
    whole of the clip it replaces, so a drain at cliplength L takes about
    MAX_QUEUED_PRESSES * L -- three seconds at the default, and fifteen at
    the five second ceiling. That is a real cost and it is the right one:
    the alternative was a cap that made a long clip look free by throwing
    away the footage nobody was allowed to reach. Both numbers in the product
    are the owner's own settings.
    """
    viewmod = retro.viewmod
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9205, "bound")
    playback = view.clip_playback()
    assert playback == pytest.approx(1.005, abs=0.001), playback
    playing_now(view)
    await fill_the_queue(retro, view, viewmod.MAX_QUEUED_PRESSES)

    # One wait per edit: the press that ran, then every queued one. The
    # running press's own wait is its author's latency and was paid whether
    # anybody queued behind it or not; what the *queue* adds is the rest.
    assert len(retro.pace_waits) == viewmod.MAX_QUEUED_PRESSES + 1
    assert all(delay <= playback for delay in retro.pace_waits)
    queued = sum(retro.pace_waits[1:])
    # An upper bound and a generous lower one, rather than an equality.
    # Each wait is the time *left* on the clip it replaces, so it is shortened
    # by however long that entry actually spent emulating and encoding -- real
    # work, on a machine running the rest of this suite at the same time. The
    # drift is per entry, so it accumulates with MAX_QUEUED_PRESSES, and an
    # `approx(..., abs=0.2)` here failed intermittently in a full-suite run
    # while passing alone. What the test is for is the *shape* of the cost --
    # a drain is a clip per entry, not a flat cap -- and half a clip each is
    # far below anything the old 1.25s cap could have produced at the lengths
    # that cap actually bit.
    assert queued <= viewmod.MAX_QUEUED_PRESSES * playback
    assert queued >= 0.5 * viewmod.MAX_QUEUED_PRESSES * playback, retro.pace_waits
    assert not view.queue and not view._draining


async def test_a_long_clip_drains_in_its_clips_rather_than_the_old_cap(retro):
    """The same drain at the length the stutter was reported from.

    Four seconds a clip: every edit waits about four seconds rather than the
    old 1.25, so the four clips of a full queue are four whole clips instead
    of four clips with 2.75 seconds cut off each. The waits are recorded
    rather than spent, so this costs nothing to check.
    """
    viewmod = retro.viewmod
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9214, "longdrain")
    view.clip_seconds = 4.0
    playback = view.clip_playback()
    assert playback == pytest.approx(4.003, abs=0.001), playback
    playing_now(view)

    await fill_the_queue(retro, view, viewmod.MAX_QUEUED_PRESSES)

    assert len(retro.pace_waits) == viewmod.MAX_QUEUED_PRESSES + 1
    for delay in retro.pace_waits:
        assert delay == pytest.approx(playback, abs=0.2), retro.pace_waits
        assert delay > 1.25, "the old cap would have truncated every one"
    assert not view.queue and not view._draining


async def test_a_paced_press_still_makes_exactly_one_edit(retro):
    """The invariant pacing must not buy its way out of.

    Waiting is not a thing anybody can see, so it cannot cost a second edit
    or a second response: the press is deferred, it waits, and then it makes
    the one edit it always made.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9206, "onepaced")
    playing_now(view)

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert view.last_pace_seconds > 0.0, "this press really did wait"
    assert interaction.kinds() == ["response.defer", "edit_original_response"]
    assert interaction.log[-1][1]["has_attachments"]


async def test_the_queue_drains_with_real_pacing_and_still_finishes(retro):
    """The real wait, at the shortest clip length, end to end.

    Short clips so the test costs tenths of a second rather than seconds, and
    real waiting so that "the drain finishes" is a statement about the gate
    rather than about the recorder. Every clip gets its full playing time,
    which is now true at every clip length; 0.2s is simply the cheapest one
    to prove it at.

    **That the drain terminates at all is the load-bearing part.** Every edit
    now waits longer than it used to at any length above a second, so a gate
    that could fail to come back would fail here first: four presses, four
    real waits, one after another, and an empty queue at the end.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9207, "draining")
    view.clip_seconds = 0.2
    playback = view.clip_playback()
    retro.real_pacing()

    playing_now(view)
    started = time.monotonic()
    first, waiting = await fill_the_queue(retro, view, 3)
    elapsed = time.monotonic() - started

    # Every press made its own single edit, in order, and the queue is empty.
    for interaction in [first] + waiting:
        assert interaction.kinds() == ["response.defer", "edit_original_response"]
    assert not view.queue and not view._draining
    # Four edits, four clips each held for their playing time -- so the drain
    # really did take about that long, and nowhere near forever.
    assert elapsed >= 3 * playback, elapsed
    assert elapsed < 4 * playback + 1.0, elapsed


async def test_pacing_never_holds_the_emulator_lock(retro):
    """One core, every channel: a wait here must cost nobody else anything.

    The clip is finished with by the time the gate waits -- `Retro.run_press`
    has returned and given the cog's emulator lock back -- so another channel
    can wake its own game and press a button *during* the wait. Which is
    exactly what this does, from inside the wait itself.
    """
    await retro.install_cores("gambatte")
    other, other_ctx, _ = await retro.posted_game(9208, "otherchannel")
    view, _, _ = await retro.posted_game(9209, "thischannel")
    assert view.live and not other.live, "one core, and this channel has it"

    playing_now(view)
    seen = {}
    elsewhere = retro.interaction(other, message=other.message)

    async def wait(release, delay):
        seen["delay"] = delay
        seen["emulator_lock"] = retro.cog.emulator_lock.locked()
        # A whole press on another channel, mid-wait: it wakes that session,
        # which evicts this one, and makes its own single edit.
        await other._press(elsewhere, "a")

    retro.pace_wait(wait)
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert seen["delay"] > 0.0, "this press really was being paced"
    assert seen["emulator_lock"] is False, "the one core was held all the way"
    assert elsewhere.kinds() == ["response.defer", "edit_original_response"]
    assert other.live, "the other channel got the core"
    # And this press still made its one edit afterwards.
    assert interaction.kinds() == ["response.defer", "edit_original_response"]


#: How this session can be taken away while a press is holding its edit back.
#: Each one has to cancel the pacing *before* it reaches for anything, or it
#: would queue up behind a purely cosmetic delay. See RetroView.cancel_pacing.
TEARDOWNS = [
    ("sleep", lambda retro, view, ctx: sleep_command(retro)(retro.cog, ctx)),
    ("end", lambda retro, view, ctx: retro.cogmod.Retro.retroend.callback(retro.cog, ctx)),
    ("reboot", lambda retro, view, ctx: reset_command(retro)(retro.cog, ctx)),
    ("evicted", lambda retro, view, ctx: retro.cog.hibernate(view, "Evicted.")),
    ("unload", lambda retro, view, ctx: retro.cog.cog_unload()),
]


@pytest.mark.parametrize("name, teardown", TEARDOWNS, ids=[n for n, _ in TEARDOWNS])
async def test_teardown_never_waits_on_pacing(retro, name, teardown):
    """Sleeping, ending, rebooting, eviction and unloading, all immediate.

    The real wait, so "it was cut short" is a measurement rather than a
    promise: the gate is asked for the better part of a second and gives it
    up as soon as the teardown says there is no point.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9210, "torndown")
    playing_now(view)
    retro.real_pacing()
    timings = {}
    running = []

    async def wait(release, delay):
        timings["delay"] = delay
        running.append(asyncio.create_task(teardown(retro, view, ctx)))
        # Long enough for the teardown to reach its cancel_pacing() and then
        # block on whatever it wants; far shorter than the wait being cut.
        await asyncio.sleep(0.02)
        started = time.monotonic()
        await retro._real_pace_wait(release, delay)
        timings["waited"] = time.monotonic() - started

    retro.pace_wait(wait)
    await view._press(retro.interaction(view, message=view.message), "a")
    await asyncio.gather(*running)

    assert timings["delay"] > 0.5, timings
    assert timings["waited"] < 0.1, timings
    # No timer left behind: the wait is an await inside the press it belongs
    # to rather than a scheduled callback, so when the press is over so is
    # it, and nothing of this cog's is still pending.
    assert not view.lock.locked() and not view._draining
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current] == []


async def test_an_undo_that_lands_during_pacing_is_dropped_rather_than_waiting(retro):
    """Undo is not queueable, and pacing does not make it so.

    A click that arrives while a press is running -- which now includes the
    moment it is holding its edit back -- is answered and dropped, as it
    always was. It does not wait, and it does not edit the message: the
    answer is one private line telling the clicker to try again when the
    next clip lands, which costs an interaction response and nothing else.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9211, "undoduring")
    await view._press(retro.interaction(view, message=view.message), "a")
    retro.pace_waits.clear()
    playing_now(view)
    clicked = {}

    async def wait(release, delay):
        undo = retro.interaction(view, message=view.message)
        await retro.control(view, "undo").callback(undo)
        clicked["kinds"] = undo.kinds()

    retro.pace_wait(wait)
    await view._press(retro.interaction(view, message=view.message), "b")

    # One interaction response and no edit of the message: the pacing wait
    # was not extended and the clip on screen was not replaced.
    assert clicked["kinds"] == ["response.send_message"], clicked


async def test_an_undo_s_own_clip_is_never_paced(retro):
    """A correction is not held back to finish showing the thing it corrects.

    The clip on the message is of a press that is about to stop having
    happened, so waiting for it to play through would be showing somebody the
    very thing they asked to take away. Same for `[p]retroreboot`.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9212, "undopace")
    await view._press(retro.interaction(view, message=view.message), "a")

    retro.pace_waits.clear()
    playing_now(view)
    await retro.control(view, "undo").callback(
        retro.interaction(view, message=view.message)
    )
    assert retro.pace_waits == [], retro.pace_waits

    playing_now(view)
    await reset_command(retro)(retro.cog, ctx)
    assert retro.pace_waits == [], retro.pace_waits


# -- None of the previous clip is shown in the new one ------------------------
#
# Pacing gets the clip on screen watched in full. It does not stop the *next*
# clip from opening on the very same picture, because the shutter falls a
# step into the window (four frames on a Game Boy at CLIP_FPS) and a core can
# be slower than that to answer a button: measured first-reaction latencies
# are gambatte/uCity 1 frame, fceumm/nestest 2, snes9x 4, mgba 11. Eleven is
# nearly three pictures.
#
# So the session remembers the still its last posted clip left in the channel
# -- as a 16 byte hash, never as pixels; a SNES frame is 688 KiB and there is
# one session per channel -- and drops any opening picture of the next clip
# that is byte-identical to it. The rules, and the frozen-screen case that
# must be left alone, are pinned against clips.trim_repeated_opening in
# test_clips.py; what is pinned here is the session wiring: who remembers,
# when it is promoted, what the pacing gate then waits for, and every path
# that has to forget.


def synthetic_clip(*seeds, ms=67):
    """A captured clip of ``len(seeds)`` pictures; equal seeds are identical.

    The fake emulator carries finished bytes rather than Pillow images (see
    fakes._FakeCapture), which is right for every other test in this file and
    useless for this one: the trim compares pixels. So these tests hand the
    view a real CapturedClip and read back the one it decided to encode --
    FakeEmulator.encode_captured returns whatever it was given, so the return
    value of _encode *is* the trimmed clip.
    """
    from PIL import Image

    from retro.clips import CapturedClip

    images = []
    for seed in seeds:
        image = Image.new("RGB", (16, 12), (20, 40, 60))
        image.putpixel((seed % 16, seed % 12), (seed % 251, 90, 7))
        images.append(image)
    return CapturedClip(images, [ms] * len(images), (32, 24))


def post(view, captured):
    """Encode a clip the way a press does, and say the edit went through."""
    encoded = view._encode(captured, None, view.emulator)
    view.note_posted(view.posted_playback())
    return encoded


async def test_a_clip_opening_on_the_still_it_replaces_is_trimmed(retro):
    """The owner's requirement, end to end through a session.

    The first clip leaves picture 2 in the channel. The second opens on two
    copies of picture 2 -- a game that had not answered the button by the
    time the shutter fell -- and what goes out starts on picture 3.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9215, "trimmed")

    first = synthetic_clip(1, 2)
    post(view, first)
    assert view._last_picture is not None

    second = view._encode(synthetic_clip(2, 2, 3, 4), None, view.emulator)

    assert len(second.images) == 2
    assert second.durations == [67, 67]
    assert second.images[0].tobytes() != first.images[-1].tobytes(), (
        "nothing the viewer is already looking at is shown again"
    )


async def test_a_trimmed_clip_is_paced_for_what_it_really_plays(retro):
    """The invariant this changes, stated precisely.

    A clip no longer always plays for as long as it emulated: it plays for
    that *minus any opening already on screen*. So the pacing deadline has to
    come from the durations really being encoded rather than from the window,
    or the gate would hold the next edit for footage this clip does not
    contain.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9216, "trimpace")

    post(view, synthetic_clip(1, 2))
    # Four pictures at 67ms, two of them the still already on screen.
    view._encode(synthetic_clip(2, 2, 3, 4), None, view.emulator)

    assert view.posted_playback() == pytest.approx(0.134, abs=0.0005)
    assert view.clip_playback() == pytest.approx(1.005, abs=0.001), (
        "the window's own arithmetic is unchanged and still honest about the window"
    )
    view.note_posted(view.posted_playback())
    assert view.pace_delay() == pytest.approx(0.134, abs=0.01)


async def test_a_frozen_screen_still_gets_a_whole_clip(retro):
    """The 17ms flash, at the session level.

    Every picture is the still already on screen, so the game has not moved
    and the clip says so for its whole length. Trimming it to the one picture
    the "never drop the last" rule keeps is the regression a previous attempt
    shipped, and it would also tell the pacing gate this clip is worth 67ms.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9217, "frozen")

    post(view, synthetic_clip(1, 5))
    frozen = synthetic_clip(5, 5, 5, 5)
    encoded = view._encode(frozen, None, view.emulator)

    assert encoded is frozen
    assert len(encoded.images) == 4
    assert view.posted_playback() == pytest.approx(0.268, abs=0.0005)


async def test_a_clip_discord_refused_is_not_what_the_next_one_is_trimmed_against(
    retro,
):
    """The still on screen is the last one *posted*, not the last one encoded.

    An edit that failed put nothing on anybody's screen, so the picture still
    sitting there is the one from the edit before it -- and that is what the
    next clip has to be compared with. The measurement rides in _encoded
    until note_posted promotes it, which only a successful edit calls.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9218, "refused")

    post(view, synthetic_clip(1, 6))
    on_screen = view._last_picture

    # Encoded, and then the edit fails: note_posted is never reached.
    view._encode(synthetic_clip(7, 8), None, view.emulator)
    assert view._last_picture == on_screen, "picture 6 is still the one on screen"

    # The next clip opens on picture 6 again, and is still trimmed against it.
    trimmed = view._encode(synthetic_clip(6, 9), None, view.emulator)
    assert len(trimmed.images) == 1


@pytest.mark.parametrize("name, teardown", TEARDOWNS, ids=[n for n, _ in TEARDOWNS])
async def test_a_teardown_forgets_the_still_it_is_taking_away(retro, name, teardown):
    """Sleeping, ending, rebooting, eviction and unloading, all the same.

    Once the picture on the message stops being this session's own to reason
    about, a hash of it must not be allowed to trim the opening off whatever
    replaces it -- the first clip after a wake is the case that matters,
    since a save state comes back on the exact frame it left and its opening
    picture is very likely the one still sitting in the channel. Every one of
    these paths already calls cancel_pacing (or forget_pacing, from a worker
    thread); the hash is forgotten in the same place.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9219, "forgets")
    post(view, synthetic_clip(1, 2))
    assert view._last_picture is not None

    await teardown(retro, view, ctx)

    assert view._last_picture is None, name
    # And a clip that opens on that very picture now goes out whole.
    if view.emulator is not None:
        whole = synthetic_clip(2, 2, 3)
        assert view._encode(whole, None, view.emulator) is whole


async def test_an_undo_forgets_the_still_from_the_press_it_is_undoing(retro):
    """A correction is never trimmed against the thing it corrects.

    capture_undo and capture_reset call forget_pacing from the worker thread
    before they touch the machine, so by the time their clip is encoded there
    is nothing to compare it with. Otherwise an undo whose first picture
    happened to match the undone press's last one would open by dropping it.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9220, "undotrim")
    await view._press(retro.interaction(view, message=view.message), "a")
    post(view, synthetic_clip(1, 2))
    assert view._last_picture is not None

    view.capture_undo()
    assert view._last_picture is None

    whole = synthetic_clip(2, 2, 3)
    assert view._encode(whole, None, view.emulator) is whole


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
    assert landed["content"] == retro.line(view, "Tester pressed A.")
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
    assert interaction.log[-1][1]["content"] == retro.line(view, expected)


async def test_the_repeat_button_says_how_many_taps_it_really_did(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9022, "taps")
    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "repeat").callback(interaction)
    assert interaction.log[-1][1]["content"] == retro.line(view, "Tester pressed A x3.")

    # A clip too short to fit three: the line follows press_plan down, like
    # the label on the button does, rather than claiming a tap that did not
    # happen.
    view.clip_seconds = 0.5
    again = retro.interaction(view, message=view.message)
    await retro.control(view, "repeat").callback(again)
    said = again.log[-1][1]["content"]
    assert said.endswith("Tester pressed A x2."), said


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
        retro.line(view, "Tester pressed A."),
        retro.line(view, "Tester pressed B."),
        retro.line(
            view,
            "Tester pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}.",
        ),
        retro.line(view, "Tester waited."),
        retro.line(view, "Tester pressed Start."),
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
    assert again.log[-1][1]["content"] == retro.line(view, "Tester pressed A.")


async def test_the_resume_line_beats_the_press_line(retro):
    """"The game was asleep and is back" is news; "you pressed A" is a label."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9025, "resumewins")
    await retro.cog.hibernate(view, None)

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert interaction.log[-1][1]["content"] == retro.line(
        view, retro.viewmod.RESUMED_NOTE
    )
    # And it is said once: the next press names itself instead.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == retro.line(view, "Tester pressed A.")


async def test_a_failed_press_explains_itself_rather_than_naming_a_button(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9026, "pressfails")

    def boom(field, repeat=1):
        raise retro.emumod.EmulatorError("the core fell over")

    view.capture_press = boom
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
    await retro.cogmod.Retro.retroreboot.callback(retro.cog, ctx)
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

    assert landed["content"] == retro.line(view, f"Rob {tail}")
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
    assert landed["content"] == retro.line(view, "Tester pressed A.")
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
    assert interaction.log[-1][1]["content"] == retro.line(
        view, "Ex Member pressed A."
    )

    # Nothing nameable: the impersonal form of the same sentence, not a
    # traceback and not a line starting with a space.
    anonymous = types.SimpleNamespace(id=100)
    interaction = retro.interaction(view, user=anonymous, message=view.message)
    await view._press(interaction, "b")
    assert interaction.log[-1][1]["content"] == retro.line(view, "Pressed B.")


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
    assert said == [
        retro.line(view, "Rob pressed A."),
        retro.line(view, "Ada pressed B."),
        retro.line(view, "Rob waited."),
    ]


# -- A session holds no footage -----------------------------------------------


async def test_a_session_holds_no_footage_at_all(retro):
    """A press builds a clip, uploads it and drops it.

    Measured rather than asserted by name (see fakes.footage_bytes), because
    the thing being guarded against is a clip being kept under *any* name:
    twenty presses must leave the session holding no bytes of picture
    whatsoever. The one attribute that legitimately holds bytes is the undo
    history, which is compressed save states and has its own bounds.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9027, "oneclip")

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
    assert only["content"] == retro.line(view, "Tester undid the last press.")
    assert not only["any_disabled"], "the controls come back enabled"
    assert only["spacers_disabled"]


async def test_nothing_is_edited_while_the_undo_is_being_emulated(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9202, "midundo")
    await view._press(retro.interaction(view, message=view.message), "a")

    original = view.capture_undo
    seen = []

    def watched():
        seen.append(list(interaction.kinds()))
        return original()

    view.capture_undo = watched
    interaction = retro.interaction(view, message=view.message)
    try:
        await retro.control(view, "undo").callback(interaction)
    finally:
        view.capture_undo = original
    assert seen == [["response.defer"]], seen


async def test_the_undo_button_stays_clickable_with_nothing_to_undo(retro):
    """It used to grey itself out, which hid the only explanation there is.

    A disabled Discord button cannot be clicked, so the private "there is
    nothing to undo yet, and here is why" line could never be reached by the
    person looking at the dead control -- and an unexplained dead control
    reads as a broken one. See _UndoButton.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9203, "deadundo")
    button = retro.control(view, "undo")
    assert not button.disabled and not view.history

    await view._press(retro.interaction(view, message=view.message), "a")
    assert not button.disabled and view.history

    await undo(retro, view)
    assert not button.disabled, "still clickable once the history is spent"
    assert not view.history


async def test_an_undo_with_nothing_to_undo_says_so_and_touches_nothing(retro):
    """The fresh-restart path: the history is memory only, like the clips."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9204, "emptyundo")
    view.forget_history()

    interaction = await undo(retro, view)

    assert interaction.kinds() == ["response.send_message"], interaction.kinds()
    snap = interaction.log[0][1]
    assert snap["ephemeral"] is True
    said = snap["content"] or ""
    assert "nothing to undo" in said
    assert "memory only" in said and "restart" in said
    # Shorter than it was: it used to be five clauses. Two sentences and the
    # reason first.
    assert said.count(".") <= 4, said
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

    assert not restored.history and restored.history_bytes == 0
    button = next(
        c for c in restored.children
        if c.custom_id == f"{retro.viewmod.CUSTOM_ID_PREFIX}:undo"
    )
    assert not button.disabled, "and it can still be clicked, which is how"
    # ...clicking it is answered with the explanation, never an error. This
    # is the case the whole "leave Undo enabled" decision is for: after a
    # restart there is nothing to undo on any message in any channel.
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
    assert history_is_consistent(view)


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
    assert history_is_consistent(view)

    # A single state larger than the whole cap keeps exactly one entry: an
    # Undo that cannot undo the press somebody just made is worse than the
    # memory.
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
    assert snap["content"] == retro.line(view, "Tester undid the last press.")


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
    assert not retro.control(view, "undo").disabled, "still clickable"


async def test_an_undo_while_the_session_is_busy_says_to_try_again(retro):
    """
    Undo is never queued, so a click that cannot run has to say so.

    Queueing it would mean undoing a press its author never saw -- see
    RetroView._undo -- so the click is refused. It used to be refused with a
    bare defer, i.e. with nothing: clicking Undo and watching the message
    carry on as though you had not is exactly the "did that register?"
    failure the press queue was built to answer.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9213, "busyundo")
    await view._press(retro.interaction(view, message=view.message), "a")
    async with view.lock:
        interaction = await undo(retro, view)
    assert interaction.kinds() == ["response.send_message"]
    (_, said), = interaction.log
    assert said["ephemeral"] is True, "only the person who clicked is told"
    assert "Undo" in said["content"]
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
    assert not retro.control(view, "undo").disabled, "still clickable"


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


async def test_the_idle_timeout_is_read_from_config_and_never_carried(retro):
    """`[p]retroset timeout` applies to the games already running.

    A session used to be handed the timeout at construction, and handed it
    again by from_record on every restart, and nothing ever read either copy:
    the sweep asks Config for the current value on every pass. That is the
    whole reason the setting takes effect immediately -- so the copy is gone,
    and this is what it would have had to disagree with.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9031, "timeoutless")
    assert not hasattr(view, "timeout_minutes")
    # ...including a session rebuilt from a record after a restart, which is
    # the other place it was threaded through.
    rebuilt = retro.viewmod.RetroView.from_record(retro.cog, view.to_record())
    assert not hasattr(rebuilt, "timeout_minutes")

    view.last_active = time.time() - 11 * 60
    await retro.cog.config.session_timeout_minutes.set(30)
    await retro.cog._hibernate_idle()
    assert view.live, "eleven minutes idle is not thirty minutes idle"

    await retro.cog.config.session_timeout_minutes.set(5)
    await retro.cog._hibernate_idle()
    assert not view.live, "and the new value reached a session already playing"


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
    assert landed["content"] == retro.line(view, retro.viewmod.RESUMED_NOTE)
    assert "Woke up" in landed["content"]
    # The header no longer says "asleep", because it is not: both halves of
    # "what state is this session in" ride on edits that happen anyway.
    assert "asleep" not in landed["content"]
    assert not landed["has_embed"]
    assert landed["n_attachments"] == 1, "and the clip came with it"

    # Said once: the next press replaces it with its own line rather than
    # repeating it.
    again = retro.interaction(view, message=view.message)
    await view._press(again, "a")
    assert again.log[-1][1]["content"] == retro.line(view, "Tester pressed A.")


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
    # undo history. Undo is still *clickable*, because a dead unexplained
    # control after every restart is what read as "Undo is broken".
    assert footage_bytes(restored) == 0
    assert not restored.history
    assert not retro.control(restored, "undo").disabled
    # ...and nothing is queued either: the queue is per-process intent.
    assert not restored.queue

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
    # A no-op as far as the game is concerned: nothing is emulated, nothing
    # is queued, and no core is loaded for it. The clicker is told where the
    # game went, privately, which is the one thing that changed.
    assert stale.kinds() == ["response.send_message"]
    assert not old.queue and old.emulator is None
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


# -- retrosleep (formerly retrostop) ------------------------------------------
#
# `[p]retrostop` never stopped anything: it saved the game and freed the
# emulator while leaving the controls live, so the next press woke it. The
# docstring admitted as much while claiming to be "the only way to stop a
# game". So the pause is called `[p]retrosleep` (with `retrostop` kept as an
# alias, unchanged in behaviour) and the thing that was genuinely missing --
# finish with a game and retire its controls -- is `[p]retroend`.


def sleep_command(retro):
    return retro.cogmod.Retro.retrosleep.callback


async def test_retrosleep_saves_frees_and_keeps_the_session(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9080, "sleepme")
    emulator = view.emulator

    await sleep_command(retro)(retro.cog, ctx)
    assert view.emulator is None and not emulator.started
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    assert retro.cog.sessions.get(channel.id) is view
    assert not any(c.disabled for c in retro.pressable(view))
    assert "carry on" in ctx.sent[-1]
    assert "retroend" in ctx.sent[-1], "and the way to really finish is offered"
    stopped = view.message.edits[-1] if view.message.edits else {}
    said = stopped.get("content") or ""
    assert "Put to sleep by Tester." in said
    # The header on that same edit says the session is asleep, which is the
    # state it will be in until somebody presses something.
    assert said.startswith(retro.header(view, asleep=True)), said
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
    await sleep_command(retro)(retro.cog, ctx)
    stopped = (view.message.edits[-1].get("content") or "")
    assert "Put to sleep by " in stopped, stopped

    view, ctx, _ = await retro.posted_game(9083, "nastyreset")
    ctx.author = nasty
    view.starter_id = nasty.id
    await retro.cogmod.Retro.retroreboot.callback(retro.cog, ctx)
    # The reply itself, not ctx.said() -- that joins in the repr of the
    # game's own post, angle brackets and all.
    reply = ctx.sent[-1]
    assert "has been rebooted by " in reply, reply

    for text in (stopped, reply):
        assert "@everyone" not in text, text
        assert not re.search(r"(?<!\\)<", text), text
        assert "\\*\\*" in text, "the markdown in the name was escaped"

    # And somebody with no usable name leaves a sentence that still reads.
    view, ctx, _ = await retro.posted_game(9084, "namelessstop")
    ctx.author = types.SimpleNamespace(id=78)
    view.starter_id = 78
    await sleep_command(retro)(retro.cog, ctx)
    assert "Put to sleep. Press a button" in (
        view.message.edits[-1].get("content") or ""
    )


async def test_retrosleep_saves_the_game_even_if_the_message_explodes(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9081, "explodes")
    emulator = view.emulator

    async def boom(*args, **kwargs):
        raise RuntimeError("message is gone")

    view.refresh = boom
    state = retro.cog._state_path(channel.id, view.slug)
    state.unlink(missing_ok=True)
    await sleep_command(retro)(retro.cog, ctx)
    assert not emulator.started
    assert state.is_file()


# -- retroreboot (formerly retroreset) ----------------------------------------
#
# Rebooting the running game, which the cog's author asked for as a *command*
# and not a button: it throws away everybody's progress-in-flight, so it has
# the permission check `[p]retrosleep` has rather than being one more thing a
# passer-by can click. `[p]retrosaves dropstate` is a different command
# entirely (it deletes a save state file on disk), which is why neither is
# called "reset" any more.


def reset_command(retro):
    return retro.cogmod.Retro.retroreboot.callback


async def test_retroreboot_reboots_the_running_game(retro):
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


async def test_retroreboot_puts_the_boot_clip_on_the_game_s_message(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9091, "resetclip")
    await view._press(retro.interaction(view, message=view.message), "a")
    before = retro.shown_clip(view)

    await reset_command(retro)(retro.cog, ctx)

    assert retro.shown_clip(view) != before, "a fresh clip was recorded"
    # One edit of the game's own message, carrying the clip and the line that
    # says what happened -- the same shape a press makes.
    edit = view.message.edits[-1]
    assert edit["content"] == retro.line(view, "Tester reset the game.")
    assert len(edit["attachments"]) == 1
    assert edit["attachments"][0].filename.endswith(".webp")
    assert not any(c.disabled for c in retro.pressable(view))
    # ...and the reply in the channel says what happened and how to get back.
    said = ctx.said()
    assert "has been rebooted" in said
    assert "Undo" in said
    # It no longer has to warn about `[p]retrosaves reset`: that command is
    # `[p]retrosaves dropstate` now, and this one is `[p]retroreboot`, so
    # the names carry the difference instead of a disclaimer in a reply.
    assert "retrosaves" not in said, said


async def test_retroreboot_is_an_undo_point_rather_than_a_dead_end(retro):
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


async def test_retroreboot_does_not_overwrite_the_save_state_on_disk(retro):
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


async def test_retroreboot_leaves_the_in_game_battery_save_alone(retro):
    """The player's own save, which a real console's reset never wiped."""
    from .fakes import FakeEmulator

    FakeEmulator.sram_bytes = 2048
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9094, "resetsram")
    saved = bytes((i * 7) % 251 for i in range(2048))
    assert view.emulator.load_sram(saved) is True

    await reset_command(retro)(retro.cog, ctx)

    assert view.emulator.save_sram() == saved, "the reset wiped the in-game save"


async def test_retroreboot_wakes_a_sleeping_session_first(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9095, "resetasleep")
    await view._press(retro.interaction(view, message=view.message), "a")
    await retro.cog.hibernate(view, None)
    assert not view.live

    await reset_command(retro)(retro.cog, ctx)

    assert view.live, "the reset woke the session up"
    assert view.emulator.resets == 1
    assert "has been rebooted" in ctx.said()


async def test_retroreboot_with_no_game_running_says_so(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9096)
    ctx = retro.context(channel)

    await reset_command(retro)(retro.cog, ctx)

    said = ctx.said()
    assert "No game is running in this channel." in said
    assert "retro <name or url>" in said


async def test_only_the_starter_a_moderator_or_the_owner_may_reset(retro):
    """The same gate `[p]retrosleep` has, and for a stronger reason."""
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9097, "resetperms")
    starter = ctx.author
    assert view.starter_id == starter.id

    stranger = retro.context(channel, author=FakeUser(uid=99))
    await reset_command(retro)(retro.cog, stranger)
    assert "can reboot it" in stranger.said()
    assert view.emulator.resets == 0, "a stranger rebooted somebody's game"

    for author in (
        starter,
        FakeUser(uid=98, manage_messages=True),  # a moderator
        FakeUser(uid=1),                         # the bot owner
    ):
        allowed = retro.context(channel, author=author)
        await reset_command(retro)(retro.cog, allowed)
        assert "has been rebooted" in allowed.said(), author.id
    assert view.emulator.resets == 3

    # And it is exactly the check retrosleep uses, rather than a second copy.
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


def test_the_two_commands_are_named_for_what_they_do(retro):
    """They used to be `[p]retroreset` and `[p]retrosaves reset`.

    One word apart, opposite in effect, and each docstring carried a bolded
    disclaimer about the other -- plus a third in a reply. That is the names
    being wrong rather than the help being thin, so they were renamed:
    `[p]retroreboot` reboots the console that is playing, `[p]retrosaves
    dropstate` deletes a save state file. Both old names still work as
    aliases.
    """
    # The callback's docstring, which is what Red turns into the help text:
    # `Command.__doc__` is the *class*'s docstring under the real Red.
    reboot = retro.cogmod.Retro.retroreboot.callback.__doc__
    drop = retro.cogmod.Retro.retrosaves_dropstate.callback.__doc__

    assert "power switch" in reboot and "title screen" in reboot
    assert "Nothing on disk is deleted or overwritten" in reboot
    assert "save state file" in drop and "deletes a *file*" in drop
    assert "last in-game save" in drop
    # Each still points at the other, once, without a bolded warning: the
    # names now carry the difference.
    assert "[p]retrosaves dropstate" in reboot
    assert "[p]retroreboot" in drop
    assert "**This is not" not in reboot and "**This is not" not in drop
    # ...and the old names are still typeable.
    assert "[p]retroreset" in reboot and "[p]retrosaves reset" in drop


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


# -- retroend: finishing with a game ------------------------------------------
#
# The capability the cog did not have. `[p]retrostop` only ever *paused* -- the
# controls stayed live and the next press woke the game -- so there was no way
# to say "we are done with this" and have the controller stop responding. This
# goes through the same _retire path a channel switching games takes, so there
# is one way to retire a session rather than two.


def end_command(retro):
    return retro.cogmod.Retro.retroend.callback


async def test_retroend_saves_the_game_and_leaves_a_resume_button(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9100, "finishme")
    emulator = view.emulator
    await view._press(retro.interaction(view, message=view.message), "a")

    await end_command(retro)(retro.cog, ctx)

    # Saved, the core freed, and out of the channel's sessions.
    assert not emulator.started
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    assert retro.cog.sessions.get(channel.id) is None
    # The message keeps exactly one button, and it is Resume.
    retired = retro.cog.retired.get(view.message_id)
    assert retired is not None
    assert [c.custom_id for c in retired.children] == [
        f"{retro.viewmod.CUSTOM_ID_PREFIX}:resume"
    ]
    # Nothing was deleted, and the reply says so.
    assert retro.cog._rom_path(view.rom_filename).is_file()
    said = ctx.said()
    assert "Resume" in said and "Nothing was deleted" in said
    assert f"retro {view.slug}" in said


async def test_retroend_makes_the_controls_inert_and_drops_the_queue(retro):
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9101, "inertme")
    assert view.enqueue_press(
        retro.interaction(view, user=FakeUser(uid=91, name="Ada")), "left"
    )

    await end_command(retro)(retro.cog, ctx)

    assert view.closed and not view.queue
    # A click on the old controls moves nothing, which is the whole
    # difference from `[p]retrosleep`. It is *answered* -- privately, with
    # where the game went -- rather than met with silence, but nothing is
    # emulated and nothing is taken down.
    stale = retro.interaction(view, message=view.message)
    await view._press(stale, "a")
    assert stale.kinds() == ["response.send_message"]
    assert not view.queue
    assert view.emulator is None


async def test_retroend_has_the_same_gate_as_sleeping_and_rebooting(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9102, "gatedend")
    stranger = retro.context(channel, author=FakeUser(uid=4242))

    await end_command(retro)(retro.cog, stranger)
    assert "bot owner can do that" in stranger.said()
    assert retro.cog.sessions.get(channel.id) is view, "nothing happened"

    allowed = retro.context(channel, author=FakeUser(uid=view.starter_id))
    await end_command(retro)(retro.cog, allowed)
    assert retro.cog.sessions.get(channel.id) is None


async def test_retroend_with_no_game_running_says_so(retro):
    ctx = retro.context(retro.channel(9103))
    await end_command(retro)(retro.cog, ctx)
    assert "No game is running" in ctx.said()


def test_retrosleep_and_retroend_say_what_each_other_is_for(retro):
    """The old name admitted it did not stop anything while claiming to.

    `[p]retrostop`'s docstring said "the controls stay live: pressing any
    button picks the game up again" *and* "this is the only way to stop a
    game". Both halves are now honest and each names the other.
    """
    sleep = retro.cogmod.Retro.retrosleep.callback.__doc__
    end = retro.cogmod.Retro.retroend.callback.__doc__

    assert "keeping its controls live" in sleep
    assert "[p]retroend" in sleep, "and the way to really finish is named"
    assert "only way to stop" not in sleep
    assert "[p]retrostop" in sleep, "the old name still works and is said so"

    assert "retire its controls" in end
    assert "[p]retrosleep" in end
    assert "Nothing is deleted" in end


# -- A queue nobody drains ----------------------------------------------------
#
# An entry is only ever taken down while something holds `view.lock`, and for
# a long time only one thing that holds it -- an ordinary press -- ever
# drained it afterwards. An undo, a `[p]retroreboot` and a `[p]retrosleep` all
# take the same lock. Each of them throws the queue away as it starts, which
# covers the presses aimed at the game they are about to move; none of them
# used to deal with a click landing *after* that and before the lock was given
# back, and that entry is the dangerous one. It survives with the current
# epoch and nothing is coming to run it, so `RetroView.busy` is true for ever:
# every later click is queued behind an entry nothing will take, and the
# controller answers nothing at all until the session hibernates.
#
# All three arrange exactly that: the click lands from inside the operation,
# after it has discarded the queue, which is the one moment that used to
# strand an entry. There is a fourth test for the backstop.


def click_lands_during(view, interaction, field="a"):
    """
    Queue a press from inside an operation that is holding ``view.lock``.

    This is precisely what :meth:`RetroView._press` does when it finds the
    session busy, and doing it here rather than through ``_press`` is what
    puts the click *after* the operation's own ``forget_queue`` -- the window
    that used to strand it.
    """
    assert view.enqueue_press(interaction, field), "the click was not taken down"


async def test_a_press_queued_during_an_undo_is_run_rather_than_stranded(retro):
    """An undo drains what arrived while it held the lock."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9310, "undodrain")
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.history, "nothing to undo, so this would prove nothing"

    late = retro.interaction(
        view, user=FakeUser(uid=781, name="Latecomer"), message=view.message
    )
    original = view.capture_undo

    def undo_then_click(*args, **kwargs):
        clip = original(*args, **kwargs)
        # The undo has discarded the queue by now, so this entry is one it
        # cannot have thrown away: a click that landed a moment too late.
        click_lands_during(view, late, "b")
        return clip

    view.capture_undo = undo_then_click
    await undo(retro, view)

    assert not view.queue, "the queued press was left with nobody to run it"
    assert not view.busy, "which is a controller that answers nothing at all"
    # It really ran, and made its own single edit through its own deferred
    # interaction -- exactly as a press queued behind a press does.
    assert late.kinds() == ["response.defer", "edit_original_response"]


async def test_a_press_queued_during_a_reboot_is_dropped_not_stranded(retro):
    """
    Rebooting drops the queue by design -- but it has to drop *all* of it.

    Draining would be wrong here: those presses were aimed at a game
    mid-play and this is the title screen. Leaving them is worse than
    either, so they go, and the line the reboot writes says how many.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9311, "rebootdrain")

    late = retro.interaction(
        view, user=FakeUser(uid=782, name="Latecomer"), message=view.message
    )
    original = view.capture_reset

    def reset_then_click(*args, **kwargs):
        clip = original(*args, **kwargs)
        click_lands_during(view, late, "b")
        return clip

    view.capture_reset = reset_then_click
    await reset_command(retro)(retro.cog, ctx)

    assert not view.queue, "an entry nothing will ever run"
    assert not view.busy
    # Dropped rather than emulated: the entry never became a press, so this
    # interaction was never edited. (It carries no defer either, because the
    # click was taken down by hand here rather than through `_press`, which
    # is what acknowledges one.)
    assert "edit_original_response" not in late.kinds()
    # And the drop is announced rather than silent -- a press that simply
    # vanishes is the whole complaint the queue exists to answer. It rides
    # out on the reboot's own line, which is why the counter reads zero by
    # now: `dropped_note` clears it as it is shown.
    assert "dropped" in view.message.edits[-1]["content"]
    assert view.queue_dropped == 0, "said once, not on every later line"


async def test_a_press_queued_during_a_sleep_is_dropped_not_woken(retro):
    """
    Sleeping drops it too, and must not drain: draining would wake the game.

    `[p]retrosleep` promises the queued presses go and the game stays asleep
    until somebody presses something. Running the stragglers would undo both
    halves of that in one go.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9312, "sleepdrain")

    late = retro.interaction(
        view, user=FakeUser(uid=783, name="Latecomer"), message=view.message
    )
    original = retro.cog.hibernate

    async def hibernate_then_click(*args, **kwargs):
        await original(*args, **kwargs)
        click_lands_during(view, late, "b")

    retro.cog.hibernate = hibernate_then_click
    try:
        await sleep_command(retro)(retro.cog, ctx)
    finally:
        retro.cog.hibernate = original

    assert not view.queue and not view.busy
    assert not view.live, "a drained press would have woken it straight back up"
    assert "edit_original_response" not in late.kinds(), "it was emulated after all"


async def test_a_stranded_queue_is_drained_by_the_next_press(retro):
    """
    The backstop, so this class of bug cannot brick a controller again.

    A queue with no runner is not supposed to exist at all. If one does --
    some future path taking the lock and walking away from what arrived --
    the next click starts a runner rather than joining the line behind it
    for ever.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9313, "stranded")

    stranded = retro.interaction(
        view, user=FakeUser(uid=784, name="Stuck"), message=view.message
    )
    assert view.enqueue_press(stranded, "left")
    assert view.busy and not view.running, "a queue with nobody working through it"

    nudger = retro.interaction(
        view, user=FakeUser(uid=785, name="Nudger"), message=view.message
    )
    await view._press(nudger, "a")

    assert not view.queue, "the stranded entry is still waiting"
    assert not view.busy
    assert stranded.kinds() == ["response.defer", "edit_original_response"]


# -- What the one core is held across -----------------------------------------
#
# `emulator_lock` serializes every core operation in every channel, so
# anything slow done while holding it is latency charged to channels that had
# nothing to do with it. Two things used to be done under it that are not core
# work at all: talking to Discord (the first message of a game, and the edit
# that tells an evicted channel it went to sleep) and writing the autosave to
# disk. A Discord rate limit on one channel's message therefore froze
# gameplay bot-wide, and every third press paid for a couple of hundred
# kilobytes of fsync'd disk before its own clip could go out.


async def test_the_first_message_of_a_game_is_sent_with_the_core_free(retro):
    """Booting holds the lock; posting does not."""
    await retro.install_cores("gambatte")
    channel = retro.channel(9320)
    ctx = retro.context(channel)
    seen = {}

    original = retro.viewmod.RetroView.post

    async def watched_post(self, ctx, clip):
        seen["locked"] = retro.cog.emulator_lock.locked()
        return await original(self, ctx, clip)

    retro.viewmod.RetroView.post = watched_post
    try:
        await retro.start_game(ctx, "postfree")
    finally:
        retro.viewmod.RetroView.post = original

    assert seen["locked"] is False, "the one core was held while Discord answered"
    assert retro.cog.sessions[channel.id].live, "and the game really started"


async def test_the_autosave_is_captured_under_the_lock_and_written_outside(retro):
    """
    Reading the state out of a core is sub-millisecond; writing it is disk.

    So the read happens where it has to -- on the emulator thread, under the
    lock, right after the press -- and the write happens once the lock is
    back. A press that had to wait for somebody else's fsync is a press
    somebody is sitting watching.
    """
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9321, "autosave")
    every = retro.viewmod.SAVE_STATE_EVERY_PRESSES
    seen = []

    original = retro.cog._write_captured

    async def watched_write(*args, **kwargs):
        seen.append(retro.cog.emulator_lock.locked())
        return await original(*args, **kwargs)

    retro.cog._write_captured = watched_write
    try:
        for _ in range(every):
            await view._press(retro.interaction(view, message=view.message), "a")
    finally:
        retro.cog._write_captured = original

    assert seen, "no autosave happened, so this proves nothing"
    assert not any(seen), "the save state was written with the one core held"
    assert retro.cog._state_path(channel.id, view.slug).is_file()


async def test_an_evicted_channel_is_told_with_the_core_free(retro):
    """
    The edit that says "your game went to sleep" is not core work either.

    It used to be awaited inside `_hibernate_locked`, i.e. under the lock,
    once per evicted session -- so one channel starting a game held the core
    while Discord thought about another channel's message.
    """
    await retro.install_cores("gambatte")
    first, _, _ = await retro.posted_game(9322, "evictedone")
    assert first.live

    seen = {}
    original = retro.viewmod.RetroView.refresh

    async def watched_refresh(self, note=None):
        if self is first:
            seen["locked"] = retro.cog.emulator_lock.locked()
        return await original(self, note)

    retro.viewmod.RetroView.refresh = watched_refresh
    try:
        second, _, _ = await retro.posted_game(9323, "evictedtwo")
    finally:
        retro.viewmod.RetroView.refresh = original

    assert not first.live and second.live, "the core changed hands"
    assert seen.get("locked") is False, "the eviction notice was sent under the lock"
    # And it was still actually said, on the evicted game's own message.
    assert "asleep" in first.message.edits[-1]["content"].lower() or (
        "sleep" in first.message.edits[-1]["content"].lower()
    )


async def test_a_session_that_lost_its_channel_is_not_popped_by_the_loser(retro):
    """
    Teardown removes a channel's session only if it is still its own.

    `_retire` and a failed start both await, and a Resume click landing
    during one installs a *live* session in the same channel. An
    unconditional pop then removes that live session from the only
    dictionary `_evict_locked` looks at -- leaving a loaded core nothing can
    reach, which with MAX_LIVE_EMULATORS at 1 is the cog dead until restart.
    """
    await retro.install_cores("gambatte")
    old, _, channel = await retro.posted_game(9324, "oldgame")
    newer, _, _ = await retro.posted_game(9325, "newergame")
    # Pretend the newer session took this channel over while `old` was being
    # retired: same channel, different view.
    retro.cog.sessions[channel.id] = newer

    assert retro.cog._forget_session_view(channel.id, old) is False
    assert retro.cog.sessions[channel.id] is newer, "the live session was evicted"

    # ...and it does remove the entry when it really is the one named.
    assert retro.cog._forget_session_view(channel.id, newer) is True
    assert channel.id not in retro.cog.sessions


async def test_the_clip_is_encoded_with_the_core_free(retro):
    """
    Encoding costs more than emulating, and needs no core.

    A clip is captured under `emulator_lock` -- that part really is the core
    -- and turned into WebP after it, so the next channel's press starts
    while this channel's picture is still being written. Doing both under
    the lock made the single core the bottleneck for the one step that never
    needed it.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9330, "encodefree")
    seen = {}

    original = view._encode

    def watched_encode(captured, limit=None, emulator=None):
        seen["locked"] = retro.cog.emulator_lock.locked()
        return original(captured, limit, emulator)

    view._encode = watched_encode
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")

    assert "locked" in seen, "nothing was encoded, so this proves nothing"
    assert seen["locked"] is False, "the one core was held while WebP was written"
    # And the press still posted a real clip.
    assert interaction.clip(), "the press posted no picture"


async def test_a_press_still_posts_its_clip_if_another_channel_takes_the_core(retro):
    """
    Encoding happens after the lock, so eviction must not cost the clip.

    The frames are already captured and encoding touches no core -- but the
    session's `emulator` is cleared the moment another channel evicts it,
    which can happen the instant the lock is given back. Reading the encoder
    off the view at that point would fail a press whose picture was sitting
    right there, finished.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9350, "evictedmidpress")
    other, _, _ = await retro.posted_game(9351, "thief")
    # `other` holds the core now, so this session is asleep; wake it so the
    # press below is an ordinary one.
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.live

    original = view._encode

    def encode_after_eviction(captured, limit=None, emulator=None):
        # Exactly what an eviction in another channel does, at the worst
        # possible moment: the lock is free, the frames are captured, and
        # the clip has not been encoded yet.
        view.emulator = None
        return original(captured, limit, emulator)

    view._encode = encode_after_eviction
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "b")

    assert interaction.clip(), "the press lost a clip it had already emulated"
    assert interaction.kinds() == ["response.defer", "edit_original_response"]


async def test_the_whole_line_reads_as_one_person_walking(retro):
    """
    The shape the line takes when somebody queues a run of directions.

    Pinned as a literal, because this is the thing anybody actually looks
    at: `**Pokemon** · Rob pressed ⬆️. *Queued: Rob ⬆️⬇️⬇️*` -- the game,
    what just happened, and the run still to come, on one line beside the
    picture.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9016, "Pokemon")
    rob = FakeUser(uid=11, name="Rob")
    up = "\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"
    down = "\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"

    running = retro.interaction(view, user=rob, message=view.message)
    original = view.capture_press
    once = []

    def slow(field, repeat=1):
        if not once:
            once.append(True)
            for queued in ("up", "down", "down"):
                assert view.enqueue_press(
                    retro.interaction(view, user=rob, message=view.message), queued
                )
        return original(field, repeat)

    view.capture_press = slow
    try:
        await view._press(running, "up")
    finally:
        view.capture_press = original

    edit = next(
        snap for kind, snap in running.log if kind == "edit_original_response"
    )
    assert edit["content"] == (
        f"**Pokemon** \N{MIDDLE DOT} Rob pressed {up}. "
        f"*Queued: Rob {up}{down}{down}*"
    ), edit["content"]
