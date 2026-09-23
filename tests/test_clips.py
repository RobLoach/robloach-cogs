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
  per-console upscale was measured away.

tests/test_emulator.py holds the same two statements against real cores:
continuity is proved there by recording two consecutive clips off a Game Boy
and comparing them with the frames emulated in between, and the geometry by
asserting a real Game Boy screenshot is still exactly 320x288.
"""

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
    "seconds", [0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 1.3, 2.0, 4.0, 15.0]
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
@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0, 15.0])
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
    "seconds", [0.2, 0.25, 0.5, 0.8, 1.0, 1.3, 2.0, 4.0, 15.0]
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


# -- Where a clip starts: the pre-roll ----------------------------------------
#
# The mechanism is in RetroEmulator.record and is proved against real cores in
# test_emulator.py; what is here is the bound, which is plain arithmetic.


@pytest.mark.parametrize("fps", sorted(FPS.values()))
@pytest.mark.parametrize("seconds", [0.2, 0.5, 0.8, 1.0, 4.0, 15.0])
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
