"""
Clips: the arithmetic, the encoder, and the fast frame grab.

Everything in here is a plain function of numbers, bytes or Pillow images.
Nothing in here touches libretro, so it imports and tests with no core, no
shared object and no ``libretro.py`` installed at all -- which is what makes
the clip arithmetic (and the pixel-exactness of the fast frame grab) cheap
enough to cover in the fast test suite. Pillow is needed to *encode* or
*decode* a clip and is imported lazily, so even that is only paid for by the
callers that do it.

Three groups, and they only meet in retro/emulator.py:

* the clip/timing arithmetic -- seconds in, emulated frames out;
* the animation encoder and decoder, and the stitching the Replay button
  does;
* the fast frame grab, which decodes a video driver's framebuffer with
  Pillow instead of letting libretro.py convert it a pixel at a time.

:class:`EmulatorError` lives here rather than next to the emulator because
both halves raise it and this is the half that cannot import the other one.
``retro.emulator`` re-exports every name below, so nothing downstream had to
change when this module was split out of it.
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
# exactly 67ms per frame -- a 1 second clip is 60 emulated frames, 15 pictures
# and measures 1.005s, which is 0.5% slow and invisible. (GIF is the format
# that stores centiseconds; see encode_animation.) 20 fps would land on a round 50ms
# and match the emulated time exactly, at about 39% more bytes and 32% more
# encoding time, so 15 stays the default.
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
MIN_CLIP_SECONDS = 0.2
MAX_CLIP_SECONDS = 15.0

# The fewest emulated frames a clip may be, whatever it was asked for. At
# CLIP_FPS against a 60 fps core one picture is four frames, so six frames is
# two pictures -- the least that is still an animation -- and leaves room for
# a press plus the aftermath frame below. MIN_CLIP_SECONDS is well clear of
# it on every console here (0.2s is 10 frames even on a 50 fps PAL core), so
# this is a floor for a core that reports a strange frame rate and for direct
# callers of record(), not something the settings can reach.
MIN_CLIP_FRAMES = 6

# How many emulated frames at the end of a clip are kept clear of input, so
# the last picture shows the game *after* the press rather than still under
# it. Counted in captured frames by input_budget(), which is what makes it
# one visible picture rather than one invisible frame.
MIN_AFTERMATH_FRAMES = 1

# How much footage the Replay button stitches back together, and the hard
# frame cap that bounds how long doing so can possibly take.
#
# Measured on the machine this was written on (a Raspberry Pi 5), decoding and
# re-encoding four buffered clips into one animation:
#
#     content                                frames   decode  encode   total
#     uCity title screen (Game Boy)              87     0.04s   1.28s   1.32s
#     Pokemon intro, real motion (Game Boy)     115     0.04s   0.36s   0.39s
#     nestest (NES)                               8     0.01s   0.04s   0.05s
#     synthetic worst case, 320x288             240     0.13s   3.29s   3.41s
#     synthetic worst case, 512x448             240     0.35s   6.60s   6.95s
#
# The synthetic rows are every 4x4 block of every frame changing every frame,
# which no real game does; they are there to show the ceiling. 300 frames is
# 20 seconds at CLIP_FPS and keeps even that ceiling inside single digits,
# while 15 seconds of real footage costs well under two.
#
# Every encode figure above is now an overestimate: they were taken with
# libwebp's minimize_size on, and it has since been measured as pure cost and
# switched off (see WEBP_MINIMIZE_SIZE), which took 25-30% off every encode
# here. The ceiling this bounds only got lower.
#
# 300 frames is also more than fifteen seconds needs at *any* clip length,
# which is what makes the seconds the binding cap rather than the frames: a
# clip contributes CLIP_FPS pictures per second of footage however it is
# sliced, so fifteen seconds is about 225 pictures whether that is four 4s
# clips, fifteen 1s clips or seventy-five 0.2s ones.
REPLAY_SECONDS = 15
MAX_REPLAY_FRAMES = 300

# How many clips one session keeps in memory to replay, and how many bytes of
# them.
#
# The count used to be 8, which was fine when a clip was four seconds and
# became the cap that bit first when the default became one: eight one-second
# clips are eight seconds, so Replay could never reach the fifteen it
# promised. It is derived from the two numbers that decide it instead --
# enough clips to cover REPLAY_SECONDS at the shortest clip length allowed,
# plus the one that straddles the fifteen-second edge (concatenate_clips
# trims that one frame by frame) -- so it cannot fall behind either again.
#
# Clip count is not what bounds the memory: bytes track *footage* far more
# than the number of files it arrived in, because a short clip holds
# proportionally fewer pictures. Measured on a Raspberry Pi 5, fifteen
# seconds of Pokemon Red in the overworld:
#
#     clip length   clips   buffer   stitched   stitch time
#     4s                5   18.5 KiB   7.8 KiB       0.15s
#     1s               16   30.7 KiB  12.9 KiB       0.22s
#     0.8s             20   35.0 KiB  12.1 KiB       0.20s
#     0.2s             76   80.8 KiB  25.2 KiB       0.40s
#
# So the shortest clips cost about four times the bytes of the longest for
# the same footage (each file repeats a keyframe), which is 81 KiB against a
# cap of 8 MiB. The cap stays where it is as a backstop against a
# pathological game, two orders of magnitude clear of anything measured, and
# MAX_REPLAY_FRAMES is still what bounds the encode time.
MAX_REPLAY_CLIPS = int(math.ceil(REPLAY_SECONDS / MIN_CLIP_SECONDS)) + 1
MAX_REPLAY_BYTES = 8 * 1024 * 1024

# Animated WebP, encoded losslessly, is what gets posted. On a 75 frame Game
# Boy clip it is 166 KiB where the equivalent GIF was 877 KiB, and on a SNES
# clip 175 KiB against 1.52 MiB -- while being pixel-exact rather than
# quantized down to 64 colours. It costs about 0.9s more to encode.
#
# Re-measured at the current defaults (see WEBP_METHOD below) on a 4 second
# clip, the gap is the same shape: 24.4 KiB against a 53.2 KiB GIF on a
# moving Game Boy screen, and 248 KiB against 998 KiB on the SNES. WebP costs
# 0.15s more on the Game Boy and 1.3s more on the SNES, which is the price of
# being exact.
#
# GIF is kept as a fallback for anywhere animated WebP is not welcome. It is
# never selected automatically.
CLIP_FORMATS = ("WEBP", "GIF")
DEFAULT_CLIP_FORMAT = "WEBP"

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
#   Game Boy, real motion (Pokemon title screen, every picture distinct)
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
# quality is a no-op here and is left at 100 for clarity: in lossless mode
# libwebp reads it as an effort dial, and dropping it to 75 or 50 produced
# byte-identical Game Boy clips and *larger* SNES ones (45,662 against 40,012
# at method=0) for no useful time saving.
WEBP_METHOD = 1
WEBP_MINIMIZE_SIZE = False

# GIF has no lossless mode, so the fallback path quantizes first. The consoles
# here have small palettes, so this is very nearly lossless for them.
GIF_COLORS = 64


def clip_extension(clip_format: str = DEFAULT_CLIP_FORMAT) -> str:
    """``"WEBP"`` -> ``".webp"``."""
    return f".{str(clip_format).lower()}"


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
#: Rotation.NINETY is deliberately absent: libretro.py 0.6.0 computes its
#: starting offset as ``(width - 4) * height * 4`` where a 90 degree rotation
#: needs ``(width - 1) * ...``, so its output is shifted by three rows and
#: wraps. That is a bug in libretro.py, but fixing it here would change what
#: the cog posts for a rotated core, so a 90 degree rotation takes the
#: official path and keeps the bug. Rotation.ONE_EIGHTY and TWO_SEVENTY are
#: proved identical to it (see tests/test_emulator.py).
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
    possible values -- invisible, but not *identical*, and identical is what
    makes a cheap regression test possible.

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

#: Reasons the fast grab has already been logged as unavailable, so a core
#: that cannot use it says so once instead of once per frame (fifteen times a
#: clip, several clips a minute).
_SLOW_GRAB_LOGGED: set = set()


def _note_slow_frame_grab(reason: str) -> None:
    """Log, once per reason per process, that the fast frame grab stood down."""
    if reason not in _SLOW_GRAB_LOGGED:
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
# been loaded (to label the repeat button, which has to say how many taps it
# will really do) and so the arithmetic can be tested without one.
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


def input_budget(fps: float, frames: int, clip_fps: int = CLIP_FPS) -> int:
    """
    The last frame of a clip that a button may still be released on.

    Everything scheduled into a clip has to be *up* by this frame, because
    the picture captured on it is the last one the clip has and a clip whose
    last picture is still mid-press does not show the player what their press
    did. Only every ``capture_step``-th frame is captured, so this is the
    last captured frame (less MIN_AFTERMATH_FRAMES - 1 further pictures),
    not simply ``frames - 1``: releasing a button on frame 59 of a 60 frame
    clip would never be seen, since the last picture was taken on frame 56.

    At four seconds this is 236 of 239 frames and no schedule ever came near
    it. At a fifth of a second it is 8 of 12, and it is what stops a 400ms
    hold, or the repeat button's three taps, from running off the end of the
    recording.
    """
    frames = max(1, int(frames))
    step = capture_step(fps, clip_fps)
    reserved = max(1, int(MIN_AFTERMATH_FRAMES)) - 1
    return max(1, ((frames - 1) // step - reserved) * step)


def clamp_clip_seconds(value: typing.Any) -> float:
    """
    Whatever was configured, as a clip length this cog will actually record.

    Every path that reads a clip length goes through here: the setting is a
    float now, but an installation that set `[p]retroset cliplength 4` before
    it was has a plain ``int`` in Config (which is a perfectly good float),
    and a hand-edited settings file can hold anything at all. Rounding to
    hundredths keeps the number something that can be printed back without
    trailing noise, and 10ms is well under one frame of any console here.
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
    wants to read "1.0 seconds" for the default. ``places=1`` is for the
    Replay button, whose figure is an approximation of a buffer anyway and
    which must not turn 3.0135 seconds of footage into "3.01s".
    """
    value = round(float(seconds), max(0, int(places)))
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def describe_seconds(seconds: float, places: int = 2) -> str:
    """``1.0`` -> ``"1 second"``, ``0.8`` -> ``"0.8 seconds"``."""
    text = format_seconds(seconds, places)
    return f"{text} second" if text == "1" else f"{text} seconds"

# -- Clips, after the fact ----------------------------------------------------
#
# Everything below works on encoded clips rather than on a running core, so it
# is importable and testable with nothing but Pillow. It is what the Replay
# button uses to stitch the last few clips back into one animation.


def _pillow():
    """Import Pillow, turning a missing dependency into an EmulatorError."""
    try:
        from PIL import Image
    except Exception as exc:  # ImportError or a broken install
        raise EmulatorError(f"Pillow could not be loaded: {exc}") from exc
    return Image


def encode_animation(images, duration_ms, clip_format: str = DEFAULT_CLIP_FORMAT) -> bytes:
    """
    Turn a list of same-sized Pillow images into one animation.

    ``duration_ms`` is either one duration for every frame or a list with one
    entry per frame, which is what stitching several clips together needs:
    Pillow's own encoder collapses runs of identical frames and adds their
    durations together, so a clip read back out does not have a uniform frame
    time any more.
    """
    buffer = io.BytesIO()
    if isinstance(duration_ms, (list, tuple)):
        durations = [max(1, int(value)) for value in duration_ms]
    else:
        durations = max(1, int(duration_ms))
    try:
        if clip_format == "WEBP":
            images[0].save(
                buffer,
                format="WEBP",
                save_all=True,
                append_images=images[1:],
                duration=durations,
                # loop=1 plays the clip through exactly once, matching
                # the Replay button; loop=0 would mean "forever" and fill
                # a busy channel with flickering.
                loop=1,
                lossless=True,
                # See WEBP_METHOD and WEBP_MINIMIZE_SIZE, which carry the
                # measurements these three numbers were chosen from.
                quality=100,
                method=WEBP_METHOD,
                minimize_size=WEBP_MINIMIZE_SIZE,
            )
        else:
            # GIF durations are stored in centiseconds, so round to 10ms
            # here instead of letting the encoder truncate and play the
            # clip too fast. This is the one place the clip's timing is
            # not exact: 67ms a frame becomes 70ms, so a GIF plays about
            # 4.5% slower than the game did. WebP has millisecond frame
            # durations and does not need this. No loop= argument on
            # purpose: Pillow only writes the looping extension when one
            # is given.
            #
            # optimize=True made these GIFs 16-40% *bigger* (the frames
            # are already palette images), as well as slower.
            if isinstance(durations, list):
                rounded = [max(10, round(value / 10) * 10) for value in durations]
            else:
                rounded = max(10, round(durations / 10) * 10)
            images[0].save(
                buffer,
                format="GIF",
                save_all=True,
                append_images=images[1:],
                duration=rounded,
                optimize=False,
            )
    except Exception as exc:
        raise EmulatorError(f"The clip could not be encoded: {exc}") from exc
    return buffer.getvalue()


def decode_clip(
    data: bytes, fallback_ms: typing.Optional[float] = None
) -> typing.Tuple[list, typing.List[int]]:
    """
    Read one encoded clip back into ``(frames, per-frame durations in ms)``.

    Durations come from the file rather than being assumed, because the
    encoder merges identical consecutive frames: a clip of a title screen that
    went in as sixty 67ms frames comes back out as two frames of 67ms and
    3948ms, and re-encoding it with a flat 67ms would play it forty times too
    fast.

    ``fallback_ms`` is for the one case where the file records no duration at
    all. A clip in which *every* picture is identical -- a title screen, a
    menu, a game waiting for input, all of which are far likelier now a clip
    is one second rather than four -- collapses to a single image, and libwebp
    then writes a plain still WebP with no animation chunks in it. That is a
    perfectly good clip and Discord shows it, but nothing in the file says it
    stood for a second of play, so a caller that knows how long the clip was
    meant to be should say so; otherwise such a frame counts as 1ms and the
    Replay button under-reports how much footage it stitched.
    """
    Image = _pillow()
    default = max(1, round(float(fallback_ms))) if fallback_ms else 1
    try:
        image = Image.open(io.BytesIO(bytes(data)))
        frames = []
        durations = []
        for index in range(max(1, int(getattr(image, "n_frames", 1)))):
            image.seek(index)
            frames.append(image.convert("RGB"))
            recorded = image.info.get("duration")
            durations.append(max(1, int(recorded)) if recorded else default)
    except EmulatorError:
        raise
    except Exception as exc:
        raise EmulatorError(f"A clip could not be read back: {exc}") from exc
    if not frames:
        raise EmulatorError("A clip had no frames in it.")
    return frames, durations


def concatenate_clips(
    clips: typing.Sequence[bytes],
    *,
    seconds: "typing.Optional[typing.Sequence[float]]" = None,
    max_seconds: float = REPLAY_SECONDS,
    max_frames: int = MAX_REPLAY_FRAMES,
    clip_format: str = DEFAULT_CLIP_FORMAT,
) -> typing.Tuple[bytes, float]:
    """
    Stitch recent clips into one animation, newest last.

    ``clips`` is oldest-first, the way the session buffered them. The result
    ends at the newest frame and reaches as far back as the budget allows, so
    a player who presses Replay always sees the moment they just played and
    however much of the run-up fits; the oldest footage is what gets dropped.
    Trimming is per *frame*, not per clip, so a single clip longer than the
    budget still works.

    ``seconds`` is how long each clip was meant to be, in the same order,
    which the session knows and the files do not always say: a clip in which
    nothing moved is written as a single still image with no timing in it at
    all (see :func:`decode_clip`). Without it such a clip counts as one
    millisecond of footage, and a replay of a menu screen reports having
    stitched nothing.

    Returns ``(encoded bytes, seconds covered)``.

    Three things bound the work, because this decodes and re-encodes real
    video on the bot's event loop's thread pool:

    * ``max_seconds`` of footage, measured from the clips' own frame timings;
    * ``max_frames``, which is what actually caps the encoder's runtime;
    * a resolution change, which ends the run. Animation formats have one size
      for the whole file, and a SNES switching to its high-resolution mode
      mid-session really does leave clips of two different sizes in the
      buffer. The newest size wins and anything older is left out.

    :raises EmulatorError: if nothing could be decoded or the result could not
        be encoded.
    """
    if not clips:
        raise EmulatorError("There are no clips to replay.")
    budget_ms = max(1.0, float(max_seconds) * 1000.0)
    max_frames = max(1, int(max_frames))

    frames: list = []
    durations: typing.List[int] = []
    total_ms = 0
    size = None
    failures = 0
    # Nominal lengths, newest first like the loop below, padded with None so
    # a caller that passes none (or too few) still works.
    lengths = list(reversed(list(seconds or ())))
    for position, data in enumerate(reversed(list(clips))):
        if total_ms >= budget_ms or len(frames) >= max_frames:
            break
        nominal = lengths[position] if position < len(lengths) else None
        try:
            clip_frames, clip_durations = decode_clip(
                data, None if nominal is None else float(nominal) * 1000.0
            )
        except EmulatorError:
            # One unreadable clip in the buffer should cost that clip, not the
            # replay. Stop here rather than skipping it: the frames are a
            # timeline, and leaving a hole in the middle would be a lie.
            failures += 1
            break
        if size is None:
            size = clip_frames[0].size
        elif clip_frames[0].size != size:
            break
        taken: list = []
        taken_durations: typing.List[int] = []
        for image, duration in zip(
            reversed(clip_frames), reversed(clip_durations), strict=False
        ):
            if len(frames) + len(taken) >= max_frames:
                break
            # The first frame is always taken even if it overruns the budget,
            # so a replay is never empty.
            if total_ms + duration > budget_ms and (taken or frames):
                break
            taken.append(image)
            taken_durations.append(duration)
            total_ms += duration
        taken.reverse()
        taken_durations.reverse()
        frames = taken + frames
        durations = taken_durations + durations

    if not frames:
        raise EmulatorError(
            "None of the buffered clips could be read back."
            if failures
            else "There are no clips to replay."
        )
    return encode_animation(frames, durations, clip_format), total_ms / 1000.0
