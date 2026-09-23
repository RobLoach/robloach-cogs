"""The clip arithmetic, with nothing installed at all.

``retro/clips.py`` imports the standard library and (lazily) Pillow, so the
two things that decide what a clip looks like can be covered here without
discord.py, without Red and without a libretro core:

* :func:`capture_plan` -- which emulated frames become pictures and how long
  each picture stands, which is what makes one clip carry on from the last
  with no frames lost in between;
* :func:`clip_plan` and :func:`playback_seconds` -- the same plan in
  milliseconds, i.e. how long the clip really plays for. That is the number
  the cog paces its message edits against, and it has to be knowable before
  there is a clip to measure;
* :func:`clip_size` -- how big the posted picture is, which is where the
  per-console upscale was measured away;
* :func:`encode_clip` -- the no-core half of recording, which turns the
  native-size pictures ``record_frames`` captured into the posted bytes.
  Those tests are the one part of this file that needs Pillow, and they skip
  without it rather than costing the rest of the file its
  nothing-installed-at-all property.

tests/test_emulator.py holds the same two statements against real cores:
continuity is proved there by recording two consecutive clips off a Game Boy
and comparing them with the frames emulated in between, and the geometry by
asserting a real Game Boy screenshot is still exactly 320x288.
"""

import io
import sys

import pytest

from .loader import load_standalone

C = load_standalone("retro_clips_standalone", "clips.py")

#: Every console's real frame rate, so the arithmetic under test is the
#: arithmetic that runs in production rather than a tidy 60.
FPS = {"gb": 59.727, "nes": 60.0988, "pal": 50.0}


# -- Which frames become pictures ---------------------------------------------


def test_a_one_second_game_boy_clip_is_this_exact_shape():
    # 60 emulated frames at a capture step of 4: fifteen pictures on the
    # cadence (frames 1, 5, ... 57) plus the closing frame 60, which is the
    # one the clip holds and the one the next clip carries on from.
    frames = C.clip_frame_count(FPS["gb"], 1.0)
    step = C.capture_step(FPS["gb"])
    assert (frames, step) == (60, 4)

    plan = C.capture_plan(frames, step)
    indices = [index for index, _ in plan]
    covered = [amount for _, amount in plan]
    assert indices == [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 59]
    assert covered == [4] * 14 + [3, 1]
    assert sum(covered) == frames


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize(
    "seconds", [0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 1.3, 2.0, 4.0, 5.0]
)
@pytest.mark.parametrize("clip_fps", [10, 15, 20])
def test_a_capture_plan_always_holds_both_ends_and_all_the_time(fps, seconds, clip_fps):
    """The three invariants every clip length has to satisfy."""
    frames = C.clip_frame_count(fps, seconds)
    step = C.capture_step(fps, clip_fps)
    plan = C.capture_plan(frames, step)
    indices = [index for index, _ in plan]
    covered = [amount for _, amount in plan]

    # 1. Both ends are photographed. Frame 0 so a press scheduled at the
    #    start of the clip is already in the first picture, and the last
    #    frame so the picture the clip holds is where the next one begins.
    assert indices[0] == 0
    assert indices[-1] == frames - 1

    # 2. No picture is taken twice and none is skipped over.
    assert indices == sorted(set(indices))
    assert all(amount >= 1 for amount in covered)

    # 3. The durations account for every emulated frame, so the clip plays
    #    for exactly as long as it emulated.
    assert sum(covered) == frames

    # ...and everything between the ends is on the regular cadence, so the
    # timing is uniform apart from the tail.
    assert all(index % step == 0 for index in indices[:-1])
    assert all(amount == step for amount in covered[:-2]) or len(covered) <= 2


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0])
def test_consecutive_clips_leave_no_frame_unphotographed(fps, seconds):
    """Clip boundaries are seamless, in absolute emulated frames.

    A picture taken on a clip's frame ``i`` shows the console after ``i + 1``
    emulated frames, so laying two clips end to end and translating both
    plans into absolute frame numbers has to give one unbroken run: the last
    picture of the first clip and the first picture of the second must be
    adjacent frames, neither repeated nor skipped.

    Before the closing frame was always photographed this failed by up to
    ``step - 1`` frames: a 60 frame clip stopped on frame 57 and the next one
    opened on frame 61, so the still picture sitting in the channel between
    presses was three frames behind the console.
    """
    frames = C.clip_frame_count(fps, seconds)
    step = C.capture_step(fps)
    plan = C.capture_plan(frames, step)

    first = [index + 1 for index, _ in plan]
    second = [frames + index + 1 for index, _ in plan]
    assert second[0] == first[-1] + 1, (first[-3:], second[:3])
    assert first[-1] == frames, "the first clip must end on its own last frame"


@pytest.mark.parametrize("frames", [1, 2, 3, 5, 6, 7, 12, 13, 60, 61, 239])
@pytest.mark.parametrize("step", [1, 2, 3, 4, 5, 6])
def test_a_capture_plan_survives_any_frame_count(frames, step):
    plan = C.capture_plan(frames, step)
    assert plan, (frames, step)
    assert plan[0][0] == 0
    assert plan[-1][0] == frames - 1
    assert sum(amount for _, amount in plan) == frames
    assert len(plan) <= frames, "a picture per frame at most"


def test_a_frame_count_already_on_the_cadence_adds_no_extra_picture():
    # k * step + 1 frames: the last frame is already on the cadence, so every
    # picture but the closing one is a whole step and nothing is appended.
    plan = C.capture_plan(61, 4)
    assert [index for index, _ in plan] == list(range(0, 61, 4))
    assert [amount for _, amount in plan] == [4] * 15 + [1]


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0, 5.0])
def test_input_is_released_before_the_last_picture_is_taken(fps, seconds):
    """What input_budget promises, restated against the capture plan.

    The budget is the last frame on the regular cadence, so it is *before*
    the closing photograph as well: a press released on it is up for both of
    the clip's final pictures.
    """
    frames = C.clip_frame_count(fps, seconds)
    step = C.capture_step(fps)
    budget = C.input_budget(fps, frames)
    indices = [index for index, _ in C.capture_plan(frames, step)]

    assert budget in indices, (budget, indices[-4:])
    assert budget <= indices[-1]
    assert budget % step == 0
    assert budget < frames


# -- How long a clip plays for ------------------------------------------------
#
# The same plan in milliseconds, which is what the encoder is handed and
# therefore what ends up in the clip's ANMF chunks. It is also what the cog
# paces its message edits against -- a clip is not replaced until it has had
# this long on screen (see MAX_PACE_SECONDS in retro/RetroView.py) -- so the
# number has to be knowable before there is a clip to measure, which is the
# whole reason it is arithmetic rather than a read of the bytes.


def test_a_clip_plan_is_the_capture_plan_in_milliseconds():
    fps, frames = FPS["gb"], 60
    step = C.capture_step(fps)
    covered = [amount for _, amount in C.capture_plan(frames, step)]
    plan = C.clip_plan(fps, frames)

    assert [index for index, _ in plan] == [
        index for index, _ in C.capture_plan(frames, step)
    ]
    assert [ms for _, ms in plan] == [
        max(1, round(1000 * amount / fps)) for amount in covered
    ]
    # Fourteen whole 67ms pictures, then the 3-frame and 1-frame tail.
    assert [ms for _, ms in plan] == [67] * 14 + [50, 17]


def test_a_default_game_boy_clip_plays_for_1_005_seconds():
    """The number the pacing gate holds an edit for, spelled out.

    Not a round second: 60 frames at 59.727 fps is 1.0046 seconds of
    emulation, and each picture's duration is a whole number of milliseconds,
    so the clip that goes out plays for 1.005. Pacing uses the encoded figure
    rather than the configured one because the encoded figure is what a
    Discord client actually spends.
    """
    assert C.playback_seconds(FPS["gb"], 60) == 1.005


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize(
    "seconds", [0.2, 0.25, 0.5, 0.8, 1.0, 1.3, 2.0, 4.0, 5.0]
)
@pytest.mark.parametrize("clip_fps", [10, 15, 20])
def test_a_clip_plays_for_as_long_as_it_emulated(fps, seconds, clip_fps):
    """Within the millisecond rounding, at every length and every rate.

    This is what makes pacing honest: if the playing time drifted from the
    emulated time, holding an edit for the playing time would either cut the
    animation off or leave the picture sitting still.
    """
    frames = C.clip_frame_count(fps, seconds)
    emulated = frames / fps
    played = C.playback_seconds(fps, frames, clip_fps)

    assert played == sum(ms for _, ms in C.clip_plan(fps, frames, clip_fps)) / 1000.0
    assert abs(played - emulated) / emulated < 0.01, (played, emulated)
    # Never zero, whatever was asked for: a clip with no duration would be a
    # clip the pacing gate thought was already over.
    assert played > 0.0


@pytest.mark.parametrize("fps", [0, -1, 0.0, float("nan")])
def test_a_nonsense_frame_rate_does_not_divide_by_zero(fps):
    """A core that reports nothing useful gets arithmetic, not a traceback.

    RetroEmulator.fps already defaults such a core to DEFAULT_FPS, so this is
    a guard for a direct caller (and for the view's own fallback while a
    session is hibernated), in the same shape as preroll_budget's.
    """
    assert C.playback_seconds(fps, 60) > 0.0
    assert all(ms >= 1 for _, ms in C.clip_plan(fps, 60))


# -- What a clip length is allowed to be --------------------------------------
#
# The bounds themselves, and the thing that makes lowering the ceiling safe:
# every path that reads a clip length goes through clamp_clip_seconds, so a
# setting stored while the ceiling was higher is corrected on the way out
# rather than needing a migration. tests/test_view.py covers the same
# function from the session's side.


def test_the_clip_length_bounds_are_the_measured_ones():
    """The ceiling is 5s, not the 15 it was, and it is a float.

    15 seconds was never measured: at the settings this ships with it is
    roughly 7 seconds of encode for a single hi-res SNES press, ~148 MiB of
    uncompressed pictures held while that happens, and fifteen seconds of a
    turn that is mostly a game which has finished reacting. See
    MAX_CLIP_SECONDS in retro/clips.py for the tables. 5 still covers
    "let a cutscene play out" at five times the default.
    """
    assert (C.MIN_CLIP_SECONDS, C.MAX_CLIP_SECONDS) == (0.2, 5.0)
    assert isinstance(C.MAX_CLIP_SECONDS, float)
    assert C.MIN_CLIP_SECONDS < C.CLIP_SECONDS < C.MAX_CLIP_SECONDS


def test_a_setting_stored_above_the_new_ceiling_is_clamped_on_read():
    """An installation that configured 15 while 15 was allowed must not break.

    Nothing rewrites Config when the ceiling moves, so the stored value is
    still 15 (and, for anyone who set it before the setting was a float, a
    plain ``int``). Reading it has to give a length this cog will really
    record, and everything downstream of it has to keep working on that
    number rather than on the one in storage.
    """
    for stored in (15.0, 15, 9999, "15"):
        assert C.clamp_clip_seconds(stored) == C.MAX_CLIP_SECONDS
        assert isinstance(C.clamp_clip_seconds(stored), float)

    # ...and the clamped value really is a recordable clip, all the way down.
    seconds = C.clamp_clip_seconds(15)
    for fps in sorted(FPS.values()):
        frames = C.clip_frame_count(fps, seconds)
        assert frames == max(C.MIN_CLIP_FRAMES, round(fps * C.MAX_CLIP_SECONDS))
        plan = C.capture_plan(frames, C.capture_step(fps))
        assert sum(covered for _, covered in plan) == frames
        assert 1 <= C.input_budget(fps, frames) < frames
        assert 0 < C.preroll_budget(fps, frames) <= frames
        assert C.playback_seconds(fps, frames) == pytest.approx(
            C.MAX_CLIP_SECONDS, abs=0.05
        )


def test_the_ceiling_is_a_length_a_game_boy_can_actually_record():
    # 5 seconds at 59.727 fps is 299 frames and 76 pictures, and it plays for
    # 5.008 -- the same millisecond rounding a one second clip pays.
    frames = C.clip_frame_count(FPS["gb"], C.MAX_CLIP_SECONDS)
    assert frames == 299
    assert len(C.capture_plan(frames, C.capture_step(FPS["gb"]))) == 76
    assert C.playback_seconds(FPS["gb"], frames) == 5.008


# -- Where a clip starts: the pre-roll ----------------------------------------
#
# The mechanism is in RetroEmulator.record and is proved against real cores in
# test_emulator.py; what is here is the bound, which is plain arithmetic.


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0, 5.0])
def test_a_preroll_is_bounded_and_never_longer_than_its_own_clip(fps, seconds):
    """The bound is what makes the pre-roll safe on a screen that never moves.

    A trim of the clip's leading duplicate pictures was rejected because on a
    frozen screen it wants to trim *everything* and leaves a 17ms flash. The
    pre-roll cannot do that -- it throws away nothing the clip recorded -- but
    it can still spend emulation looking for a change that is never coming, so
    it is capped.
    """
    frames = C.clip_frame_count(fps, seconds)
    budget = C.preroll_budget(fps, frames)

    assert 0 < budget <= frames, (budget, frames)
    assert budget <= C.frame_count(fps, C.PREROLL_SECONDS)
    # A quarter of a second is the cap, so at every length a player can
    # configure past 0.25s the pre-roll is a fraction of the clip rather than
    # the whole of it.
    if seconds >= 1.0:
        assert budget <= frames // 4


def test_the_preroll_bound_covers_the_worst_case_that_was_measured():
    """The numbers behind PREROLL_SECONDS, restated as an assertion.

    The most frames any real core took to show a difference after a press was
    ten, on a GBA homebrew that reacts to the button coming *up* rather than
    going down -- which is exactly the default 160ms hold. See PREROLL_SECONDS
    in retro/clips.py for the whole table. The bound has to clear that, or the
    case it was written for is the case it misses.
    """
    for fps in sorted(FPS.values()):
        frames = C.clip_frame_count(fps, 1.0)
        assert C.preroll_budget(fps, frames) >= 10, fps
    # ...and it clears it with room, rather than sitting exactly on it.
    assert C.frame_count(FPS["gb"], C.PREROLL_SECONDS) == 15
    assert 0.2 <= C.PREROLL_SECONDS <= 0.5


def test_a_preroll_stops_before_the_next_press_in_the_schedule():
    """Why preroll_budget takes a ``next_press``.

    The pre-roll runs the schedule out without photographing it, so left
    unbounded on a screen that does not move it would run straight through the
    repeat button's second tap and the clip would open after a press the
    player asked to watch. It stops on that frame instead, which puts the tap
    in the clip's own first picture.
    """
    frames = C.clip_frame_count(FPS["gb"], 1.0)
    cap = C.preroll_budget(FPS["gb"], frames)

    # The default one second x3 schedule taps at frames 0, 23 and 46, so the
    # cap bites first and the taps are never in danger.
    assert C.preroll_budget(FPS["gb"], frames, 23) == cap == 15
    # A short clip with a short hold brings them together, and then the tap
    # wins.
    assert C.preroll_budget(FPS["gb"], frames, 13) == 13
    assert C.preroll_budget(FPS["gb"], frames, 0) == 0
    # None means "nothing to protect", which is the ordinary single press.
    assert C.preroll_budget(FPS["gb"], frames, None) == cap


def test_a_preroll_can_be_turned_off_by_arithmetic_alone():
    """Zero seconds is exactly the behaviour from before the pre-roll existed.

    Not a setting -- there is no ``[p]retroset`` for this -- but it is what the
    measurements and the before/after tests compare against, so it has to mean
    "photograph the very first frame" rather than "one frame at least".
    """
    frames = C.clip_frame_count(FPS["gb"], 1.0)
    assert C.preroll_budget(FPS["gb"], frames, seconds=0.0) == 0
    assert C.preroll_budget(FPS["gb"], frames, 5, seconds=0.0) == 0


# -- How big the posted picture is --------------------------------------------
#
# (frame width, frame height, the aspect ratio the core reports) -> the size
# the clip is posted at. Everything here was read off the real core; see the
# measurements above MIN_CLIP_WIDTH in retro/clips.py for the encode times
# that chose the rule, and for the two consoles no free ROM was available for.

CONSOLE_SIZES = {
    # Handheld-sized frames are doubled, because 160 or 240 pixels of width
    # is not readable on a phone. These three are unchanged by the rule.
    "gb": ((160, 144, 1.1111), (320, 288)),
    "gba": ((240, 160, 1.5), (480, 320)),
    # ...and every TV console is posted at its own resolution, which halved
    # the time a NES or SNES clip takes to encode.
    "nes": ((256, 224, 1.3061), (293, 224)),
    "snes": ((256, 224, 1.3333), (299, 224)),
    "snes-hires": ((512, 448, 1.3333), (597, 448)),
    "genesis": ((256, 224, 1.3061), (293, 224)),
    "sms": ((256, 192, 1.5238), (293, 192)),
    # A 320-wide Genesis mode would *lose* columns at 1x (293 < 320), so it
    # doubles instead.
    "genesis-320": ((320, 224, 1.3061), (585, 448)),
}


@pytest.mark.parametrize("key", sorted(CONSOLE_SIZES))
def test_each_console_is_posted_at_the_size_that_was_measured(key):
    (width, height, aspect), expected = CONSOLE_SIZES[key]
    assert C.clip_size(width, height, aspect) == expected


def test_the_game_boy_is_still_exactly_320x288():
    """Pinned: there are tests and documentation resting on this number."""
    assert C.clip_size(160, 144, 1.1111) == (320, 288)
    assert C.clip_scale(160, 144, 1.1111) == 2


@pytest.mark.parametrize("key", sorted(CONSOLE_SIZES))
def test_the_posted_picture_never_loses_a_column_or_a_line(key):
    """The aspect correction may repeat pixels; it must never drop them."""
    (width, height, aspect), (out_width, out_height) = CONSOLE_SIZES[key]
    assert out_width >= width, "a NEAREST downscale would throw columns away"
    assert out_height >= height


@pytest.mark.parametrize("key", sorted(CONSOLE_SIZES))
def test_the_posted_picture_keeps_the_aspect_ratio_the_core_reports(key):
    (width, height, aspect), (out_width, out_height) = CONSOLE_SIZES[key]
    assert out_width / out_height == pytest.approx(aspect, abs=0.005)


@pytest.mark.parametrize("key", sorted(CONSOLE_SIZES))
def test_a_scale_of_one_means_the_console_s_own_resolution(key):
    """What `screenshot(scale=1)` asks for, and what the test suite hashes."""
    (width, height, aspect), _ = CONSOLE_SIZES[key]
    assert C.clip_size(width, height, aspect, max_scale=1)[1] == height
    assert C.clip_scale(width, height, aspect, max_scale=1) == 1


def test_the_width_floor_is_the_only_thing_that_decides_the_multiple():
    # The window the measurements leave for MIN_CLIP_WIDTH: above the Game
    # Boy Advance's 240 (or it stops doubling) and at or below the NES's 293
    # (or the NES starts doubling again). If this fails, the constant has
    # moved out of the range the measurements support.
    assert 240 < C.MIN_CLIP_WIDTH <= 293
    assert C.MAX_CLIP_SCALE >= 2

    # Which is exactly the statement that the handhelds double and the TV
    # consoles do not.
    assert C.clip_scale(160, 144, 1.1111) == 2
    assert C.clip_scale(240, 160, 1.5) == 2
    assert C.clip_scale(256, 224, 1.3061) == 1
    assert C.clip_scale(256, 224, 1.3333) == 1


def test_nothing_is_ever_enlarged_past_the_cap():
    # A core reporting a 1x1 frame would otherwise have this doubling until
    # the picture cleared MIN_CLIP_WIDTH.
    for width, height in ((1, 1), (2, 3), (8, 8), (32, 24)):
        scale = C.clip_scale(width, height, width / height)
        assert 1 <= scale <= C.MAX_CLIP_SCALE, (width, height, scale)
        size = C.clip_size(width, height, width / height)
        assert size == (width * scale, height * scale)


def test_a_core_reporting_no_aspect_ratio_falls_back_to_the_frame_s_own():
    # RetroEmulator.aspect_ratio already defaults to 4/3, but clip_size is a
    # plain function and a caller can hand it anything. With no ratio the
    # frame's own shape is used, so the pixels come out square: a 256x224
    # frame is then 256 wide at 1x, under the floor, so it doubles.
    for aspect in (0.0, -1.0):
        assert C.clip_size(256, 224, aspect) == (512, 448)
        assert C.clip_size(320, 240, aspect) == (320, 240)
    assert C.clip_size(160, 144, 0.0) == (320, 288)


# -- Encoding a captured clip ---------------------------------------------------
#
# encode_clip is the half of RetroEmulator.record that needs no core: it takes
# the native-size pictures record_frames captured (a CapturedClip) and
# produces the posted bytes, enlarging each distinct picture once on the way.
# What matters is pixel identity with what record() always produced -- resize
# every frame, then encode_animation -- because record() is now nothing but
# these two halves glued together. Pillow is the one dependency, so these
# tests skip without it rather than dragging it into the rest of this file,
# which runs with nothing installed at all.


def anmf_durations(data):
    """Each animation frame's duration in ms, straight out of the WebP.

    A cut-down tests/test_emulator.py:anmf -- the durations live in bytes
    12-14 of every ANMF chunk -- kept here because this file has no emulator
    and no core to borrow it from.
    """
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", data[:12]
    offset, durations = 12, []
    while offset + 8 <= len(data):
        fourcc = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if fourcc == b"ANMF":
            durations.append(int.from_bytes(data[offset + 20 : offset + 23], "little"))
        offset += 8 + size + (size & 1)
    return durations


def _pictures(count, size=(16, 12), seed=3):
    """``count`` small RGB images, every one distinct, plus the Image module."""
    Image = pytest.importorskip("PIL.Image", reason="encoding a clip needs Pillow")
    images = []
    for index in range(count):
        image = Image.new("RGB", size, (20, 40, 60))
        image.putpixel((index % size[0], 0), ((seed * (index + 1)) % 251, 90, 7))
        images.append(image)
    return Image, images


def test_encode_clip_is_the_resize_record_used_to_do_then_encode_animation():
    """Pixel identity of the split against the old inline resize.

    record() used to enlarge every captured frame to the posted size and hand
    the lot to encode_animation; encode_clip does the same NEAREST resize at
    encode time instead. Same resize, same encoder, same arguments -- so the
    bytes must match exactly, which is what lets record() stay a thin wrapper
    without changing a single posted clip.
    """
    Image, images = _pictures(4)
    durations = [67, 67, 50, 17]
    size = (32, 24)
    old_way = C.encode_animation(
        [image.resize(size, Image.NEAREST) for image in images], durations
    )
    new_way = C.encode_clip(C.CapturedClip(images, durations, size))
    assert new_way == old_way
    assert new_way[:4] == b"RIFF" and new_way[8:12] == b"WEBP"


def test_a_picture_already_at_the_posted_size_is_not_resized_or_copied():
    # A frame whose native size *is* the posted size (a square-pixel core at
    # 1x) goes to the encoder untouched, so the common TV-console case pays
    # for no comparison bytes either.
    Image, images = _pictures(3)
    durations = [67, 50, 17]
    same = C.encode_clip(C.CapturedClip(images, durations, images[0].size))
    assert same == C.encode_animation(images, durations)


def test_a_run_of_identical_pictures_costs_one_resize_and_the_same_bytes():
    """The dedup, and that it is invisible in the output.

    libwebp merges identical consecutive frames anyway (that is why a static
    screen costs a handful of bytes), so enlarging each copy of a picture the
    encoder was about to fold away was pure waste -- a one second clip of a
    menu is sixteen captures of one picture. The bytes comparison that spots
    the run must only skip work, never change what the encoder is given.
    """
    Image, distinct = _pictures(2)
    still, moved = distinct
    images = [still, still.copy(), still.copy(), moved]
    durations = [67, 67, 50, 17]
    size = (32, 24)

    resizes = []
    original = Image.Image.resize

    def counting(self, *args, **kwargs):
        resizes.append(1)
        return original(self, *args, **kwargs)

    Image.Image.resize = counting
    try:
        deduped = C.encode_clip(C.CapturedClip(images, durations, size))
    finally:
        Image.Image.resize = original

    assert len(resizes) == 2, "three copies of one picture should resize once"
    naive = C.encode_animation(
        [image.resize(size, Image.NEAREST) for image in images], durations
    )
    assert deduped == naive


def test_one_duration_for_the_whole_animation_is_a_shape_that_still_works():
    """The scalar ``duration_ms``, which is not the dead branch it looked like.

    The cog always passes a list, because capture_plan gives the closing
    picture a shorter nominal duration than the rest -- but tests/fakes.py
    builds every stand-in clip in the fast suite with
    ``encode_animation(images, FAKE_FRAME_MS)``, so deleting the branch would
    take most of the cog's own test suite with it. Pinned here so that is
    visible from clips.py's own tests rather than only from whatever breaks.
    """
    Image, images = _pictures(3)
    scalar = C.encode_animation(images, 1000)
    listed = C.encode_animation(images, [1000, 1000, 1000])

    assert scalar == listed, "one duration must mean the same as N of it"
    assert scalar[:4] == b"RIFF" and scalar[8:12] == b"WEBP"
    clip = Image.open(io.BytesIO(scalar))
    assert clip.n_frames == 3
    # Read off the file rather than out of Pillow: this build of the WebP
    # plugin does not put a per-frame duration in ``info`` on seek, and what
    # is under test is what really got written.
    assert anmf_durations(scalar) == [1000, 1000, 1000]
    # The 1ms floor applies to a scalar too: WebP reads a duration of 0 as
    # "as fast as the decoder can manage".
    assert C.encode_animation(images, 0) == C.encode_animation(images, 1)


def test_a_mid_clip_geometry_change_still_encodes_frames_of_one_size():
    """SET_GEOMETRY mid-clip: the pinned size wins, in one resize per frame.

    The posted size is decided from the first captured frame (see
    CapturedClip.size); a picture of any other geometry -- the SNES switching
    to hi-res part-way through a clip -- is taken from its own resolution to
    the posted one in a single NEAREST step, exactly as the capture-time
    resize did, so no animation frame can disagree with its siblings about
    the size and no pixel is ever resized twice on the way.
    """
    Image, _ = _pictures(1)
    lores = Image.new("RGB", (16, 12), (10, 20, 30))
    hires = Image.new("RGB", (32, 24), (40, 50, 60))
    hires.putpixel((31, 23), (1, 2, 3))
    size = (20, 12)
    data = C.encode_clip(C.CapturedClip([lores, hires], [67, 17], size))
    expected = C.encode_animation(
        [lores.resize(size, Image.NEAREST), hires.resize(size, Image.NEAREST)],
        [67, 17],
    )
    assert data == expected


# -- What retro.emulator re-exports from here ----------------------------------
#
# clips.py was split out of emulator.py, and emulator.py re-exports the names
# of it that the rest of the cog imports from there, so the split cost nothing
# at the call sites. That shim used to carry *everything* clips.py defines,
# private names included, which is a promise nobody is owed: a Red cog is
# installed whole and imported by Red's cog manager, so nothing outside this
# repository can import retro.emulator at all.
#
# emulator.py imports nothing but the standard library and this module at
# module scope -- libretro.py and Pillow are both imported lazily, inside the
# functions that need them -- so the shim is checkable here, with nothing
# installed, rather than only in the emulator suite.

E = load_standalone("retro_emulator_for_the_shim", "emulator.py")

#: The copy of clips.py that ``E`` itself imported. The standalone loader
#: gives this file its own copy (``C``) and emulator.py's ``from .clips
#: import`` builds another, so "is it a re-export or a second definition?"
#: has to be asked against the module the emulator is really holding.
EC = sys.modules[f"{E.__package__}.clips"]

#: Names of clips.py that something in retro/ or tests/ imports from
#: ``retro.emulator`` rather than from ``retro.clips``. Grepped, not guessed:
#: retro/Retro.py, retro/RetroView.py, retro/cores.py, retro/saves.py and
#: tests/fakes.py import from the shim, as do tests/test_view.py,
#: tests/test_cog_session.py, tests/test_leaks.py and tests/test_emulator.py.
#: A name that stops being imported from there belongs in clips.py only.
SHIMMED = {
    "CLIP_EXTENSION", "CLIP_SECONDS", "EmulatorError", "FAST_RAW_MODES",
    "FAST_ROTATIONS", "MAX_CLIP_SECONDS", "MIN_CLIP_FRAMES",
    "MIN_CLIP_SECONDS", "capture_plan", "capture_step", "clamp_clip_seconds",
    "clip_frame_count", "clip_plan", "describe_seconds", "encode_animation",
    "encode_clip", "fast_frame_image", "fast_frame_size", "format_seconds",
    "frame_count", "input_budget", "playback_seconds", "preroll_budget",
}


def test_the_shim_re_exports_the_same_objects_it_names():
    for name in SHIMMED:
        assert name in E.__all__, f"{name} is imported from retro.emulator"
        assert getattr(E, name) is getattr(EC, name), f"{name} is a second copy"


def test_the_shim_carries_nothing_private_and_nothing_unused():
    """No underscore names, and no clips name the cog does not import here."""
    assert not [name for name in E.__all__ if name.startswith("_")]

    # These three were re-exported and nothing ever imported them from the
    # emulator; the fast frame grab's own tests reach into retro/clips.py.
    for private in ("_SLOW_GRAB_LOGGED", "_channel_expansion_table",
                    "_note_slow_frame_grab"):
        assert hasattr(C, private), "the private names still live in clips.py"
        assert not hasattr(E, private), f"{private} is re-exported again"

    # Public clips names nothing imports via the emulator are gone from
    # __all__ too, whether or not emulator.py happens to use them itself.
    for unused in ("MIN_AFTERMATH_FRAMES", "MIN_CLIP_WIDTH", "PREROLL_SECONDS",
                   "WEBP_METHOD", "WEBP_MINIMIZE_SIZE", "FAST_POINT_TABLES",
                   "clip_scale", "CLIP_FPS", "MAX_CLIP_SCALE", "CapturedClip",
                   "clip_size"):
        assert unused not in E.__all__, f"{unused} is re-exported unused"

    # ...and every clips name that is still in __all__ is in SHIMMED, so
    # adding one back has to be a deliberate edit to both lists.
    from_clips = {name for name in E.__all__ if hasattr(C, name)}
    assert from_clips == SHIMMED, from_clips ^ SHIMMED


def test_pillow_is_imported_through_the_one_helper():
    """RetroEmulator._pillow was a byte-for-byte copy of clips._pillow."""
    assert not hasattr(E.RetroEmulator, "_pillow")
    assert E._pillow is EC._pillow


# -- A clip too big for the server it is going to -----------------------------


def _big_capture(frames=8, size=(320, 288)):
    """A capture whose lossless encode is comfortably over a small limit."""
    Image = pytest.importorskip("PIL.Image")
    images = []
    for index in range(frames):
        # Noise, not flat colour: lossless WebP is very good at console art,
        # so a clip that is actually large has to be something it cannot fold
        # away. This is also the case the fallback exists for.
        image = Image.new("RGB", size)
        image.putdata(
            [
                ((x * 7 + y * 13 + index * 29) % 256, (x * 3) % 256, (y * 5) % 256)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )
        images.append(image)
    return C.CapturedClip(
        images=images, durations=[67] * frames, size=size
    )


@pytest.fixture
def photographic():
    """
    A capture lossy WebP really does shrink: noise, not console art.

    The fallback exists for a clip that is genuinely too big, and the honest
    way to test "it makes it smaller" is content where lossy wins. Console
    art is the opposite case and has its own test above.
    """
    Image = pytest.importorskip("PIL.Image")
    import random

    size = (320, 288)
    frames = 8
    rng = random.Random(20240923)
    images = []
    for _ in range(frames):
        image = Image.new("RGB", size)
        image.putdata(
            [
                (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(size[0] * size[1])
            ]
        )
        images.append(image)
    return C.CapturedClip(images=images, durations=[67] * frames, size=size)


def test_a_clip_that_fits_is_left_alone():
    """The ordinary path: nothing is given up when nothing needs to be."""
    captured = _big_capture(frames=2, size=(32, 32))
    lossless = C.encode_clip(captured)
    assert C.shrink_clip(captured, len(lossless), lossless) == lossless


def test_lossy_is_never_handed_back_when_it_is_bigger():
    """
    The reason `shrink_clip` is given what the caller already has.

    Console frames are flat-shaded with hard edges, which is precisely what
    lossless WebP is best at -- so a lossy pass can come out *many times
    larger*, spending bits approximating the edges. Handing that back would
    make the very problem it was called about worse.
    """
    captured = _big_capture()
    lossless = C.encode_clip(captured)
    lossy = C.encode_clip(captured, lossless=False, quality=80)
    assert len(lossy) > len(lossless), "this fixture no longer shows the hazard"

    result = C.shrink_clip(captured, len(lossless) // 3, lossless)
    assert len(result) <= len(lossless), "it handed back something bigger"


def test_an_over_limit_clip_is_re_encoded_until_it_fits(photographic):
    captured = photographic
    lossless = C.encode_clip(captured)
    limit = len(lossless) // 3
    assert limit > 0

    smaller = C.shrink_clip(captured, limit, lossless)

    assert smaller is not None
    assert len(smaller) <= limit, "it was not brought under the limit"
    assert len(smaller) < len(lossless)
    # Still a real animated clip rather than a still or a stub.
    Image = pytest.importorskip("PIL.Image")
    with Image.open(io.BytesIO(smaller)) as opened:
        assert opened.format == "WEBP"
        assert getattr(opened, "n_frames", 1) > 1


def test_an_impossible_limit_still_hands_back_the_smallest_attempt(photographic):
    """
    One byte is not a limit anything can meet, and a press still gets a clip.

    Posting something Discord may refuse is strictly better than refusing it
    here: the 40005 handling is the backstop, and it is reached only in the
    case nothing could have helped.
    """
    lossless = C.encode_clip(photographic)
    smallest = C.shrink_clip(photographic, 1, lossless)
    assert smallest is not None
    assert len(smallest) < len(lossless)


def test_every_shrink_step_really_gives_something_up(photographic):
    """Each step is smaller than the one before it, or it is not a step."""
    captured = photographic
    sizes = [
        len(C.encode_clip(captured, lossless=lossless, quality=quality))
        if scale == 1.0
        else len(
            C.encode_clip(
                captured._replace(
                    size=(
                        int(captured.size[0] * scale),
                        int(captured.size[1] * scale),
                    )
                ),
                lossless=lossless,
                quality=quality,
            )
        )
        for lossless, quality, scale in C.CLIP_SHRINK_STEPS
    ]
    assert sizes == sorted(sizes, reverse=True), sizes
