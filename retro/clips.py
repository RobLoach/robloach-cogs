"""
Clips: the arithmetic, the encoder, and the fast frame grab.

Everything in here is a plain function of numbers, bytes or Pillow images.
Nothing in here touches libretro, so it imports and tests with no core, no
shared object and no ``libretro.py`` installed at all -- which is what makes
the clip arithmetic (and the pixel-exactness of the fast frame grab) cheap
enough to cover in the fast test suite. Pillow is needed to *encode* a clip
and is imported lazily, so even that is only paid for by the callers that do
it. Encoding is the only direction: nothing here reads a clip back.

Three groups, and they only meet in retro/emulator.py:

* the clip/timing arithmetic -- seconds in, emulated frames out;
* the animation encoder;
* the fast frame grab, which decodes a video driver's framebuffer with
  Pillow instead of letting libretro.py convert it a pixel at a time.

:class:`EmulatorError` lives here rather than next to the emulator because
both halves raise it and this is the half that cannot import the other one.
``retro.emulator`` re-exports every name below.
"""

import io
import logging
import math
import typing

# Deliberately the same logger as retro/emulator.py, which this was split out
# of: the name is what an operator turns up in a logging config, and moving
# code between files is no reason to invalidate that.
log = logging.getLogger("red.robloach.retro.emulator")


class EmulatorError(RuntimeError):
    """Raised when the emulator cannot be started or run."""


# How many seconds of play one clip shows, and how many frames per second the
# clip itself is encoded at. Sampling every 4th emulated frame is enough to
# read the action and keeps the clip a quarter of the size.
#
# One second is the default because a turn is a round trip: press, wait for
# the clip to encode and upload, look at it, press again. Four seconds of
# footage made every one of those round trips four times as long to watch and
# to record, and almost all of it was the game sitting still after the press
# had already played out. A second shows the press land and its result, which
# is what the next press is decided from.
#
# On the timing: animated WebP stores each frame's duration in *milliseconds*,
# so 15 fps against a 59.73 fps core is 4 emulated frames per clip frame and
# exactly 67ms per frame -- a 1 second clip is 60 emulated frames and 16
# pictures (fifteen on the cadence plus the closing frame, see capture_plan)
# and measures 1.004s, which is 0.4% slow and invisible. 20 fps would land on
# a round 50ms and match the emulated time exactly, at about 39% more bytes
# and 32% more encoding time, so 15 stays the default.
#
# Lowering it was measured and rejected. Now that a frame is posted at the
# console's own resolution (see MIN_CLIP_WIDTH) the encode is small enough
# that dropping a third of the pictures buys very little, and it buys it by
# making the animation visibly choppier. One second of real motion, best of
# three on a Raspberry Pi 5, encode time and bytes:
#
#              15 fps (16 pics)   12 fps (13)     10 fps (11)
#   NES         32.5ms / 1,682    24.0ms / 1,400  17.6ms / 1,202
#   SNES        71.4ms / 26,934   55.6ms / 23,428 49.8ms / 20,956
#
# So 10 fps saves 15ms on a NES clip and 22ms on a SNES one, out of the 77ms
# and 128ms a whole clip takes end to end. Not worth a third of the frames.
CLIP_SECONDS = 1.0
CLIP_FPS = 15

# Bounds for the configurable clip length, which is a float: 0.8 is a real
# answer, and at these lengths the difference between 0.8 and 1 is a fifth of
# the turn.
#
# The floor is 0.2s rather than something smaller because of what a clip is
# made of. At 59.73 fps it is 12 emulated frames: three pictures at CLIP_FPS,
# so still an animation, and an input budget of 8 frames (see input_budget)
# which is ~134ms of button hold -- above the ~100ms where a game polling its
# controller a few times a second can miss a press entirely. Halve it again
# and the clip is two pictures and the hold is four frames, i.e. a press that
# may not register at all and a "clip" nobody can read.
#
# The ceiling is 5s, and used to be 15 with nothing behind it. Three costs
# grow with the clip -- encode time, the uncompressed working set and the
# turn itself -- and all three are linear in it, so this constant is the only
# thing bounding any of them.
#
# Encode first. Measured on a Raspberry Pi 5 against the real cores, best of
# three, one press end to end (emulate, then encode; the encode is the half
# that runs with the emulator lock released, see encode_clip):
#
#                        pics   emulate   encode    total   native RAM
#   gambatte / uCity, which animates every frame (320x288 posted)
#     1s                   16     48ms     24ms      72ms     1.1 MiB
#     4s                   61    105ms     54ms     160ms     4.0 MiB
#     5s                   76    131ms     64ms     195ms     5.0 MiB
#     15s                 225    366ms    205ms     571ms    14.8 MiB
#   fceumm / nestest with a direction held (293x224 posted)
#     1s                   16     48ms      8ms      57ms     2.6 MiB
#     4s                   61    192ms     24ms     216ms    10.0 MiB
#     5s                   76    243ms     28ms     272ms    12.5 MiB
#     15s                 226    802ms     83ms     885ms    37.1 MiB
#
# Encode time is linear in pictures past the first clip (which carries a
# fixed few milliseconds of setup): 0.84-0.91ms a picture on the Game Boy and
# 0.37-0.39ms on the NES at every length from 4s up.
#
# Those are the two cheapest consoles here, and they are not what the ceiling
# is for. The expensive case is a Super Nintendo in hi-res, which posts at
# 597x448 (see MIN_CLIP_WIDTH) and which the WEBP_METHOD table above measures
# at 342ms of encode for a one second clip and 1,863ms for a four second one
# at the settings this ships with. At the same per-picture rate 15 seconds is
# 225 pictures and roughly 7 seconds of encode, for one button press, on the
# thread every channel's emulation shares. 5 seconds is 76 pictures and
# roughly 2.3 -- still the slowest thing this cog does, but bounded, and only
# reachable by someone who asked for it.
#
# Second, the working set. A recording holds every picture as an
# uncompressed native-size Pillow image until the encode has finished (see
# CapturedClip.images), so the peak is pictures x frame bytes. 512x448 RGB is
# 688 KiB a picture, which is the "native RAM" column above taken to the
# console that has the biggest frames:
#
#                    1s         5s        15s
#   SNES hi-res    10.5 MiB   49.9 MiB   147.7 MiB
#   Game Boy        1.1 MiB    5.0 MiB    14.8 MiB
#
# 148 MiB of images for one press is not a thing a bot sharing a host should
# be askable for from a chat box.
#
# Third, the turn, which is the same argument that made CLIP_SECONDS one
# second rather than four -- a press is a round trip, and the clip is the
# part the player spends watching. At 15 seconds the watching is fifteen
# times the default and almost all of it is a game that has finished
# reacting. The pacing gate does not even deliver that footage reliably: it
# holds an edit for playback_seconds but caps one wait at MAX_PACE_SECONDS
# (1.25s, see retro/RetroView.py), so anything longer than that is replaced
# before it has played out whenever somebody is queued behind it.
#
# 5 seconds still covers the thing the long end was wanted for -- letting a
# cutscene or a long text box play out without pressing anything -- at five
# times the default. :func:`clamp_clip_seconds` is on every path that reads
# the setting, so an installation configured at 15 before this changed is
# clamped down on read rather than needing a migration.
MIN_CLIP_SECONDS = 0.2
MAX_CLIP_SECONDS = 5.0

# The fewest emulated frames a clip may be, whatever it was asked for. At
# CLIP_FPS against a 60 fps core one picture is four frames, so six frames is
# three pictures (frames 1, 5 and -- because the closing frame is always
# photographed, see capture_plan -- 6), which is more than the least that is
# still an animation, and leaves room for a press plus the aftermath frame
# below. MIN_CLIP_SECONDS is well clear of it on every console here (0.2s is
# 10 frames even on a 50 fps PAL core), so this is a floor for a core that
# reports a strange frame rate and for direct callers of record(), not
# something the settings can reach.
MIN_CLIP_FRAMES = 6

# How many emulated frames at the end of a clip are kept clear of input, so
# the last picture shows the game *after* the press rather than still under
# it. Counted in captured frames by input_budget(), which is what makes it
# one visible picture rather than one invisible frame.
MIN_AFTERMATH_FRAMES = 1

# -- The seam: where one clip stops and the next one starts -------------------
#
# A picture is taken *after* an emulated frame, never before one, so the
# picture at index ``i`` is the game after ``i + 1`` frames and the last
# picture of a clip is the game after all ``frames`` of them (see
# :func:`capture_plan`, which always photographs both ends). The next clip
# emulates one frame and photographs it. **The seam is therefore exactly one
# emulated frame, every time, on every console**: press, clip, press, clip is
# one unbroken run of console frames with nothing shown twice, nothing skipped,
# and no way for the console to get ahead of what has been posted.
#
# That is the contract, it is what ``[p]retro``'s Wait button always did, and
# it is worth stating in one place because the thing that used to live here
# broke it.
#
# **The pre-roll, and why it is gone.** A clip that opened with a press used to
# run the press out *unphotographed* -- up to a quarter of a second of it --
# until the picture stopped being the one the previous clip had left standing
# in the channel, and only then start recording. It was written for a real
# report ("when I click the button, the clip seems to replay a bit from the
# previous clip"), because one frame after a button goes down a game that was
# sitting still is still sitting still, so the opening picture was the closing
# picture over again.
#
# It did not fix that report, and it cost the seam to not fix it. Measured on
# this Raspberry Pi 5 against the real cores and ROMs, four consecutive one
# second clips each, default 160ms hold, the button held from frame 0 of every
# clip -- emulated frames between the last picture of one clip and the first
# picture of the next ("seam"), and whether that first picture is byte
# identical to the last one ("again?"):
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
# The seam was ``1 + the frames the pre-roll used``, exactly, in every row.
# And wherever the screen was *static* -- a menu, an overworld, a paused game,
# which is most of what this cog is actually played on -- the pre-roll ran its
# whole 15 frame bound, found nothing, opened the clip on the repeated picture
# anyway, and charged a quarter of a second of game time (251ms on a Game Boy)
# per press for it. That
# is the report, still, with the fix that was supposed to answer it switched
# on: "the new clips still seem to start right before the previous clip
# ended".
#
# So the pre-roll is removed rather than retuned. No bound could have saved it:
# the only way to escape a repeated opening picture is to skip forward until
# the picture changes, and on a screen that never changes that is either a
# jump (what it did) or an unbounded one (worse). What is left is the honest
# reading -- the game *has not moved*, and the clip says so.
#
# What that costs, measured the same way, in pictures of the sixteen a one
# second clip photographs that are the held one again before anything moves:
#
#   fceumm / nestest      down     0 -> 1    (nothing -> 67ms of held picture)
#   gambatte / uCity      down     4 -> 8    on the seam where uCity's menu
#                                            has just stopped animating
#   gambatte / Libbet     down    11 -> 16   on the seam where its screen has
#                                            just settled
#   fceumm / nestest      start   16 -> 16   unchanged, and dmg-acid2 the
#                                            same: a screen that never moves
#                                            was all repeat either way
#
# So a clip gains between nothing and a few pictures of visible latency at its
# start, which is the latency the game really has, and the encoder folds that
# run of identical pictures into one stored frame with their durations added
# together (see :func:`encode_animation`) -- it is a held picture, not a
# stutter. In exchange every clip starts one frame after the last one ended,
# the console never runs a quarter of a second ahead of the pictures, and a
# clip of a static screen is still a full length clip -- not the 17ms flash
# that the *trim* of leading duplicates, tried and rejected before the
# pre-roll, turned it into.
#
# **The frame rate is not a lever on any of this**, which is the first thing
# that was tried. :func:`capture_plan` puts a picture on frame 0 and on the
# final frame whatever the cadence is, so the seam is one frame at every
# CLIP_FPS -- measured at 10, 15, 20 and 60 against all five rows above, and
# the seam column is identical in all four. All CLIP_FPS changes is how many
# pictures fill the middle.

# Animated WebP, encoded losslessly, is the one format a clip is ever posted
# in, and GIF is not worth reintroducing as an alternative: measured on a 4
# second clip, WebP is 24.4 KiB against a 53.2 KiB GIF on a moving Game Boy
# screen and 248 KiB against 998 KiB on the SNES, *and* pixel-exact rather
# than quantized down to 64 colours. It costs 0.15s more to encode on the
# Game Boy and 1.3s on the SNES, which is the price of being exact. GIF also
# stores frame durations in centiseconds, so it cannot hold the 67ms a frame
# of a 15 fps clip lasts (see CLIP_FPS) without playing ~4.5% slow.
CLIP_EXTENSION = ".webp"

# How hard libwebp is told to work. These are not exposed as settings: they
# are a speed/size trade with one right answer, and the answer is measured
# rather than guessed. Every combination below is *lossless*, so none of this
# costs a single pixel of fidelity.
#
# Measured on a Raspberry Pi 5, three kinds of real content at three clip
# lengths, best of three runs each (bytes / encode ms):
#
#                       method=0              method=1
#                    min=T      min=F      min=T      min=F
#   Game Boy, mostly still (uCity, 2 distinct pictures)
#     0.5s        1140/10     1140/7      588/12      588/8
#     1s          1140/16     1140/12     588/17      588/10
#     4s          1594/50     1650/31    1016/52     1056/42
#   Game Boy, real motion (an animated title screen, every picture distinct)
#     0.5s      10128/32    11218/19    5380/55     6408/33
#     1s        15448/52    19538/40    9550/110   11248/79
#     4s        30892/133   40176/132  21552/232   25058/177
#   Super Nintendo, rotozoom with a direction held (597x448, all distinct)
#     0.5s      19290/129   27536/93   14870/248   22276/143
#     1s        40012/251   57750/181  31012/477   47232/342
#     4s       219540/1231 303988/907 172004/2313 253682/1863
#
# minimize_size is switched off. It costs 30-40% more encode time on every
# clip and buys nothing where bytes could ever matter:
#
#   * on a still screen it saves 0% (the encoder has already merged the
#     identical pictures into one);
#   * on real motion it saves 15-34%, of a file that is 6-47 KiB at the
#     default clip length -- under 1% of the 8 MiB this cog assumes it may
#     attach;
#   * on the pathological case, 240 frames of pure 4x4 noise, it saves
#     *0.0%* (12.59 MiB either way) and still costs 4.3 seconds. The one
#     input big enough to be rejected by Discord is the one input
#     minimize_size cannot shrink, so it is not what keeps clips postable --
#     `Retro._send_clip` handling an over-limit attachment is.
#
# That is also why there is no "minimize_size above N seconds" rule: the
# longer clips are the ones whose bytes are least compressible, so a
# threshold would spend the most time in the case with the least to gain.
#
# method stays at 1. On a Game Boy -- the console this cog is played on most,
# and the only one whose frames are small -- method=1 is both *smaller* and
# *faster* than method=0 on a still screen, and 38-43% smaller for 2x the
# time on a moving one. method=0 is only a clear time win on large busy
# frames (the SNES row: 181ms against 342ms at 1s) and it pays 22% more bytes
# for it on every clip from every console. method>=5 was measured earlier at
# 20-370x the time for no measurable saving, and method=6 takes minutes.
#
# A per-console or per-frame-size method was measured once the frames got
# smaller (see MIN_CLIP_WIDTH) and rejected: shrinking the picture took the
# absolute cost of method=1 out of the range where the trade was interesting.
# One second of real motion at the resolutions now posted:
#
#                        method=0            method=1
#   NES     293x224   18.8ms /  6,362    31.0ms /  1,682
#   SNES    299x224   69.4ms / 74,390   117.2ms / 39,780
#
# method=1 is 3.8x smaller on the NES for 12ms and 47% smaller on the SNES
# for 48ms. It was the 195ms the 2x upscale cost that made method=0 look
# attractive on a SNES clip, and that upscale is gone.
#
# quality is a no-op here and is left at 100 for clarity: in lossless mode
# libwebp reads it as an effort dial, and dropping it to 75 or 50 produced
# byte-identical Game Boy clips and *larger* SNES ones (45,662 against 40,012
# at method=0) for no useful time saving.
WEBP_METHOD = 1
WEBP_MINIMIZE_SIZE = False

# -- Grabbing a frame ---------------------------------------------------------
#
# libretro.py's ArrayVideoDriver.screenshot() converts the core's native
# framebuffer into RGBA in a *per-pixel Python loop*: 23,040 iterations for a
# 160x144 Game Boy frame, 21ms of the ~55ms a captured picture used to cost on
# a Raspberry Pi 5. A one second clip is fifteen pictures, so that loop was
# 41% of the whole recording.
#
# Pillow can do exactly the same conversion in C, because every pixel format
# libretro defines is one Pillow already has a raw decoder for. Decoding the
# driver's buffer in place -- with the driver's own pitch as the stride, so
# the padding cores leave at the end of each row is skipped rather than
# copied -- is ~100x faster (0.17ms against 21ms on gambatte) and, with the
# lookup table below, byte-for-byte identical to what screenshot() returns.
#
# This reaches into ArrayVideoDriver's private attributes, which is a hazard:
# libretro.py is free to rename any of them. Every access is therefore
# guarded and the official screenshot() is still there as the fallback, so a
# libretro.py that has moved on gets slow rather than broken. The
# pixel-identity tests in tests/test_emulator.py are what would notice.

#: libretro pixel format -> the Pillow raw decoder that reads it. libretro's
#: names describe the channel order in a little-endian *word*, Pillow's
#: describe it in memory, which is why they look reversed: RGB565's low byte
#: holds blue, so in byte order it is "BGR;16".
FAST_RAW_MODES = {
    "RGB565": "BGR;16",
    "XRGB8888": "BGRX",
    "RGB1555": "BGR;15",
}

#: Rotation -> the Pillow transpose that reproduces libretro.py's own
#: rotation of the same name, or None when no transpose is needed.
#:
#: **Rotation.NINETY is deliberately absent and must stay absent.**
#: libretro.py 0.6.0 and 0.11.1 alike compute its starting offset as
#: ``(width - 4) * height * 4`` where a 90 degree rotation needs
#: ``(width - 1) * ...``, so the rows come out cyclically shifted by three
#: and the three that wrap round overwrite the bottom of the picture. There
#: is therefore no correct path for it at all -- the official screenshot() is
#: the broken one -- and silently fixing it here would make the fast and slow
#: grabs of the same frame disagree. So the fast path stands down and the
#: frame goes the slow way. No console in systems.py asks for a rotation (an
#: emulator test holds them to it), so this is a guard against a future core.
#:
#: ONE_EIGHTY and TWO_SEVENTY are correct upstream and are proved
#: byte-identical to it (see tests/test_frame_grab.py).
FAST_ROTATIONS = {
    "NONE": None,
    "ONE_EIGHTY": "ROTATE_180",
    "TWO_SEVENTY": "ROTATE_270",
}


def _channel_expansion_table(bits: int) -> list:
    """
    One channel's 8-bit values, remapped from Pillow's rounding to libretro's.

    A 5- or 6-bit channel has to be stretched to 8 bits, and the two
    implementations disagree by a hair: Pillow scales (``c * 255 // hi``)
    while libretro.py replicates the high bits (``c << (8 - bits) | c >> ...``).
    On a 5-bit channel that is a difference of at most 1 on 21 of the 32
    possible values -- invisible, but not *identical*. Identical is worth
    having because :func:`fast_frame_image` stands down to ``screenshot()``
    for whole classes of frame, so both paths can be used within one session
    and their pictures must not differ at all.

    Only the values a decoded channel can actually hold are remapped; the rest
    of the table is the identity, so applying it to a channel that was already
    8 bits would do nothing.
    """
    high = (1 << bits) - 1
    table = list(range(256))
    for value in range(high + 1):
        table[value * 255 // high] = (value << (8 - bits)) | (value >> (2 * bits - 8))
    return table


#: pixel format -> a 768 entry Image.point() table (R, then G, then B), or
#: None for a format that needs no correction. XRGB8888 is already 8 bits per
#: channel, so Pillow's decoder copies the bytes through untouched.
FAST_POINT_TABLES = {
    "RGB565": (
        _channel_expansion_table(5) + _channel_expansion_table(6) + _channel_expansion_table(5)
    ),
    "RGB1555": _channel_expansion_table(5) * 3,
    "XRGB8888": None,
}

#: How many distinct reasons to remember having logged. Two of the reasons
#: below interpolate the frame geometry the *core* chose, so a core that
#: changes geometry mid-game can mint new strings indefinitely -- and this is
#: a module-level set in a process that runs for months.
MAX_SLOW_GRAB_REASONS = 64

#: Reasons the fast grab has already been logged as unavailable, so a core
#: that cannot use it says so once instead of once per frame (fifteen times a
#: clip, several clips a minute). Bounded by MAX_SLOW_GRAB_REASONS.
_SLOW_GRAB_LOGGED: set = set()


def _note_slow_frame_grab(reason: str) -> None:
    """Log, once per reason per process, that the fast frame grab stood down."""
    if reason not in _SLOW_GRAB_LOGGED:
        if len(_SLOW_GRAB_LOGGED) >= MAX_SLOW_GRAB_REASONS:
            # Something is generating reasons rather than hitting one, so
            # start again rather than growing. The worst this costs is that a
            # reason already reported is reported a second time.
            _SLOW_GRAB_LOGGED.clear()
        _SLOW_GRAB_LOGGED.add(reason)
        log.debug(
            "Reading the video driver's framebuffer directly is not possible (%s); "
            "falling back to libretro.py's own screenshot(), which is slower.",
            reason,
        )


def _frame_dimensions(driver) -> typing.Optional[typing.Tuple[int, int, int]]:
    """
    ``(width, height, pitch)`` of the driver's last frame, or None.

    libretro.py <= 0.6.x keeps these as three attributes; 0.7+ replaced them
    with a single ``_frame_dims`` namedtuple. Both are read here so the fast
    path applies on either, and anything else falls back.
    """
    dims = getattr(driver, "_frame_dims", None)
    if dims is not None:
        values = (
            getattr(dims, "width", None),
            getattr(dims, "height", None),
            getattr(dims, "pitch", None),
        )
    else:
        values = (
            getattr(driver, "_last_width", None),
            getattr(driver, "_last_height", None),
            getattr(driver, "_last_pitch", None),
        )
    if not all(isinstance(value, int) for value in values):
        # Before the first video refresh these are all None, which is the one
        # case that is completely normal and not worth a log line.
        return None
    if not all(value > 0 for value in values):
        return None
    return typing.cast(typing.Tuple[int, int, int], values)


def fast_frame_image(driver, Image):
    """
    A video driver's current frame as an RGB image, or None to use screenshot().

    None means "nothing here is wrong, but this frame is not one we know how
    to read": a driver that is not an ArrayVideoDriver, a pixel format or
    rotation not in the tables above, a framebuffer shorter than the
    dimensions claim, or a libretro.py that has renamed an attribute. Every
    one of those is a fallback rather than an error.
    """
    frame = getattr(driver, "_frame", None)
    if frame is None:
        return None
    dimensions = _frame_dimensions(driver)
    if dimensions is None:
        return None
    width, height, pitch = dimensions

    pixel_format = getattr(driver, "_pixel_format", None)
    raw_mode = FAST_RAW_MODES.get(getattr(pixel_format, "name", None))
    if raw_mode is None:
        _note_slow_frame_grab(f"pixel format {getattr(pixel_format, 'name', pixel_format)!r}")
        return None
    if pitch < width * getattr(pixel_format, "bytes_per_pixel", 0):
        _note_slow_frame_grab(f"a pitch of {pitch} is too small for {width} pixels")
        return None

    rotation = getattr(driver, "_rotation", None)
    rotation_name = getattr(rotation, "name", None)
    if rotation_name not in FAST_ROTATIONS:
        _note_slow_frame_grab(f"rotation {rotation_name or rotation!r}")
        return None
    transpose = FAST_ROTATIONS[rotation_name]

    # A memoryview rather than bytes(frame[:n]): the array slice would copy
    # the buffer and bytes() would copy it again, and Pillow's raw decoder is
    # happy to read the driver's own memory. 0.17ms -> 0.05ms on a NES frame.
    # It is released before returning: ArrayVideoDriver replaces its array
    # rather than resizing it, so an exported view cannot actually block a
    # refresh, but a view on someone else's buffer is not a thing to leave
    # lying around.
    buffer = None
    try:
        buffer = memoryview(frame)
        if buffer.itemsize != 1:
            _note_slow_frame_grab(f"a framebuffer of {buffer.itemsize}-byte items")
            return None
        wanted = pitch * height
        if buffer.nbytes < wanted:
            _note_slow_frame_grab(f"a {buffer.nbytes} byte framebuffer where {wanted} was needed")
            return None
        image = Image.frombuffer("RGB", (width, height), buffer[:wanted], "raw", raw_mode, pitch, 1)
        table = FAST_POINT_TABLES[pixel_format.name]
        if table is not None:
            # A C loop over the whole image, ~0.02ms, and what makes this
            # exactly equal to screenshot() rather than merely equivalent.
            image = image.point(table)
        if transpose is not None:
            image = image.transpose(getattr(Image.Transpose, transpose))
        return image
    except Exception as exc:  # a Pillow without one of these raw decoders
        _note_slow_frame_grab(f"Pillow could not decode {raw_mode!r} ({exc})")
        return None
    finally:
        if buffer is not None:
            try:
                buffer.release()
            except BufferError:  # something is still holding a slice of it
                pass


def fast_frame_size(driver):
    """
    A video driver's frame size, without converting a single pixel.

    ``output_size()`` only ever wanted the height, and paid for a full
    screenshot() to get it -- which is why recording fifteen pictures used to
    convert sixteen frames. The width and height are swapped for a sideways
    rotation, exactly as screenshot() does it.
    """
    dimensions = _frame_dimensions(driver)
    if dimensions is None:
        return None
    width, height, _pitch = dimensions
    rotation = getattr(driver, "_rotation", None)
    rotation_name = getattr(rotation, "name", None)
    if rotation_name in ("NINETY", "TWO_SEVENTY"):
        # Sideways: the picture is as tall as the frame is wide. This covers
        # NINETY even though fast_frame_image does not, because the *size* of
        # a 90 degree rotation is right even where libretro.py's pixels are
        # not.
        return height, width
    if rotation_name in ("NONE", "ONE_EIGHTY"):
        return width, height
    return None

# -- Clip arithmetic ----------------------------------------------------------
#
# Seconds in, frames out. These are plain functions of a frame rate rather
# than methods so the view can lay a press schedule out before a core has
# been loaded (to decide whether to draw the repeat button at all, and what
# number to put on it) and so the arithmetic can be tested without one.
# RetroEmulator's methods of the same names (retro/emulator.py) are thin
# wrappers that pass the core's own fps.


def frame_count(fps: float, seconds: float) -> int:
    """How many emulated frames last roughly this long, at least one."""
    return max(1, round(float(fps) * float(seconds)))


def clip_frame_count(fps: float, seconds: float) -> int:
    """
    How many emulated frames one clip of this length covers.

    Never fewer than MIN_CLIP_FRAMES, so a clip is always an animation with
    room for a press in it.
    """
    return max(MIN_CLIP_FRAMES, frame_count(fps, seconds))


def capture_step(fps: float, clip_fps: int = CLIP_FPS) -> int:
    """
    How many emulated frames one picture of the clip covers.

    4 for a 15 fps clip off a 59.73 fps Game Boy. Only every ``step``-th
    emulated frame is captured, which is what makes the clip a quarter of the
    size for no loss of readability.
    """
    rate = max(1, min(round(float(fps)), int(clip_fps)))
    return max(1, round(float(fps) / rate))


def capture_plan(
    frames: int, step: int
) -> typing.List[typing.Tuple[int, int]]:
    """
    Which emulated frames of a clip are photographed, and for how long each.

    Returns ``[(frame index, emulated frames that picture stands for), ...]``,
    oldest first, where the index is the 0-based frame of the recording --
    exactly what ``RetroEmulator.record_frames`` counts with. The picture taken on
    index ``i`` shows the game after ``i + 1`` emulated frames, and stands
    until the next picture is taken, so the durations always add up to
    ``frames`` and the clip plays for as long as it emulated.

    Two frames are always photographed, whatever ``step`` is, and between them
    they are the whole of why clip boundaries are seamless -- see the seam
    block above, which is the one-frame rule these two halves add up to:

    * **frame 0**, so a press scheduled at the start of the clip is already
      landing in the first picture the player sees, and so the clip opens one
      emulated frame after the previous clip's closing picture rather than
      some multiple of ``step`` later;
    * **the last frame**, so the picture the clip finishes on -- and holds,
      since clips are encoded with ``loop=1`` -- is the exact state the *next*
      clip carries on from. Without it the clip stopped on the last frame that
      happened to fall on the ``step`` cadence (frame 57 of a 60 frame Game
      Boy clip) while frames 58, 59 and 60 were emulated but never shown, so
      the still picture sitting in the channel between presses was three
      frames behind the console.

    Neither of them depends on ``step``, which is why the clip's frame rate
    cannot move the seam: at any ``step`` at all, the last picture of one clip
    is emulated frame ``frames`` of its window and the first picture of the
    next is frame 1 of the following window.

    Everything in between is on the regular ``step`` cadence, so the clip's
    timing is uniform except for the tail: a 60 frame clip at ``step`` 4 is
    fourteen 4-frame pictures, then a 3-frame one (frame 57) and a 1-frame one
    (frame 60). The last picture is the one that gets held after playback, so
    its short nominal duration costs nothing.

    When the last frame is already on the cadence -- ``frames`` of
    ``k * step + 1`` -- nothing is added and every picture but the last is a
    whole step.
    """
    frames = max(1, int(frames))
    step = max(1, int(step))
    indices = list(range(0, frames, step))
    if indices[-1] != frames - 1:
        indices.append(frames - 1)
    bounds = indices[1:] + [frames]
    return [
        (index, following - index)
        for index, following in zip(indices, bounds, strict=True)
    ]


def clip_plan(
    fps: float, frames: int, clip_fps: int = CLIP_FPS
) -> typing.List[typing.Tuple[int, int]]:
    """
    :func:`capture_plan` in milliseconds: which frames, shown for how long.

    ``[(frame index, that picture's duration in ms), ...]``, oldest first --
    exactly the durations ``RetroEmulator.record_frames`` captures with, and
    therefore exactly what ends up in the clip's ANMF chunks. A picture
    stands for ``covered`` emulated frames and is shown for as long as those
    frames took to emulate, which is what makes the clip play for as long as
    the window it photographed (see CLIP_FPS for the 0.4% the millisecond
    rounding costs).

    The floor of 1ms is the encoder's: WebP reads a duration of 0 as "as fast
    as the decoder can manage". It is applied here so that what this returns
    is what really gets written.

    ``fps`` is clamped to at least 1, which is what every other function here
    does with a frame rate it has to divide by.
    """
    rate = max(1.0, float(fps))
    step = capture_step(rate, clip_fps)
    return [
        (index, max(1, round(1000 * covered / rate)))
        for index, covered in capture_plan(frames, step)
    ]


def playback_seconds(fps: float, frames: int, clip_fps: int = CLIP_FPS) -> float:
    """
    How long the clip of a window this long plays for, in seconds.

    The sum of :func:`clip_plan`'s durations, i.e. the clip's real playing
    time rather than the nominal ``frames / fps``: a 60 frame Game Boy clip
    emulates 1.0046 seconds and plays for 1.005, because each picture's
    duration is a whole number of milliseconds.

    This is the number the message edits are paced against -- a clip is not
    replaced until it has had this long on screen -- which is why it is worth
    being the *encoded* duration rather than an approximation of it. See
    MAX_PACE_SECONDS in retro/RetroView.py.
    """
    return sum(duration for _, duration in clip_plan(fps, frames, clip_fps)) / 1000.0


def input_budget(fps: float, frames: int, clip_fps: int = CLIP_FPS) -> int:
    """
    The last frame of a clip that a button may still be released on.

    Everything scheduled into a clip has to be *up* by this frame, because
    the picture captured on it is the last one the clip has that is worth a
    whole step of playback, and a clip whose closing pictures are still
    mid-press does not show the player what their press did. Only every
    ``capture_step``-th frame is captured, so this is the last frame on that
    cadence (less MIN_AFTERMATH_FRAMES - 1 further pictures), not simply
    ``frames - 1``: releasing a button on frame 59 of a 60 frame clip would
    only ever be seen in the closing picture, which is held rather than
    played.

    :func:`capture_plan` also photographs the clip's final frame, so there is
    always *more* aftermath on screen than this reserves, never less -- the
    closing picture is taken after the budget and therefore after the
    release too.

    At four seconds this is 236 of 239 frames and no schedule ever came near
    it. At a fifth of a second it is 8 of 12, and it is what stops a 400ms
    hold, or the repeat button's three taps, from running off the end of the
    recording.
    """
    frames = max(1, int(frames))
    step = capture_step(fps, clip_fps)
    reserved = max(1, int(MIN_AFTERMATH_FRAMES)) - 1
    return max(1, ((frames - 1) // step - reserved) * step)


# -- How big the posted picture is -------------------------------------------
#
# A frame is drawn at a whole-number multiple of the console's own height and
# the width follows from the aspect ratio the core reports, so a NES frame
# comes out 4:3-ish instead of tall and narrow and the resize stays NEAREST
# (pixel art must never be smoothed). The only question is the multiple, and
# it used to be a flat 2 for anything up to 256 pixels tall.
#
# That was wasted work on every console bigger than a handheld. Discord scales
# an attached image down to the message column, so a 597x448 SNES clip is
# shrunk again in the client while costing ~4x the encode of a native-sized
# one. Measured on a Raspberry Pi 5, one second of real motion per console,
# best of three (a 1s clip is 60 emulated frames and 16 pictures):
#
#                     pixels     emulate   Pillow   encode    total    bytes
#   Game Boy   1x    160x144      20.7ms    6.4ms    6.9ms   31.6ms    3,576
#              2x    320x288      20.7ms    6.4ms   21.2ms   48.3ms    3,962
#   NES        1x    293x224      41.1ms    3.7ms   32.5ms   77.2ms    1,682
#              2x    585x448      41.1ms    8.9ms  124.9ms  174.9ms    1,920
#   SNES       1x    299x224      47.6ms    9.1ms   71.4ms  128.1ms   26,934
#              2x    597x448      47.6ms   16.6ms  201.5ms  265.7ms   33,482
#   Genesis    1x    293x224      43.6ms    -        23.1ms       -   12,700
#              2x    585x448      51.0ms    -        68.2ms       -   16,440
#   Master Sys 1x    293x192      29.4ms    -         6.4ms       -    1,554
#              2x    585x384      34.7ms    -        28.8ms       -    1,986
#
# So doubling a TV console costs 2.3-4x the encode and *more* bytes, for a
# picture Discord shrinks anyway: dropping to 1x takes a NES clip from 175ms
# to 77ms and a SNES clip from 266ms to 128ms, both roughly halved end to
# end. The Game Boy is the opposite case and genuinely needs the double --
# 160x144 is not readable on a phone -- and it costs 17ms there.
#
# Hence a rule in terms of the *posted* width rather than a per-core table:
# double (and double again) only while the picture would be narrower than
# MIN_CLIP_WIDTH, and never at all once it is as wide as the frame itself.
#
# MIN_CLIP_WIDTH has to sit in (240, 293] to say what the measurements say:
# above 240 so the Game Boy (160 wide at 1x) and the Game Boy Advance (240)
# still double, and at or below 293 so the NES does not. 280 is the middle of
# that window. The consoles land at:
#
#   Game Boy        160x144 -> 320x288     (2x, unchanged, and pinned by a test)
#   Game Boy Adv.   240x160 -> 480x320     (2x, unchanged)
#   NES             256x224 -> 293x224     (1x, was 585x448)
#   Super Nintendo  256x224 -> 299x224     (1x, was 597x448)
#   ...in hi-res    512x448 -> 597x448     (1x, unchanged)
#   Genesis         256x224 -> 293x224     (1x, was 585x448)
#   Master System   256x192 -> 293x192     (1x, was 585x384)
#
# Every row above is a frame size and aspect ratio read off the real core.
# Three cases follow from the same rule rather than from a measurement,
# because no freely-redistributable ROM was to hand for them: a
# 320-pixel-wide Genesis mode (320x224 at the 1.306 the core reports) doubles
# to 585x448, and the Neo Geo Pocket (160x152) and PC Engine (256x232) should
# land at roughly 320x304 and 303x232. Worth re-measuring the day somebody
# plays one.
#
# 293-303 pixels is a little under the 320-400 a message column would ideally
# be filled with, and there is nothing in between to pick: the next multiple
# is 585, because the height multiple is a whole number and the width follows
# from a fixed aspect ratio. Thirty pixels of width is not worth 100-200ms of
# every button press, so the narrower one wins.
#
# One knock-on worth knowing about: the old cutoff happened to give a SNES
# the same 597x448 in both its resolutions, and now lo-res is 299x224 and
# hi-res 597x448. A game that switches mid-session therefore posts clips of
# two different sizes, one press to the next, which costs nothing now that a
# clip is only ever encoded and posted on its own. Making them agree would
# mean either doubling every SNES clip again or throwing away half the
# columns of a hi-res one.
#
# The second condition -- never narrower than the frame -- is what stops the
# aspect correction from *losing* pixels. A Genesis game in its 320-pixel
# mode would be 293 wide at 1x, which with NEAREST means dropping 27 columns
# of somebody's picture, so it doubles instead. Widening 256 to 293 only
# repeats columns, which is the ordinary cost of a non-square pixel.
#
#: The narrowest a clip is posted at, before the frame's own width has a say.
MIN_CLIP_WIDTH = 280

#: The most a frame is ever enlarged. Nothing here reaches it -- the widest
#: thing needing two is the Game Boy Advance -- so it is a backstop against a
#: core reporting a tiny frame or a nonsense aspect ratio, which would
#: otherwise have this multiplying until the width cleared MIN_CLIP_WIDTH.
MAX_CLIP_SCALE = 2


def clip_scale(
    frame_width: int,
    frame_height: int,
    aspect: float,
    max_scale: int = MAX_CLIP_SCALE,
) -> int:
    """
    How many times over to draw a frame of this size, 1 or more.

    The smallest whole number that makes the picture at least
    MIN_CLIP_WIDTH wide *and* at least as wide as the frame itself, capped at
    ``max_scale``. See the block above for the measurements behind both
    conditions. ``max_scale`` of 1 means "native", which is what the
    screenshot helpers in the tests ask for.
    """
    frame_width = max(1, int(frame_width))
    frame_height = max(1, int(frame_height))
    aspect = float(aspect) if float(aspect) > 0.0 else frame_width / frame_height
    wanted = max(int(MIN_CLIP_WIDTH), frame_width)
    scale = 1
    while scale < max(1, int(max_scale)):
        if max(1, round(frame_height * scale * aspect)) >= wanted:
            break
        scale += 1
    return scale


def clip_size(
    frame_width: int,
    frame_height: int,
    aspect: float,
    max_scale: int = MAX_CLIP_SCALE,
) -> typing.Tuple[int, int]:
    """
    The size a frame of this shape should be posted at, in pixels.

    The height is a whole multiple of the console's own (see
    :func:`clip_scale`) and the width follows from the aspect ratio the core
    reports, so a Game Boy's square pixels land on exactly 320x288 and a NES
    frame is 4:3-ish rather than tall and narrow.
    """
    frame_width = max(1, int(frame_width))
    frame_height = max(1, int(frame_height))
    ratio = float(aspect) if float(aspect) > 0.0 else frame_width / frame_height
    scale = clip_scale(frame_width, frame_height, ratio, max_scale)
    height = max(1, frame_height * scale)
    return max(1, round(height * ratio)), height


def clamp_clip_seconds(value: typing.Any) -> float:
    """
    Whatever was configured, as a clip length this cog will actually record.

    Every path that reads a clip length goes through here: the setting is a
    float now, but an installation that set `[p]retroset cliplength 4` before
    it was has a plain ``int`` in Config (which is a perfectly good float),
    and a hand-edited settings file can hold anything at all. Rounding to
    hundredths keeps the number something that can be printed back without
    trailing noise, and 10ms is well under one frame of any console here.

    Clamping on *read* rather than on write is what makes lowering
    MAX_CLIP_SECONDS safe. An installation that set 15 while the ceiling was
    15 still has 15 in Config; the next session to read it gets 5, nothing
    raises, and the stored number is corrected the next time anybody runs
    `[p]retroset cliplength` (which clamps through here too). The same is
    true of a value below MIN_CLIP_SECONDS and of a value that is not a
    number at all.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(CLIP_SECONDS)
    if not math.isfinite(seconds):
        return float(CLIP_SECONDS)
    return float(min(MAX_CLIP_SECONDS, max(MIN_CLIP_SECONDS, round(seconds, 2))))


def format_seconds(seconds: float, places: int = 2) -> str:
    """
    A clip length as short as it can honestly be written.

    ``1.0`` -> ``"1"``, ``0.8`` -> ``"0.8"``, ``0.25`` -> ``"0.25"``. Nobody
    wants to read "1.0 seconds" for the default. ``places`` is for a figure
    that is an approximation rather than a setting, where a third decimal
    would be noise: at ``places=1``, 3.0135 seconds reads "3".
    """
    value = round(float(seconds), max(0, int(places)))
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def describe_seconds(seconds: float, places: int = 2) -> str:
    """``1.0`` -> ``"1 second"``, ``0.8`` -> ``"0.8 seconds"``."""
    text = format_seconds(seconds, places)
    return f"{text} second" if text == "1" else f"{text} seconds"

# -- Encoding a clip ----------------------------------------------------------
#
# Everything below works on Pillow images rather than on a running core, so it
# is importable and testable with nothing but Pillow.


def _pillow():
    """Import Pillow, turning a missing dependency into an EmulatorError."""
    try:
        from PIL import Image
    except Exception as exc:  # ImportError or a broken install
        raise EmulatorError(f"Pillow could not be loaded: {exc}") from exc
    return Image


def encode_animation(
    images, duration_ms, *, lossless: bool = True, quality: int = 100
) -> bytes:
    """
    Turn a list of same-sized Pillow images into one animated WebP.

    ``duration_ms`` is either a list with one entry per frame or a single
    duration for all of them. The cog itself only ever passes a list, because
    :func:`capture_plan` gives the closing picture a shorter nominal duration
    than the rest -- but the scalar form is not dead: ``tests/fakes.py``
    builds its stand-in clips with ``encode_animation(images, FAKE_FRAME_MS)``
    and every fast test that reads a clip back depends on it. It is also the
    honest shape for "all the same length", which is what a caller with no
    capture plan in hand has.
    """
    buffer = io.BytesIO()
    if isinstance(duration_ms, (list, tuple)):
        durations = [max(1, int(value)) for value in duration_ms]
    else:
        durations = max(1, int(duration_ms))
    try:
        images[0].save(
            buffer,
            format="WEBP",
            save_all=True,
            append_images=images[1:],
            duration=durations,
            # loop=1 plays the clip through exactly once and holds its last
            # frame, which is what makes the picture left in the channel the
            # state the next press carries on from; loop=0 would mean
            # "forever" and fill a busy channel with flickering.
            loop=1,
            # Lossless, at quality 100, is what every ordinary clip is: these
            # are flat-shaded console frames with hard edges, which is
            # exactly what lossless WebP is good at, and a lossy pass on
            # sprite art shows its mistakes. The knobs exist for one case --
            # a clip too big for the server to accept, where a visibly
            # softer picture beats no picture at all. See shrink_clip.
            lossless=lossless,
            # See WEBP_METHOD and WEBP_MINIMIZE_SIZE, which carry the
            # measurements these three numbers were chosen from.
            quality=quality,
            method=WEBP_METHOD,
            minimize_size=WEBP_MINIMIZE_SIZE,
        )
    except Exception as exc:
        raise EmulatorError(f"The clip could not be encoded: {exc}") from exc
    return buffer.getvalue()


class CapturedClip(typing.NamedTuple):
    """A recorded-but-not-yet-encoded clip: what ``record_frames`` hands over.

    Everything :func:`encode_clip` needs and nothing that touches a core,
    which is the point of splitting the two: capturing frames needs the
    emulator (and therefore whatever lock serializes access to it), encoding
    them does not, so a caller can capture under the lock, release it, and
    encode -- the most expensive CPU step of a button press -- while the next
    press is already emulating.
    """

    #: The captured pictures as Pillow images, oldest first, each at the
    #: core's own resolution rather than the posted size. That is what makes
    #: holding a clip's worth of them cheap: the posted picture is up to
    #: MAX_CLIP_SCALE times the frame in each direction (a Game Boy's 160x144
    #: posts at 320x288), so output-sized frames cost scale-squared the
    #: memory of native ones for no information at all -- the enlargement
    #: only ever repeats pixels.
    images: typing.List
    #: Milliseconds each picture is shown for, one entry per image; see
    #: :func:`clip_plan`, whose durations these are.
    durations: typing.List[int]
    #: The ``(width, height)`` the clip is posted at (see :func:`clip_size`),
    #: decided from the first captured frame and pinned there, so a core that
    #: changes resolution mid-clip (libretro's SET_GEOMETRY; the SNES does)
    #: cannot change the answer part-way through.
    size: typing.Tuple[int, int]


def encode_clip(
    captured: CapturedClip, *, lossless: bool = True, quality: int = 100
) -> bytes:
    """Turn a :class:`CapturedClip` into the WebP bytes the cog posts.

    The other half of ``RetroEmulator.record``, and deliberately a plain
    function of the captured data rather than a method: it needs no core, so
    a caller serializing emulator access can run it with the lock released.

    Each picture is enlarged from the core's resolution to ``captured.size``
    with NEAREST here -- the same single resize the recording used to do per
    frame at capture time, so the bytes that come out are identical -- and
    only once per *run* of identical pictures: libwebp already merges
    identical consecutive frames (which is why a static screen costs a
    handful of bytes, see :func:`encode_animation`), so resizing every copy
    of a picture the encoder was about to fold away was pure waste. Spotting
    a run costs one ``tobytes()`` per native-sized frame, which is far
    cheaper than the resize it skips.

    A picture whose size is not ``captured.size`` -- every frame of a 1x
    console, and any frame from a mid-clip geometry change -- goes straight
    from its own resolution to the posted one in that single step, never
    through a chain, so no frame handed to the encoder can disagree with its
    siblings about the size.
    """
    images, durations, size = captured
    Image = _pillow()
    size = tuple(size)
    posted = []
    last_key = None
    last_resized = None
    for image in images:
        if image.size != size:
            key = (image.size, image.tobytes())
            if key != last_key:
                last_key = key
                last_resized = image.resize(size, Image.NEAREST)
            image = last_resized
        posted.append(image)
    return encode_animation(posted, durations, lossless=lossless, quality=quality)


#: What to try, in order, when a lossless clip is bigger than the server will
#: accept. Each step is (lossless, quality, scale-of-the-posted-size).
#:
#: The order is "give up the least first". Lossy at 80 is the cheapest thing
#: that helps and is still perfectly readable on console art; 60 is visibly
#: soft but legible; halving the picture is the last resort, because a
#: smaller screen costs everybody in the channel something a slightly mushier
#: one does not.
#:
#: The alternative -- what this cog did before -- was to post it, let Discord
#: refuse it, and tell the player to ask the owner to lower `cliplength`.
#: That spends the whole round trip (emulate, encode, upload) before
#: failing, the press is gone, and the person told to change a setting is
#: usually not the person who can.
CLIP_SHRINK_STEPS = (
    (False, 80, 1.0),
    (False, 60, 1.0),
    (False, 60, 0.5),
)


def shrink_clip(
    captured: CapturedClip, limit: int, current: typing.Optional[bytes] = None
) -> bytes:
    """
    Re-encode a clip until it fits ``limit`` bytes, or give up gracefully.

    Only ever reached when the ordinary lossless encode came out too big for
    the server it is going to (Discord's per-guild attachment limit, which
    boosting raises). Tries CLIP_SHRINK_STEPS in order and returns the first
    result that fits.

    ``current`` is what the caller already has -- the lossless encode that
    was too big -- and passing it is what makes this safe, because **lossy
    is not reliably smaller here**. These are flat-shaded console frames with
    large areas of one colour and hard edges: lossless WebP is extremely good
    at exactly that, and a lossy pass can come out *many times larger* by
    spending bits on the edges it is trying to approximate. (Measured on a
    synthetic 320x288 pattern: 1.1 KB lossless against 188 KB at quality 80.)
    So every attempt is compared against what the caller already had, and
    nothing bigger is ever handed back.

    If nothing fits, the **smallest** candidate is returned rather than an
    error: the caller still has to hand Discord something, a clip that is
    merely probably-too-big is worth the attempt, and the existing 40005
    handling is the backstop underneath it. Never raises anything an
    ordinary encode would not.
    """
    smallest = current
    if current is not None and len(current) <= limit:
        return current
    for lossless, quality, scale in CLIP_SHRINK_STEPS:
        if scale != 1.0:
            width, height = captured.size
            resized = (max(1, int(width * scale)), max(1, int(height * scale)))
            attempt = captured._replace(size=resized)
        else:
            attempt = captured
        encoded = encode_clip(attempt, lossless=lossless, quality=quality)
        if len(encoded) <= limit:
            return encoded
        if smallest is None or len(encoded) < len(smallest):
            smallest = encoded
    return smallest
