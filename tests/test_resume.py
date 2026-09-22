"""Coming back to a game: starting it again, and the Resume button.

Two things that used to lose a channel's progress:

* starting a game by name in a channel that already had a save state for it
  cold-booted straight over the top of that save state;
* replacing a channel's game left the old message with dead buttons and a
  line of text telling the player to type the game's name again.

Both are covered here, including the fallback chain a restore uses -- save
state, then the cartridge's battery save, then the beginning -- and the part
that has to keep working after the bot has been restarted.
"""

import types

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import ROM_BYTES, FakeEmulator  # noqa: E402


async def play(retro, ctx, game, cog=None):
    # These tests start games in bursts to build a scenario; the start
    # cooldown is exercised on its own in test_limits.py.
    retro.forgive_cooldowns()
    await retro.cogmod.Retro.retro.callback(cog or retro.cog, ctx, game=game)


# -- Starting a game the channel already has a save for -----------------------


async def test_restarting_a_game_resumes_from_its_save_state(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9600)
    ctx = retro.context(channel)

    first = await retro.start_game(ctx, "carryon")
    first.emulator.advance(5000)
    await retro.cog._write_state(first)
    saved_frame = first.emulator.frame
    state_path = retro.cog._state_path(channel.id, first.slug)
    assert state_path.is_file()

    # Play something else, so the session is retired and popped...
    retro.serve("other.gbc", ROM_BYTES)
    await play(retro, ctx, "https://example.com/other.gbc")
    assert retro.cog.sessions[channel.id].slug != first.slug

    # ...then come back to the first game by name. This used to cold-boot.
    retro.serve("carryon.gbc", ROM_BYTES)
    await play(retro, ctx, "https://example.com/carryon.gbc")

    back = retro.cog.sessions[channel.id]
    assert back.slug == first.slug
    assert back is not first
    assert back.boot_outcome == "state"
    assert back.emulator.loaded_from == saved_frame
    assert state_path.is_file(), "a usable state is kept"


async def test_the_resumed_start_says_it_picked_up_where_it_left_off(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9601)
    ctx = retro.context(channel)
    view = await retro.start_game(ctx, "sayso")
    await retro.cog._write_state(view)
    retro.cog.sessions.pop(channel.id)

    ctx2 = retro.context(channel)
    await retro.start_game(ctx2, "sayso")
    said = ctx2.said()
    assert "Picked up from where this channel left off" in said


async def test_a_corrupt_save_state_falls_back_and_is_thrown_away(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9602)
    ctx = retro.context(channel)
    view = await retro.start_game(ctx, "corrupt")
    state_path = retro.cog._state_path(channel.id, view.slug)
    state_path.write_bytes(b"NOT A STATE AT ALL")
    retro.cog.sessions.pop(channel.id)

    ctx2 = retro.context(channel)
    back = await retro.start_game(ctx2, "corrupt")

    assert back.boot_outcome == "fresh"
    assert back.live, "an unusable state costs the moment, never the game"
    assert not state_path.exists(), "and the unusable file is not kept"
    assert "could not be used" in ctx2.said()
    assert "from the beginning" in ctx2.said()


async def test_a_corrupt_save_state_still_restores_the_battery_save(retro):
    await retro.install_cores("gambatte")
    FakeEmulator.sram_bytes = 8192
    channel = retro.channel(9603)
    ctx = retro.context(channel)
    view = await retro.start_game(ctx, "batterycorrupt")
    view.emulator.sram[:] = b"\x5a" * 8192
    await retro.cog._write_sram(view)
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"RUBBISH")
    retro.cog.sessions.pop(channel.id)

    ctx2 = retro.context(channel)
    back = await retro.start_game(ctx2, "batterycorrupt")

    assert back.boot_outcome == "sram"
    assert back.emulator.loaded_sram == b"\x5a" * 8192
    assert "in-game save survived" in ctx2.said()


async def test_only_a_battery_save_starts_the_game_with_it_in_place(retro):
    await retro.install_cores("gambatte")
    FakeEmulator.sram_bytes = 8192
    channel = retro.channel(9604)
    ctx = retro.context(channel)
    view = await retro.start_game(ctx, "batteryonly")
    view.emulator.sram[:] = b"\x77" * 8192
    await retro.cog._write_sram(view)
    retro.cog._state_path(channel.id, view.slug).unlink(missing_ok=True)
    retro.cog.sessions.pop(channel.id)

    ctx2 = retro.context(channel)
    back = await retro.start_game(ctx2, "batteryonly")

    assert back.boot_outcome == "sram"
    assert back.emulator.loaded_sram == b"\x77" * 8192
    assert "in-game save already in place" in ctx2.said()


async def test_a_game_with_nothing_saved_cold_boots_quietly(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9605)
    ctx = retro.context(channel)
    view = await retro.start_game(ctx, "brandnew")
    assert view.boot_outcome == "fresh"
    assert view.notice is None
    assert "could not be used" not in ctx.said()


async def test_a_state_from_another_channel_is_not_picked_up(retro):
    await retro.install_cores("gambatte")
    one = retro.channel(9606)
    two = retro.channel(9607)
    view = await retro.start_game(retro.context(one), "shared")
    view.emulator.advance(9000)
    await retro.cog._write_state(view)

    other = await retro.start_game(retro.context(two), "shared")
    assert other.boot_outcome == "fresh", "saves are per channel"


async def test_an_unreadable_state_file_does_not_stop_the_start(retro, monkeypatch):
    await retro.install_cores("gambatte")
    channel = retro.channel(9608)
    view = await retro.start_game(retro.context(channel), "unreadable")
    await retro.cog._write_state(view)
    retro.cog.sessions.pop(channel.id)

    from pathlib import Path

    real = Path.read_bytes

    def refuse(self, *args, **kwargs):
        if self.suffix == ".state":
            raise OSError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    back = await retro.start_game(retro.context(channel), "unreadable")
    assert back.live and back.boot_outcome == "fresh"


# -- The Resume button on a retired message -----------------------------------


@pytest.fixture
async def retired(retro):
    """A channel that has moved on from one game to another."""
    await retro.install_cores("gambatte")
    channel = retro.channel(9650)
    ctx = retro.context(channel)
    old = await retro.start_game(ctx, "leftbehind")
    old.emulator.advance(4000)
    saved_frame = old.emulator.frame
    await retro.cog._write_state(old)

    retro.serve("current.gbc", ROM_BYTES)
    await play(retro, ctx, "https://example.com/current.gbc")
    current = retro.cog.sessions[channel.id]
    view = retro.cog.retired[old.message_id]

    import types

    return types.SimpleNamespace(
        retro=retro,
        channel=channel,
        ctx=ctx,
        old=old,
        current=current,
        view=view,
        saved_frame=saved_frame,
    )


async def test_a_retired_message_keeps_exactly_one_button(retired):
    children = retired.view.children
    assert len(children) == 1
    button = children[0]
    assert button.label == "Resume"
    assert button.custom_id.endswith(":resume")
    assert not button.disabled


async def test_the_resume_record_has_what_it_needs_to_restart(retro, retired):
    stored = (await retro.cog.config.all_channels())[retired.channel.id]["retired"]
    record = stored[str(retired.old.message_id)]
    assert record["game_name"] == "leftbehind"
    assert record["slug"] == retired.old.slug
    assert record["rom_filename"] == retired.old.rom_filename
    assert record["source"] == retired.old.source
    assert record["core"] == "gambatte"
    assert record["system"] == retired.old.system.key


async def test_resume_restarts_the_game_from_its_save_state(retro, retired):
    saved = retired.saved_frame
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    back = retro.cog.sessions[retired.channel.id]
    assert back.slug == retired.old.slug
    assert back.live
    assert back.boot_outcome == "state"
    assert back.emulator.loaded_from == saved
    # The clicked message is the one that came back to life.
    assert back.message_id == retired.old.message_id
    assert interaction.log[-1][0] == "edit_original_response"
    assert interaction.log[-1][1]["n_attachments"] == 1
    assert not interaction.log[-1][1]["all_disabled"]


async def test_a_resumed_game_draws_its_repeat_button_from_the_real_core(
    retro, retired
):
    """Whether the x3 button exists at all is decided from the real fps.

    A session laid out while hibernated has no core, so it counts taps
    against DEFAULT_FPS; the count is worked out again the moment the core is
    up, which is before the message goes out. At a fifth of a second the
    answer is one tap -- what the console's own A button already does -- so
    the button is not drawn, and the resumed message went out without it
    rather than with a dead "A x1" on it.
    """
    await retro.cog.config.clip_seconds.set(0.2)
    retired.old.clip_seconds = 0.2
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    back = retro.cog.sessions[retired.channel.id]
    assert back.repeat_taps == 1 and not back.has_repeat_button
    assert retro.control(back, "repeat") is None
    # The row the controls sit on still has Wait and Undo, in that order.
    row = max(c.row for c in back.children)
    assert [c.label for c in back.children if c.row == row][-2:] == ["Wait", "Undo"]
    # ...and the message the resume posted was drawn from that same view, so
    # what went out has no x3 button on it either.
    labels = interaction.log[-1][1]["labels"]
    assert not any((label or "").startswith("A x") for label in labels), labels


async def test_resume_hibernates_whatever_else_was_live(retro, retired):
    assert retired.current.live
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    live = [v for v in retro.cog.sessions.values() if v.live]
    assert len(live) <= retro.cogmod.MAX_LIVE_EMULATORS
    assert not retired.current.live, "the game it replaced is saved and asleep"


async def test_resume_retires_the_game_it_replaces_in_turn(retro, retired):
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    # The game that was live has its own Resume button now.
    assert retired.current.message_id in retro.cog.retired
    swapped = retro.cog.retired[retired.current.message_id]
    assert swapped.record["slug"] == retired.current.slug
    assert "Resume" in retired.current.message.edits[-1]["content"]


async def test_the_resume_record_is_forgotten_once_it_is_used(retro, retired):
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    assert retired.old.message_id not in retro.cog.retired
    stored = (await retro.cog.config.all_channels())[retired.channel.id]["retired"]
    assert str(retired.old.message_id) not in stored
    assert retired.view.alive is False, "and the old view can never fire again"


async def test_a_second_click_on_a_used_resume_button_explains_itself(retro, retired):
    # discord.py keeps routing a message's old custom_ids to the old view
    # after a new one is registered for the same message, so this really can
    # be clicked again -- and starting a second copy would leave two messages
    # fighting over one save state.
    await retired.view.children[0].callback(
        retro.interaction(retired.view, message=retired.old.message)
    )
    stale = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(stale)
    kind, snap = stale.log[0]
    assert kind == "response.send_message" and snap["ephemeral"] is True
    assert "already been started again" in snap["content"]
    assert len([v for v in retro.cog.sessions.values() if v.live]) <= 1


async def test_starting_a_retired_game_by_name_stands_its_button_down(retro, retired):
    # Same protection, reached the other way: `[p]retro leftbehind` rather
    # than the button.
    retro.serve("leftbehind.gbc", ROM_BYTES)
    await play(retro, retired.ctx, "https://example.com/leftbehind.gbc")

    assert retired.old.message_id not in retro.cog.retired
    assert retired.view.alive is False
    stored = (await retro.cog.config.all_channels())[retired.channel.id]["retired"]
    assert str(retired.old.message_id) not in stored
    # And the game really did come back from its own save state.
    back = retro.cog.sessions[retired.channel.id]
    assert back.slug == retired.old.slug and back.boot_outcome == "state"


async def test_a_press_elsewhere_mid_resume_never_loads_a_second_core(
    retro, retired, monkeypatch
):
    """The window between "the core is up" and "the channel has it".

    ``resume_retired`` booted its core inside the emulator lock, let the lock
    go, and only *then* retired the game it was replacing (which re-takes the
    lock and awaits a message edit) before finally attaching the core to the
    view and putting it in ``cog.sessions``. A press in another channel
    landing in that window takes the lock, looks through ``cog.sessions`` for
    something live to evict, finds nothing -- the resumed session is not in
    there yet and its view's ``emulator`` is still None -- and loads a second
    libretro core into a process that may only ever have one
    (MAX_LIVE_EMULATORS). Two live cores share process-global state and
    segfault the bot.

    So the press is fired from inside ``_retire``, which is the first thing
    the resume does after the lock is released.
    """
    elsewhere = retro.channel(9660)
    other = await retro.start_game(retro.context(elsewhere), "elsewhere")
    await retro.cog.hibernate(other)
    assert not other.live, "the other channel has to be asleep to be woken"

    live_during_the_press = []
    real_retire = retro.cogmod.Retro._retire

    async def retire_and_press(self, view, reason):
        await self.run_press(other, "a")
        live_during_the_press.append(
            [e for e in FakeEmulator.instances if e.started]
        )
        return await real_retire(self, view, reason)

    monkeypatch.setattr(retro.cogmod.Retro, "_retire", retire_and_press)

    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    assert live_during_the_press, "_retire was never reached, so nothing is proved"
    assert len(live_during_the_press[0]) == 1, (
        "a second libretro core was loaded while the resumed one was still "
        f"running: {live_during_the_press[0]}"
    )
    # And the invariant still holds once everything has settled.
    assert sum(1 for e in FakeEmulator.instances if e.started) == 1
    assert len([v for v in retro.cog.sessions.values() if v.live]) <= 1


async def test_a_resumed_session_is_in_sessions_before_anything_awaits(retro, retired):
    """The narrower statement the fix is actually made of.

    By the time ``_retire`` -- the first thing the resume awaits after
    letting the emulator lock go -- is called, the channel's entry already
    points at the new view and that view already holds the core. Anything
    that looks for a live session from here on finds it.
    """
    seen = {}
    real_retire = retro.cogmod.Retro._retire

    async def note(self, view, reason):
        registered = self.sessions.get(retired.channel.id)
        seen["view"] = registered
        seen["emulator"] = getattr(registered, "emulator", None)
        seen["replaced"] = view
        return await real_retire(self, view, reason)

    retro.cogmod.Retro._retire = note
    try:
        interaction = retro.interaction(retired.view, message=retired.old.message)
        await retired.view.children[0].callback(interaction)
    finally:
        retro.cogmod.Retro._retire = real_retire

    back = retro.cog.sessions[retired.channel.id]
    assert seen["replaced"] is retired.current, "the wrong game was retired"
    assert seen["view"] is back, "the channel still pointed at the old session"
    assert seen["emulator"] is not None, "the core was not attached yet"
    assert back.emulator is seen["emulator"]


async def test_resume_says_so_gracefully_when_the_rom_has_been_pruned(retro, retired):
    retro.cog._rom_path(retired.old.rom_filename).unlink()
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    kind, snap = interaction.log[0]
    assert kind == "response.send_message"
    assert snap["ephemeral"] is True
    assert "cleaned up" in snap["content"]
    assert "save is still here" in snap["content"]
    # It tells the player to start the game again, so it has to name the
    # command with the prefix this bot answers to. A button click has no
    # ctx.clean_prefix, and Red only rewrites `[p]` in a docstring, so this
    # reply used to reach the channel saying `[p]retro <name or url>` --
    # which is not something anybody can type. See Retro._prefix_for.
    assert "[p]" not in snap["content"], snap["content"]
    assert "`!retro <name or url>`" in snap["content"], snap["content"]
    # Nothing was broken by the attempt.
    assert retro.cog.sessions[retired.channel.id] is retired.current
    assert retired.view.alive is True


async def test_the_resume_reply_follows_the_bot_s_own_prefix(retro, retired):
    """Whatever the owner set it to, and never the bot's own mention.

    Red's ``--mentionable`` puts ``<@id>`` at the front of the prefix list,
    and a raw mention in the middle of a sentence reads as a bug rather than
    as an instruction, so the first *typable* prefix is the one used.
    """
    retro.bot.prefixes = ["<@123456> ", "retro!", "?"]
    retro.cog._rom_path(retired.old.rom_filename).unlink()
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    content = interaction.log[0][1]["content"]
    assert "`retro!retro <name or url>`" in content, content
    assert "<@123456>" not in content, content


async def test_a_bot_that_cannot_say_its_prefix_still_answers(retro):
    """Degrades to naming the command bare, never to printing `[p]`."""

    class Mute:
        async def get_valid_prefixes(self, guild=None):
            raise RuntimeError("no prefix cache yet")

    retro.cog.bot = Mute()
    assert await retro.cog._prefix_for(None) == ""
    retro.cog.bot = types.SimpleNamespace(
        get_valid_prefixes=lambda guild=None: _returning([])
    )
    assert await retro.cog._prefix_for(None) == ""


async def _returning(value):
    return value


async def test_resume_says_so_when_the_core_has_gone(retro, retired):
    await retro.cog.config.cores.set({})
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)
    kind, snap = interaction.log[0]
    assert kind == "response.send_message"
    assert "not installed any more" in snap["content"]


async def test_a_failed_resume_puts_the_button_back(retro, retired, monkeypatch):
    def boom(self, emulator, state=None, sram=None):
        raise RuntimeError("the core fell over")

    monkeypatch.setattr(retro.viewmod.RetroView, "_boot", boom)
    interaction = retro.interaction(retired.view, message=retired.old.message)
    await retired.view.children[0].callback(interaction)

    assert retired.old.message_id in retro.cog.retired, "still resumable"
    assert "could not be started" in (interaction.log[-1][1]["content"] or "")
    # The game that was playing was only put to sleep, not retired: its own
    # controls still work, so a failed Resume costs the channel nothing.
    assert retro.cog.sessions[retired.channel.id] is retired.current
    assert retired.current.message_id not in retro.cog.retired
    assert not retired.current.closed


# -- Surviving a restart ------------------------------------------------------


async def test_the_resume_button_still_works_after_a_bot_restart(retro, retired):
    # Everything in memory goes away; only Config and the data folder remain.
    cog2, bot2 = retro.make_cog()
    bot2.channels[retired.channel.id] = retired.channel
    await cog2._restore_sessions()

    revived = cog2.retired.get(retired.old.message_id)
    assert revived is not None, "the Resume button was rebuilt from Config"
    assert revived.record["game_name"] == "leftbehind"
    assert (revived, retired.old.message_id) in [
        (view, mid) for view, mid in bot2.added_views
    ], "and Discord will route clicks to it"

    interaction = retro.interaction(revived, message=retired.old.message)
    await revived.children[0].callback(interaction)

    back = cog2.sessions[retired.channel.id]
    assert back.slug == retired.old.slug and back.live
    assert back.boot_outcome == "state"


async def test_a_retired_view_is_persistent(retro, retired):
    assert retired.view.is_persistent()
    assert retired.view.timeout is None


async def test_only_so_many_resume_buttons_are_kept_per_channel(retro):
    """Two bounds, and the tighter of the two is the cached ROM.

    ``MAX_RETIRED_PER_CHANNEL`` is the ceiling on the records themselves,
    but a record is only worth keeping while the ROM it names is still on
    disk: the per-channel game cap prunes the oldest cached ROMs, and the
    Resume buttons that pointed at them are dropped with them (see
    ``Retro._forget_pruned_roms``). So a channel that works through games
    keeps one record per *cached* game that is not the one playing, which is
    one fewer than the record cap.
    """
    await retro.install_cores("gambatte")
    channel = retro.channel(9660)
    ctx = retro.context(channel)
    limit = retro.cogmod.MAX_RETIRED_PER_CHANNEL
    cached = retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL

    for index in range(limit + 3):
        retro.serve(f"game{index}.gbc", ROM_BYTES)
        await play(retro, ctx, f"https://example.com/game{index}.gbc")

    stored = (await retro.cog.config.all_channels())[channel.id]["retired"]
    assert len(stored) == cached - 1 <= limit, stored
    # The oldest are the ones that went.
    assert "game0" not in " ".join(r["game_name"] for r in stored.values())
    # And every record that is left can really bring its game back: its ROM
    # is still cached, so clicking it does not have to apologise.
    for record in stored.values():
        assert retro.cog._rom_path(record["rom_filename"]).is_file(), record
    assert len(retro.cog.retired) == len(stored), "the views followed the records"


async def test_an_unreadable_retired_record_is_ignored_not_fatal(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9661)
    async with retro.cog.config.channel_from_id(channel.id).retired() as retired:
        retired["123"] = {"nonsense": True}
        retired["456"] = None
    cog2, bot2 = retro.make_cog()
    bot2.channels[channel.id] = channel
    await cog2._restore_sessions()  # must not raise
