"""Battery saves: the cartridge's own memory, kept beside the save state.

A save state is a snapshot of one build of one core and is rejected outright
when that core is updated. The cartridge's battery save is not, which is the
whole reason the cog keeps both -- an in-game save has to survive a core
update even when the exact moment cannot.
"""

import time

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import NES_BYTES, FakeEmulator  # noqa: E402


def marker_bytes(size):
    """A recognisable pattern exactly ``size`` bytes long."""
    return (bytes(range(256)) * (size // 256 + 1))[:size]


@pytest.fixture
async def battery(retro):
    """A live session whose cartridge has an 8 KiB battery."""
    await retro.install_cores("gambatte")
    FakeEmulator.sram_bytes = 8192
    view, ctx, channel = await retro.posted_game(9800, "batterygame")
    return view, ctx, channel


async def test_a_cartridge_with_no_battery_leaves_no_srm_behind(retro):
    await retro.install_cores("gambatte", "fceumm")
    FakeEmulator.sram_bytes = 0
    view, _, channel = await retro.posted_game(
        9801, "nobattery", data=NES_BYTES, filename="nobattery.nes"
    )
    assert view.emulator.sram_size == 0

    await retro.cog.hibernate(view, None)
    assert not retro.cog._sram_path(channel.id, view.slug).is_file()
    assert retro.cog._state_path(channel.id, view.slug).is_file(), "the state is still written"


async def test_a_battery_save_is_written_next_to_the_save_state(retro, battery):
    view, _, channel = battery
    size = view.emulator.sram_size
    assert size > 0
    marker = marker_bytes(size)
    assert view.emulator.load_sram(marker)

    await retro.cog._write_state(view)
    srm = retro.cog._sram_path(channel.id, view.slug)
    assert srm.is_file()
    assert srm.read_bytes() == marker
    assert srm.parent == retro.cog._state_path(channel.id, view.slug).parent


async def test_hibernating_captures_the_battery_before_freeing_the_core(retro, battery):
    view, _, channel = battery
    marker = marker_bytes(view.emulator.sram_size)
    view.emulator.load_sram(marker)
    srm = retro.cog._sram_path(channel.id, view.slug)

    await retro.cog.hibernate(view, None)
    assert srm.is_file()
    assert srm.read_bytes() == marker
    assert not view.live


async def test_a_good_save_state_resumes_silently(retro, battery):
    view, _, channel = battery
    await retro.cog.hibernate(view, None)
    await retro.cog.run_press(view, None)
    assert view.live
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    assert view.notice is None, "nothing went wrong, so nothing is said"


async def test_a_state_a_core_update_invalidated_falls_back_to_the_battery(retro, battery):
    view, _, channel = battery
    marker = marker_bytes(view.emulator.sram_size)
    view.emulator.load_sram(marker)
    srm = retro.cog._sram_path(channel.id, view.slug)
    await retro.cog.hibernate(view, None)

    # What an updated core does to every save state on disk.
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"STATE-FROM-AN-OLDER-CORE")
    srm.write_bytes(marker)

    await retro.cog.run_press(view, None)
    assert not retro.cog._state_path(channel.id, view.slug).is_file(), "the bad state is dropped"
    assert view.live
    assert view.emulator.save_sram() == marker

    notice = view.notice or ""
    assert "could not be used" in notice
    assert "in-game save survived" in notice
    assert "game's own menu" in notice


async def test_the_notice_rides_on_the_next_clip_exactly_once(retro, battery):
    view, _, channel = battery
    view.emulator.load_sram(marker_bytes(view.emulator.sram_size))
    await retro.cog.hibernate(view, None)
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"STATE-FROM-AN-OLDER-CORE")
    await retro.cog.run_press(view, None)

    shown = view._content()
    assert "in-game save survived" in (shown or "")
    assert view.notice is None
    # And the message goes back to carrying nothing but the header, which is
    # the stable "what game is this" prefix rather than a line about a
    # restore. See RetroView.header.
    assert view._content() == retro.line(view)
    assert "in-game save survived" not in view._content()


async def test_a_bad_state_with_no_battery_starts_over_without_claiming_otherwise(
    retro, battery
):
    view, _, channel = battery
    await retro.cog.hibernate(view, None)
    retro.cog._state_path(channel.id, view.slug).write_bytes(b"STATE-FROM-AN-OLDER-CORE")
    retro.cog._sram_path(channel.id, view.slug).unlink(missing_ok=True)

    await retro.cog.run_press(view, None)
    notice = view.notice or ""
    assert view.live
    assert "started over from the beginning" in notice
    assert "in-game save survived" not in notice


async def test_cog_unload_captures_the_battery_before_freeing_the_core(retro, battery):
    view, _, channel = battery
    marker = marker_bytes(view.emulator.sram_size)
    assert view.emulator.load_sram(marker)
    srm = retro.cog._sram_path(channel.id, view.slug)
    srm.unlink(missing_ok=True)

    await retro.cog.cog_unload()
    assert srm.is_file()
    assert srm.read_bytes() == marker


async def test_pruning_a_game_takes_its_battery_save_with_it(retro):
    channel_id = 9810
    cog = retro.cog
    for index in range(retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL + 3):
        (cog._roms_dir() / f"{channel_id}-pruned{index}.gb").write_bytes(b"r")
        cog._state_path(channel_id, f"pruned{index}").write_bytes(b"s")
        cog._sram_path(channel_id, f"pruned{index}").write_bytes(b"b")
        time.sleep(0.01)

    cog._prune_cached_games(channel_id, "pruned0")

    leftover = list(cog._data_dir("states").glob(f"{channel_id}-pruned*.srm"))
    assert len(leftover) < retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL + 3
    orphans = [p for p in leftover if not (cog._roms_dir() / f"{p.stem}.gb").is_file()]
    assert not orphans, "no battery save outlives its ROM"
