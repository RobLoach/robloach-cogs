"""The emulator, against real libretro cores and real ROMs.

Everything here is marked ``emulator`` and skips cleanly when libretro.py,
Pillow, a core or a ROM is missing -- ``pytest`` on a bare checkout runs the
rest of the suite and says so. See tests/README.md for how to fetch the
assets.

A libretro core is a shared object with process-global state: two live
sessions segfault the interpreter, which is why the cog's MAX_LIVE_EMULATORS
is 1 and why the ``emu`` fixture below keeps exactly one alive at a time.
These tests must not be run in parallel with each other.
"""

import hashlib
import io
import logging
import struct
import subprocess
import sys

import pytest

from .loader import REPO_ROOT, load_standalone

pytestmark = [pytest.mark.emulator, pytest.mark.slow]

E = load_standalone("retro_emulator_standalone", "emulator.py")
S = load_standalone("retro_systems_for_emulator", "systems.py")


@pytest.fixture
def emu():
    """Start emulators, guaranteeing only one core is ever loaded."""
    made = []

    def build(core, rom, start=True, **kwargs):
        for previous in made:
            if previous.started:
                previous.stop()
        emulator = E.RetroEmulator(core, rom, **kwargs)
        if start:
            emulator.start()
        made.append(emulator)
        return emulator

    yield build

    for emulator in made:
        try:
            emulator.stop()
        except Exception:  # noqa: BLE001 - teardown must never fail a test
            pass


@pytest.fixture
def image():
    from PIL import Image

    return Image


def anmf(data):
    """(frame durations in ms, loop count) straight out of a WebP."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", data[:12]
    offset, durations, loop = 12, [], None
    while offset + 8 <= len(data):
        fourcc = data[offset : offset + 4]
        size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        body = data[offset + 8 : offset + 8 + size]
        if fourcc == b"ANMF":
            durations.append(int.from_bytes(body[12:15], "little"))
        elif fourcc == b"ANIM":
            loop = struct.unpack("<H", body[4:6])[0]
        offset += 8 + size + (size & 1)
    return durations, loop


def frame_hashes(payload, Image):
    clip = Image.open(io.BytesIO(payload))
    out = []
    for index in range(clip.n_frames):
        clip.seek(index)
        out.append(hashlib.sha1(clip.convert("RGB").tobytes()).hexdigest()[:10])
    return out


# -- 1. Every console boots and records a clip --------------------------------

CASES = [
    ("gb", "gambatte", "ucity.gbc", "right"),
    ("nes", "fceumm", "nestest.nes", "start"),
    ("snes", "snes9x", "snes_rotzoom.sfc", "a"),
]


@pytest.mark.parametrize("key, core, rom_name, button", CASES, ids=[c[0] for c in CASES])
def test_a_console_boots_and_records_a_playable_clip(
    assets, emu, image, key, core, rom_name, button
):
    assert S.system_by_key(key).core == core, "systems.py and this test disagree"
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))

    assert 45 <= emulator.fps <= 70, emulator.fps
    assert 0.5 < emulator.aspect_ratio < 3.0, emulator.aspect_ratio

    emulator.advance(emulator.frames_for_seconds(2))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    clip = emulator.record(frames, presses=[(button, 0, emulator.frames_for_ms(200))])

    assert clip[:4] == b"RIFF" and clip[8:12] == b"WEBP", clip[:12]
    picture = image.open(io.BytesIO(clip))
    assert getattr(picture, "n_frames", 1) > 1, "the clip is not animated"
    # loop=1 means "play through once", which is what Replay exists for.
    assert picture.info.get("loop") == 1
    assert picture.size == emulator.output_size()
    assert len(clip) < 10 * 1024 * 1024, "over Discord's free attachment limit"


def test_a_game_boy_screenshot_is_exactly_320x288(assets, emu, image, gambatte, dmg_acid2):
    emulator = emu(gambatte, dmg_acid2)
    emulator.advance(120)
    picture = image.open(io.BytesIO(emulator.screenshot())).convert("RGB")
    # The Game Boy's pixels are square, so 160x144 doubles to exactly this
    # once the core's reported aspect ratio is applied.
    assert picture.size == (320, 288)
    # ...and a clip is the same size, which is the number CI has always
    # asserted on the posted .webp.
    assert emulator.output_size() == (320, 288)
    assert len(picture.getcolors(maxcolors=1 << 24)) >= 3


# -- 1a. No shipped core asks for a screen rotation ---------------------------
#
# libretro.py's 90 degree rotation is broken -- it starts at (width - 4)
# instead of (width - 1), so the picture comes back with its rows shifted and
# partly overwritten, on 0.6.0 and on 0.11.1 alike. There is no correct path
# for it: the fast grab in retro/clips.py declines, and the "official"
# screenshot() path it falls back to is the broken one. See
# test_libretro_s_ninety_degree_rotation_is_still_broken_upstream in
# tests/test_frame_grab.py, which pins the bug itself.
#
# The cog's answer is not to handle rotation but to ship no console that asks
# for one: the WonderSwan (mednafen_wswan) was dropped for exactly this, since
# a good half of its library is played with the console turned on its side and
# the core says so. This is the check that keeps that true, so that adding a
# core which rotates has to be a deliberate decision rather than a silently
# mangled clip.

#: core -> a ROM in the assets directory that will boot it. A core with no
#: bootable ROM here cannot be checked and is skipped; the aggregate test
#: below refuses to let *every* core skip.
ROTATION_CASES = {
    "gambatte": "ucity.gbc",
    "mgba": "ucity.gbc",
    "fceumm": "nestest.nes",
    "snes9x": "snes_rotzoom.sfc",
}


def booted_rotation(emulator):
    """The rotation a booted core has asked its video driver for."""
    rotation = getattr(emulator._video, "_rotation", None)
    assert rotation is not None, "the video driver stopped reporting a rotation"
    return getattr(rotation, "name", rotation)


@pytest.mark.parametrize("core", sorted(S.CORES))
def test_no_shipped_core_asks_for_a_screen_rotation(assets, emu, core):
    rom_name = ROTATION_CASES.get(core)
    if rom_name is None:
        pytest.skip(f"no ROM here boots {core}, so its rotation cannot be read")
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    # A core may not set its rotation until it has drawn something, so give
    # it a second of play before believing the answer.
    emulator.advance(emulator.frames_for_seconds(1))

    assert booted_rotation(emulator) == "NONE", (
        f"{core} asks for a {booted_rotation(emulator)} rotation. libretro.py "
        "renders a 90 degree rotation as a destroyed picture and the cog has "
        "no path that survives it -- see the comment above this test before "
        "shipping this core."
    )
    # And the fast grab really is willing to take its frames, which is the
    # same statement from the other side.
    assert emulator._video._rotation.name in E.FAST_ROTATIONS


def test_the_rotation_check_actually_ran_on_something(assets):
    """
    A guard the *coverage* of which cannot quietly rot away.

    A machine with no assets is an honest skip, like everything else in this
    file. A machine that has the cores and still cannot boot two of them is
    not: it means ROTATION_CASES has fallen behind the core list and the
    check above is passing by skipping.
    """
    present = [core for core in S.CORES if assets.core(core)]
    if len(present) < 2:
        pytest.skip(f"only {len(present)} of {len(S.CORES)} cores are in RETRO_TEST_ASSETS")
    bootable = [
        core
        for core in present
        if ROTATION_CASES.get(core) and assets.rom(ROTATION_CASES[core])
    ]
    assert len(bootable) >= 2, (
        f"{len(present)} cores are installed but only {bootable} can be "
        "booted, so the rotation guard proved almost nothing. Add an entry to "
        "ROTATION_CASES (and a ROM to tests/fetch_assets.py) for the rest."
    )


def test_a_rotating_core_would_be_caught_rather_than_posted(emu, image, gambatte, ucity):
    """
    The check above is only worth something if a rotation is really fatal.

    Gambatte does not rotate, so the rotation is forced onto its driver here
    and the frame grab is asked for a picture. The fast path must decline
    (retro/clips.py refuses NINETY on purpose) rather than hand back
    something that looks like a frame.
    """
    from libretro import Rotation

    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(1))
    upright = E.fast_frame_image(emulator._video, image)
    assert upright is not None, "the unrotated frame should use the fast path"

    emulator._video._rotation = Rotation.NINETY
    assert E.fast_frame_image(emulator._video, image) is None
    # The size is still answered, because a sideways size is right even where
    # libretro.py's pixels are not.
    assert E.fast_frame_size(emulator._video) == (upright.height, upright.width)


# -- 1b. The fast frame grab, against the cores themselves --------------------
#
# retro.emulator.fast_frame_image decodes the video driver's framebuffer with
# Pillow rather than letting libretro.py convert it a pixel at a time, which
# is ~100x faster and reaches into libretro.py's privates to do it. The
# synthetic cover -- every pixel format, every rotation, every guard -- is in
# tests/test_frame_grab.py and runs in the fast suite. These two are the same
# assertion against whatever real cores this machine has: a core whose
# framebuffer is laid out in a way the tables above did not expect would show
# up here and nowhere else.

#: (core, ROM) for every core here that some available ROM will boot. mgba,
#: nestopia and quicknes are not cores systems.py recommends, so they are
#: only tested on a machine that happens to have them.
GRAB_CASES = [
    ("gambatte", "ucity.gbc"),
    ("gambatte", "dmg-acid2.gb"),
    ("gambatte", "pokemon.gb"),
    ("mgba", "ucity.gbc"),
    ("fceumm", "nestest.nes"),
    ("nestopia", "nestest.nes"),
    ("quicknes", "nestest.nes"),
    ("snes9x", "snes_rotzoom.sfc"),
]


@pytest.mark.parametrize(
    "core, rom_name", GRAB_CASES, ids=[f"{c}-{r}" for c, r in GRAB_CASES]
)
def test_the_fast_frame_grab_is_byte_identical_on_a_real_core(assets, emu, image, core, rom_name):
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(2))
    driver = emulator._video

    for index in range(24):
        # A held button, so the frames differ from each other and the
        # comparison is not made twenty-four times over one still picture.
        emulator._pressed = frozenset({"a"})
        emulator.advance(3)
        emulator._pressed = frozenset()

        fast = E.fast_frame_image(driver, image)
        assert fast is not None, f"{core} fell back to the slow grab"
        shot = driver.screenshot()
        official = image.frombuffer(
            "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
        ).convert("RGB")
        assert fast.size == official.size, index
        assert fast.tobytes() == official.tobytes(), f"{core} frame {index} differs"
        assert E.fast_frame_size(driver) == (shot.width, shot.height)

    assert driver._pixel_format.name in E.FAST_RAW_MODES


def test_a_recorded_clip_is_the_same_whichever_grab_made_it(emu, gambatte, ucity, monkeypatch):
    # End to end rather than frame by frame: the same recording, once with
    # the fast grab and once with it refusing to run, has to come out as the
    # same bytes -- same pictures, same upscale, same encode.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(2))
    state = emulator.save_state()
    presses = [("right", 0, emulator.frames_for_ms(400))]

    # Restored before *both* recordings, not just the second: a Game Boy that
    # has run two seconds and one that has been rewound to the same point are
    # not quite in the same state (the audio timing differs), which is enough
    # to change a byte of the clip.
    emulator.load_state(state)
    fast_clip = emulator.record(emulator.clip_frames(1.0), presses=presses)

    monkeypatch.setattr(E, "fast_frame_image", lambda driver, Image: None)
    emulator.load_state(state)
    slow_clip = emulator.record(emulator.clip_frames(1.0), presses=presses)

    assert fast_clip == slow_clip


def test_recording_converts_one_frame_per_picture_and_no_more(emu, gambatte, ucity):
    # output_size() used to pay for a whole screenshot() to read the frame
    # height off it, so fifteen pictures cost sixteen conversions. Nothing in
    # a recording should reach the slow path at all now.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(2))
    calls = []
    original = type(emulator)._screenshot
    try:
        type(emulator)._screenshot = lambda self: (calls.append(1), original(self))[1]
        emulator.record(emulator.clip_frames(1.0))
    finally:
        type(emulator)._screenshot = original
    assert calls == [], f"{len(calls)} pixel-by-pixel conversions in one recording"


# -- 2. Frame arithmetic ------------------------------------------------------


def test_frames_are_counted_from_the_core_s_own_frame_rate(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    assert emulator.frames_for_ms(160) == 10, "~10 frames at 59.7fps"
    assert emulator.frames_for_ms(400) == 24
    assert emulator.frames_for_ms(0) == 1, "a hold is never zero frames"
    assert emulator.frames_for_seconds(4) == 239
    assert E.CLIP_SECONDS == 1.0
    # The default hold has to stay under a Game Boy walk cycle (16 frames) or
    # one press walks two tiles; see the Pokemon test below.
    assert emulator.frames_for_ms(160) < 16


@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0])
def test_a_fractional_clip_length_is_a_real_number_of_frames(
    emu, gambatte, ucity, seconds
):
    """Clip lengths are floats now, and the frames come from the real fps."""
    emulator = emu(gambatte, ucity)
    frames = emulator.clip_frames(seconds)
    assert frames == max(E.MIN_CLIP_FRAMES, round(emulator.fps * seconds))
    assert frames >= E.MIN_CLIP_FRAMES, "a clip is never empty"
    assert emulator.fps != 60, "the frame count must come from the core"

    step = emulator.capture_step()
    budget = emulator.input_budget(frames)
    # Input has to be released on a frame that is actually photographed, and
    # at least one picture of the clip has to be left over to show it.
    assert budget % step == 0
    assert 1 <= budget <= frames - 1
    assert -(-frames // step) >= 2, "two pictures is the least that animates"


def test_a_longer_hold_really_does_reach_the_core(emu, gambatte, ucity):
    def screen_after(hold_frames, field="down"):
        emulator = emu(gambatte, ucity)
        emulator.advance(emulator.frames_for_seconds(6))
        emulator.press("start", hold_frames=emulator.frames_for_ms(200), release_frames=120)
        emulator.press(field, hold_frames=hold_frames, release_frames=90)
        return hashlib.sha256(emulator.screenshot(scale=1)).hexdigest()[:12]

    assert screen_after(1) != screen_after(60)


def test_one_press_walks_exactly_one_tile(assets, emu):
    """The Pokemon Red two-tile bug, measured in the game's own WRAM.

    The report was that pressing a direction walked the character TWO tiles.
    A Game Boy walk cycle is 16 frames, so any hold that outlasts it starts a
    second step. This drives the real ROM into the overworld and reads the
    player's tile coordinates (wYCoord/wXCoord at 0xD361/0xD362) after a
    press of each length.
    """
    core = assets.need_core("gambatte")
    rom = assets.need_rom("pokemon.gb")
    poke = emu(core, rom)

    wram = poke._session.core.get_memory(2)  # RETRO_MEMORY_SYSTEM_RAM
    assert wram is not None and len(wram) == 8192, "no 8 KiB of Game Boy work RAM"

    def coords():
        return (wram[0x1361], wram[0x1362])

    def tap(button, hold=10, gap=14):
        poke._pressed = frozenset({button})
        poke.advance(hold)
        poke._pressed = frozenset()
        poke.advance(gap)

    # Mash through Oak's intro and the two name menus.
    poke.advance(300)
    for index in range(128):
        tap(("a", "a", "down", "a")[index % 4])
    poke.advance(120)
    assert wram[0x135E] == 0x26, f"never reached the overworld ({hex(wram[0x135E])})"

    # Walk to the right-hand wall, then one step left, so the player already
    # faces left (Gen 1 turns in place on the first press of a new direction)
    # with clear tiles ahead.
    poke._pressed = frozenset({"right"})
    poke.advance(400)
    poke._pressed = frozenset()
    poke.advance(120)
    poke._pressed = frozenset({"left"})
    poke.advance(30)
    poke._pressed = frozenset()
    poke.advance(150)
    ready = poke.save_state()
    # The default clip is one second now, not four, so this is measured
    # inside the clip the cog really records -- a hold that walks one tile is
    # no use if the step does not finish before the clip does.
    clip = poke.clip_frames(E.CLIP_SECONDS)
    assert clip == 60, f"a one second Game Boy clip is 60 frames, not {clip}"

    def tiles(hold_ms, frames=None):
        frames = clip if frames is None else frames
        poke.load_state(ready)
        start = coords()
        # Exactly what the cog schedules: the hold is capped at the last
        # frame of the clip that is still photographed, so what is measured
        # here is what a player would actually see.
        hold = min(poke.frames_for_ms(hold_ms), poke.input_budget(frames))
        poke._pressed = frozenset({"left"})
        poke.advance(hold)
        poke._pressed = frozenset()
        poke.advance(max(0, frames - hold))
        end = coords()
        return abs(end[0] - start[0]) + abs(end[1] - start[1])

    walked = {ms: tiles(ms) for ms in (400, 300, 250, 200, 160, 150, 100)}
    assert walked[400] == 2, f"the OLD 400ms d-pad hold no longer walks two tiles: {walked}"
    assert walked[160] == 1, f"the NEW 160ms hold must walk exactly one: {walked}"
    assert walked[200] == 1, walked
    assert all(walked[ms] == 1 for ms in (250, 200, 160, 150, 100)), walked

    # One second is enough aftermath to *see* the step complete: the tile has
    # changed by the last frame the clip photographs, not merely by the end of
    # the emulation. A Game Boy walk cycle is 16 frames and the hold is 10, so
    # the step lands around frame 26 of 60 and the clip shows the rest.
    assert tiles(160, poke.input_budget(clip)) == 1, "the step finishes inside the clip"

    # The shortest clip the settings allow still registers the press: the
    # hold is cut to the 8 frames a 12 frame clip can show being released,
    # which is ~134ms and still over the ~100ms a game needs to see a press.
    # A walk cycle is 16 frames and the clip is 12, so the tile it walks to
    # is credited during the *next* clip -- the press is not lost, it lands a
    # clip later, which is the honest cost of a 0.2 second clip.
    short = poke.clip_frames(E.MIN_CLIP_SECONDS)
    assert tiles(160, short) == 0, "unexpectedly quick: recheck the comment above"
    assert tiles(160, short * 2) == 1, "a 0.2s clip lost the press altogether"


# -- 3. Clip timing: what the player actually sees ----------------------------


@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0])
def test_a_clip_plays_for_as_long_as_it_emulated(emu, gambatte, ucity, seconds):
    """Playback time tracks emulated time at every clip length, whole or not."""
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(seconds)
    data = emulator.record(frames)
    durations, loop = anmf(data)
    step = emulator.capture_step()

    assert durations, "no animation frames at all"
    # WebP stores milliseconds; a 15 fps clip off a 59.73 fps core is 67ms a
    # frame. GIF stores centiseconds and cannot be exact, which is why it is
    # only ever a fallback.
    frame_ms = round(1000 * step / emulator.fps)
    plan = E.capture_plan(frames, step)
    captured = len(plan)
    assert loop == 1, "the clip plays through exactly once"
    # The encoder merges runs of identical frames and adds their durations
    # together, so a clip of a static screen legitimately comes back with
    # fewer frames than were captured -- never more, and never a zero-length
    # one, and always the same total length.
    assert 1 <= len(durations) <= captured
    assert all(duration > 0 for duration in durations)
    if len(durations) == captured:
        # Every picture stands until the next one is taken, which is a whole
        # step except in the tail: a 30 frame clip is seven 67ms pictures,
        # then frame 29 for 33ms and the closing frame 30 for 17ms. Calling
        # any of those a full 67ms played a half-second clip 7% slow.
        expected = [
            max(1, round(1000 * covered / emulator.fps)) for _, covered in plan
        ]
        assert durations == expected, (durations, expected)
        assert set(durations[:-2]) <= {frame_ms} or captured <= 2, set(durations)
        assert 0 < durations[-1] <= frame_ms, durations[-1]
    emulated = 1000 * frames / emulator.fps
    assert abs(sum(durations) - emulated) / emulated < 0.01, (sum(durations), emulated)
    # Every picture of the clip is worth having: a fifth of a second is four
    # of them, not one.
    assert captured >= 2 and len(data) > 0


def test_a_static_screen_collapses_to_a_still_that_is_still_a_clip(
    emu, image, gambatte, ucity
):
    """A short clip of a screen where nothing moves really is one frame.

    libwebp merges identical consecutive frames, and when *every* captured
    picture is the same it writes a plain still WebP with no animation chunks
    in it at all. That is fine -- Discord shows it, and it costs a few dozen
    bytes -- but it means the file no longer says how long the clip was, which
    is what decode_clip's fallback and concatenate_clips' `seconds` exist for.
    Far more likely at a one second clip than it was at four.
    """
    emulator = emu(gambatte, ucity)
    # Frame 1 of a cold boot: the screen is blank and stays blank.
    frames = emulator.clip_frames(0.2)
    data = emulator.record(frames)
    picture = image.open(io.BytesIO(data))

    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert getattr(picture, "n_frames", 1) == 1, "uCity's boot is not static"
    assert anmf(data)[0] == [], "a still has no frame timings to read"
    assert 0 < len(data) < 4096, len(data)

    # Nothing divides by zero, and no frame is left with no duration at all.
    decoded, decoded_durations = E.decode_clip(data)
    assert len(decoded) == 1 and decoded_durations == [1]
    _, told = E.decode_clip(data, 200)
    assert told == [200], "a caller that knows the length can say so"

    # And it still stitches, with the session's own lengths standing in for
    # the timing the file does not have.
    stitched, covered = E.concatenate_clips([data, data])
    assert len(stitched) > 0 and covered == pytest.approx(0.002)
    stitched, covered = E.concatenate_clips([data, data], seconds=[0.2, 0.2])
    assert len(stitched) > 0 and covered == pytest.approx(0.4)


def test_a_clip_never_opens_on_the_picture_from_before_the_press(emu, image, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    before = hashlib.sha1(
        image.open(io.BytesIO(emulator.screenshot())).convert("RGB").tobytes()
    ).hexdigest()[:10]

    moved = emulator.record(frames, presses=[("down", 0, emulator.frames_for_ms(160))])
    hashes = frame_hashes(moved, image)
    assert hashes[0] != before
    assert hashes[0] != hashes[1], "the first two frames are duplicates"


def test_one_clip_carries_on_from_the_last_with_no_frames_lost(emu, gambatte, ucity):
    """The other half of "seamless movement", proved against a real core.

    A clip's pictures are every ``step``-th emulated frame *plus the last
    one* (see capture_plan), so the picture a clip finishes on -- and holds,
    since clips play through once -- is the exact state the next clip starts
    from. Before the closing frame was photographed a 60 frame clip stopped
    on frame 57 while 58, 59 and 60 were emulated and never shown, so the
    still picture sitting in the channel between presses was three frames
    behind the console.

    So: record two consecutive clips, then rewind and emulate the same frames
    one at a time to get a reference picture for each. The first clip's last
    picture has to be reference frame N, and the second clip's first picture
    reference frame N + 1 -- adjacent, neither repeated nor skipped.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    size = emulator.output_size()
    state = emulator.save_state()

    # load_state runs a frame of its own, so both the recordings and the
    # reference below start from the same place: the state plus one frame.
    emulator.load_state(state)
    first = E.decode_clip(emulator.record(frames))[0]
    second = E.decode_clip(emulator.record(frames))[0]

    emulator.load_state(state)
    reference = []
    for _ in range(frames + 1):
        emulator.advance(1)
        reference.append(emulator._frame_image(size).tobytes())

    assert first[-1].tobytes() == reference[frames - 1], (
        "the first clip does not end on its own last emulated frame"
    )
    assert second[0].tobytes() == reference[frames], (
        "the second clip does not open on the very next emulated frame"
    )
    # And the two really are different pictures, or none of the above means
    # anything: a static screen would satisfy it by accident.
    assert reference[frames - 1] != reference[frames], "nothing moved at all"


@pytest.mark.parametrize("key, core, rom_name, button", CASES, ids=[c[0] for c in CASES])
def test_each_console_is_posted_at_the_size_that_was_measured(
    assets, emu, image, key, core, rom_name, button
):
    """The per-console upscale, read back off the real cores.

    The flat 2x doubled every console up to 256 lines tall, which on a NES or
    a SNES cost 2-4x the encode for a picture Discord shrinks to the message
    column anyway. See MIN_CLIP_WIDTH in retro/clips.py for the timings.
    tests/test_clips.py holds the same table with no core at all.
    """
    expected = {"gb": (320, 288), "nes": (293, 224), "snes": (299, 224)}[key]
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(2))

    frame_width, frame_height = emulator._frame_size()
    assert emulator.output_size() == expected, (
        f"{key} is {frame_width}x{frame_height} at {emulator.aspect_ratio:.4f}"
    )
    # Never a downscale: the aspect correction may repeat columns, but a
    # NEAREST resize that shrinks would throw somebody's pixels away.
    assert expected[0] >= frame_width and expected[1] >= frame_height
    # ...and the clip really is posted at it.
    clip = emulator.record(emulator.clip_frames(0.2))
    assert image.open(io.BytesIO(clip)).size == expected
    # scale=1 is still "the console's own resolution", which is what the
    # screenshot hashes in this file rely on.
    assert emulator.output_size(scale=1) == (
        max(1, round(frame_height * emulator.aspect_ratio)),
        frame_height,
    )


def test_a_resumed_clip_picks_up_where_the_last_one_left_off(emu, image, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    state = emulator.save_state()
    # load_state deliberately runs one frame, so a restored session is at
    # T+1; the session it is compared against is put there too, and the two
    # clips then have to be the same clip.
    emulator.advance(1)
    direct = frame_hashes(emulator.record(frames), image)

    # One core at a time, exactly as the cog does it.
    fresh = emu(gambatte, ucity)
    fresh.load_state(state)
    resumed = frame_hashes(fresh.record(frames), image)

    # libwebp merges runs of identical frames (and sums their durations), so
    # a static screen produces fewer frames than were captured; both clips
    # merge the same way, so comparing them frame for frame still works.
    assert len(resumed) == len(direct)
    same = sum(1 for a, b in zip(direct, resumed, strict=True) if a == b)
    assert same >= len(direct) - 2, f"only {same}/{len(direct)} frames identical"
    assert len(resumed) < 2 or resumed[0] != resumed[1], "a duplicated opening frame"


def test_the_gif_fallback_rounds_to_whole_centiseconds(emu, image, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(180)
    gif = emulator.record(60, clip_format="GIF")
    assert (image.open(io.BytesIO(gif)).info.get("duration", 0)) % 10 == 0


# -- 4. Press schedules -------------------------------------------------------


def test_one_tap_and_three_taps_give_different_clips(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(4))
    state = emulator.save_state()
    one = emulator.record(120, presses=[("a", 0, 12)])
    emulator.load_state(state)
    three = emulator.record(120, presses=[("a", 0, 12), ("a", 27, 12), ("a", 54, 12)])
    assert one != three


def test_an_unknown_button_is_rejected(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    with pytest.raises(E.EmulatorError, match="Unknown button"):
        emulator.record(30, presses=[("nope", 0, 5)])


def test_every_retropad_field_is_accepted():
    assert len(E.BUTTONS) == 16, E.BUTTONS
    assert all(E.RetroEmulator._check_button(field) == field for field in E.BUTTONS)


def test_an_unknown_clip_format_is_rejected(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    with pytest.raises(E.EmulatorError, match="Unknown clip format"):
        emulator.record(30, clip_format="JPEG")


def test_the_gif_fallback_still_produces_a_playable_gif(emu, image, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(180)
    gif = emulator.record(120, clip_format="GIF", presses=[("right", 0, 24)])
    assert gif[:6] in (b"GIF87a", b"GIF89a"), gif[:6]
    picture = image.open(io.BytesIO(gif))
    assert getattr(picture, "n_frames", 1) >= 1
    assert picture.info.get("loop") is None, "a GIF must not loop forever"


def test_webp_is_smaller_than_gif_on_a_busy_picture(assets, emu, image):
    """WebP is the default for a reason; GIF is only the fallback."""
    rom = assets.rom("snes_rotzoom.sfc") or assets.rom("pokemon.gb")
    if rom is None:
        pytest.skip("no busy ROM in RETRO_TEST_ASSETS")
    core = assets.need_core("snes9x" if str(rom).endswith(".sfc") else "gambatte")
    held = ("right", "a") if str(rom).endswith(".sfc") else ("start",)

    sizes, frames_seen = {}, {}
    for clip_format in ("WEBP", "GIF"):
        emulator = emu(core, str(rom))
        emulator.advance(emulator.frames_for_seconds(3))
        count = emulator.clip_frames(E.CLIP_SECONDS)
        data = emulator.record(
            count, presses=[(b, 0, count) for b in held], clip_format=clip_format
        )
        sizes[clip_format] = len(data)
        frames_seen[clip_format] = getattr(image.open(io.BytesIO(data)), "n_frames", 1)

    assert sizes["WEBP"] < sizes["GIF"], sizes
    assert frames_seen["WEBP"] == frames_seen["GIF"], frames_seen


# -- 5. Save states -----------------------------------------------------------


def test_a_save_state_replays_identically_in_a_fresh_instance(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(4))
    state = emulator.save_state()

    def replay(target, first_release):
        target.press("down", hold_frames=24, release_frames=first_release)
        target.press("a", hold_frames=12, release_frames=119)
        return hashlib.sha256(target.screenshot(scale=1)).hexdigest()[:12]

    start = hashlib.sha256(emulator.screenshot(scale=1)).hexdigest()[:12]
    first = replay(emulator, 119)
    assert first != start, "the machine never moved on"

    # load_state deliberately runs one frame, so a restored state is T+1.
    # Gambatte's blob embeds a real-time-clock stamp, so the picture the
    # replay lands on is the contract, not the bytes.
    emulator.load_state(state)
    second = replay(emulator, 118)
    assert first == second, "load_state did not rewind the whole machine"

    fresh = emu(gambatte, ucity)
    fresh.load_state(state)
    assert replay(fresh, 118) == second


def test_a_corrupt_save_state_is_rejected(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    with pytest.raises(E.EmulatorError):
        emulator.load_state(b"junk")


def test_a_state_the_core_will_not_take_leaves_the_battery_usable(emu, gambatte, ucity):
    # When a core is updated its serialize_size() changes and every state on
    # disk becomes unloadable. Faked by truncating one, which is the same
    # failure from load_state()'s point of view.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    marker = bytes(range(256)) * 512  # exactly 131072 bytes
    assert emulator.load_sram(marker)
    good = emulator.save_state()

    after = emu(gambatte, ucity)
    with pytest.raises(E.EmulatorError, match="expects") as error:
        after.load_state(good[: len(good) // 2])
    assert after.started, "the emulator must still be usable afterwards"
    assert "expects" in str(error.value)
    assert after.load_sram(marker) is True
    after.advance(120)
    assert after.save_sram() == marker, "the player's in-game save must survive"

    # And a matching state still loads, so the fallback really is a fallback.
    again = emu(gambatte, ucity)
    again.load_state(good)


# -- 6. ROMs that are not ROMs ------------------------------------------------


def test_a_file_too_small_to_be_a_rom_is_refused(emu, gambatte, tmp_path):
    tiny = tmp_path / "tiny.gb"
    tiny.write_bytes(b"\0" * 100)
    with pytest.raises(E.EmulatorError, match="too small"):
        emu(gambatte, str(tiny))
    assert E.MIN_ROM_SIZE == 1024, "a generic floor, not a Game Boy header check"


# -- 7. The system (BIOS) directory -------------------------------------------


def test_the_core_is_given_our_system_directory(emu, gambatte, ucity, tmp_path):
    root = tmp_path / "bios"
    system_dir = root / "system"
    emulator = emu(gambatte, ucity, system_dir=system_dir)

    assert emulator.system_directory == str(system_dir)
    assert system_dir.is_dir()
    # libretro.py 0.6.x's path driver needs all four of these to exist.
    assert all((root / name).is_dir() for name in ("assets", "save", "playlist"))
    # Left alone, libretro.py hands every core a throwaway temp directory.
    assert "libretro.py-" not in (emulator.system_directory or "")


def environment_driver(session):
    """
    The object libretro.py dispatches RETRO_ENVIRONMENT_* calls on.

    Two shapes, both of which this cog supports: libretro.py 0.6.x hangs a
    separate driver off ``Session._environment``, while 0.7+ made the Session
    *be* its own CompositeEnvironmentDriver. Either way the method that
    answers GET_SYSTEM_DIRECTORY is ``_get_system_directory`` on the object's
    type, and the dispatch table looks it up on ``self`` at call time, so
    replacing it on the class really is seen.
    """
    for candidate in (getattr(session, "_environment", None), session):
        if candidate is None:
            continue
        if getattr(type(candidate), "_get_system_directory", None) is not None:
            return candidate
    return None


def test_the_core_really_asks_for_the_system_directory(emu, gambatte, ucity, tmp_path):
    # Reaches into libretro.py's internals, which moved between releases --
    # see environment_driver() above. This used to skip on anything newer
    # than 0.6.x, which quietly stopped testing anything at all on the
    # release people actually install; if it ever has to skip again, that is
    # a libretro.py change worth failing over rather than shrugging at.
    system_dir = tmp_path / "bios" / "system"
    probe = emu(gambatte, ucity, system_dir=system_dir)
    environment = environment_driver(probe._session)
    assert environment is not None, (
        "no _get_system_directory anywhere on this libretro.py's session; the "
        "environment dispatch has been rearranged again and retro/emulator.py "
        "needs re-reading, not this test relaxing"
    )
    environment_type = type(environment)
    real_get = environment_type._get_system_directory
    observed = []

    def spy(self, dir_ptr):
        ok = real_get(self, dir_ptr)
        value = dir_ptr[0] if ok and dir_ptr[0] else None
        # 0.6.x hands back bytes, 0.11.x a str.
        if isinstance(value, bytes):
            value = value.decode()
        observed.append(value)
        return ok

    probe.stop()
    environment_type._get_system_directory = spy
    try:
        watched = emu(gambatte, ucity, system_dir=system_dir)
        watched.advance(10)
        watched.stop()
    finally:
        environment_type._get_system_directory = real_get

    assert observed, "the core never asked for the system directory"
    assert all(value == str(system_dir) for value in observed), observed[:3]


def test_a_file_left_in_the_system_directory_survives_a_session(emu, gambatte, ucity, tmp_path):
    # The whole point: libretro.py's default TempDirPathDriver deletes its
    # system directory when the session ends.
    system_dir = tmp_path / "bios" / "system"
    system_dir.mkdir(parents=True)
    (system_dir / "pretend_bios.bin").write_bytes(b"\xab" * 64)

    emulator = emu(gambatte, ucity, system_dir=system_dir)
    emulator.advance(5)
    emulator.stop()
    assert (system_dir / "pretend_bios.bin").read_bytes() == b"\xab" * 64


def test_a_core_still_boots_with_no_system_directory_at_all(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(5)
    assert emulator.started


def test_the_command_line_wires_up_the_system_directory(gambatte, dmg_acid2, tmp_path):
    # The same check CI used to do inline: run the module's own CLI and see
    # that the path it reports back is the one it was given.
    system_dir = tmp_path / "biostest" / "system"
    output = tmp_path / "out.png"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "retro" / "emulator.py"),
         gambatte, dmg_acid2, str(output), "60"],
        env={**__import__("os").environ, "RETRO_SYSTEM_DIR": str(system_dir)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert f"system directory: {system_dir}" in result.stdout, result.stdout
    assert system_dir.is_dir()
    assert output.is_file() and output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# -- 8. Audio and logging cannot swamp the bot --------------------------------


def test_the_audio_buffer_is_drained_between_clips(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    assert emulator._audio_buffer is not None, "the audio buffer was not found"

    # Four short clips rather than four long ones: what is under test is that
    # the buffer is empty *again* after each one, which does not get any truer
    # with more frames in between -- and the frames are WebP encodes.
    lengths = []
    for _ in range(4):
        emulator.record(24)
        lengths.append(len(emulator._session.audio.buffer))
    assert lengths == [0, 0, 0, 0], lengths

    # advance() has to drain too, not just record(). The control below runs
    # the same 120 frames with the drain disabled, so the two numbers are
    # directly comparable: same input, opposite outcome.
    emulator.advance(120)
    assert len(emulator._session.audio.buffer) == 0

    # Without the drain it would be ~176 KiB per emulated second.
    emulator._audio_buffer = None
    emulator.advance(120)
    assert len(emulator._session.audio.buffer) > 100_000, "the control never grew"


def test_nothing_a_core_logs_reaches_the_bot_s_log(emu, gambatte, ucity):
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    E.core_log.addHandler(handler)
    E.core_log.setLevel(logging.DEBUG)
    libretro_logger = logging.getLogger("libretro")
    before = len(libretro_logger.handlers)
    try:
        emulator = emu(gambatte, ucity)
        emulator.advance(120)
        emulator.stop()
    finally:
        E.core_log.removeHandler(handler)

    assert len(libretro_logger.handlers) == before, "libretro.py's logger was hijacked"
    assert all(r.levelno < logging.INFO for r in records), [
        (r.levelname, r.getMessage()) for r in records if r.levelno >= logging.INFO
    ][:5]
    assert not any(E.FORMAT_SPECIFIER.search(r.getMessage()) for r in records)


def test_the_log_driver_drops_noise_and_caps_errors():
    driver = E._make_log_driver()
    assert driver is not None

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    class Level:
        def __init__(self, name):
            self.name = name

    handler = Capture()
    E.core_log.addHandler(handler)
    E.core_log.setLevel(logging.DEBUG)
    try:
        driver.log(Level("INFO"), b"[Gambatte] %s")
        assert records == [], "an unfilled printf specifier must be dropped"

        driver.log(Level("INFO"), b"loaded 100% of the rom")
        assert len(records) == 1, "an honest percentage must survive"
        assert records[-1].levelno == logging.DEBUG

        records.clear()
        driver.log(Level("INFO"), b"   ")
        assert records == [], "a blank line must be dropped"
        driver.log(Level("INFO"), b"failed to open %d files")
        assert records == []

        records.clear()
        for _ in range(E.MAX_CORE_WARNINGS + 5):
            driver.log(Level("ERROR"), b"something broke")
        warned = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warned) == E.MAX_CORE_WARNINGS, "core errors are visible but capped"
        assert len(records) == E.MAX_CORE_WARNINGS + 5, "the rest are demoted to DEBUG"

        driver.log(Level("INFO"), b"\xff\xfe invalid utf-8")  # must not raise
    finally:
        E.core_log.removeHandler(handler)


# -- 9. Core options, read from real cores ------------------------------------

#: What each core declared when this was written, with libretro.py 0.6.0.
#: A core update may add options, so the assertion is a floor rather than an
#: equality -- the buildbot job is where core drift is meant to show up.
PROBE_EXPECT = {"gambatte": 32, "snes9x": 43, "genesis_plus_gx": 62, "fceumm": 0}


@pytest.mark.parametrize("core, observed", sorted(PROBE_EXPECT.items()))
def test_a_core_s_options_can_be_read_with_no_rom_at_all(assets, core, observed):
    # Most cores declare their options from retro_set_environment, before any
    # content exists, so `[p]retroset coreoptions` can read them by loading
    # the core on its own. FCEUmm declares nothing until a ROM is in, which
    # is exactly why the cog caches what it learns from a running game.
    definitions = E.probe_core_options(assets.need_core(core))
    if observed == 0:
        assert definitions == {}
        return
    assert len(definitions) >= observed - 2, (core, len(definitions), observed)
    assert all(isinstance(key, str) for key in definitions), "keys must be text, not bytes"
    assert all(
        isinstance(value, str)
        for definition in definitions.values()
        for value in (definition["desc"], definition["info"], definition["default"])
    )
    # libretro pads the value array with NULLs; none of that may leak.
    assert all(all(pair[0] for pair in d["values"]) for d in definitions.values())


def test_gambatte_s_colorization_option_reads_back_in_full(assets):
    definitions = E.probe_core_options(assets.need_core("gambatte"))
    option = definitions["gambatte_gb_colorization"]
    assert option["desc"] == "GB Colorization"
    assert option["default"] == "disabled"
    values = [value for value, _ in option["values"]]
    assert all(values)
    for wanted in ("disabled", "auto", "GBC", "SGB"):
        assert wanted in values, (wanted, values)
    assert option["info"], "the option should carry its help text"


def test_a_live_fceumm_session_reports_the_options_the_probe_could_not(assets, emu):
    emulator = emu(assets.need_core("fceumm"), assets.need_rom("nestest.nes"))
    emulator.advance(60)
    live = emulator.option_definitions()

    assert len(live) >= 40, len(live)
    assert "fceumm_region" in live
    assert live["fceumm_region"]["default"] == "Auto"
    assert [v for v, _ in live["fceumm_region"]["values"]] == ["Auto", "NTSC", "PAL", "Dendy"]
    assert "fceumm_game_genie" in live
    assert "fceumm_show_adv_sound_options" in live


@pytest.mark.parametrize("core", sorted(S.CORES))
def test_no_core_uses_the_reset_sentinel_as_a_real_value(assets, core):
    # `[p]retroset coreoptions <core> <key> reset` puts a core's default
    # back, so the sentinel must not also be something a core accepts.
    # "none" is out because snes9x and genesis_plus_gx both offer it, "off"
    # because mGBA does and "disabled" because most of them do.
    definitions = E.probe_core_options(assets.need_core(core))
    clash = [
        key
        for key, option in definitions.items()
        if any(value.lower() == "reset" for value, _ in option["values"])
    ]
    assert not clash, f"{core} uses 'reset' as a value of {clash}"


def test_the_cores_between_them_declare_a_useful_number_of_options(assets):
    total = 0
    read = 0
    for core in sorted(S.CORES):
        path = assets.core(core)
        if path is None:
            continue
        read += 1
        total += len(E.probe_core_options(str(path)))
    if read < len(S.CORES):
        pytest.skip(f"only {read} of {len(S.CORES)} cores are available")
    # FCEUmm declares nothing until a ROM is loaded, so its zero is expected.
    assert total > 150, f"only {total} options across {len(S.CORES)} cores"


# -- 10. A core option changes what the core actually does --------------------


def test_a_core_option_changes_what_the_core_draws(emu, image, gambatte, dmg_acid2):
    # dmg-acid2 is a plain Game Boy ROM, so Gambatte's colorization option is
    # the difference between the four Game Boy greys and a palette. If the
    # option never reached the core, all three would be identical.
    shots = {}
    for wanted in ("disabled", "GBC", "SGB"):
        emulator = emu(
            gambatte, dmg_acid2, options={"gambatte_gb_colorization": wanted}
        )
        emulator.advance(emulator.frames_for_seconds(4))
        png = emulator.screenshot()
        picture = image.open(io.BytesIO(png)).convert("RGB")
        assert emulator.option_value("gambatte_gb_colorization") == wanted
        shots[wanted] = (
            len(picture.getcolors(1 << 24)),
            hashlib.sha256(png).hexdigest(),
        )

    assert shots["disabled"][0] == 4, "the four Game Boy greys"
    assert shots["GBC"][0] > 4, shots["GBC"]
    assert len({digest for _, digest in shots.values()}) == 3, shots


def test_an_unusable_core_option_degrades_to_the_default(emu, gambatte, dmg_acid2):
    # A key or value the core does not know must not stop a game: the option
    # driver validates and hands the core its own default.
    emulator = emu(
        gambatte,
        dmg_acid2,
        options={
            "gambatte_not_a_real_option": "x",
            "gambatte_gb_colorization": "not-a-real-value",
        },
    )
    emulator.advance(60)
    assert emulator.started and emulator.screenshot()
    assert emulator.set_option("gambatte_gb_colorization", "SGB")


# -- 11. Battery saves --------------------------------------------------------

SRAM_CASES = [
    ("gambatte", "ucity.gbc", 131072),
    ("gambatte", "dmg-acid2.gb", 0),
    ("fceumm", "nestest.nes", 0),
    ("snes9x", "snes_rotzoom.sfc", 0),
]


@pytest.mark.parametrize(
    "core, rom_name, expected", SRAM_CASES, ids=[f"{c[1]}" for c in SRAM_CASES]
)
def test_a_cartridge_reports_its_battery_honestly(assets, emu, core, rom_name, expected):
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(60)
    assert emulator.sram_size == expected

    blob = emulator.save_sram()
    if expected:
        assert isinstance(blob, bytes) and len(blob) == expected
    else:
        # The ordinary case, and it must be silent rather than an error.
        assert blob is None
        assert emulator.load_sram(b"x" * 16) is False


def test_a_battery_save_round_trips_and_survives_a_cold_boot(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    size = emulator.sram_size
    assert size == 131072, f"expected 128 KiB of SRAM, got {size}"

    marker = (bytes(range(256)) * (size // 256 + 1))[:size]
    assert emulator.load_sram(marker) is True
    assert emulator.save_sram() == marker
    assert emulator.load_sram(b"\0" * 100) is False, "a wrong size must be refused"
    assert emulator.load_sram(b"") is False
    emulator.advance(120)
    kept = emulator.save_sram()

    fresh = emu(gambatte, ucity)
    assert fresh.save_sram() != kept, "a fresh boot should not have it yet"
    assert fresh.load_sram(kept) is True
    fresh.advance(120)
    assert fresh.save_sram() == kept, "and it is still there once the game has run"


# -- 12. Stitching clips back together (the Replay button) --------------------
#
# Replay decodes the session's buffered clips and re-encodes them as one
# animation. It runs on real video, so the interesting questions are whether
# the timeline survives the round trip and how long the round trip takes.


def test_buffered_clips_stitch_back_into_one_animation(emu, image, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.frames_for_seconds(4)
    clips = [emulator.record(frames, presses=[("start", 0, 10)]) for _ in range(4)]

    stitched, seconds = E.concatenate_clips(clips)

    assert stitched[:4] == b"RIFF" and stitched[8:12] == b"WEBP"
    durations, loop = anmf(stitched)
    assert loop == 1, "a replay plays through once, like every other clip"
    # 16 seconds of footage, trimmed to the 15 second window.
    assert 14.0 <= seconds <= E.REPLAY_SECONDS + 0.5, seconds
    assert abs(sum(durations) / 1000.0 - seconds) < 0.1

    # The last frame of the replay is the last frame of the newest clip: a
    # replay always ends on the moment the player just played.
    assert frame_hashes(stitched, image)[-1] == frame_hashes(clips[-1], image)[-1]


def test_a_stitched_replay_is_trimmed_from_the_oldest_end(emu, image, gambatte, ucity):
    # The clips are deliberately short: what is under test is *which end* the
    # trim comes off, which is the same question at 0.4 seconds a clip as at
    # two, and a WebP encode is the most expensive thing in this file. The
    # budget is half the footage, so the trim has to bite either way.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.frames_for_seconds(0.4)
    clips = [emulator.record(frames) for _ in range(3)]

    _, full = E.concatenate_clips(clips, max_seconds=100)
    short, seconds = E.concatenate_clips(clips, max_seconds=0.6)
    assert seconds <= 0.8 < full
    # It is the *newest* 0.6 seconds, so the end still matches.
    assert frame_hashes(short, image)[-1] == frame_hashes(clips[-1], image)[-1]
    # And it really did drop something, rather than the budget being generous
    # enough to keep the lot.
    assert len(frame_hashes(short, image)) < len(frame_hashes(clips[-1], image)) * 3


def test_one_clip_longer_than_the_window_is_trimmed_by_frame(emu, gambatte, ucity):
    # "Longer than the window" is relative to max_seconds, which is passed in
    # here, so one second against a 0.3 second budget tests exactly what six
    # seconds against two did -- for a sixth of the encoding.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    long_clip = emulator.record(emulator.frames_for_seconds(1.0))
    _, full = E.concatenate_clips([long_clip], max_seconds=100)
    _, seconds = E.concatenate_clips([long_clip], max_seconds=0.3)
    assert full > 0.5, "the single clip is not longer than the window any more"
    assert seconds <= 0.5


def test_the_frame_cap_bounds_the_work(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    clips = [emulator.record(emulator.frames_for_seconds(0.5)) for _ in range(2)]
    uncapped, _ = E.concatenate_clips(clips, max_seconds=100, max_frames=1000)
    stitched, _ = E.concatenate_clips(clips, max_seconds=100, max_frames=10)
    durations, _ = anmf(stitched)
    # The cap has to actually be doing something: without this the test would
    # pass just as well on footage that never reached ten frames.
    assert len(anmf(uncapped)[0]) > 10, "there was nothing for the cap to cut"
    assert len(durations) <= 10


def test_a_resolution_change_ends_the_replay_rather_than_corrupting_it(
    emu, image, gambatte, ucity
):
    # Animation formats have one size for the whole file, and the SNES really
    # does change resolution mid-session, so an older clip of another size
    # must be dropped rather than stretched or crashed on.
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    normal = emulator.record(emulator.frames_for_seconds(0.4))
    small = E.encode_animation(
        [image.new("RGB", (16, 16)) for _ in range(3)], 67, "WEBP"
    )
    stitched, _ = E.concatenate_clips([small, normal])
    assert image.open(io.BytesIO(stitched)).size == image.open(io.BytesIO(normal)).size
    assert len(frame_hashes(stitched, image)) == len(frame_hashes(normal, image))


def test_an_unreadable_clip_in_the_buffer_costs_only_the_older_footage(
    emu, gambatte, ucity
):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    good = emulator.record(emulator.frames_for_seconds(0.4))
    stitched, seconds = E.concatenate_clips([b"not a clip at all", good])
    assert stitched[:4] == b"RIFF"
    assert seconds > 0


def test_a_buffer_of_nothing_but_rubbish_raises(emu):
    with pytest.raises(E.EmulatorError):
        E.concatenate_clips([b"nope", b"also nope"])
    with pytest.raises(E.EmulatorError):
        E.concatenate_clips([])


def test_gif_clips_stitch_too(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    clips = [
        emulator.record(emulator.frames_for_seconds(0.4), clip_format="GIF")
        for _ in range(2)
    ]
    stitched, seconds = E.concatenate_clips(clips, clip_format="GIF")
    assert stitched[:6] in (b"GIF87a", b"GIF89a")
    assert seconds > 0


def test_stitching_fifteen_seconds_is_quick_enough_to_do_on_a_button_press(
    emu, gambatte, ucity
):
    """The number the feature lives or dies on; see REPLAY_SECONDS."""
    import time

    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    clips = [
        emulator.record(emulator.frames_for_seconds(4), presses=[("start", 0, 10)])
        for _ in range(4)
    ]

    started = time.perf_counter()
    stitched, seconds = E.concatenate_clips(clips)
    elapsed = time.perf_counter() - started

    # Eight seconds is the point at which a Discord button press stops feeling
    # like it worked. Real footage measures well under two on the machine this
    # was written on; the margin is for slower hardware, not for a change that
    # makes this ten times more expensive.
    assert elapsed < 8.0, f"{elapsed:.2f}s to stitch {seconds:.1f}s of footage"
    # And it has to be postable: Discord's floor for a bot attachment is
    # 10 MiB, and this must stay nowhere near it.
    assert len(stitched) < 8 * 1024 * 1024, len(stitched)
