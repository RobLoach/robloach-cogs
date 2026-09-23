"""`[p]retrosaves`: seeing, exporting, importing and destroying progress.

A channel's progress is three files per game -- the cached ROM, the save
state and the cartridge's battery save -- and this group is a window onto
exactly those. The tests that matter most are the ones about a *live* session:
a running emulator holds the authoritative copy of both saves and writes them
back on its next automatic save, so anything that changes the files under one
has to put it to sleep first or be silently undone by the next button press.
"""

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import (  # noqa: E402
    FakeAttachment,
    FakeConfirm,
    FakeEmulator,
    FakeUser,
    history_is_consistent,
)

SRAM_BYTES = 8192


def marker_bytes(size, seed=0):
    """A recognisable pattern exactly ``size`` bytes long."""
    return bytes((index + seed) % 251 for index in range(size))


def command(retro, name):
    return getattr(retro.cogmod.Retro, name).callback


@pytest.fixture
def battery(retro):
    """A channel playing a cartridge with an 8 KiB battery, live."""
    FakeEmulator.sram_bytes = SRAM_BYTES
    return retro


async def playing(retro, cid=9200, name="ucity", **kwargs):
    await retro.install_cores("gambatte")
    return await retro.posted_game(cid, name, **kwargs)


# -- The shape of the group ---------------------------------------------------


@pytest.mark.redbot
def test_the_group_and_its_subcommands_are_where_they_say_they_are(retro):
    walked = {
        command.qualified_name
        for top in retro.cogmod.Retro.__cog_commands__
        for command in [top, *getattr(top, "walk_commands", lambda: ())()]
    }
    for name in ("list", "info", "export", "import", "dropstate", "delete"):
        assert f"retrosaves {name}" in walked, name


@pytest.mark.redbot
def test_nothing_else_in_the_cog_already_answers_to_these_names(retro):
    # A second command (or alias) with the same name would make one of them
    # unreachable, and Red raises at load time rather than picking one.
    seen = {}
    for top in retro.cogmod.Retro.__cog_commands__:
        if top.parent is not None:
            continue
        for name in (top.name, *top.aliases):
            assert name not in seen, f"{name} is claimed by {seen.get(name)} too"
            seen[name] = top.qualified_name
    assert "retrosaves" in seen
    assert seen["saves"] == "retrosaves", "the short alias points at the group"


@pytest.mark.redbot
def test_only_exporting_asks_discord_for_the_attach_files_permission(retro):
    export = retro.cogmod.Retro.retrosaves_export
    assert {name for name, on in export.requires.bot_perms if on} == {"attach_files"}
    listing = retro.cogmod.Retro.retrosaves_list
    assert not {name for name, on in listing.requires.bot_perms if on}


@pytest.mark.redbot
def test_reds_confirmview_is_the_shape_the_cog_drives_it_as(retro):
    """The four things `SavesMixin._confirm` uses, read off the class.

    ``inspect.getsource(ConfirmView)`` used to be searched for the string
    ``"result"``, which is an assertion about how Red happens to have written
    a private module rather than about what the cog needs.
    """
    import inspect

    from redbot.core.utils.views import ConfirmView

    parameters = inspect.signature(ConfirmView.__init__).parameters
    assert "timeout" in parameters and "disable_buttons" in parameters
    assert inspect.iscoroutinefunction(ConfirmView.wait)
    assert isinstance(getattr(ConfirmView("nobody"), "result", ...), (bool, type(None)))


# -- Listing ------------------------------------------------------------------


async def test_a_channel_with_nothing_saved_says_how_to_start(retro):
    channel = retro.channel(9201)
    ctx = retro.context(channel)
    await command(retro, "retrosaves_list")(retro.cog, ctx)
    assert "no saved games" in ctx.said().lower()
    assert "!retro" in ctx.said()


async def test_the_listing_names_the_game_its_slug_and_both_halves(battery):
    view, _, channel = await playing(battery, 9202, "ucity")
    core = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await core._write_state(view)

    ctx = battery.context(channel)
    await command(battery, "retrosaves_list")(core, ctx)
    said = ctx.said()

    assert "ucity" in said
    assert "`ucity`" in said, "the slug the other commands take"
    assert "Game Boy" in said
    assert "save state" in said and "in-game save" in said
    assert "8.0 KiB" in said, "the in-game save's size"
    assert "<t:" in said, "a timestamp Discord renders in the reader's timezone"
    assert "playing now" in said


async def test_the_listing_marks_a_game_the_channel_has_moved_on_from(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9203)
    ctx = retro.context(channel)
    first = await retro.start_game(ctx, "firstgame")
    await retro.cog._write_state(first)
    await retro.start_game(ctx, "secondgame")

    await command(retro, "retrosaves_list")(retro.cog, ctx)
    said = ctx.said()
    assert "firstgame" in said and "secondgame" in said
    assert "resumable" in said, "its ROM is still cached, so it can come back"
    assert "2 game(s)" in said


async def test_a_save_whose_rom_was_pruned_is_still_listed(retro):
    cog = retro.cog
    cog._state_path(9204, "orphan").write_bytes(b"STATE:1")
    channel = retro.channel(9204)
    ctx = retro.context(channel)

    await command(retro, "retrosaves_list")(cog, ctx)
    said = ctx.said()
    assert "orphan" in said
    assert "ROM pruned" in said, "the save outlives the ROM and is still managed"


async def test_the_listing_gathers_its_files_off_the_event_loop(retro):
    """The stats and globs behind a listing must not stall every press.

    ``_saved_games`` runs on `[p]retrosaves`, and everything filesystem-shaped
    in it -- five glob passes plus a stat per file -- used to run directly on
    the event loop, where a big states directory froze every session in every
    channel for the length of a directory listing.
    """
    import threading

    cog = retro.cog
    cog._state_path(9206, "threaded").write_bytes(b"STATE:1")
    main = threading.get_ident()
    seen = {}
    real = cog._stored_slugs

    def spy(channel_id):
        seen["thread"] = threading.get_ident()
        return real(channel_id)

    cog._stored_slugs = spy
    entries = await cog._saved_games(9206)

    assert [entry.slug for entry in entries] == ["threaded"]
    assert seen["thread"] != main, "the directory walk ran on the event loop"


async def test_a_long_listing_is_paginated(retro):
    cog = retro.cog
    for index in range(40):
        cog._state_path(9205, f"game{index:02d}").write_bytes(b"STATE:1" * 100)
    channel = retro.channel(9205)
    ctx = retro.context(channel)

    await command(retro, "retrosaves_list")(cog, ctx)
    from .fakes import FakeMenu

    assert len(FakeMenu.last_pages) > 1
    assert "Page 1 of" in FakeMenu.last_pages[0]


# -- Info ---------------------------------------------------------------------


async def test_info_says_what_starting_the_game_now_would_do(battery):
    view, _, channel = await playing(battery, 9210, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)

    ctx = battery.context(channel)
    await command(battery, "retrosaves_info")(cog, ctx, game="ucity")
    said = ctx.said()

    assert "Save state" in said and "Battery save" in said
    assert "gambatte" in said and "Game Boy" in said
    assert "Cached ROM" in said and "9210-ucity.gbc" in said
    assert "from its save state" in said


async def test_info_says_a_game_with_only_a_battery_save_starts_at_the_title(retro):
    cog = retro.cog
    cog._sram_path(9211, "onlysram").write_bytes(b"\x01" * 1024)
    ctx = retro.context(retro.channel(9211))

    await command(retro, "retrosaves_info")(cog, ctx, game="onlysram")
    said = ctx.said()
    assert "from the title screen, with the in-game save in place" in said
    assert "**Save state**: none" in said


async def test_info_says_when_a_rom_is_gone_and_what_to_do(retro):
    cog = retro.cog
    cog._sram_path(9212, "pruned").write_bytes(b"\x01" * 1024)
    ctx = retro.context(retro.channel(9212))

    await command(retro, "retrosaves_info")(cog, ctx, game="pruned")
    assert "**Cached ROM**: gone" in ctx.said()


async def test_an_unknown_game_is_told_what_the_channel_does_have(retro):
    cog = retro.cog
    cog._state_path(9213, "realgame").write_bytes(b"STATE:1")
    ctx = retro.context(retro.channel(9213))

    await command(retro, "retrosaves_info")(cog, ctx, game="nothinglikeit")
    said = ctx.said()
    assert "nothing saved for" in said
    assert "`realgame`" in said


async def test_an_ambiguous_name_is_reported_rather_than_guessed(retro):
    cog = retro.cog
    for slug in ("marioland", "mariokart"):
        cog._state_path(9214, slug).write_bytes(b"STATE:1")
    ctx = retro.context(retro.channel(9214))

    await command(retro, "retrosaves_info")(cog, ctx, game="mario")
    said = ctx.said()
    assert "matches 2" in said
    assert "marioland" in said and "mariokart" in said


async def test_one_channels_saves_are_invisible_to_another(retro):
    cog = retro.cog
    cog._state_path(9215, "secret").write_bytes(b"STATE:1")
    ctx = retro.context(retro.channel(9216))

    await command(retro, "retrosaves_list")(cog, ctx)
    assert "secret" not in ctx.said()


# -- Names never reach the filesystem -----------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32",
        "ucity/../../../root",
        "\x00etc",
    ],
)
async def test_no_name_typed_into_discord_can_name_a_path(retro, attack):
    cog = retro.cog
    cog._state_path(9217, "ucity").write_bytes(b"STATE:1")
    ctx = retro.context(retro.channel(9217), author=FakeUser(uid=1))

    await command(retro, "retrosaves_delete")(cog, ctx, game=attack)
    # It resolves to nothing rather than to a path, and the real save is
    # untouched either way.
    assert "nothing saved for" in ctx.said() or "matches" in ctx.said()
    assert cog._state_path(9217, "ucity").is_file()


# -- Exporting ----------------------------------------------------------------


async def test_export_sends_the_battery_save_byte_for_byte(battery):
    view, _, channel = await playing(battery, 9220, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    await cog._write_state(view)

    ctx = battery.context(channel)
    await command(battery, "retrosaves_export")(cog, ctx, game="ucity")

    uploaded = ctx.uploaded()
    assert set(uploaded) == {"ucity.srm"}, "the state is not sent unless asked for"
    assert uploaded["ucity.srm"] == marker
    assert "in-game save" in ctx.said()
    # Streamed from the file on disk, not copied through memory first: the
    # attachment's stream is the open file itself (a BytesIO has no name).
    assert getattr(ctx.uploads[0].fp, "name", None)


async def test_export_both_sends_the_state_as_well_with_a_warning(battery):
    view, _, channel = await playing(battery, 9221, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)

    ctx = battery.context(channel)
    await command(battery, "retrosaves_export")(cog, ctx, game="both ucity")

    assert set(ctx.uploaded()) == {"ucity.srm", "ucity.state"}
    assert "only loads on the same build" in ctx.said()


async def test_export_state_sends_only_the_state(battery):
    view, _, channel = await playing(battery, 9222, "ucity")
    await battery.cog._write_state(view)
    ctx = battery.context(channel)

    await command(battery, "retrosaves_export")(battery.cog, ctx, game="state ucity")
    assert set(ctx.uploaded()) == {"ucity.state"}


async def test_a_game_actually_called_state_is_still_exportable(retro):
    cog = retro.cog
    cog._sram_path(9223, "state").write_bytes(b"\x02" * 512)
    ctx = retro.context(retro.channel(9223))

    await command(retro, "retrosaves_export")(cog, ctx, game="state")
    assert set(ctx.uploaded()) == {"state.srm"}


async def test_a_save_too_large_for_the_server_is_explained_not_attached(retro):
    cog = retro.cog
    cog._state_path(9224, "huge").write_bytes(b"\x00" * (3 * 1024 * 1024))
    cog._sram_path(9224, "huge").write_bytes(b"\x01" * 1024)
    # A server that will not take more than a megabyte.
    ctx = retro.context(retro.channel(9224), filesize_limit=1024 * 1024)

    await command(retro, "retrosaves_export")(cog, ctx, game="both huge")
    said = ctx.said()
    assert set(ctx.uploaded()) == {"huge.srm"}, "the battery save still goes"
    assert "over the" in said and "left out" in said


async def test_exporting_a_game_with_nothing_saved_says_so(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9225)
    ctx = retro.context(channel)
    await retro.start_game(ctx, "unsaved")

    asking = retro.context(channel)
    await command(retro, "retrosaves_export")(retro.cog, asking, game="unsaved")
    assert "nothing saved yet" in asking.said()
    assert not asking.uploaded()


async def test_export_falls_back_to_the_state_when_there_is_no_in_game_save(retro):
    """A bare `export <game>` on a state-only game used to dead-end.

    A cartridge with no battery -- most homebrew, everything that saved by
    password -- never writes a `.srm` at all, so the default half simply does
    not exist for it. The old reply said there was no in-game save and never
    mentioned the save state sitting right next to it, which left the one
    file the channel actually had unreachable without knowing the `export
    state <slug>` spelling.
    """
    cog = retro.cog
    cog._state_path(9227, "stateonly").write_bytes(b"STATE:1" * 64)
    ctx = retro.context(retro.channel(9227))

    await command(retro, "retrosaves_export")(cog, ctx, game="stateonly")

    assert set(ctx.uploaded()) == {"stateonly.state"}
    said = ctx.said()
    assert "has no in-game save" in said, "and it says why it sent the other half"
    assert "only loads on the same build" in said, "with the usual caveat"


async def test_naming_the_in_game_save_is_answered_about_that_half_alone(retro):
    # Only the *default* falls through. Somebody who typed a half meant that
    # half and is told the truth about it -- and pointed at the other one
    # rather than left at the dead end.
    cog = retro.cog
    cog._state_path(9228, "stateonly").write_bytes(b"STATE:1" * 64)
    ctx = retro.context(retro.channel(9228))

    await command(retro, "retrosaves_export")(cog, ctx, game="save stateonly")

    assert not ctx.uploaded(), "an explicit half is never swapped for the other"
    said = ctx.said()
    assert "no in-game save" in said
    assert "retrosaves export state stateonly" in said


async def test_anyone_in_the_channel_may_export(battery):
    view, _, channel = await playing(battery, 9226, "ucity")
    await battery.cog._write_state(view)
    stranger = FakeUser(uid=4242, name="Passerby")
    ctx = battery.context(channel, author=stranger)

    await command(battery, "retrosaves_export")(battery.cog, ctx, game="ucity")
    assert ctx.uploaded(), "exporting changes nothing, so it is open to the channel"


# -- Who may destroy a save ---------------------------------------------------


@pytest.mark.parametrize("subcommand", ["retrosaves_dropstate", "retrosaves_delete"])
async def test_a_passer_by_may_not_destroy_someone_elses_save(battery, subcommand):
    view, _, channel = await playing(battery, 9230, "ucity")
    cog = battery.cog
    await cog._write_state(view)
    ctx = battery.context(channel, author=FakeUser(uid=4242, name="Passerby"))

    await command(battery, subcommand)(cog, ctx, game="ucity")

    assert "Only the person who started" in ctx.said()
    assert cog._state_path(9230, "ucity").is_file(), "nothing was touched"
    assert view.live, "and the game was not disturbed either"


@pytest.mark.parametrize(
    "who",
    [
        pytest.param(lambda view: FakeUser(uid=view.starter_id), id="the-starter"),
        pytest.param(lambda view: FakeUser(uid=4242, manage_messages=True), id="a-moderator"),
        pytest.param(lambda view: FakeUser(uid=1), id="the-bot-owner"),
    ],
)
async def test_the_same_three_who_may_stop_a_game_may_reset_its_save(battery, who):
    view, _, channel = await playing(battery, 9231, "ucity")
    cog = battery.cog
    await cog._write_state(view)
    ctx = battery.context(channel, author=who(view))

    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")
    assert not cog._state_path(9231, "ucity").is_file()
    assert "Only the person who started" not in ctx.said()


# -- Reset: drop the save state, keep the in-game save ------------------------


async def test_reset_drops_the_state_and_keeps_the_battery_save(battery):
    view, _, channel = await playing(battery, 9240, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    await cog._write_state(view)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")

    assert not cog._state_path(9240, "ucity").is_file()
    assert cog._sram_path(9240, "ucity").read_bytes() == marker
    said = ctx.said()
    assert "in-game save is untouched" in said
    assert "title screen" in said


async def test_resetting_a_live_game_is_not_undone_by_the_next_press(battery):
    # The bug this whole feature could most easily have: a running core still
    # holds the save state in memory and writes it back on its next automatic
    # save, so deleting the file under it would achieve nothing at all.
    view, _, channel = await playing(battery, 9241, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    for _ in range(4):
        await cog.run_press(view, "a")
    assert view.live and cog._state_path(9241, "ucity").is_file()
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")

    assert not view.live, "the live session was saved and put to sleep first"
    assert "put to sleep" in ctx.said()
    assert not cog._state_path(9241, "ucity").is_file()

    # Now play on. The session wakes from what is left on disk, which is the
    # battery save and nothing else.
    for _ in range(4):
        await cog.run_press(view, "a")
    assert view.boot_outcome == "sram", "it cold-booted with the in-game save"
    assert view.emulator.loaded_from is None, "no save state was restored"
    assert view.emulator.save_sram() == marker


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("retrosaves_dropstate", {"game": "ucity"}),
        ("retrosaves_delete", {"game": "ucity"}),
        ("retrosaves_rollback", {"game": "ucity"}),
    ],
)
async def test_changing_the_saves_takes_the_undo_history_with_them(
    battery, name, kwargs
):
    """Or one click of Undo would put back the save that was just destroyed.

    An undo restores a state from *memory* and writes it straight to disk
    (see Retro.run_undo), so the in-memory history has to go whenever a
    command has been asked to destroy or replace what is on disk. Otherwise
    `[p]retrosaves delete` -- "start this game completely fresh" -- would be
    one button press away from coming back.
    """
    view, _, channel = await playing(battery, 9250, "ucity")
    cog = battery.cog
    # Seven presses: two automatic saves, so there is a previous generation
    # for `rollback` to have something to do.
    for _ in range(7):
        await cog.run_press(view, "a")
    assert view.history and cog._state_path(9250, "ucity").is_file()
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, name)(cog, ctx, **kwargs)

    assert not view.history, name
    assert history_is_consistent(view)
    # The button stays clickable -- a click with nothing to undo is answered
    # privately rather than by a dead control. See _UndoButton.
    assert not battery.control(view, "undo").disabled


async def test_a_sleeping_session_loses_its_undo_history_too(battery):
    # _pause_for_saves returns early for a session that is already asleep,
    # which is exactly when the history is still full of states from before
    # the command. It has to be dropped before that early return.
    view, _, channel = await playing(battery, 9251, "ucity")
    cog = battery.cog
    for _ in range(4):
        await cog.run_press(view, "a")
    await cog.hibernate(view, None)
    assert not view.live and view.history

    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))
    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")

    assert not view.history


async def test_a_session_that_will_not_hibernate_cleanly_still_frees_its_core(battery):
    # The same belt and braces `[p]retrosleep` has: a stale view, or a message
    # the bot can no longer edit, must not leave a core running -- everything
    # after this point assumes the files on disk are the only copy.
    view, _, channel = await playing(battery, 9243, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    await cog._write_state(view)

    async def _explode(*args, **kwargs):
        raise RuntimeError("the message is gone")

    view.refresh = _explode
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")

    assert not view.live, "the core was freed anyway"
    assert not cog._state_path(9243, "ucity").is_file()
    assert cog._sram_path(9243, "ucity").read_bytes() == marker, "and saved first"


async def test_resetting_a_game_with_no_state_says_there_is_nothing_to_do(battery):
    view, _, channel = await playing(battery, 9242, "ucity")
    cog = battery.cog
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_dropstate")(cog, ctx, game="ucity")
    assert "no save state" in ctx.said()
    assert view.live, "and a game with nothing to reset is not disturbed"


# -- Delete: wipe everything --------------------------------------------------


async def test_delete_asks_first_and_a_no_changes_nothing(battery):
    view, _, channel = await playing(battery, 9250, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)
    FakeConfirm.reset(answer=False)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(cog, ctx, game="ucity")

    assert FakeConfirm.asked == 1, "it really did ask"
    assert cog._state_path(9250, "ucity").is_file()
    assert cog._sram_path(9250, "ucity").is_file()
    assert "Left" in ctx.said()
    assert view.live, "a refused delete does not even put the game to sleep"


async def test_the_question_says_what_would_be_lost_and_offers_reset_instead(battery):
    view, _, channel = await playing(battery, 9251, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)
    FakeConfirm.reset(answer=False)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(cog, ctx, game="ucity")
    said = ctx.said()
    # One word per concept, everywhere a player can see it: "save state"
    # for the exact moment and "in-game save" for what the player saved from
    # inside the game. Never "battery save", never "SRAM".
    assert "save state" in said and "in-game save" in said
    assert "battery save" not in said and "SRAM" not in said
    assert "can be undone" in said
    assert "retrosaves dropstate ucity" in said, "the gentler option is offered"


async def test_delete_wipes_both_halves_and_keeps_the_rom(battery):
    view, _, channel = await playing(battery, 9252, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(cog, ctx, game="ucity")

    assert not cog._state_path(9252, "ucity").is_file()
    assert not cog._sram_path(9252, "ucity").is_file()
    assert cog._cached_rom(9252, "ucity") is not None, "the ROM is not save data"
    assert "very beginning" in ctx.said()


async def test_deleting_a_live_games_save_really_starts_it_over(battery):
    # The same hazard as reset, and worse: the live core holds the battery
    # memory too, so without hibernating first the player's in-game save would
    # be written straight back over the wipe.
    view, _, channel = await playing(battery, 9253, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    for _ in range(4):
        await cog.run_press(view, "a")
    assert cog._sram_path(9253, "ucity").read_bytes() == marker
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(cog, ctx, game="ucity")
    assert not view.live

    for _ in range(4):
        await cog.run_press(view, "a")
    assert view.boot_outcome == "fresh"
    assert view.emulator.save_sram() != marker, "the in-game save is really gone"
    assert view.emulator.save_sram() == b"\xff" * SRAM_BYTES, "an erased cartridge"


async def test_a_delete_that_only_half_worked_says_which_half(battery, monkeypatch):
    """A partial delete is the one shape nothing else here produces.

    The four files go one at a time, so a data folder that turns read-only
    part way through leaves a game with no save state and an intact in-game
    save -- a state neither the confirmation nor the old reply described.
    Giving up on the first failure and saying "the save files could not be
    deleted" sent somebody off to start a game they had been told was
    untouched, and got them the middle of it instead of the beginning.
    """
    from pathlib import Path

    view, _, channel = await playing(battery, 9255, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(marker)
    await cog._write_state(view)
    real_unlink = Path.unlink

    def the_in_game_save_will_not_go(self, missing_ok=False):
        if self.name.endswith(".srm"):
            raise PermissionError(13, "read-only file system")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", the_in_game_save_will_not_go)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(cog, ctx, game="ucity")

    assert not cog._state_path(9255, "ucity").is_file(), "what could go, went"
    assert cog._sram_path(9255, "ucity").read_bytes() == marker
    # The reply itself, not the whole transcript: the confirmation that came
    # before it promises the very beginning, and the point of this test is
    # that the *answer* stops promising it.
    said = str(ctx.sent[-1])
    assert "Only part" in said
    assert "Gone: its save state" in said
    assert "Still there: its in-game save" in said
    # And the sentence that matters most: where the game really comes back
    # from now, read off the files as they are rather than as they were meant
    # to be.
    assert "title screen, with the in-game save in place" in said
    assert "very beginning" not in said, "it would not, and saying so was the bug"


async def test_a_delete_that_removed_nothing_claims_nothing(retro, monkeypatch):
    from pathlib import Path

    cog = retro.cog
    cog._state_path(9257, "stuck").write_bytes(b"STATE:1")
    cog._sram_path(9257, "stuck").write_bytes(b"\x01" * 512)

    def nothing_goes(self, missing_ok=False):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(Path, "unlink", nothing_goes)
    FakeConfirm.reset(answer=True)
    ctx = retro.context(retro.channel(9257), author=FakeUser(uid=1))

    await command(retro, "retrosaves_delete")(cog, ctx, game="stuck")

    said = ctx.said()
    assert "None of" in said and "could be deleted" in said
    assert "Still there: its save state and its in-game save" in said
    assert "Wiped" not in said and "Gone:" not in said
    assert cog._state_path(9257, "stuck").is_file()
    assert cog._sram_path(9257, "stuck").is_file()


async def test_deleting_a_game_with_nothing_saved_does_not_even_ask(battery):
    view, _, channel = await playing(battery, 9254, "ucity")
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(battery.cog, ctx, game="ucity")
    assert FakeConfirm.asked == 0
    assert "nothing saved" in ctx.said()


# -- Rollback: swap with the previous generation --------------------------------


async def test_rollback_is_a_swap_and_its_own_undo(retro):
    """Rolling back twice puts everything exactly as it was.

    The command promises a swap, not a delete: what is rolled back from lands
    in the backup slot, so a rollback that turned out to be a mistake is
    undone by running it again. And a completed swap leaves no ``.rollback``
    parking file behind for the sweeper to find.
    """
    cog = retro.cog
    state = cog._state_path(9256, "swapper")
    sram = cog._sram_path(9256, "swapper")
    state.write_bytes(b"STATE:2")
    cog._backup_path(state).write_bytes(b"STATE:1")
    sram.write_bytes(b"\x02" * 512)
    cog._backup_path(sram).write_bytes(b"\x01" * 512)
    owner = FakeUser(uid=1)

    ctx = retro.context(retro.channel(9256), author=owner)
    await command(retro, "retrosaves_rollback")(cog, ctx, game="swapper")

    assert state.read_bytes() == b"STATE:1"
    assert cog._backup_path(state).read_bytes() == b"STATE:2"
    assert sram.read_bytes() == b"\x01" * 512
    assert cog._backup_path(sram).read_bytes() == b"\x02" * 512
    said = ctx.said()
    assert "rolled back" in said and "again puts it back" in said

    again = retro.context(retro.channel(9256), author=owner)
    await command(retro, "retrosaves_rollback")(cog, again, game="swapper")

    assert state.read_bytes() == b"STATE:2"
    assert sram.read_bytes() == b"\x02" * 512
    assert not list(cog._data_dir("states").glob("*.rollback"))


# -- Importing ----------------------------------------------------------------


async def test_import_with_no_attachment_says_what_to_attach(battery):
    view, _, channel = await playing(battery, 9260, "ucity")
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    said = ctx.said()
    assert ".srm" in said and ".state" in said
    assert "retrosaves export ucity" in said


async def test_a_file_that_is_not_a_save_is_refused_by_its_name(battery):
    view, _, channel = await playing(battery, 9261, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(b"\x00" * 64, "holiday.jpg")],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    assert "not a save this cog knows" in ctx.said()
    assert not battery.cog._sram_path(9261, "ucity").is_file()


async def test_an_enormous_battery_save_is_refused_before_it_is_downloaded(battery):
    view, _, channel = await playing(battery, 9262, "ucity")
    # `size` is what Discord reports; nothing is read at all if it is too big.
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(4 * 1024 * 1024, "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    assert "past the 1 MiB limit" in ctx.said()


async def test_an_empty_save_is_refused(battery):
    view, _, channel = await playing(battery, 9263, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(b"", "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    assert "is empty" in ctx.said()


async def test_a_battery_save_of_the_wrong_size_is_refused_with_both_numbers(battery):
    view, _, channel = await playing(battery, 9264, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(marker_bytes(2048), "somethingelse.srm")],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    said = ctx.said()
    assert "2,048 bytes" in said and "8,192 bytes" in said
    assert "Nothing was changed" in said
    # The file on disk is the session's own, written when it was put to sleep
    # for the check -- never the refused import.
    assert battery.cog._sram_path(9264, "ucity").read_bytes() != marker_bytes(2048)


async def test_a_save_state_the_core_will_not_load_is_a_sentence_not_a_traceback(battery):
    view, _, channel = await playing(battery, 9265, "ucity")
    cog = battery.cog
    await cog._write_state(view)
    good = cog._state_path(9265, "ucity").read_bytes()
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(b"FROM-ANOTHER-EMULATOR-ENTIRELY", "ucity.state")],
    )
    FakeConfirm.reset(answer=True)

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    said = ctx.said()
    assert "not one" in said and "can load" in said
    assert "exact build of the exact core" in said
    assert "Nothing was changed" in said
    assert cog._state_path(9265, "ucity").read_bytes() == good, "and nothing was"


async def test_a_battery_save_is_installed_and_takes_the_old_state_with_it(battery):
    # A save state is restored before SRAM is even looked at, so leaving the
    # old one in place would restore the game over the top of the import.
    view, _, channel = await playing(battery, 9266, "ucity")
    cog = battery.cog
    await cog._write_state(view)
    assert cog._state_path(9266, "ucity").is_file()
    incoming = marker_bytes(SRAM_BYTES, seed=7)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(incoming, "ucity.srm")],
    )
    FakeConfirm.reset(answer=True)

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert cog._sram_path(9266, "ucity").read_bytes() == incoming
    assert not cog._state_path(9266, "ucity").is_file()
    assert "old save state was removed with it" in ctx.said()


async def test_the_import_question_never_promises_a_rollback_it_cannot_do(battery):
    # Two fates, and the question keeps them straight: the overwritten
    # in-game save is rotated into the backup slot (rollback recovers it),
    # while the save state a battery-only import removes is deleted outright,
    # both generations of it -- so the only honest advice is to export first.
    view, _, channel = await playing(battery, 9273, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)
    FakeConfirm.reset(answer=False)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(marker_bytes(SRAM_BYTES, seed=3), "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert FakeConfirm.asked == 1
    said = ctx.said()
    assert "kept as the previous generation" in said, "the in-game save's fate"
    assert "outright" in said, "the save state's fate is a deletion"
    assert "cannot be rolled back" in said
    assert "export both ucity" in said, "the copy worth taking includes the state"


async def test_a_full_import_keeps_what_it_overwrites_one_generation_deep(battery):
    """The promise the confirmation makes, held to: rollback undoes an import.

    Both halves of a full import are written with the backup rotation, so
    what was there lands in the backup slots and one `[p]retrosaves rollback`
    puts the pre-import save back exactly.
    """
    view, _, channel = await playing(battery, 9274, "ucity")
    cog = battery.cog
    old_sram = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(old_sram)
    # Asleep first, so the files on disk are settled and the import's own
    # pause rewrites nothing.
    await cog.hibernate(view, None)
    state_path = cog._state_path(9274, "ucity")
    sram_path = cog._sram_path(9274, "ucity")
    old_state = state_path.read_bytes()

    new_state = b"STATE:777".ljust(64, b"\0")
    new_sram = marker_bytes(SRAM_BYTES, seed=9)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[
            FakeAttachment(new_sram, "ucity.srm"),
            FakeAttachment(new_state, "ucity.state"),
        ],
    )
    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert state_path.read_bytes() == new_state
    assert sram_path.read_bytes() == new_sram
    assert cog._backup_path(state_path).read_bytes() == old_state
    assert cog._backup_path(sram_path).read_bytes() == old_sram

    roll = battery.context(channel, author=FakeUser(uid=view.starter_id))
    await command(battery, "retrosaves_rollback")(cog, roll, game="ucity")
    assert state_path.read_bytes() == old_state
    assert sram_path.read_bytes() == old_sram


async def test_a_press_during_the_import_check_cannot_undo_the_import(battery):
    """The TOCTOU the import used to have, made deterministic.

    _pause_for_saves puts the game to sleep, but the core-boot validation
    between it and the write takes whole seconds, and the session's controls
    stay live the entire time: a press in that window wakes the game from the
    old files, and its next automatic save would silently write those old
    files back over the import. _mutate_saves closes it by re-hibernating
    under the view's own lock and writing before letting it go.
    """
    view, _, channel = await playing(battery, 9275, "ucity")
    cog = battery.cog
    old = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(old)
    for _ in range(4):
        await cog.run_press(view, "a")
    assert cog._sram_path(9275, "ucity").read_bytes() == old

    real_check = cog._check_import

    async def check_and_then_a_press_lands(entry, state, sram, prefix=""):
        problem = await real_check(entry, state, sram, prefix)
        # The moment the race lives in: the check is done, the write has not
        # happened, and a player presses a button.
        await cog.run_press(view, "a")
        assert view.live, "the press really did wake the game mid-command"
        return problem

    cog._check_import = check_and_then_a_press_lands
    incoming = marker_bytes(SRAM_BYTES, seed=99)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(incoming, "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert not view.live, "the rewoken session was put back to sleep for the write"
    assert cog._sram_path(9275, "ucity").read_bytes() == incoming

    # Playing on wakes from the imported save rather than writing the old
    # one back over it.
    for _ in range(4):
        await cog.run_press(view, "a")
    assert view.emulator.save_sram() == incoming
    assert cog._sram_path(9275, "ucity").read_bytes() == incoming


async def test_mutate_saves_touches_the_files_only_while_nothing_is_live(battery):
    """The helper's contract, asserted directly: mutate runs with the game asleep."""
    view, _, channel = await playing(battery, 9276, "ucity")
    cog = battery.cog
    assert view.live
    entries = await cog._saved_games(channel.id)
    entry = next(e for e in entries if e.slug == "ucity")
    seen = {}

    def mutate():
        seen["live"] = view.live
        return "changed"

    ctx = battery.context(channel)
    paused, result = await cog._mutate_saves(ctx, entry, "being tested", mutate)

    assert (paused, result) == (True, "changed")
    assert seen["live"] is False, "the files were changed under a core that could autosave"


async def test_importing_over_an_existing_save_asks_first(battery):
    view, _, channel = await playing(battery, 9267, "ucity")
    cog = battery.cog
    kept = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(kept)
    await cog._write_state(view)
    FakeConfirm.reset(answer=False)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(marker_bytes(SRAM_BYTES, seed=3), "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert FakeConfirm.asked == 1
    assert "overwrite" in ctx.said()
    assert cog._sram_path(9267, "ucity").read_bytes() == kept


async def test_importing_under_a_live_game_survives_the_next_press(battery):
    # The live-session hazard again, from the other side: the running core
    # holds the old battery save and would write it straight back over the
    # import on its next automatic save.
    view, _, channel = await playing(battery, 9268, "ucity")
    cog = battery.cog
    old = marker_bytes(SRAM_BYTES)
    view.emulator.load_sram(old)
    for _ in range(4):
        await cog.run_press(view, "a")
    assert cog._sram_path(9268, "ucity").read_bytes() == old

    incoming = marker_bytes(SRAM_BYTES, seed=99)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(incoming, "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert not view.live, "the live session was saved and put to sleep first"
    assert "put to sleep" in ctx.said()
    assert cog._sram_path(9268, "ucity").read_bytes() == incoming

    for _ in range(4):
        await cog.run_press(view, "a")
    assert view.emulator.save_sram() == incoming, "the imported save is what came back"
    assert cog._sram_path(9268, "ucity").read_bytes() == incoming


async def test_a_state_and_a_battery_save_can_come_in_together(battery):
    view, _, channel = await playing(battery, 9269, "ucity")
    cog = battery.cog
    await cog._write_state(view)
    state = cog._state_path(9269, "ucity").read_bytes()
    cog._state_path(9269, "ucity").unlink()
    sram = marker_bytes(SRAM_BYTES, seed=11)
    FakeConfirm.reset(answer=True)
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[
            FakeAttachment(sram, "ucity.srm"),
            FakeAttachment(state, "ucity.state"),
        ],
    )

    await command(battery, "retrosaves_import")(cog, ctx, game="ucity")

    assert cog._sram_path(9269, "ucity").read_bytes() == sram
    assert cog._state_path(9269, "ucity").read_bytes() == state
    assert "save state" in ctx.said()


async def test_two_of_the_same_kind_are_refused(battery):
    view, _, channel = await playing(battery, 9270, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[
            FakeAttachment(marker_bytes(SRAM_BYTES), "one.srm"),
            FakeAttachment(marker_bytes(SRAM_BYTES), "two.sav"),
        ],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    assert "one of each at most" in ctx.said()


async def test_a_state_cannot_be_imported_for_a_game_whose_rom_is_gone(retro):
    cog = retro.cog
    cog._sram_path(9271, "pruned").write_bytes(b"\x01" * 1024)
    ctx = retro.context(
        retro.channel(9271),
        author=FakeUser(uid=1),
        attachments=[FakeAttachment(b"STATE:1".ljust(64, b"\0"), "pruned.state")],
    )
    FakeConfirm.reset(answer=True)

    await command(retro, "retrosaves_import")(cog, ctx, game="pruned")
    said = ctx.said()
    assert "cleaned up" in said and "cannot be checked" in said
    assert not cog._state_path(9271, "pruned").is_file()
    # The reply tells the player to start the game, so it has to name the
    # command with the prefix this bot actually answers to. Red only
    # substitutes `[p]` in a docstring, so a sent string that says `[p]retro`
    # says exactly that; see test_packaging.py's source-level guard.
    assert f"`{ctx.clean_prefix}retro <name or url>`" in said, said
    assert "[p]" not in said, said


async def test_an_import_that_could_not_be_checked_admits_it(retro):
    """An unvalidated in-game save is accepted, and now says it was.

    With the ROM pruned there is no cartridge to boot, so the size check that
    every other import gets cannot run. The file is still stored -- it costs
    nothing and is very likely right -- but a `.sav` that turns out to be the
    wrong size is passed over at the next boot without a word, which from the
    channel's side looks exactly like an import that worked and a game that
    lost it. The reply is the only moment anybody can be told.
    """
    cog = retro.cog
    cog._sram_path(9277, "pruned").write_bytes(b"\x01" * 1024)
    incoming = marker_bytes(2048, seed=4)
    ctx = retro.context(
        retro.channel(9277),
        author=FakeUser(uid=1),
        attachments=[FakeAttachment(incoming, "pruned.srm")],
    )
    FakeConfirm.reset(answer=True)

    await command(retro, "retrosaves_import")(cog, ctx, game="pruned")

    assert cog._sram_path(9277, "pruned").read_bytes() == incoming, "it still goes in"
    said = ctx.said()
    assert "not** checked" in said
    assert "cleaned up" in said, "and why it could not be"
    assert "will ignore it" in said, "and what that means later"
    assert f"`{ctx.clean_prefix}retro <name or url>`" in said, "and the way out"
    assert "[p]" not in said, said


async def test_an_unchecked_import_still_says_what_became_of_the_save_state(retro):
    # The same path deletes *both* generations of the save state, exactly as
    # the confirmation warns, so the success message accounts for both of
    # them rather than for the live one alone.
    cog = retro.cog
    state = cog._state_path(9278, "pruned")
    state.write_bytes(b"STATE:2" * 8)
    cog._backup_path(state).write_bytes(b"STATE:1" * 8)
    cog._sram_path(9278, "pruned").write_bytes(b"\x01" * 1024)
    ctx = retro.context(
        retro.channel(9278),
        author=FakeUser(uid=1),
        attachments=[FakeAttachment(marker_bytes(1024, seed=2), "pruned.srm")],
    )
    FakeConfirm.reset(answer=True)

    await command(retro, "retrosaves_import")(cog, ctx, game="pruned")

    assert not state.is_file()
    assert not cog._backup_path(state).is_file()
    assert (
        "old save state and the previous generation of it were removed"
        in ctx.said()
    )


async def test_a_stranger_may_not_import_over_someone_elses_save(battery):
    view, _, channel = await playing(battery, 9272, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=4242),
        attachments=[FakeAttachment(marker_bytes(SRAM_BYTES), "ucity.srm")],
    )

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")
    assert "Only the person who started" in ctx.said()
    assert not battery.cog._sram_path(9272, "ucity").is_file()


# -- What the cooldown is charged for -----------------------------------------
#
# `import` and `export` are limited to four a minute each because one moves a
# file and the other boots a core. Red charges that before the callback runs,
# which is before every mistake somebody makes on the way to a working
# command -- so the cog hands the slot back on each path that gives up before
# an attachment has been downloaded or a file has been read off disk.
# Otherwise fixing your own typo is what locks you out for a minute.


def charged(ctx):
    """Give a context a command to refund, and watch for the refund.

    These tests call the command callbacks directly, so Red's real cooldown
    never runs and there is nothing to spend. What is asserted is the only
    half the cog controls: whether it asks for the slot back. See
    ``SavesMixin._refund_cooldown``.
    """
    import types

    refunds = []
    ctx.command = types.SimpleNamespace(reset_cooldown=refunds.append)
    return refunds


@pytest.mark.parametrize(
    "name, game, attachments",
    [
        pytest.param("retrosaves_import", "ucity", (), id="nothing-attached"),
        pytest.param(
            "retrosaves_import",
            "ucity",
            (("holiday.jpg", b"\x00" * 64),),
            id="not-a-save-file",
        ),
        pytest.param(
            "retrosaves_import",
            "ucity",
            (("ucity.srm", 4 * 1024 * 1024),),
            id="over-the-size-ceiling",
        ),
        pytest.param(
            "retrosaves_import",
            "ucity",
            (("one.srm", 8192), ("two.sav", 8192)),
            id="two-of-the-same-kind",
        ),
        pytest.param(
            "retrosaves_import",
            "nothinglikeit",
            (("ucity.srm", 8192),),
            id="import-for-a-game-that-is-not-there",
        ),
        pytest.param(
            "retrosaves_export", "nothinglikeit", (), id="export-of-an-unknown-game"
        ),
        pytest.param(
            "retrosaves_export", "ucity", (), id="export-with-nothing-saved-yet"
        ),
    ],
)
async def test_giving_up_before_reading_anything_hands_the_cooldown_back(
    battery, name, game, attachments
):
    view, _, channel = await playing(battery, 9290, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(data, filename) for filename, data in attachments],
    )
    refunds = charged(ctx)

    await command(battery, name)(battery.cog, ctx, game=game)

    assert refunds == [ctx], ctx.said()


async def test_a_refused_import_hands_the_cooldown_back_too(battery):
    # Nothing was downloaded, and the person who tried cannot fix it by
    # waiting either. The same call `[p]retro` makes on every path that does
    # not fetch a ROM.
    view, _, channel = await playing(battery, 9291, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=4242, name="Passerby"),
        attachments=[FakeAttachment(marker_bytes(SRAM_BYTES), "ucity.srm")],
    )
    refunds = charged(ctx)

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")

    assert "Only the person who started" in ctx.said()
    assert refunds == [ctx]


async def test_an_attachment_that_really_was_downloaded_keeps_the_cooldown(battery):
    # The other side of the line: these bytes came off Discord, so the
    # invocation cost what the limit exists to ration.
    view, _, channel = await playing(battery, 9292, "ucity")
    ctx = battery.context(
        channel,
        author=FakeUser(uid=view.starter_id),
        attachments=[FakeAttachment(b"", "ucity.srm")],
    )
    refunds = charged(ctx)

    await command(battery, "retrosaves_import")(battery.cog, ctx, game="ucity")

    assert "is empty" in ctx.said()
    assert refunds == []


async def test_an_export_that_uploaded_something_keeps_the_cooldown(battery):
    view, _, channel = await playing(battery, 9293, "ucity")
    cog = battery.cog
    view.emulator.load_sram(marker_bytes(SRAM_BYTES))
    await cog._write_state(view)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))
    refunds = charged(ctx)

    await command(battery, "retrosaves_export")(cog, ctx, game="ucity")

    assert ctx.uploaded()
    assert refunds == []


# -- The group's own front door -----------------------------------------------


async def test_the_bare_group_lists_and_a_name_shows_one_game(battery):
    view, _, channel = await playing(battery, 9280, "ucity")
    cog = battery.cog
    await cog._write_state(view)

    listing = battery.context(channel)
    await command(battery, "retrosaves")(cog, listing, game=None)
    assert "in this channel" in listing.said()

    detail = battery.context(channel)
    await command(battery, "retrosaves")(cog, detail, game="ucity")
    assert "Console:" in detail.said()


async def test_the_round_trip_is_a_round_trip(battery):
    # Export, wipe, import: what comes back is what went out, byte for byte.
    view, _, channel = await playing(battery, 9281, "ucity")
    cog = battery.cog
    marker = marker_bytes(SRAM_BYTES, seed=5)
    view.emulator.load_sram(marker)
    await cog._write_state(view)
    owner = FakeUser(uid=1)

    exported = battery.context(channel, author=owner)
    await command(battery, "retrosaves_export")(cog, exported, game="ucity")
    payload = exported.uploaded()["ucity.srm"]

    FakeConfirm.reset(answer=True)
    wiped = battery.context(channel, author=owner)
    await command(battery, "retrosaves_delete")(cog, wiped, game="ucity")
    assert not cog._sram_path(9281, "ucity").is_file()

    brought_back = battery.context(
        channel, author=owner, attachments=[FakeAttachment(payload, "ucity.srm")]
    )
    await command(battery, "retrosaves_import")(cog, brought_back, game="ucity")

    assert cog._sram_path(9281, "ucity").read_bytes() == marker
    await cog.run_press(view, None)
    assert view.emulator.save_sram() == marker
