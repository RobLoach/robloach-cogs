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

import contextlib
import hashlib
import io
import logging
import struct

import pytest

from .conftest import LIBBET_BOOT_SECONDS
from .loader import load_standalone

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


# -- What the emulator does not wrap ------------------------------------------
#
# retro/clips.py's timing functions are plain functions of a frame rate -- the
# cog calls them that way, laying a press schedule out before a core exists
# (see RetroView.press_plan) -- so RetroEmulator has no methods for them and
# these three fill in the core's own rate. The read-backs below reach into
# libretro.py's drivers, which is the only way to see that a value really
# crossed into the core; nothing in the cog asks, so nothing in the cog has a
# property for it either.


def frames_for_ms(emulator, milliseconds):
    return E.frame_count(emulator.fps, float(milliseconds) / 1000.0)


def capture_step(emulator):
    return E.capture_step(emulator.fps)


def input_budget(emulator, frames):
    return E.input_budget(emulator.fps, frames)


def press(emulator, button, hold_frames=12, release_frames=40):
    """Hold a button, release it, run on -- and photograph nothing.

    ``record()`` is the only way the cog drives a core, and it always
    photographs what it emulates; a couple of the tests below want the plain
    version instead.
    """
    emulator._pressed = frozenset({emulator._check_button(button)})
    try:
        emulator.advance(hold_frames)
    finally:
        emulator._pressed = frozenset()
    emulator.advance(release_frames)


def system_directory(emulator):
    """What libretro.py reports back as this session's system directory."""
    for source in (emulator._session, emulator._path_driver):
        value = getattr(source, "system_directory", None) or getattr(
            source, "system_dir", None
        )
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, str):
            return value
    return None


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


def clip_frames_of(payload, Image):
    """Every picture of a clip, as RGB images.

    Opened with Pillow here rather than through the emulator module: nothing
    in the cog reads a clip back any more (that went with the Replay button's
    stitching), so reading one is a test's own business.
    """
    clip = Image.open(io.BytesIO(payload))
    out = []
    for index in range(clip.n_frames):
        clip.seek(index)
        out.append(clip.convert("RGB"))
    return out


def frame_hashes(payload, Image):
    clip = Image.open(io.BytesIO(payload))
    out = []
    for index in range(clip.n_frames):
        clip.seek(index)
        out.append(hashlib.sha1(clip.convert("RGB").tobytes()).hexdigest()[:10])
    return out


# -- 1. Every console boots and records a clip --------------------------------

#: RETRO_MEMORY_SYSTEM_RAM: the console's own work RAM, which is the emulated
#: machine itself rather than a serialization of it. See STATE_SLACK in
#: test_saves_roundtrip.py for why a save state is not the thing to compare.
RETRO_MEMORY_SYSTEM_RAM = 2

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
    clip = emulator.record(frames, presses=[(button, 0, frames_for_ms(emulator, 200))])

    assert clip[:4] == b"RIFF" and clip[8:12] == b"WEBP", clip[:12]
    picture = image.open(io.BytesIO(clip))
    assert getattr(picture, "n_frames", 1) > 1, "the clip is not animated"
    # loop=1 means "play through once and hold the last frame", which is what
    # makes the picture left in the channel the state the next press continues
    # from -- and keeps a busy channel from flickering.
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
    ("gambatte", "gb-rpg.gb"),
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
    presses = [("right", 0, frames_for_ms(emulator, 400))]

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


# -- 1c. Capture and encode are two halves -------------------------------------
#
# record() is record_frames() -- everything that needs the core -- handed to
# encode_clip(), which needs no core at all. The cog serializes every touch of
# the emulator behind one lock, and the encode is the most expensive CPU step
# of a button press, so the split is what lets a caller release the lock
# before encoding. The contract that makes that safe is proved here: the
# captured data is complete (encoding it after the emulator is *stopped* still
# works), the pictures are native-sized (the memory half of the fix), and the
# bytes that come out are the bytes record() has always produced.


def test_record_is_exactly_capture_then_encode(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(2))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    presses = [("right", 0, frames_for_ms(emulator, 160))]
    state = emulator.save_state()

    # Restored before *both* recordings so they start level; see
    # test_a_recorded_clip_is_the_same_whichever_grab_made_it for why.
    emulator.load_state(state)
    whole = emulator.record(frames, presses=presses)

    emulator.load_state(state)
    captured = emulator.record_frames(frames, presses=presses)
    native = emulator._frame_size()
    posted = emulator.output_size()

    # The capture carries everything the encode needs: pictures at the
    # core's own resolution (a Game Boy clip in memory is 160x144 a frame,
    # not 320x288), clip_plan's durations, and the posted size pinned from
    # the first frame.
    assert captured.size == posted != native
    assert all(picture.size == native for picture in captured.images)
    assert captured.durations == [ms for _, ms in E.clip_plan(emulator.fps, frames)]
    assert len(captured.images) == len(captured.durations)

    # ...and the encode is a pure function of it. The emulator is stopped
    # first, which is the proof that no core access hides in encode_clip --
    # i.e. that a caller really can drop the emulator lock between the two
    # halves -- and the bytes are identical to the one-call form, which is
    # what keeps record() an honest thin wrapper.
    emulator.stop()
    assert E.encode_clip(captured) == whole


# -- 2. Frame arithmetic ------------------------------------------------------


def test_frames_are_counted_from_the_core_s_own_frame_rate(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    assert frames_for_ms(emulator, 160) == 10, "~10 frames at 59.7fps"
    assert frames_for_ms(emulator, 400) == 24
    assert frames_for_ms(emulator, 0) == 1, "a hold is never zero frames"
    assert emulator.frames_for_seconds(4) == 239
    assert E.CLIP_SECONDS == 1.0
    # The default hold has to stay under a Game Boy walk cycle (16 frames) or
    # one press walks two tiles; see the walk-cycle test below.
    assert frames_for_ms(emulator, 160) < 16


# The two ends of what `[p]retroset cliplength` accepts are in this list on
# purpose: 0.2 is MIN_CLIP_SECONDS and 5.0 is MAX_CLIP_SECONDS, which came
# down from an unmeasured 15 (see the tables above MAX_CLIP_SECONDS in
# retro/clips.py). A ceiling nothing records at is a ceiling nobody has
# checked, and 5 seconds of Game Boy is 299 frames and 75 pictures.
CLIP_LENGTHS = [0.2, 0.5, 0.8, 1.0, 4.0, 5.0]


@pytest.mark.parametrize("seconds", CLIP_LENGTHS)
def test_a_fractional_clip_length_is_a_real_number_of_frames(
    emu, gambatte, ucity, seconds
):
    """Clip lengths are floats now, and the frames come from the real fps."""
    emulator = emu(gambatte, ucity)
    frames = emulator.clip_frames(seconds)
    assert frames == max(E.MIN_CLIP_FRAMES, round(emulator.fps * seconds))
    assert frames >= E.MIN_CLIP_FRAMES, "a clip is never empty"
    assert emulator.fps != 60, "the frame count must come from the core"

    step = capture_step(emulator)
    budget = input_budget(emulator, frames)
    plan = E.capture_plan(frames, step)
    # Input has to be released on a frame that is actually photographed and
    # worth a whole step of playback, and at least one picture of the clip has
    # to be left over to show it. The cadence is the *end* of each span, so a
    # captured frame is one short of a multiple of the step.
    assert (budget + 1) % step == 0
    assert dict(plan)[budget] == step
    assert 1 <= budget <= frames - 1
    assert len(plan) >= 2, "two pictures is the least that animates"


def test_a_longer_hold_really_does_reach_the_core(emu, gambatte, ucity):
    def screen_after(hold_frames, field="down"):
        emulator = emu(gambatte, ucity)
        emulator.advance(emulator.frames_for_seconds(6))
        press(emulator, "start", hold_frames=frames_for_ms(emulator, 200), release_frames=120)
        press(emulator, field, hold_frames=hold_frames, release_frames=90)
        return hashlib.sha256(emulator.screenshot(scale=1)).hexdigest()[:12]

    assert screen_after(1) != screen_after(60)


def test_one_press_walks_exactly_one_tile(assets, emu):
    """The two-tile bug, measured in a Game Boy RPG's own WRAM.

    The report was that pressing a direction walked the character TWO tiles.
    A Game Boy walk cycle is 16 frames, so any hold that outlasts it starts a
    second step. This drives the real ROM into the overworld and reads the
    player's tile coordinates (wYCoord/wXCoord at 0xD361/0xD362) after a
    press of each length.
    """
    core = assets.need_core("gambatte")
    rom = assets.need_rom("gb-rpg.gb")
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
        hold = min(frames_for_ms(poke, hold_ms), input_budget(poke, frames))
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
    assert tiles(160, input_budget(poke, clip)) == 1, "the step finishes inside the clip"

    # The shortest clip the settings allow still registers the press: a 12
    # frame clip's input budget is frame 11, so the whole 160ms (ten frames)
    # fits and is not cut at all -- it used to be clamped to the 8 frames the
    # old capture cadence could show being released, i.e. ~134ms. A walk cycle
    # is 16 frames and the clip is 12 either way, so the tile it walks to is
    # credited during the *next* clip -- the press is not lost, it lands a
    # clip later, which is the honest cost of a 0.2 second clip.
    short = poke.clip_frames(E.MIN_CLIP_SECONDS)
    assert tiles(160, short) == 0, "unexpectedly quick: recheck the comment above"
    assert tiles(160, short * 2) == 1, "a 0.2s clip lost the press altogether"


# -- 3. Clip timing: what the player actually sees ----------------------------


@pytest.mark.parametrize("seconds", CLIP_LENGTHS)
def test_a_clip_plays_for_as_long_as_it_emulated(emu, gambatte, ucity, seconds):
    """Playback time tracks emulated time at every clip length, whole or not.

    **The rule, stated plainly, because it was nearly weakened:** a clip
    plays for exactly as long as the window it recorded emulated -- start to
    finish, with nothing dropped off either end. Not "about as long", and not
    "as much of it as had something new in it". A second of clip is a second
    of console.

    That was in question because a clip *opens* on pictures where the press
    has not visibly landed yet (the button is held for 160ms and a game reacts
    more slowly still), which reads as the clip showing a moment from before
    the press -- and on a game that sits still until it is prodded, the
    opening picture really is the previous clip's closing picture over again.
    Trimming those opening pictures out of the recording was measured and
    rejected, because on a frozen screen the rule wants to trim the whole clip
    and leaves a 17ms flash. A bounded pre-roll -- playing the press out
    unphotographed in front of the recording -- was then tried and is also
    gone, for a different reason: it put up to sixteen emulated frames into
    the seam between one clip and the next. See section 3a below.

    So the rule stays unqualified and the opening duplicates stay in the clip:
    ``frames`` frames go in and ``frames`` frames' worth of durations come
    out, at every length. The encoder merges a run of identical pictures into
    one stored frame and adds their durations together, so what the player
    sees is a held picture for as long as the game really took, which costs
    the rule nothing. See retro/README.md for the same statement in prose.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(seconds)
    data = emulator.record(frames)
    durations, loop = anmf(data)
    step = capture_step(emulator)

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
        # Every picture stands for the frames ending on it, which is a whole
        # step except for the appended closing one: a 30 frame clip is seven
        # 67ms pictures and then frame 30 for 33ms. Calling that short one a
        # full 67ms played a half-second clip 7% slow.
        expected = [
            max(1, round(1000 * covered / emulator.fps)) for _, covered in plan
        ]
        assert durations == expected, (durations, expected)
        # Only the closing picture can ever be short, which is tighter than
        # it used to be: the old frame-0 cadence left a ragged two-picture
        # tail, so this had to allow for durations[-2] as well.
        assert set(durations[:-1]) <= {frame_ms} or captured <= 1, set(durations)
        assert 0 < durations[-1] <= frame_ms, durations[-1]
    emulated = 1000 * frames / emulator.fps
    assert abs(sum(durations) - emulated) / emulated < 0.01, (sum(durations), emulated)
    # And the cog can work that total out *before* it has a clip, which is
    # what lets it hold the next edit back until this one has played through
    # (see MAX_PACE_SECONDS in retro/RetroView.py). Merged frames or not, the
    # arithmetic and the bytes agree exactly.
    assert sum(durations) == round(1000 * E.playback_seconds(emulator.fps, frames))
    # Every picture of the clip is worth having: a fifth of a second is three
    # of them, not one.
    assert captured >= 2 and len(data) > 0


def test_a_static_screen_collapses_to_a_still_that_is_still_a_clip(
    emu, image, gambatte, ucity
):
    """A short clip of a screen where nothing moves really is one frame.

    libwebp merges identical consecutive frames, and when *every* captured
    picture is the same it writes a plain still WebP with no animation chunks
    in it at all. That is fine -- Discord shows it, and it costs a few dozen
    bytes -- but it means the file no longer says how long the clip was. That
    cost something while the Replay button stitched buffered clips back
    together and had to know how long each one stood for; now that a clip is
    only ever posted on its own, nothing reads a clip's timing back at all.
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
    # Still a picture Discord will show, and still the picture the next press
    # carries on from, which is all a clip has to be.
    assert frame_hashes(data, image) == [frame_hashes(data, image)[0]]


def photographed(emulator, frames, presses=(), clip_fps=E.CLIP_FPS):
    """Record a clip and report the pictures it actually *took*.

    ``record`` is driven for real -- no second copy of its loop here -- and
    :meth:`RetroEmulator._native_frame_image` is wrapped on the way through,
    because it is called exactly once per photographed picture. The encoded
    file cannot answer this on its own: libwebp merges runs of identical
    pictures and adds their durations together, so a clip of a static screen
    comes back as one stored frame however many were captured.

    The hashes are of the *native* frames, which is what a recording now
    stores (the NEAREST enlargement happens once per distinct picture at
    encode time, see encode_clip). That changes nothing about what the
    hashes can say: the enlargement is deterministic and pinned to one size
    per clip, so two frames are equal at native size exactly when they are
    equal posted -- as long as everything compared against them (see
    held_picture) is hashed at native size too.

    Returns ``(clip bytes, one hash per picture)``.
    """
    shots = []
    original = type(emulator)._native_frame_image

    def spy(self):
        picture = original(self)
        shots.append(hashlib.sha1(picture.tobytes()).hexdigest()[:10])
        return picture

    type(emulator)._native_frame_image = spy
    try:
        payload = emulator.record(frames, presses=list(presses), fps=clip_fps)
    finally:
        type(emulator)._native_frame_image = original
    return payload, shots


def held_picture(emulator):
    """The picture the previous clip left standing in the channel.

    Hashed at the core's own resolution, like the shots ``photographed``
    reports, so the two are comparable.
    """
    return hashlib.sha1(emulator._native_frame_image().tobytes()).hexdigest()[:10]


def opening_repeats(shots, held):
    """How many of a clip's opening pictures are the held one, again."""
    count = 0
    for shot in shots:
        if shot != held:
            break
        count += 1
    return count


def test_a_clip_of_a_moving_game_opens_on_a_picture_nobody_has_seen(
    emu, image, gambatte, ucity
):
    """uCity animates every frame, so one frame is enough to move the picture.

    The control for the whole seam section below: on a game that is never
    still, the clip's opening picture -- taken a whole capture step after the
    previous clip's closing one -- is already new, and so is the one after it.
    A game that reacts more slowly is a different matter and is covered
    further down, by the latency probes.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    before = hashlib.sha1(
        image.open(io.BytesIO(emulator.screenshot())).convert("RGB").tobytes()
    ).hexdigest()[:10]

    moved = emulator.record(frames, presses=[("down", 0, frames_for_ms(emulator, 160))])
    hashes = frame_hashes(moved, image)
    assert hashes[0] != before
    assert hashes[0] != hashes[1], "the first two frames are duplicates"


# -- 3a. The seam: where one clip stops and the next one starts ---------------
#
# A picture is taken *after* an emulated frame, so the last picture of a clip
# is its window's last frame and the next clip picks the console up on the
# very next one. That is the whole contract: press, clip, press, clip is an
# unbroken run of console frames with nothing emulated twice and nothing run
# past unaccounted for, and the still left in the channel is exactly where the
# next clip resumes.
#
# The next clip's first *picture* is a capture step later than that, not a
# frame: capture_plan photographs the end of each span so that a press
# scheduled on frame 0 has had time to land. The frames in between are
# emulated and counted in that picture's duration, never skipped -- which is
# the difference between this and the pre-roll below, and which the frame
# counts these tests take are what prove.
#
# It was not always kept. A clip that opened with a press used to run a
# bounded **pre-roll** first -- the press down, the core running,
# unphotographed, until the picture stopped being the one the previous clip
# had left in the channel -- which put between 1 and 16 emulated frames into
# the seam. Measured here on a Raspberry Pi 5, four consecutive one second
# clips per row, the default 160ms hold, the button held from frame 0 of each:
#
#                                    with the pre-roll     without it
#   core / ROM              button    seam      again?    seam    again?
#   gambatte / uCity         down     1,1,16    n,n,y     1,1,1   n,n,y
#   gambatte / Libbet        down    16,1,16    y,n,y     1,1,1   y,y,y
#   fceumm / nestest         start   16,16,16   y,y,y     1,1,1   y,y,y
#   fceumm / nestest         down     2,2,2     n,n,n     1,1,1   y,y,y
#   gambatte / dmg-acid2     a       16,16,16   y,y,y     1,1,1   y,y,y
#   ...any of the above, with no press at all   1,1,1     1,1,1
#
# ("seam" is emulated frames between one clip's last picture and the next
# clip's first; "again?" is whether that first picture is byte-identical to
# the last one.) The seam was ``1 + the frames the pre-roll used``, exactly,
# in every row -- and on the three static rows, which are what this cog is
# played on, the pre-roll ran its whole 15 frame bound, opened the clip on the
# repeated picture anyway and charged 251ms of game time a press for it. So it
# is gone; see the seam block in retro/clips.py for the reasoning and for the
# trade that replaces it (a game's own reaction latency is now shown as a held
# opening picture rather than skipped over).
#
# What the tests below pin, against real cores:
#
# * the windows are adjacent, with a press and without one, and the pictures
#   keep a uniform step across the boundary;
# * a game that is moving never repeats a picture across it, and a game that
#   answers within a step of the press does not either;
# * a game that is *not* moving repeats it and still gets a full length clip,
#   which is the case a lead-in trim would have turned into a 17ms flash;
# * the clip's own frame rate cannot move any of this.

#: Probes for the seam, one per shape of game: (core, ROM, button, boot
#: seconds, whether the game moves by itself).
#:
#: Libbet is the reported shape and the reason it is in tests/fetch_assets.py:
#: a Game Boy screen that sits completely still until it is prodded, like an
#: overworld or a menu. uCity animates constantly and dmg-acid2 never moves at
#: all, so between them the three cover both ends. See LIBBET_BOOT_SECONDS in
#: conftest.py.
#:
#: Libbet is driven with `down`, which it ignores, rather than with `a`:
#: pressing A on its title screen makes gambatte *dupe* a frame (video_refresh
#: with a NULL framebuffer, meaning "draw the last one again") and
#: libretro.py 0.11+ raises a TypeError out of its own environment callback
#: when it sees one, so the recording comes back as "the core crashed while
#: running". libretro.py 0.6.0 handles the same frame fine, so that is an
#: upstream regression and nothing to do with the seam.
SEAM_PROBES = {
    "ucity": ("gambatte", "ucity.gbc", "down", 3, True),
    "libbet": ("gambatte", "libbet.gb", "down", LIBBET_BOOT_SECONDS, False),
    "nestest": ("fceumm", "nestest.nes", "start", 3, False),
    "nestest-down": ("fceumm", "nestest.nes", "down", 3, False),
    "dmg-acid2": ("gambatte", "dmg-acid2.gb", "a", 3, False),
    # Not in test-assets/ on the machine this was measured on, so they skip
    # here; they are the two consoles whose reaction to a press is slowest
    # (3 and 10 frames, which is what the pre-roll's bound was sized from) and
    # so the two most likely to expose a seam that is off by a frame.
    "snes": ("snes9x", "snes_rotzoom.sfc", "a", 3, False),
    "snes-title": ("snes9x", "snes_rotzoom.sfc", "start", 3, False),
    "gba": ("mgba", "measure_gba.gba", "a", 3, False),
}


@contextlib.contextmanager
def frames_emulated(emulator):
    """Collect what was held on each emulated frame, for the block's duration.

    ``record`` is the only thing that advances a session, so the length of
    the list this yields is exactly the window a recording ran through --
    which is "the console does not get ahead of the pictures" stated in a way
    that a frozen screen cannot satisfy by accident, since it counts frames
    rather than comparing pictures.
    """
    timeline = []
    original = type(emulator).advance

    def spy(self, count=1):
        for _ in range(max(0, int(count))):
            timeline.append(frozenset(self._pressed))
            original(self, 1)

    type(emulator).advance = spy
    try:
        yield timeline
    finally:
        type(emulator).advance = original


def frame_by_frame(emulator, schedule, button, total):
    """Emulate ``total`` frames one at a time and hash every one.

    ``schedule`` is the set of frame numbers the button is held on, so the
    core sees exactly the input the recordings gave it and the pictures are
    comparable. Native hashes, like ``photographed``'s.
    """
    reference = []
    try:
        for index in range(total):
            emulator._pressed = (
                frozenset({button}) if index in schedule else frozenset()
            )
            emulator.advance(1)
            reference.append(
                hashlib.sha1(emulator._native_frame_image().tobytes()).hexdigest()[:10]
            )
    finally:
        emulator._pressed = frozenset()
    return reference


@pytest.mark.parametrize("probe", sorted(SEAM_PROBES))
@pytest.mark.parametrize("pressed", [True, False], ids=["press", "wait"])
def test_the_seam_between_two_clips_is_exactly_one_emulated_frame(
    assets, emu, probe, pressed
):
    """The report, answered: "new clips start a frame after the previous one".

    Two clips back to back, then the same window rewound and emulated one
    frame at a time with exactly the same input, for a reference picture per
    absolute frame. The first clip has to finish on reference frame N, so the
    still picture left in the channel is the state the second clip picks the
    console up on; the second clip's own window is N+1 onwards, and every
    picture of both clips has to be its reference frame -- nothing repeated,
    nothing skipped -- whether the clips have a press in them or not, and
    whether the game is animating or sitting still.

    The second clip's first *picture* is a step past N rather than N+1,
    because capture_plan photographs the end of each span (which is what gives
    a press at frame 0 time to land). Frames N+1 .. N+step-1 are emulated all
    the same and stand behind that picture's duration; the `timeline` count
    below is what proves it.
    """
    core, rom_name, button, boot, _moves = SEAM_PROBES[probe]
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(boot))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    step = capture_step(emulator)
    hold = frames_for_ms(emulator, 160)
    presses = [(button, 0, hold)] if pressed else []
    captured = len(E.capture_plan(frames, step))
    state = emulator.save_state()

    emulator.load_state(state)
    with frames_emulated(emulator) as timeline:
        first, first_shots = photographed(emulator, frames, presses)
        second, second_shots = photographed(emulator, frames, presses)

    # Two clips, two windows, and not one frame more: whatever the pictures
    # turn out to say, the console cannot have run on past them. This is the
    # half a frozen screen would otherwise satisfy by accident, because every
    # one of its pictures matches every other.
    assert len(timeline) == 2 * frames, (len(timeline), frames)

    # The same two windows again, a frame at a time. Both clips are emulated
    # for exactly `frames` frames -- there is no pre-roll to account for any
    # more -- so the press schedule is frames 0..hold-1 of each of them.
    emulator.load_state(state)
    schedule = (
        set(range(hold)) | {frames + index for index in range(hold)}
        if pressed
        else set()
    )
    reference = frame_by_frame(emulator, schedule, button, 2 * frames)

    assert first_shots[-1] == reference[frames - 1], (
        "the first clip does not finish on its own last emulated frame"
    )
    plan = [index for index, _ in E.capture_plan(frames, step)]
    assert second_shots[0] == reference[frames + plan[0]], (
        "the second clip does not open on the end of its first span"
    )
    assert plan[0] == step - 1, "the opening picture is not a whole step in"
    # ...and it opens on that frame rather than merely matching it by
    # accident on a screen where several frames look alike: the whole of both
    # clips is the reference run, in order, with nothing missing.
    assert first_shots == [reference[index] for index in plan]
    assert second_shots == [reference[frames + index] for index in plan]
    # Neither clip lost a picture to any of this, and both are still clips.
    assert len(first_shots) == len(second_shots) == captured
    assert len(first) > 0 and len(second) > 0


@pytest.mark.parametrize("clip_fps", [10, 15, 20, 60])
def test_the_clip_frame_rate_does_not_move_the_seam(emu, gambatte, ucity, clip_fps):
    """"Is there something we can do in the framerate to fix it?" -- no.

    capture_plan photographs the window's final frame at every cadence, so the
    picture left in the channel is the state the next clip resumes from at 10,
    15, 20 and 60 fps alike, and the next window still begins on the very next
    emulated frame. uCity is the probe because it moves every frame, so an
    off-by-one at either end would show up as a picture that does not match
    its reference.

    What the rate *does* change is how many pictures fill a clip and, with
    them, how far into its window the opening picture is taken -- one step,
    which at 60 fps is a single frame. That is the wrong direction for the
    reaction-latency problem this cog actually had, and it is why the answer
    was the sampling phase rather than CLIP_FPS.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    step = E.capture_step(emulator.fps, clip_fps)
    state = emulator.save_state()

    emulator.load_state(state)
    _first, first_shots = photographed(emulator, frames, clip_fps=clip_fps)
    _second, second_shots = photographed(emulator, frames, clip_fps=clip_fps)

    emulator.load_state(state)
    reference = frame_by_frame(emulator, set(), "a", frames + step)

    assert first_shots[-1] == reference[frames - 1]
    assert second_shots[0] == reference[frames + step - 1]
    # The cadence really did change, or the above says nothing about it.
    assert len(first_shots) == len(E.capture_plan(frames, step))
    assert reference[frames - 1] != reference[frames], "nothing moved at all"


@pytest.mark.parametrize(
    "probe", [name for name, row in sorted(SEAM_PROBES.items()) if row[4]]
)
def test_a_moving_game_never_repeats_a_picture_across_the_seam(assets, emu, probe):
    """The half of the report that is about pictures rather than frames.

    On a game that is actually moving, "one emulated frame later" and "a
    picture nobody has seen" are the same thing, so the clip cannot open by
    re-showing what the player is already looking at. (On a game that is *not*
    moving they are not the same thing, and the next test is that case.)
    """
    core, rom_name, button, boot, _moves = SEAM_PROBES[probe]
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(boot))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    presses = [(button, 0, frames_for_ms(emulator, 160))]

    _first, first_shots = photographed(emulator, frames, presses)
    _second, second_shots = photographed(emulator, frames, presses)

    assert second_shots[0] != first_shots[-1], "the seam repeats a picture"
    # ...and the clip goes on moving rather than the seam happening to differ
    # once: the second clip is not one picture over and over.
    assert len(set(second_shots)) > 1, "the game stopped moving; recheck the probe"


@pytest.mark.parametrize(
    "probe", ["dmg-acid2", "libbet", "nestest", "snes-title"],
)
def test_a_completely_static_screen_still_produces_a_whole_clip(assets, emu, probe):
    """The trade the seam is bought with, and the flash it must not become.

    Three shapes of "nothing happens": a ROM that draws one picture and holds
    it for ever (dmg-acid2), a game sitting on a static screen with a button
    it ignores pressed (Libbet and the d-pad -- the paused-game case, and the
    likeliest of them in real play), a menu that has already answered the
    button it is being given (nestest and Start), and a static title screen on
    another console.

    On all of them *every* captured picture is the pre-press one. That is the
    honest reading -- the game has not moved -- and it is what the seam costs:
    the pre-roll used to skip forward looking for a change, which on these
    rows it never found, so it spent a quarter of a second of game time and
    opened the clip on the repeated picture regardless.

    What must not happen is the other failure: a rule that trimmed the opening
    duplicates would trim the entire clip and leave the single picture it is
    obliged to keep, i.e. a one second clip played as a 17ms flash. So this
    pins the full length -- every picture the plan asked for, durations adding
    up to the whole second that was emulated.
    """
    core, rom_name, button, boot, _moves = SEAM_PROBES[probe]
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(boot))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    presses = [(button, 0, frames_for_ms(emulator, 160))]
    plan = E.capture_plan(frames, capture_step(emulator))

    # One press first, so the screen is where a *second* press finds it. That
    # is the shape of the complaint -- the player is already on the menu, or
    # the cursor is already where Start put it -- and it is the difference
    # between nestest answering the button and nestest ignoring it.
    emulator.record(frames, presses=presses)
    held = held_picture(emulator)
    payload, shots = photographed(emulator, frames, presses)

    if opening_repeats(shots, held) != len(shots):
        pytest.skip(
            f"{core}/{rom_name} is not static under this build "
            f"({opening_repeats(shots, held)} of {len(shots)} pictures "
            "unchanged); the all-static case needs a screen that really does "
            "hold one frame"
        )

    # Not a flash: every picture the plan asked for was taken, and the
    # durations handed to the encoder still add up to the second that was
    # emulated. (The encoder is free to merge them into one stored frame, and
    # does -- see test_a_static_screen_collapses_to_a_still_that_is_still_a_clip.)
    assert len(shots) == len(plan) >= 2
    durations = [max(1, round(1000 * covered / emulator.fps)) for _, covered in plan]
    emulated = 1000 * frames / emulator.fps
    assert abs(sum(durations) - emulated) / emulated < 0.01
    assert sum(durations) == round(1000 * E.playback_seconds(emulator.fps, frames))
    # ...and it is still a clip, not an error.
    assert len(payload) > 0


#: Probes for reaction latency: (core, ROM, button, boot seconds, the first
#: emulated frame after the button goes down whose picture differs at all).
#:
#: Measured on a Raspberry Pi 5 with the button held from frame 0, after one
#: press has already been made so the screen is where a *second* press finds
#: it. These are what chose the capture phase: capture_plan takes its first
#: picture at the end of the first span, i.e. after ``step`` emulated frames
#: (four at the default CLIP_FPS), so every core whose latency is at or under
#: the step opens its clip on the game already reacting.
#:
#: mgba is the exception and is in the table to say so honestly: eleven frames
#: is nearly three pictures, so a GBA clip can still open on one or two copies
#: of the held picture. No sampling phase can fix that without skipping game
#: time, which is what the pre-roll did and why it is gone.
PRESS_LATENCY = {
    "ucity": ("gambatte", "ucity.gbc", "down", 3, 1),
    "nestest-down": ("fceumm", "nestest.nes", "down", 3, 2),
    "snes": ("snes9x", "snes_rotzoom.sfc", "a", 3, 4),
    "gba": ("mgba", "measure_gba.gba", "a", 3, 11),
}


@pytest.mark.parametrize("probe", sorted(PRESS_LATENCY))
def test_a_clip_opens_on_the_game_already_reacting_to_the_press(assets, emu, probe):
    """The report, and the fix: "it still looks like it starts before I moved".

    A clip's opening picture used to be taken on frame 0 of its window -- one
    emulated frame after the button went down, which on every core but an
    already-animating one is a frame on which nothing has happened yet. So the
    new clip opened by re-showing the still the previous clip had left in the
    channel, for a whole step of playback, on every single press.

    capture_plan now photographs the *end* of the first span instead, giving
    the console ``step`` frames to answer, and this is that stated against the
    real cores: record a clip with a press in it, then a second one the same
    way, and require the second clip's first picture to differ from the
    picture the first one finished on. The latency is measured off the core
    here rather than trusted from the table, so a core build that reacts
    differently says so instead of quietly passing.

    Cores slower than the step are excluded rather than asserted -- mgba's
    eleven frames is nearly three pictures, and one repeated opening picture
    remains possible there. That is the game's own latency and showing it is
    the honest answer; see the seam block in retro/clips.py.
    """
    core, rom_name, button, boot, table_latency = PRESS_LATENCY[probe]
    emulator = emu(assets.need_core(core), assets.need_rom(rom_name))
    emulator.advance(emulator.frames_for_seconds(boot))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    step = capture_step(emulator)
    hold = frames_for_ms(emulator, 160)
    presses = [(button, 0, hold)]
    plan = [index for index, _ in E.capture_plan(frames, step)]

    # One press first, so the screen is where a *second* press finds it, and
    # so "the picture the previous clip left in the channel" is a real one.
    _first, first_shots = photographed(emulator, frames, presses)
    held = first_shots[-1]
    state = emulator.save_state()

    # How long this core really takes to answer, read off it a frame at a
    # time with exactly the input the recording gives it.
    emulator.load_state(state)
    reference = frame_by_frame(emulator, set(range(hold)), button, frames)
    latency = opening_repeats(reference, held) + 1

    emulator.load_state(state)
    _second, shots = photographed(emulator, frames, presses)

    # The clip really is the reference run sampled at the plan's frames, so
    # everything below is about *which* frames rather than about luck.
    assert shots == [reference[index] for index in plan]

    if latency != table_latency:
        pytest.skip(
            f"{core}/{rom_name} answers {button} on frame {latency} under this "
            f"build, not the {table_latency} the table was measured at; "
            "re-measure PRESS_LATENCY rather than loosening this"
        )
    if latency > step:
        pytest.skip(
            f"{core}/{rom_name} takes {latency} frames to react and a picture "
            f"is only {step}, so an opening repeat is expected and honest here"
        )

    # The fix, in one line: the first thing the player sees is not the thing
    # they were already looking at.
    assert shots[0] != held, "the clip opens on the previous clip's last picture"
    assert opening_repeats(shots, held) == 0
    # ...and it is the sampling phase that did it, not luck about this core:
    # the opening picture is a whole step into the window.
    assert plan[0] == step - 1, "the opening picture is not a whole step in"


def test_a_games_own_reaction_latency_is_shown_rather_than_skipped(
    assets, emu, libbet
):
    """What replaced the pre-roll, stated as the thing a player will see.

    Libbet's title screen answers Start on its third frame. With the pre-roll
    the clip skipped the two frames before that and opened on the change; now
    the clip's window starts on the frame after the previous one ended and the
    opening picture is taken at the end of the first span, so those two frames
    are emulated, counted, and shown -- either inside the opening picture's
    duration (they are, at the default step of four) or as a held picture if
    the step is shorter than the latency.

    Either way the count is exact: the clip opens on the held picture for
    precisely the pictures whose frames fall before the game answers, no more
    (nothing is dragged out) and no fewer (nothing is skipped to hide it).
    """
    emulator = emu(assets.need_core("gambatte"), libbet)
    emulator.advance(emulator.frames_for_seconds(LIBBET_BOOT_SECONDS))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    step = capture_step(emulator)
    hold = frames_for_ms(emulator, 160)
    presses = [("start", 0, hold)]

    held = held_picture(emulator)
    state = emulator.save_state()
    emulator.load_state(state)
    payload, shots = photographed(emulator, frames, presses)

    # How long the game really takes, read off the core rather than assumed.
    emulator.load_state(state)
    reference = frame_by_frame(emulator, set(range(hold)), "start", frames)
    latency = opening_repeats(reference, held)
    if not 0 < latency < frames:
        pytest.skip(
            f"Libbet answers Start after {latency} of {frames} frames under "
            "this build, which is not the delayed-reaction shape this pins"
        )

    # The clip opens on the held picture for exactly that many frames' worth
    # of pictures -- no more (nothing is dragged out) and no fewer (nothing is
    # skipped to hide it).
    assert opening_repeats(shots, held) == len(
        [index for index, _ in E.capture_plan(frames, step) if index < latency]
    )
    # And the clip moves on afterwards rather than being a still, so the
    # latency really is being shown and then left behind.
    assert shots[-1] != held
    assert len(shots) == len(E.capture_plan(frames, step))
    assert len(payload) > 0


def test_the_hold_is_honoured_in_full_and_released_before_the_last_picture(
    assets, emu, libbet
):
    """The press schedule, read off the frames the core really saw.

    The button goes down before the clip's first emulated frame and comes up
    ``hold`` frames later, and ``input_budget`` promises that is before the
    last picture worth a whole step of playback -- so the clip's closing
    pictures show what the press *did* rather than the game still under it.

    This used to have to account for a pre-roll shifting the recording later
    while the press stayed put. Now the clip's frames and the window's frames
    are the same frames, which is what makes the promise the schedule's own
    arithmetic rather than something the recording loop has to preserve.
    """
    emulator = emu(assets.need_core("gambatte"), libbet)
    emulator.advance(emulator.frames_for_seconds(LIBBET_BOOT_SECONDS))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    hold = frames_for_ms(emulator, 160)
    budget = input_budget(emulator, frames)

    with frames_emulated(emulator) as timeline:
        emulator.record(frames, presses=[("a", 0, hold)])

    # The window is the clip and nothing else: no frame is emulated that the
    # clip does not account for.
    assert len(timeline) == frames
    down = [index for index, pressed in enumerate(timeline) if "a" in pressed]
    assert down == list(range(hold)), (down[:4], down[-4:], hold)
    assert max(down) < budget <= frames - 1
    assert not timeline[-1], "the clip's last frame is emulated with nothing held"


def test_a_clip_with_no_input_in_it_covers_its_window_from_the_first_frame(
    assets, emu, libbet
):
    """Wait, Undo, a boot and a reset: the same seam as everything else.

    A recording with no schedule in it emulates its window from frame 0 and
    accounts for every frame of it, which was already true before the capture
    phase moved -- it is the clips *with* a press that the phase is about.
    Libbet on its static screen is the probe that would notice a stray skip:
    it is the one ROM here where something waiting for the picture to change
    would run for ever.
    """
    emulator = emu(assets.need_core("gambatte"), libbet)
    emulator.advance(emulator.frames_for_seconds(LIBBET_BOOT_SECONDS))
    frames = emulator.clip_frames(E.CLIP_SECONDS)

    held = held_picture(emulator)
    with frames_emulated(emulator) as timeline:
        waited, shots = photographed(emulator, frames)

    assert len(timeline) == frames, "a clip with no press in it ran extra frames"
    assert not any(timeline), "a clip with no press in it held a button"
    # The screen is frozen, so the Wait clip is the held picture for its whole
    # length -- which is the truth about a paused game and what Wait is for.
    assert opening_repeats(shots, held) == len(shots)
    assert len(shots) == len(E.capture_plan(frames, capture_step(emulator)))
    assert len(waited) > 0


def test_one_clip_carries_on_from_the_last_with_no_frames_lost(emu, image, gambatte, ucity):
    """The other half of "seamless movement", proved against a real core.

    A clip's pictures are the end of every ``step``-frame span *plus the last
    frame of the window* (see capture_plan), so the picture a clip finishes on
    -- and holds, since clips play through once -- is the exact state the next
    clip starts from. Before the closing frame was photographed a 60 frame
    clip stopped on frame 57 while 58, 59 and 60 were emulated and never
    shown, so the still picture sitting in the channel between presses was
    three frames behind the console.

    So: record two consecutive clips, then rewind and emulate the same frames
    one at a time to get a reference picture for each. The first clip's last
    picture has to be reference frame N -- the frame the next window picks up
    from -- and the second clip's first picture reference frame N + step,
    since it is taken at the end of that window's first span. Nothing in
    between is repeated or skipped; it is emulated and counted in that
    picture's duration.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    frames = emulator.clip_frames(E.CLIP_SECONDS)
    step = capture_step(emulator)
    size = emulator.output_size()
    state = emulator.save_state()

    # load_state runs a frame of its own, so both the recordings and the
    # reference below start from the same place: the state plus one frame.
    emulator.load_state(state)
    first = clip_frames_of(emulator.record(frames), image)
    second = clip_frames_of(emulator.record(frames), image)

    emulator.load_state(state)
    reference = []
    for _ in range(frames + step):
        emulator.advance(1)
        reference.append(emulator._frame_image(size).tobytes())

    assert first[-1].tobytes() == reference[frames - 1], (
        "the first clip does not end on its own last emulated frame"
    )
    assert second[0].tobytes() == reference[frames + step - 1], (
        "the second clip does not open on the end of its own first span"
    )
    # And the pictures really are different, or none of the above means
    # anything: a static screen would satisfy it by accident.
    assert reference[frames - 1] != reference[frames], "nothing moved at all"
    assert reference[frames - 1] != reference[frames + step - 1]


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


# -- 5. Save states -----------------------------------------------------------


def test_a_save_state_replays_identically_in_a_fresh_instance(emu, gambatte, ucity):
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(4))
    state = emulator.save_state()

    def replay(target, first_release):
        press(target, "down", hold_frames=24, release_frames=first_release)
        press(target, "a", hold_frames=12, release_frames=119)
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

    assert system_directory(emulator) == str(system_dir)
    assert system_dir.is_dir()
    # libretro.py 0.6.x's path driver needs all four of these to exist.
    assert all((root / name).is_dir() for name in ("assets", "save", "playlist"))
    # Left alone, libretro.py hands every core a throwaway temp directory.
    assert "libretro.py-" not in (system_directory(emulator) or "")


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


# -- 12. Resetting a running core ----------------------------------------------
#
# libretro's retro_reset, i.e. the power switch, which is what `[p]retroreset`
# goes through. Two halves to it, and both of them are claims about a real
# machine rather than about our code: the console goes back to its boot state,
# and the cartridge's battery memory does not.


#: How many bytes of a Game Boy's 32 KiB of work RAM may differ between a
#: machine that was reset and one that cold-booted.
#:
#: Not zero, and that is the hardware rather than a bug: ``retro_reset`` is
#: the reset line, not the power rail, and a Game Boy's work RAM is not
#: cleared by it -- so any byte the boot code does not write keeps whatever
#: the game that was running left there. Measured on uCity under Gambatte:
#: five bytes of 32,768 (indices 1022, 1347-1348 and 4078-4079), against 147
#: that differ between mid-play and a boot. So the difference this allows is
#: thirty times smaller than the difference it is distinguishing, the reset
#: is *stable* (resetting twice gives the same RAM byte for byte), and the
#: picture -- which is the thing a player sees -- is identical.
RESET_RAM_SLACK = 16


def work_ram(emulator):
    """The console's own RAM: the emulated machine, not a serialization of it."""
    memory = emulator._session.core.get_memory(RETRO_MEMORY_SYSTEM_RAM)
    assert memory is not None, "this core exposes no system RAM"
    return bytes(memory)


def differing_bytes(left, right):
    assert len(left) == len(right), (len(left), len(right))
    return sum(1 for a, b in zip(left, right, strict=True) if a != b)


def test_a_reset_puts_a_real_machine_back_to_its_boot_state(emu, gambatte, ucity):
    """The claim `[p]retroreset` rests on, against the core itself.

    Held to the machine rather than to the bytes of a save state: see the
    STATE_SLACK note in test_saves_roundtrip.py for why a state is not quite
    a pure function of the emulated console. Work RAM is, bar the bytes a
    reset leaves alone; see RESET_RAM_SLACK.
    """
    emulator = emu(gambatte, ucity)
    boot_frames = emulator.frames_for_seconds(3)
    emulator.advance(boot_frames)
    booted = work_ram(emulator)

    # Play for a while, so there is something to lose.
    for _ in range(4):
        emulator.record(emulator.clip_frames(0.4), presses=[("start", 0, 8)])
    mid_game = work_ram(emulator)
    moved = differing_bytes(mid_game, booted)
    assert moved > RESET_RAM_SLACK * 4, (
        f"the game only moved {moved} bytes on, so this proves nothing"
    )

    emulator.reset()
    emulator.advance(boot_frames)
    after = work_ram(emulator)

    # Back at the boot, and a long way from where the play had reached.
    assert differing_bytes(after, booted) <= RESET_RAM_SLACK
    assert differing_bytes(after, mid_game) > RESET_RAM_SLACK * 4

    # And it is stable: a second reset lands on exactly the same machine, so
    # the bytes above are RAM a reset does not touch rather than noise.
    emulator.reset()
    emulator.advance(boot_frames)
    assert work_ram(emulator) == after


def test_a_reset_matches_a_freshly_loaded_core_and_its_picture(
    emu, image, gambatte, ucity
):
    """The same claim from the other side: a reset really is a new boot.

    The reference is a second instance of the same core loading the same ROM
    from scratch, which is the most that can be asked of "as if you had
    flipped the power switch". The *picture* is byte-identical -- it is what
    the player is shown, and it is what the cog posts -- and the machine is
    compared as a distance rather than for equality, because the handful of
    work RAM bytes a reset leaves behind (RESET_RAM_SLACK) are exactly the
    ones that carry whatever the previous game happened to write there, so
    how many of them differ depends on how much was played.
    """
    emulator = emu(gambatte, ucity)
    boot_frames = emulator.frames_for_seconds(3)
    emulator.advance(boot_frames)
    for _ in range(3):
        emulator.record(emulator.clip_frames(0.4), presses=[("a", 0, 8)])
    played = frame_hashes(emulator.record(emulator.clip_frames(0.4)), image)[-1]
    played_machine = work_ram(emulator)

    emulator.reset()
    emulator.advance(boot_frames)
    reset_picture = frame_hashes(emulator.record(emulator.clip_frames(0.4)), image)[-1]
    reset_machine = work_ram(emulator)

    # A brand new core, the same ROM, the same boot: one core at a time, so
    # the one above is stopped by the `emu` fixture building this one.
    fresh = emu(gambatte, ucity)
    fresh.advance(boot_frames)
    fresh_picture = frame_hashes(fresh.record(fresh.clip_frames(0.4)), image)[-1]
    fresh_machine = work_ram(fresh)

    assert reset_picture != played, "the reset left the game mid-play"
    assert reset_picture == fresh_picture, "a reset is not a fresh boot"
    # An order of magnitude closer to a fresh boot than the play it replaced:
    # about 20 bytes of 32,768 against about 150.
    near = differing_bytes(reset_machine, fresh_machine)
    far = differing_bytes(played_machine, fresh_machine)
    assert near * 5 < far, (near, far)


def test_a_reset_keeps_the_cartridge_s_battery_save(emu, gambatte, ucity):
    """Resetting a console has never wiped a save file, and this must not.

    ``retro_reset`` does not reallocate the RETRO_MEMORY_SAVE_RAM region, so
    the player's own in-game save survives -- which is the whole distinction
    between `[p]retroreset` and `[p]retrosaves delete`.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(2))
    saved = bytes((index * 7 + 3) % 251 for index in range(emulator.sram_size))
    assert saved, "uCity's cartridge reports no battery at all"
    assert emulator.load_sram(saved) is True

    emulator.reset()
    emulator.advance(emulator.frames_for_seconds(1))

    assert emulator.save_sram() == saved, "the reset wiped the in-game save"
    assert emulator.sram_size == len(saved)


def test_resetting_a_core_that_is_not_running_is_refused(emu, gambatte, ucity):
    emulator = E.RetroEmulator(gambatte, ucity)
    with pytest.raises(E.EmulatorError, match="not running"):
        emulator.reset()


def test_a_clip_recorded_straight_after_a_reset_is_not_a_stale_frame(
    emu, image, gambatte, ucity
):
    """reset() runs a frame, for the same reason load_state() does.

    Without it the video driver still holds the picture from before the
    reset, and the clip the channel is shown would open on the game that was
    just thrown away.
    """
    emulator = emu(gambatte, ucity)
    emulator.advance(emulator.frames_for_seconds(3))
    for _ in range(3):
        emulator.record(emulator.clip_frames(0.4), presses=[("start", 0, 8)])
    before = frame_hashes(emulator.screenshot(), image)[0]

    emulator.reset()
    opening = frame_hashes(emulator.record(emulator.clip_frames(0.4)), image)[0]
    assert opening != before, "the clip opened on the picture from before the reset"
