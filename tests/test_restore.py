"""One restore chain, two callers: starting a game and waking one up.

A game comes back the same way whichever door it comes through: the save
state first, then the cartridge's battery save, then the beginning. There used
to be a copy of that chain in ``RetroView._boot`` and another in
``Retro._wake_locked``, and they were kept in step by these tests rather than
by construction -- so one of them could (and did) grow a behaviour the other
did not have.

There is one implementation now, :func:`retro.restore.restore_into` (which
RetroView re-exports, so `retro.RetroView.restore_into` is the same object), and one
place that decides what to say and what to throw away afterwards,
``Retro._settle_boot``. These tests hold the two callers against each other
for every outcome the chain has.
"""

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import FakeEmulator  # noqa: E402

SRAM_BYTES = 8192

#: A state FakeEmulator will load, in the shape its save_state() writes.
GOOD_STATE = b"STATE:1234".ljust(64, b"\0")
#: What every save state on disk turns into when the core is updated.
BAD_STATE = b"STATE-FROM-AN-OLDER-CORE"


def marker_bytes(size):
    return (bytes(range(256)) * (size // 256 + 1))[:size]


SRAM = marker_bytes(SRAM_BYTES)

#: (state on disk, battery save on disk) for each way a restore can go.
CASES = {
    "a good save state": (GOOD_STATE, SRAM),
    "a save state the core rejects": (BAD_STATE, SRAM),
    "a battery save and nothing else": (None, SRAM),
    "nothing at all": (None, None),
}


def put(cog, channel_id, slug, state, sram):
    """Lay one channel's saved progress out on disk, exactly."""
    for path, data in (
        (cog._state_path(channel_id, slug), state),
        (cog._sram_path(channel_id, slug), sram),
    ):
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)


def announced(view):
    """What the player was told about the restore, however it reached them.

    A notice is cleared as it is rendered, so a fresh start's has already been
    spent on the message the first clip arrived on, while a wake's is still
    pending and rides on the clip of the press that woke it.

    The header (``**game**``) is stripped off, because it is on every line
    the session ever writes and says nothing about a restore. What is left is
    None when the restore had nothing to announce -- which is the assertion
    most of this module makes.
    """
    if view.notice is not None:
        return view.notice
    message = getattr(view, "message", None)
    content = (getattr(message, "kwargs", None) or {}).get("content")
    if content is None:
        return None
    head, separator, tail = content.partition(" \N{MIDDLE DOT} ")
    if not separator:
        # Nothing but the header, i.e. nothing was said.
        return None
    return tail


def outcome_of(cog, view, channel_id):
    """What a restore actually did, in a form the two paths can be compared in."""
    return {
        "outcome": view.boot_outcome,
        # A state that could not be used is deleted, so it is not retried on
        # every press for the rest of the game's life.
        "state_kept": cog._state_path(channel_id, view.slug).is_file(),
        # What the core is really holding, rather than what is on disk.
        "sram_in_core": view.emulator.save_sram(),
        "live": view.live,
    }


async def start_with(retro, channel_id, name, state, sram):
    """A game started from scratch with this progress already on disk."""
    cog = retro.cog
    put(cog, channel_id, cog._slug(name), state, sram)
    view, ctx, _ = await retro.posted_game(channel_id, name)
    return outcome_of(cog, view, channel_id), view, ctx


async def wake_with(retro, channel_id, name, state, sram):
    """A hibernated session woken up with this progress on disk."""
    cog = retro.cog
    view, ctx, _ = await retro.posted_game(channel_id, name)
    await cog.hibernate(view, None)
    # After hibernating, not before: hibernating writes the live core's own
    # state and battery save out, which would otherwise land on top of these.
    put(cog, channel_id, view.slug, state, sram)
    await cog.run_press(view, None)
    return outcome_of(cog, view, channel_id), view, ctx


@pytest.fixture
def battery(retro):
    """Every session in this module runs a cartridge with an 8 KiB battery."""
    FakeEmulator.sram_bytes = SRAM_BYTES
    return retro


@pytest.mark.parametrize("case", sorted(CASES), ids=lambda name: name.replace(" ", "-"))
async def test_waking_and_starting_restore_identically(battery, case):
    retro = battery
    state, sram = CASES[case]
    await retro.install_cores("gambatte")

    started, _, _ = await start_with(retro, 9100, "parityfresh", state, sram)
    woken, _, _ = await wake_with(retro, 9101, "paritywake", state, sram)

    assert started == woken, case


@pytest.mark.parametrize("case", sorted(CASES), ids=lambda name: name.replace(" ", "-"))
async def test_the_outcome_is_the_one_the_files_promised(battery, case):
    # And it is the outcome SaveInfo.restore predicts from the files alone,
    # which is what `[p]retrosaves info` tells people to expect.
    retro = battery
    state, sram = CASES[case]
    await retro.install_cores("gambatte")
    expected = {
        "a good save state": "state",
        "a save state the core rejects": "sram",
        "a battery save and nothing else": "sram",
        "nothing at all": "fresh",
    }[case]

    started, view, _ = await start_with(retro, 9110, "promised", state, sram)
    assert started["outcome"] == expected
    assert started["live"]


async def test_an_unusable_state_is_dropped_by_both_paths(battery):
    retro = battery
    await retro.install_cores("gambatte")

    started, fresh_view, _ = await start_with(retro, 9120, "dropfresh", BAD_STATE, SRAM)
    woken, wake_view, _ = await wake_with(retro, 9121, "dropwake", BAD_STATE, SRAM)

    assert started["state_kept"] is False
    assert woken["state_kept"] is False
    # And the sentence the player sees is the same sentence, from the same
    # place, whichever way the game came back.
    assert announced(fresh_view) == announced(wake_view)
    assert "could not be used" in (announced(fresh_view) or "")
    assert "in-game save survived" in (announced(fresh_view) or "")


async def test_a_state_that_loads_never_runs_the_boot_frames(battery):
    # The point of restoring a state is landing back where the game was, so
    # the three seconds of boot only run on a cold start.
    retro = battery
    await retro.install_cores("gambatte")
    _, view, _ = await start_with(retro, 9130, "noboot", GOOD_STATE, None)
    # FakeEmulator.load_state sets the frame counter from the blob and runs
    # exactly one frame; anything more would be the cold-boot path.
    assert view.emulator.loaded_from == 1234


async def test_only_a_fresh_start_narrates_a_restore_that_worked(battery):
    # The one deliberate difference between the two callers, and the reason
    # _settle_boot takes the notice as an argument: starting a game says where
    # it picked up from, and waking one says nothing, because a sleeping game
    # waking on a button press is not news.
    retro = battery
    await retro.install_cores("gambatte")

    _, fresh_view, _ = await start_with(retro, 9140, "narratefresh", GOOD_STATE, SRAM)
    _, wake_view, _ = await wake_with(retro, 9141, "narratewake", GOOD_STATE, SRAM)

    assert announced(fresh_view) == "Picked up from where this channel left off."
    assert announced(wake_view) is None


async def test_a_battery_save_that_will_not_fit_is_never_announced(battery):
    # outcome "fresh" with a battery save on disk: load_sram refused it (a
    # save for another revision of the cartridge), so claiming the in-game
    # save is in place would be a lie. Neither path says anything.
    retro = battery
    await retro.install_cores("gambatte")
    wrong_size = marker_bytes(SRAM_BYTES // 2)

    started, fresh_view, _ = await start_with(retro, 9150, "misfitfresh", None, wrong_size)
    woken, wake_view, _ = await wake_with(retro, 9151, "misfitwake", None, wrong_size)

    assert started["outcome"] == woken["outcome"] == "fresh"
    assert announced(fresh_view) is None
    assert announced(wake_view) is None


async def test_the_chain_itself_is_one_function(battery):
    # Not a style check: the duplication this replaced is how the two paths
    # drifted in the first place.
    import inspect

    source = inspect.getsource(retro_module := battery.cogmod)
    assert "restore_into" in source
    assert "def _resume()" not in source, "the wake path has its own copy again"
    assert retro_module.restore_into is battery.viewmod.restore_into
    # And the view's boot goes through it too.
    assert "restore_into(" in inspect.getsource(battery.viewmod.RetroView._boot)


# -- The backups are promises, not reads --------------------------------------
#
# Every start, every resume and every wake builds a Progress, and on nearly
# all of them the newest save state loads -- so the two backup blobs were
# megabytes read off disk for nothing, before every single boot (a Game Boy
# state is 178 KiB, a Genesis one 1012 KiB). A backup may now be a Path or a
# callable, read at the moment the chain actually reaches for it, which is
# inside the worker thread restore_into already runs in. The eager `bytes`
# form still works exactly as it did, which is what every test above uses.


def lazy_emulator(retro, tmp_path, name="lazy"):
    """A fake core with a real ROM file behind it, ready to be restored into."""
    rom = tmp_path / f"{name}.gb"
    rom.write_bytes(b"\x00" * 64)
    return FakeEmulator(tmp_path / "core.so", rom)


def counter(value):
    """A backup that says how many times it has been asked for."""
    reads = []

    def read():
        reads.append(value)
        return value

    return read, reads


def test_no_backup_is_read_when_the_newest_save_state_loads(battery, tmp_path):
    """The common case, and the whole reason for the laziness."""
    viewmod = battery.restoremod
    state_read, state_reads = counter(GOOD_STATE)
    sram_read, sram_reads = counter(SRAM)
    progress = viewmod.Progress(
        state=GOOD_STATE,
        state_backup=state_read,
        sram=SRAM,
        sram_backup=sram_read,
    )

    emulator = lazy_emulator(battery, tmp_path)
    assert viewmod.restore_into(emulator, progress, "lazy") == "state"
    assert state_reads == [] and sram_reads == []


def test_the_state_backup_is_read_exactly_once_and_only_when_it_is_needed(
    battery, tmp_path
):
    viewmod = battery.restoremod
    read, reads = counter(GOOD_STATE)
    progress = viewmod.Progress(state=BAD_STATE, state_backup=read)

    emulator = lazy_emulator(battery, tmp_path)
    assert viewmod.restore_into(emulator, progress, "lazy") == "backup-state"
    assert reads == [GOOD_STATE], "read when it was reached, and not twice"
    assert emulator.loaded_from == 1234


def test_a_backup_may_be_a_path_and_is_read_off_disk_when_reached(battery, tmp_path):
    viewmod = battery.restoremod
    backup = tmp_path / "state.bak"
    backup.write_bytes(GOOD_STATE)
    progress = viewmod.Progress(state=BAD_STATE, state_backup=backup)

    emulator = lazy_emulator(battery, tmp_path)
    assert viewmod.restore_into(emulator, progress, "lazy") == "backup-state"
    assert emulator.loaded_from == 1234


def test_the_in_game_save_backup_is_only_read_once_the_newest_is_refused(
    battery, tmp_path
):
    viewmod = battery.restoremod
    read, reads = counter(SRAM)

    fitting = viewmod.Progress(sram=SRAM, sram_backup=read)
    emulator = lazy_emulator(battery, tmp_path, "fits")
    assert viewmod.restore_into(emulator, fitting, "lazy") == "sram"
    assert reads == []

    # A newest generation the cartridge will not take -- an import, or a core
    # update -- is exactly when the one before it is worth reading.
    misfit = viewmod.Progress(sram=SRAM[: SRAM_BYTES // 2], sram_backup=read)
    emulator = lazy_emulator(battery, tmp_path, "misfit")
    assert viewmod.restore_into(emulator, misfit, "lazy") == "backup-sram"
    assert reads == [SRAM]


def test_a_backup_that_cannot_be_read_carries_on_down_the_chain(battery, tmp_path):
    """A file deleted between the stat and the read, or a reader that threw.

    A backup is the rung that is tried *because* the one above it failed, so
    it cannot be allowed to end the boot: the cartridge's battery save is
    still worth having, and the title screen after that.
    """
    viewmod = battery.restoremod

    def explode():
        raise OSError("the save went away")

    for backup in (tmp_path / "never-written.bak", explode):
        progress = viewmod.Progress(state=BAD_STATE, state_backup=backup, sram=SRAM)
        emulator = lazy_emulator(battery, tmp_path, "gone")
        assert viewmod.restore_into(emulator, progress, "lazy") == "sram"


def test_has_state_asks_whether_there_is_one_rather_than_reading_it(battery, tmp_path):
    """The cheap question, kept cheap: it is asked before every boot.

    Retro._settle_boot reads it to decide whether to delete the state files
    that did not work, so it has to mean the same thing it always did --
    "was there a state that should have worked?" -- without spending the
    megabyte this laziness exists to save.
    """
    viewmod = battery.restoremod
    real = tmp_path / "real.bak"
    real.write_bytes(GOOD_STATE)
    empty = tmp_path / "empty.bak"
    empty.write_bytes(b"")
    missing = tmp_path / "missing.bak"

    def boom():
        raise AssertionError("has_state read the backup")

    assert not viewmod.Progress().has_state
    assert viewmod.Progress(state=GOOD_STATE).has_state
    # The eager form, unchanged.
    assert viewmod.Progress(state_backup=GOOD_STATE).has_state
    assert not viewmod.Progress(state_backup=b"").has_state
    # A path: one stat. An empty file is not a save state, exactly as b"" is
    # not one.
    assert viewmod.Progress(state_backup=real).has_state
    assert not viewmod.Progress(state_backup=empty).has_state
    assert not viewmod.Progress(state_backup=missing).has_state
    # A callable cannot be asked without being called, so it is taken at its
    # word -- and it really is not called.
    assert viewmod.Progress(state_backup=boom).has_state

    # ...and the same question about the in-game save, which the cog needs
    # because a Path is truthy whether or not there is a file behind it.
    assert not viewmod.Progress().has_sram
    assert viewmod.Progress(sram=SRAM).has_sram
    assert viewmod.Progress(sram_backup=real).has_sram
    assert not viewmod.Progress(sram_backup=missing).has_sram


def test_the_eager_form_still_builds_the_same_progress(battery):
    """Nothing that hands over four blobs has to change; see Progress."""
    viewmod = battery.restoremod
    progress = viewmod.Progress(GOOD_STATE, BAD_STATE, SRAM, SRAM)
    assert progress.state == GOOD_STATE
    assert progress.read_state_backup() == BAD_STATE
    assert progress.read_sram_backup() == SRAM
    assert progress.has_state and progress.has_sram
