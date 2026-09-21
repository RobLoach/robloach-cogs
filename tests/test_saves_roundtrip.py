"""`[p]retrosaves` export and import against a real core and a real cartridge.

Everywhere else the cog's save handling is driven against FakeEmulator, which
answers a fixed number of bytes because a test of a *command* has no business
loading a shared object. That leaves one thing unproved, and it is the thing
the feature exists for: that a battery save exported from here is really the
cartridge's own memory, and that importing it back really puts the player's
in-game save where the game will find it.

So this module runs the whole round trip on the real thing: the real Gambatte
core, the real uCity cartridge (which reports 131,072 bytes of battery-backed
save RAM), and the real Retro cog on top of them. Marked ``emulator`` and
skipped cleanly without the assets, like the rest of the slow half.
"""

import pytest

pytest.importorskip("discord", reason="these tests drive the cog")

from .fakes import FakeAttachment, FakeConfirm, FakeUser  # noqa: E402

pytestmark = [pytest.mark.emulator, pytest.mark.slow]

CHANNEL = 9300
#: uCity's cartridge: 128 KiB of battery-backed save RAM.
UCITY_SRAM_BYTES = 131072


def command(retro, name):
    return getattr(retro.cogmod.Retro, name).callback


@pytest.fixture
def real(retro, monkeypatch, gambatte, ucity):
    """The cog with its real emulator put back, and a real core installed.

    The ``retro`` fixture swaps RetroEmulator out for the fake; everything in
    here wants the genuine article, so it is swapped back. One libretro core
    may be loaded per process, and the cog enforces that itself
    (MAX_LIVE_EMULATORS is 1), so nothing extra is needed beyond not running
    these in parallel with each other.
    """
    from retro.emulator import RetroEmulator

    retro.patch("RetroEmulator", RetroEmulator, monkeypatch)
    # A real core at a real path, recorded the way the downloader records it.
    monkeypatch.setattr(retro.cog, "_core_path", _fixed_path(gambatte))
    retro.rom_bytes = open(ucity, "rb").read()
    return retro


def _fixed_path(path):
    from pathlib import Path

    async def _core_path(core):
        return Path(path)

    return _core_path


@pytest.fixture
async def city(real):
    """uCity running in a channel, with its save memory already written to."""
    channel = real.channel(CHANNEL)
    ctx = real.context(channel)
    view = await real.start_game(
        ctx, "ucity", data=real.rom_bytes, filename="ucity.gbc"
    )
    assert view is not None and view.live, "the real core did not come up"
    assert view.emulator.sram_size == UCITY_SRAM_BYTES, view.emulator.sram_size
    return view, ctx, channel


def in_game_save(size=UCITY_SRAM_BYTES, seed=0):
    """A recognisable 'the player saved their city' pattern, cartridge-sized."""
    return bytes((index * 7 + seed) % 251 for index in range(size))


async def test_the_cartridge_really_has_a_battery(city):
    view, _, _ = city
    assert view.emulator.sram_size == UCITY_SRAM_BYTES
    assert len(view.emulator.save_sram()) == UCITY_SRAM_BYTES


async def test_export_wipe_import_leaves_the_in_game_save_intact(real, city):
    view, _, channel = city
    cog = real.cog
    owner = FakeUser(uid=1)

    # 1. The player saves from inside the game. Writing the cartridge's own
    #    memory is exactly what a save from the game's menu does to it.
    saved = in_game_save()
    assert view.emulator.load_sram(saved) is True
    await cog._write_state(view)
    on_disk = cog._sram_path(channel.id, view.slug).read_bytes()
    assert on_disk == saved
    assert len(on_disk) == UCITY_SRAM_BYTES

    # 2. Export it.
    exporting = real.context(channel, author=owner)
    await command(real, "retrosaves_export")(cog, exporting, game="ucity")
    exported = exporting.uploaded()
    assert set(exported) == {"ucity.srm"}
    assert exported["ucity.srm"] == saved
    assert "128.0 KiB" in exporting.said()

    # 3. Wipe everything, with the game live -- the case that has to hibernate
    #    the session first or the running core writes it all straight back.
    FakeConfirm.reset(answer=True)
    wiping = real.context(channel, author=owner)
    await command(real, "retrosaves_delete")(cog, wiping, game="ucity")
    assert not cog._sram_path(channel.id, view.slug).is_file()
    assert not cog._state_path(channel.id, view.slug).is_file()
    assert not view.live, "the live core was saved and freed before the wipe"

    # The game really is back to an untouched cartridge.
    await cog.run_press(view, None)
    assert view.boot_outcome == "fresh"
    assert view.emulator.save_sram() != saved

    # 4. Bring the export back in.
    FakeConfirm.reset(answer=True)
    importing = real.context(
        channel,
        author=owner,
        attachments=[FakeAttachment(exported["ucity.srm"], "ucity.srm")],
    )
    await command(real, "retrosaves_import")(cog, importing, game="ucity")
    assert cog._sram_path(channel.id, view.slug).read_bytes() == saved
    assert not view.live, "and the session was put to sleep for the import too"

    # 5. Play on: the core comes back holding the player's save, byte for byte.
    await cog.run_press(view, None)
    assert view.boot_outcome == "sram"
    assert view.emulator.save_sram() == saved


async def test_a_state_exported_from_here_imports_back_into_the_same_core(real, city):
    view, _, channel = city
    cog = real.cog
    owner = FakeUser(uid=1)
    await cog.run_press(view, "down")
    await cog._write_state(view)

    exporting = real.context(channel, author=owner)
    await command(real, "retrosaves_export")(cog, exporting, game="both ucity")
    exported = exporting.uploaded()
    assert set(exported) == {"ucity.srm", "ucity.state"}
    # Gambatte's save state for a Game Boy Color cartridge; a real size, not a
    # sentinel, and far larger than the 128 KiB of battery memory.
    assert len(exported["ucity.state"]) > UCITY_SRAM_BYTES

    FakeConfirm.reset(answer=True)
    wiping = real.context(channel, author=owner)
    await command(real, "retrosaves_delete")(cog, wiping, game="ucity")

    FakeConfirm.reset(answer=True)
    importing = real.context(
        channel,
        author=owner,
        attachments=[
            FakeAttachment(exported["ucity.srm"], "ucity.srm"),
            FakeAttachment(exported["ucity.state"], "ucity.state"),
        ],
    )
    await command(real, "retrosaves_import")(cog, importing, game="ucity")
    assert "Imported" in importing.said()

    await cog.run_press(view, None)
    assert view.boot_outcome == "state", "the real core took its own state back"


async def test_a_save_state_from_somewhere_else_is_refused_by_the_real_core(real, city):
    # The case `[p]retrosaves import` validates for: a state that is not this
    # core's is rejected by the core, and that has to arrive as a sentence.
    view, _, channel = city
    cog = real.cog
    await cog._write_state(view)
    good = cog._state_path(channel.id, view.slug).read_bytes()
    FakeConfirm.reset(answer=True)
    ctx = real.context(
        channel,
        author=FakeUser(uid=1),
        # Truncated, which is exactly what a core update does to every state
        # on disk as far as load_state can tell.
        attachments=[FakeAttachment(good[: len(good) // 2], "ucity.state")],
    )

    await command(real, "retrosaves_import")(cog, ctx, game="ucity")

    said = ctx.said()
    assert "expects" in said, "the core's own complaint, not a traceback"
    assert "exact build of the exact core" in said
    assert "Nothing was changed" in said
    # The state on disk is still a whole one this core wrote -- the session's
    # own, rewritten when it was put to sleep for the check -- and never the
    # half a state that was refused.
    kept = cog._state_path(channel.id, view.slug).read_bytes()
    assert len(kept) == len(good)
    await cog.run_press(view, None)
    assert view.boot_outcome == "state", "and it still loads"


async def test_a_battery_save_for_another_cartridge_is_refused_by_size(real, city):
    view, _, channel = city
    cog = real.cog
    FakeConfirm.reset(answer=True)
    ctx = real.context(
        channel,
        author=FakeUser(uid=1),
        # 8 KiB: a plain Game Boy cartridge's battery, not uCity's 128 KiB.
        attachments=[FakeAttachment(in_game_save(8192), "someothergame.srm")],
    )

    await command(real, "retrosaves_import")(cog, ctx, game="ucity")

    said = ctx.said()
    assert "8,192 bytes" in said and "131,072 bytes" in said
    assert "Nothing was changed" in said


async def test_the_listing_reports_the_cartridges_real_numbers(real, city):
    view, _, channel = city
    cog = real.cog
    view.emulator.load_sram(in_game_save())
    await cog._write_state(view)

    ctx = real.context(channel)
    await command(real, "retrosaves_info")(cog, ctx, game="ucity")
    said = ctx.said()
    assert "128.0 KiB" in said, "uCity's battery, reported by the core itself"
    assert "gambatte" in said
    assert "from its save state" in said
