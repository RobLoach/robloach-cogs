"""One restore chain, two callers: starting a game and waking one up.

A game comes back the same way whichever door it comes through: the save
state first, then the cartridge's battery save, then the beginning. There used
to be a copy of that chain in ``RetroView._boot`` and another in
``Retro._wake_locked``, and they were kept in step by these tests rather than
by construction -- so one of them could (and did) grow a behaviour the other
did not have.

There is one implementation now, :func:`retro.RetroView.restore_into`, and one
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
    """
    if view.notice is not None:
        return view.notice
    message = getattr(view, "message", None)
    return (getattr(message, "kwargs", None) or {}).get("content")


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
