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


async def test_deleting_a_game_with_nothing_saved_does_not_even_ask(battery):
    view, _, channel = await playing(battery, 9254, "ucity")
    FakeConfirm.reset(answer=True)
    ctx = battery.context(channel, author=FakeUser(uid=view.starter_id))

    await command(battery, "retrosaves_delete")(battery.cog, ctx, game="ucity")
    assert FakeConfirm.asked == 0
    assert "nothing saved" in ctx.said()


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
