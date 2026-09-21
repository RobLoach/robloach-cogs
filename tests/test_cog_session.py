"""A session's whole life: start, press, sleep, wake, restart, stop.

The real Retro and the real RetroView, driven against the fakes in
tests/fakes.py. Nothing here needs a libretro core.
"""

import asyncio
import os
import time

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

import types  # noqa: E402

import discord  # noqa: E402

from .fakes import NES_BYTES, ROM_BYTES, FakeUser  # noqa: E402

# -- Defaults -----------------------------------------------------------------


async def test_the_clip_and_hold_defaults_reach_a_new_install(retro):
    from retro.emulator import CLIP_SECONDS

    assert CLIP_SECONDS == 4
    assert await retro.cog.config.clip_seconds() == 4
    assert await retro.cog.config.hold_ms() == retro.viewmod.DEFAULT_HOLD_MS == 160


async def test_an_already_configured_value_survives_a_new_default(retro):
    await retro.cog.config.clip_seconds.set(9)
    await retro.cog.config.hold_ms.set(420)
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8050, "defaults")
    assert view.clip_seconds == 9
    assert view.hold_ms == 420


async def test_retroset_cliplength_reports_and_clamps(retro):
    channel = retro.channel(8051)
    ctx = retro.context(channel)
    cliplength = retro.cogmod.Retro.retroset_cliplength.callback

    await cliplength(retro.cog, ctx, 4)
    assert await retro.cog.config.clip_seconds() == 4
    assert "4 seconds" in ctx.sent[-1]
    await cliplength(retro.cog, ctx, 0)
    assert await retro.cog.config.clip_seconds() == 1
    await cliplength(retro.cog, ctx, 9999)
    assert await retro.cog.config.clip_seconds() == 15


async def test_nothing_of_the_stop_button_is_left_in_the_view(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8052, "stopless")
    assert not hasattr(retro.viewmod, "_StopButton")
    assert not hasattr(view, "stop_item")
    assert not hasattr(view, "_stop")
    assert not hasattr(view, "_sync_children")


async def test_can_stop_is_kept_for_retrostop(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(8053, "whosegame")
    assert await view.can_stop(FakeUser(uid=view.starter_id))
    assert not await view.can_stop(FakeUser(uid=4242))
    # The bot owner may always stop a game; FakeBot says owner is user 1.
    assert await view.can_stop(FakeUser(uid=1))


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
    assert max(t[1] + t[2] for t in taps) < emulator.frames_for_seconds(view.clip_seconds)


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
        assert not snap["has_embed"], snap
    final = interaction.log[-1][1]
    assert final["n_attachments"] == 1
    assert final["filenames"][0].endswith(".webp")
    assert final["content"] is None
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


async def test_a_press_greys_the_controls_then_posts_exactly_one_clip(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9002, "ucity")
    assert view.live

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert interaction.kinds() == ["response.edit_message", "edit_original_response"]
    first, second = interaction.log[0][1], interaction.log[1][1]
    assert first["all_disabled"], "the immediate response greys everything out"
    assert not first["has_attachments"], "and uploads nothing"
    assert not second["any_disabled"], "the final edit brings them back"
    assert second["has_attachments"] and second["n_attachments"] == 1
    assert sum(1 for _, snap in interaction.log if snap.get("has_attachments")) == 1


async def test_the_clip_is_cached_for_replay(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9003, "cached")
    await view._press(retro.interaction(view, message=view.message), "a")
    assert isinstance(view.last_clip, bytes) and view.last_clip
    assert view.last_clip[:4] == b"RIFF" and view.last_clip[8:12] == b"WEBP"


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
        replay = retro.interaction(view, message=view.message)
        await retro.control(view, "replay").callback(replay)
    assert held.kinds() == ["response.defer"]
    assert replay.kinds() == ["response.defer"]


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

    kinds = sorted([tuple(first.kinds()), tuple(second.kinds())])
    assert kinds == [
        ("response.defer",),
        ("response.edit_message", "edit_original_response"),
    ]
    clips = sum(
        1 for i in (first, second) for _, snap in i.log if snap.get("has_attachments")
    )
    assert clips == 1


# -- Replay -------------------------------------------------------------------


async def test_replay_re_uploads_the_only_cached_clip(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9020, "replayme")
    assert len(view.clips) == 1, "the boot clip is the whole buffer so far"

    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "replay").callback(interaction)
    kind, snap = interaction.log[0]
    assert kind == "response.edit_message"
    assert snap["n_attachments"] == 1
    assert snap["filenames"][0].endswith(".webp")
    assert not snap["any_disabled"], "a one-clip replay leaves the buttons alone"


async def test_replay_stitches_the_last_few_clips_into_one(retro):
    pytest.importorskip("PIL", reason="stitching clips back together needs Pillow")
    from .fakes import FAKE_CLIP_FRAMES

    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9022, "stitched")
    for _ in range(2):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert len(view.clips) == 3

    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "replay").callback(interaction)

    kinds = interaction.kinds()
    assert kinds[0] == "response.edit_message", "the controls grey out first"
    assert interaction.log[0][1]["all_disabled"]
    assert "seconds together" in (interaction.log[0][1]["content"] or "")
    final = interaction.log[-1][1]
    assert final["n_attachments"] == 1
    assert not final["any_disabled"]
    assert "replayed" in (final["content"] or "")

    # The result really is all three clips, end to end and in order, and the
    # buffer itself is left alone -- a replay is not a new clip.
    from retro.emulator import concatenate_clips, decode_clip

    stitched, seconds = concatenate_clips([data for data, _ in view.clips])
    frames, _ = decode_clip(stitched)
    assert len(frames) == 3 * FAKE_CLIP_FRAMES
    assert seconds == pytest.approx(9.0)
    last_frame, _ = decode_clip(view.clips[-1][0])
    assert frames[-1].tobytes() == last_frame[-1].tobytes(), (
        "the newest footage is at the end"
    )
    assert len(view.clips) == 3


async def test_a_stitched_replay_is_bounded_by_seconds(retro):
    pytest.importorskip("PIL", reason="stitching clips back together needs Pillow")
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9023, "bounded")
    for _ in range(10):
        await view._press(retro.interaction(view, message=view.message), "a")

    # Four-second clips, fifteen seconds of replay: four clips, never eleven.
    assert view.buffered_seconds <= retro.viewmod.REPLAY_SECONDS
    assert len(view.clips) <= retro.cogmod.REPLAY_SECONDS / view.clip_seconds + 1
    assert sum(len(data) for data, _ in view.clips) <= retro.viewmod.MAX_REPLAY_BYTES


async def test_the_replay_button_says_how_much_it_will_replay(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9024, "labelled")
    button = retro.control(view, "replay")
    assert button.label == "Replay", "one clip, nothing extra to promise"
    assert not button.disabled

    await view._press(retro.interaction(view, message=view.message), "a")
    assert button.label == f"Replay {round(view.buffered_seconds)}s"
    assert not button.disabled


async def test_the_replay_button_is_dead_and_says_so_with_an_empty_buffer(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9021, "nocache")
    view.last_clip = None
    button = retro.control(view, "replay")
    assert button.disabled and button.label == "Replay"

    interaction = retro.interaction(view, message=view.message)
    await button.callback(interaction)
    kind, snap = interaction.log[0]
    assert kind == "response.send_message"
    assert snap["ephemeral"] is True
    assert "memory" in (snap["content"] or "")


async def test_a_restored_session_has_a_dead_replay_button(retro):
    # The buffer is memory only, so a message that survived a restart has
    # nothing to replay and must not pretend otherwise.
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9025, "afterboot")
    await retro.cog.hibernate(view, None)

    cog2, bot2 = retro.make_cog()
    bot2.channels[channel.id] = channel
    await cog2._restore_sessions()
    restored = cog2.sessions[channel.id]
    button = next(
        c for c in restored.children
        if c.custom_id == f"{retro.viewmod.CUSTOM_ID_PREFIX}:replay"
    )
    assert button.disabled and not restored.clips


async def test_a_replay_that_cannot_be_stitched_falls_back_to_the_last_clip(
    retro, monkeypatch
):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9026, "brokenstitch")
    await view._press(retro.interaction(view, message=view.message), "a")

    def boom(*args, **kwargs):
        raise RuntimeError("no encoder here")

    monkeypatch.setattr(retro.viewmod, "concatenate_clips", boom)
    interaction = retro.interaction(view, message=view.message)
    await retro.control(view, "replay").callback(interaction)
    final = interaction.log[-1][1]
    assert final["n_attachments"] == 1, "the game is fine; show what we have"
    assert not final["any_disabled"]


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
    assert not any(c.disabled for c in retro.playable(view))
    assert all(c.disabled for c in view.children if isinstance(c, retro.viewmod._SpacerButton))
    assert retro.cog.config.channels[channel.id]["session"]["slug"] == view.slug


async def test_a_press_resumes_a_sleeping_session_and_says_so_once(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9031, "wakeup")
    await retro.cog.hibernate(view, None)
    assert not view.live

    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert view.live
    assert "Resuming" in (interaction.log[0][1]["content"] or "")
    assert not interaction.log[0][1]["has_embed"]
    assert interaction.log[-1][1]["content"] is None, "the resume line is cleared with the clip"


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
    assert restored.last_clip is None, "clips live in memory only"

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
    assert not any(getattr(c, "disabled", False) for c in retro.playable(view))
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
    assert not any(c.disabled for c in retro.playable(view))
    assert "carry on" in ctx.sent[-1]
    stopped = view.message.edits[-1] if view.message.edits else {}
    assert "Stopped by" in (stopped.get("content") or "")
    assert "embed" not in stopped


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
