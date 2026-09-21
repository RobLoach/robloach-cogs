"""Saves, and Undo, against a real core and a real cartridge.

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

The Undo button is here for the same reason. FakeEmulator's save state is a
frame counter, so everything about *when* a state is taken and *which* one
goes back in is provable without a core (test_cog_session.py) -- but whether
a real libretro core, handed a real compressed state, really puts the
machine and the picture back where they were is not. That needs the real
thing, and it is what the feature is for.
"""

import hashlib
import io
import zlib

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


# -- Undo, against the real core ----------------------------------------------


#: RETRO_MEMORY_SYSTEM_RAM: the console's own work RAM, which for a Game Boy
#: Color is 32 KiB of it.
RETRO_MEMORY_SYSTEM_RAM = 2
GBC_WRAM_BYTES = 32768

#: How many bytes of a save state are allowed to differ between two machines
#: that are in the same state.
#:
#: Not zero, which was a surprise worth writing down: a libretro save state
#: is not quite a pure function of the emulated machine. Gambatte's state
#: carries a four-byte ``time`` field that a cartridge with no real-time
#: clock never writes, so two states serialized from an identical machine
#: come out differing in those four bytes as soon as anything else in the
#: process has allocated in between -- an ``await``, a hop into a worker
#: thread. Four bytes out of 182,530, measured.
#:
#: So the comparisons below are made on the console's memory and on the
#: picture, which *are* exactly the emulated machine, and the state itself is
#: only held to "the same, bar those". It costs nothing in practice: the
#: field is unused by a cartridge without an RTC, and a state is only ever
#: loaded back into the same core, which ignores it too.
STATE_SLACK = 16


def differing_bytes(left, right):
    assert len(left) == len(right), (len(left), len(right))
    return sum(1 for a, b in zip(left, right, strict=True) if a != b)


def machine(emulator):
    """The emulated machine itself: the console's work RAM and the cartridge's."""
    wram = emulator._session.core.get_memory(RETRO_MEMORY_SYSTEM_RAM)
    assert wram is not None and len(wram) == GBC_WRAM_BYTES, wram
    return bytes(wram), emulator.save_sram()


def last_picture(clip):
    """A hash of the final frame of a clip, i.e. what stays on the message.

    A clip plays through once and holds its last frame, so this really is
    the picture the channel is left looking at. The clip itself is read back
    off the edit that carried it (``interaction.clip()`` for a button,
    ``retro.shown_clip(view)`` for a command that edits the message): a
    session keeps no copy of its footage, so the message is where it lives
    and is also what a player sees.
    """
    from PIL import Image

    assert isinstance(clip, bytes) and clip, "no clip reached the message"
    animation = Image.open(io.BytesIO(clip))
    animation.seek(animation.n_frames - 1)
    return hashlib.sha1(animation.convert("RGB").tobytes()).hexdigest()


async def test_undo_puts_a_real_core_and_its_picture_back(real, city):
    """The statement the whole feature rests on, on the real thing.

    Undo is "restore the state the press started from, then record a clip
    forward from it", so what it has to be held to is that the machine ends
    up on the *pre-press* timeline -- not that it is frozen at the exact
    moment of the click. The reference is therefore built by hand out of the
    same two steps, from the same state, in the same core: load, record one
    clip of no input, and nothing else. Gambatte is deterministic, so the
    console's memory, the cartridge's memory and the final picture all have
    to come out identical; see STATE_SLACK for the four bytes of the save
    state that do not.
    """
    pytest.importorskip("PIL", reason="comparing pictures needs Pillow")
    view, _, _ = city
    cog = real.cog
    emulator = view.emulator

    before = emulator.save_state()
    interaction = real.interaction(view, message=view.message)
    await view._press(interaction, "start")
    pressed_state = emulator.save_state()
    pressed_machine = machine(emulator)
    pressed_picture = last_picture(interaction.clip())
    assert differing_bytes(pressed_state, before) > 100, "the press moved the game on"

    # The undo point is the state the press began from, compressed.
    assert len(view.history) == 1
    stored = zlib.decompress(view.history[-1])
    assert differing_bytes(stored, before) <= STATE_SLACK
    assert len(view.history[-1]) < len(before) // 4, "and it is worth compressing"
    assert len(view.history[-1]) == view.history_bytes

    undoing = real.interaction(view, message=view.message)
    await real.control(view, "undo").callback(undoing)
    undone_state = emulator.save_state()
    undone_machine = machine(emulator)
    undone_clip = undoing.clip()

    # The reference: the same state, the same core, one clip of no input.
    emulator.load_state(before)
    reference_clip = view._record(emulator, None)
    reference_state = emulator.save_state()

    assert undone_machine == machine(emulator), "the machine is where it was"
    assert undone_machine != pressed_machine
    assert differing_bytes(undone_state, reference_state) <= STATE_SLACK
    assert last_picture(undone_clip) == last_picture(reference_clip), (
        "and so is the picture the channel is left looking at"
    )
    assert last_picture(undone_clip) != pressed_picture, (
        "the press really did change the screen, so the comparison means "
        "something"
    )
    # Gambatte encodes the same frames to the same bytes, so the whole clip
    # matches too; it is the picture above that says what is being claimed.
    assert undone_clip == reference_clip

    # One edit, as a press is. Undoing is not allowed to cost two.
    assert undoing.kinds() == ["response.defer", "edit_original_response"]
    assert undoing.log[-1][1]["n_attachments"] == 1
    # And the save on disk is the undone moment, written through rather than
    # left for the autosave to catch up with.
    on_disk = cog._state_path(view.channel_id, view.slug).read_bytes()
    assert differing_bytes(on_disk, undone_state) <= STATE_SLACK
    assert differing_bytes(on_disk, pressed_state) > 100


async def test_undo_reaches_back_across_a_real_core_being_freed(real, city):
    """A state outlives the core instance that made it, which is why this works.

    The session sleeps (the shared object is unloaded), then Undo wakes it:
    a brand new core, the ROM loaded again, the *disk* state restored -- and
    then the undo point from before the sleep goes in on top.
    """
    view, _, _ = city
    emulator = view.emulator

    before = emulator.save_state()
    await view._press(real.interaction(view, message=view.message), "start")
    await real.cog.hibernate(view, None)
    assert not view.live and len(view.history) == 1

    await real.control(view, "undo").callback(
        real.interaction(view, message=view.message)
    )

    assert view.live, "the undo woke it up"
    fresh = view.emulator
    assert fresh is not emulator, "on a freshly loaded core"
    undone_machine = machine(fresh)
    assert not view.history

    # The same reference as the test above, built on the new core: the undo
    # point crossed the unload intact, so loading it and running one clip
    # lands in the same place the undo did.
    fresh.load_state(before)
    view._record(fresh, None)
    assert machine(fresh) == undone_machine


async def test_a_real_eight_deep_history_is_about_a_hundred_kilobytes(real, city):
    """The memory figure the depth was chosen against, measured for real."""
    view, _, _ = city
    viewmod = real.viewmod
    raw = len(view.emulator.save_state())

    for index in range(viewmod.UNDO_DEPTH + 2):
        await view._press(
            real.interaction(view, message=view.message),
            "start" if index % 2 else "a",
        )

    assert len(view.history) == viewmod.UNDO_DEPTH, "the depth cap bit"
    assert view.history_bytes == sum(len(blob) for blob in view.history)
    assert view.history_bytes <= viewmod.MAX_UNDO_BYTES
    # uCity's states are 182 KiB each raw and about 16 KiB compressed, so a
    # full history is a fraction of what the raw states would be -- and, now
    # that the replay buffer is gone, the only thing a session holds beyond
    # the one clip on its message.
    assert view.history_bytes < raw, (view.history_bytes, raw)
    assert view.history_bytes < 300 * 1024, view.history_bytes
    assert max(len(blob) for blob in view.history) < raw // 4


# -- retroreset, against the real core ----------------------------------------


async def test_retroreset_reboots_a_real_core_through_the_cog(real, city):
    """`[p]retroreset` on the real thing, end to end.

    FakeEmulator's reset is a frame counter going back to zero, which proves
    the plumbing and nothing about the machine; this is the statement the
    command actually makes. The reference is the same core booting the same
    ROM from scratch in ``restore_into``'s own words -- a cold boot -- which
    is what "as if you had flipped the power switch" has to mean.

    The work RAM is compared as a distance rather than for equality: a Game
    Boy reset is the reset line and not the power rail, so a handful of bytes
    the boot code never writes keep whatever the previous game left there.
    See RESET_RAM_SLACK in test_emulator.py, where the same claim is made
    against the core on its own.
    """
    pytest.importorskip("PIL", reason="comparing pictures needs Pillow")
    view, ctx, channel = city
    cog = real.cog
    emulator = view.emulator
    state_path = cog._state_path(channel.id, view.slug)

    # A cold boot of this ROM, for reference, and the machine it leaves.
    booted_wram, _ = machine(emulator)

    # Play, and save, so there is something for the reset to threaten.
    saved = in_game_save()
    assert emulator.load_sram(saved) is True
    for field in ("start", "a", "down"):
        # A press edits the *interaction*, so the clip it posted is read back
        # from there; `[p]retroreset` edits the message itself, which is what
        # `shown_clip` reads below.
        playing = real.interaction(view, message=view.message)
        await view._press(playing, field)
    await cog._write_state(view)
    assert state_path.is_file()
    on_disk_before = state_path.read_bytes()
    played_wram, _ = machine(emulator)
    played_picture = last_picture(playing.clip())
    assert differing_bytes(played_wram, booted_wram) > 0, "the play moved nothing"

    await command(real, "retroreset")(cog, ctx)

    # 1. The machine is back at boot, and a long way from where the play got.
    reset_wram, reset_sram = machine(emulator)
    near = differing_bytes(reset_wram, booted_wram)
    far = differing_bytes(reset_wram, played_wram)
    assert near * 5 < far, (near, far)
    assert far > 0, "the reset did nothing at all"

    # 2. The picture the channel is left looking at is the game booting, not
    #    the game that was thrown away.
    assert last_picture(real.shown_clip(view)) != played_picture

    # 3. The player's own in-game save is untouched: a real console's reset
    #    never wiped one, and neither does this.
    assert reset_sram == saved
    assert cog._sram_path(channel.id, view.slug).read_bytes() == saved

    # 4. And the save state on disk still holds the moment *before* the
    #    reset, so an accidental reset is recoverable. This is the decision:
    #    a reset does not write, and the file is only replaced when the game
    #    saves of its own accord.
    assert state_path.read_bytes() == on_disk_before
    # The undo point the reset pushed *is* that moment -- the autosave above
    # wrote the same machine -- so the two agree bar the four bytes of a
    # Gambatte state that are not a function of the console (STATE_SLACK).
    assert differing_bytes(
        zlib.decompress(view.history[-1]), on_disk_before
    ) <= STATE_SLACK, "the undo point is not the moment before the reset"

    # 5. One click of Undo puts the real machine back on the pre-reset
    #    timeline. Not frozen at the exact moment: an undo records one clip
    #    forward from the state it restores (see run_undo), so the reference
    #    is built by hand out of those same two steps, exactly as
    #    test_undo_puts_a_real_core_and_its_picture_back does it.
    await real.control(view, "undo").callback(
        real.interaction(view, message=view.message)
    )
    undone = machine(emulator)
    assert view.live
    assert undone[1] == saved, "the undo lost the in-game save"
    # Far closer to the play than to the reboot, which is the point.
    assert differing_bytes(undone[0], played_wram) * 5 < differing_bytes(
        reset_wram, played_wram
    )

    emulator.load_state(on_disk_before)
    view._record(emulator, None)
    assert machine(emulator) == undone, "the undo is not the pre-reset timeline"


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
