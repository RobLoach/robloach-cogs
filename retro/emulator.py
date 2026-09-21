"""
Standalone retro console emulator built on libretro.py.

This module has no Red-DiscordBot or discord.py imports so it can be used and
tested on its own. All methods are synchronous and not thread-safe; async
callers should run them in a single worker thread (e.g. asyncio.to_thread)
and serialize access with a lock.

Supports both the SessionBuilder API of libretro.py <= 0.6.x (the newest
release available on Python 3.11, which Red-DiscordBot requires) and the
Session constructor API of libretro.py >= 0.7.
"""

import io
import logging
import math
import re
import typing
from pathlib import Path

__all__ = [
    "RetroEmulator",
    "EmulatorError",
    "BUTTONS",
    "MIN_ROM_SIZE",
    "DEFAULT_FPS",
    "CLIP_SECONDS",
    "CLIP_FPS",
    "MIN_CLIP_SECONDS",
    "MAX_CLIP_SECONDS",
    "MIN_CLIP_FRAMES",
    "MIN_AFTERMATH_FRAMES",
    "REPLAY_SECONDS",
    "MAX_REPLAY_FRAMES",
    "MAX_REPLAY_CLIPS",
    "MAX_REPLAY_BYTES",
    "CLIP_FORMATS",
    "DEFAULT_CLIP_FORMAT",
    "MAX_SRAM_SIZE",
    "RETRO_MEMORY_SAVE_RAM",
    "capture_step",
    "clamp_clip_seconds",
    "clip_extension",
    "clip_frame_count",
    "concatenate_clips",
    "decode_clip",
    "describe_definitions",
    "describe_seconds",
    "encode_animation",
    "format_seconds",
    "frame_count",
    "input_budget",
    "probe_core_options",
]

log = logging.getLogger("red.robloach.retro.emulator")

# Everything a core itself prints goes here, on its own logger so it can be
# turned up in isolation. See _CoreLogDriver for why it is nearly all DEBUG.
core_log = logging.getLogger("red.robloach.retro.core")

# A printf conversion specifier that made it into a log message verbatim. The
# flag characters are deliberately restrictive (no space flag) so that an
# honest "100% complete" is not mistaken for one.
FORMAT_SPECIFIER = re.compile(
    r"%[-+#0]*[0-9]*(?:\.[0-9]+)?(?:hh|h|ll|l|L|z|j|t)?[diouxXeEfFgGaAcsp]"
)

# How many core errors are allowed through at WARNING before the rest of the
# session's are demoted to DEBUG. A core that fails once per frame must not be
# able to fill the bot's log.
MAX_CORE_WARNINGS = 5

# Every field of libretro's RetroPad, in RETRO_DEVICE_ID_JOYPAD order. Which
# of them a console actually uses (and what its own manual calls them) lives
# in systems.py; this is just the set the emulator will accept.
BUTTONS = (
    "b", "y", "select", "start", "up", "down", "left", "right",
    "a", "x", "l", "r", "l2", "r2", "l3", "r3",
)

# The smallest file worth handing to a core. This is a sanity floor, not a
# header check: the consoles here range from 2 KiB Atari 2600 carts upwards,
# and every core does its own validation anyway. Anything under this is a
# truncated download or an error page.
MIN_ROM_SIZE = 1024

# Consoles do not agree on a frame rate (Game Boy 59.73, NES 60.10, PAL
# machines 50), so timings are taken from the core's own av_info at runtime.
# This is only the fallback for a core that reports nothing useful.
DEFAULT_FPS = 60.0

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
# that stores centiseconds; see _encode.) 20 fps would land on a round 50ms
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

# Frames taller than this are shown at 1x; anything smaller is doubled. The
# SNES switches to a 512x478 interlaced mode mid-game, and doubling *that*
# would be a 1274x956 clip, so the cutoff keeps every console in the same
# ballpark without a per-core table to maintain.
DOUBLE_UP_TO_HEIGHT = 256

# Animated WebP, encoded losslessly, is what gets posted. On a 75 frame Game
# Boy clip it is 166 KiB where the equivalent GIF was 877 KiB, and on a SNES
# clip 175 KiB against 1.52 MiB -- while being pixel-exact rather than
# quantized down to 64 colours. It costs about 0.9s more to encode.
#
# GIF is kept as a fallback for anywhere animated WebP is not welcome. It is
# never selected automatically.
CLIP_FORMATS = ("WEBP", "GIF")
DEFAULT_CLIP_FORMAT = "WEBP"

# GIF has no lossless mode, so the fallback path quantizes first. The consoles
# here have small palettes, so this is very nearly lossless for them.
GIF_COLORS = 64

# RETRO_MEMORY_SAVE_RAM, i.e. the cartridge's battery-backed save memory. It is
# 0 in libretro.h and has been since libretro existed, but it is spelled out
# here so the SRAM code reads as something other than a magic number.
RETRO_MEMORY_SAVE_RAM = 0

# A sanity ceiling for a battery save. The largest cartridge SRAM any of these
# consoles ever shipped is 128 KiB (µCity's Game Boy Color cart reports exactly
# that), so anything past a megabyte is a core reporting nonsense and is not
# worth writing to disk on every few button presses.
MAX_SRAM_SIZE = 1024 * 1024


def clip_extension(clip_format: str = DEFAULT_CLIP_FORMAT) -> str:
    """``"WEBP"`` -> ``".webp"``."""
    return f".{str(clip_format).lower()}"


# -- Clip arithmetic ----------------------------------------------------------
#
# Seconds in, frames out. These are plain functions of a frame rate rather
# than methods so the view can lay a press schedule out before a core has
# been loaded (to label the repeat button, which has to say how many taps it
# will really do) and so the arithmetic can be tested without one. The
# RetroEmulator methods below are thin wrappers that pass the core's own fps.


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


# -- Core options -------------------------------------------------------------
#
# libretro cores expose their own settings (region, sound quality, palette,
# ...) through RETRO_ENVIRONMENT_SET_CORE_OPTIONS and friends. Keys, values and
# labels all cross the boundary as C strings, so everything libretro.py hands
# back here is ``bytes``; the cog deals in text and in JSON-serializable dicts,
# so the two conversions live here rather than being repeated at every call
# site.


def _text(value) -> str:
    """Whatever libretro.py handed us, as a plain string."""
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _encode_options(options) -> typing.Dict[bytes, bytes]:
    """``{"gambatte_gb_colorization": "GBC"}`` -> ``{b"...": b"..."}``."""
    encoded: typing.Dict[bytes, bytes] = {}
    for key, value in dict(options or {}).items():
        key = key if isinstance(key, bytes) else str(key).encode("utf-8")
        value = value if isinstance(value, bytes) else str(value).encode("utf-8")
        if key and value:
            encoded[key] = value
    return encoded


def describe_definitions(definitions) -> typing.Dict[str, dict]:
    """
    Turn libretro.py's option definitions into plain, storable dictionaries.

    The input is whatever ``OptionDriver.definitions`` returns: a mapping of
    ``bytes`` key to ``retro_core_option_v2_definition``. The output is all
    text and all JSON-serializable, so the cog can keep it in Config and list
    a core's options without loading that core again::

        {"gambatte_gb_colorization": {
            "desc": "GB Colorization",
            "info": "Enables colorization of Game Boy games.",
            "default": "disabled",
            "values": [["disabled", "disabled"], ["auto", "Auto"], ...],
        }}

    ``values`` is a list of ``[value, label]`` pairs: the value is what the
    core is actually given, the label is what RetroArch would show. A
    definition's value array is fixed-length and NULL-padded, so the empty
    trailing entries are dropped here rather than everywhere downstream.
    """
    described: typing.Dict[str, dict] = {}
    for raw_key, definition in dict(definitions or {}).items():
        key = _text(raw_key) or _text(getattr(definition, "key", ""))
        if not key:
            continue
        values: typing.List[typing.List[str]] = []
        for entry in getattr(definition, "values", ()) or ():
            value = _text(getattr(entry, "value", None))
            if not value:
                # The NULL padding at the end of the array.
                continue
            label = _text(getattr(entry, "label", None)) or value
            values.append([value, label])
        described[key] = {
            "desc": _text(getattr(definition, "desc", None)),
            "info": _text(getattr(definition, "info", None)),
            "default": _text(getattr(definition, "default_value", None)),
            "values": values,
        }
    return described


def probe_core_options(core_path, options=None) -> typing.Dict[str, dict]:
    """
    Read a core's option definitions without loading a game.

    Most cores register their options from ``retro_set_environment``, which
    runs before any content exists: Gambatte declares all 32 of its settings
    that way, as do snes9x (43) and Genesis Plus GX (62). Some do not --
    FCEUmm registers nothing at all until a ROM is loaded, and then declares
    44 -- so an empty result means "ask again once a game is running", never
    "this core has no options".

    This deliberately drives ``Core`` directly rather than going through
    ``libretro.defaults(...).build()``, because the builder insists on
    content: with none, and with a core that does not advertise no-content
    support, it raises ``ValueError("No content provided and core did not
    register support for no-content mode")``.

    Blocking C code, like everything else here: run it in a worker thread, and
    only while no other core is loaded (a libretro core is a shared object
    with process-global state).
    """
    try:
        from libretro import ArrayAudioDriver, ArrayVideoDriver, Core, JoypadState
        from libretro.drivers.environment.composite import CompositeEnvironmentDriver
        from libretro.drivers.input.iterable import IterableInputDriver
        from libretro.drivers.options.dict import DictOptionDriver
    except Exception as exc:
        raise EmulatorError(
            f"This libretro.py cannot be asked for a core's options: {exc}"
        ) from exc

    core_path = Path(core_path).resolve()
    if not core_path.is_file():
        raise EmulatorError(f"Libretro core not found: {core_path}")

    def _input_generator():
        while True:
            yield JoypadState()

    option_driver = DictOptionDriver(
        version=2,
        categories_supported=True,
        variables=_encode_options(options),
    )
    drivers = {
        "audio": ArrayAudioDriver(),
        "video": ArrayVideoDriver(),
        "input": IterableInputDriver(_input_generator),
        "options": option_driver,
        "log": _make_log_driver(),
    }
    # A driver slot that is present but None is rejected, so drop the ones we
    # could not build. The constructor itself changed shape between releases:
    # libretro.py 0.6.x takes one positional dict of drivers, 0.7+ takes them
    # as keyword arguments. Passing a dict to the newer one would silently
    # bind it to `audio` and fail with a confusing TypeError, so try the
    # keyword form first and fall back.
    drivers = {name: driver for name, driver in drivers.items() if driver is not None}
    try:
        environment = CompositeEnvironmentDriver(**drivers)
    except TypeError:
        environment = CompositeEnvironmentDriver(drivers)

    core = None
    initialised = False
    try:
        core = Core(str(core_path))
        core.set_environment(environment.environment)
        core.init()
        initialised = True
        return describe_definitions(option_driver.definitions)
    except EmulatorError:
        raise
    except Exception as exc:
        log.warning("Could not read the options of the core %s", core_path, exc_info=True)
        raise EmulatorError(f"The core's options could not be read: {exc}") from exc
    finally:
        # Always unload: leaving a half-initialised core in the process is
        # exactly the state MAX_LIVE_EMULATORS exists to prevent.
        if core is not None and initialised:
            try:
                core.deinit()
            except Exception:
                log.warning("The core %s did not deinitialise cleanly.", core_path, exc_info=True)
        del core


class EmulatorError(RuntimeError):
    """Raised when the emulator cannot be started or run."""


class RetroEmulator:
    """
    Wraps a libretro core (Gambatte, snes9x, ...) and a loaded ROM.

    Usage::

        emulator = RetroEmulator("gambatte_libretro.so", "game.gb")
        emulator.start()
        emulator.advance(120)
        emulator.press("start", hold_frames=12, release_frames=40)
        png_bytes = emulator.screenshot()
        clip_bytes = emulator.record(presses=[("a", 0, 12)])
        state = emulator.save_state()
        sram = emulator.save_sram()   # None if the cart has no battery
        emulator.stop()

    ``options`` is a mapping of libretro core option keys to values (for
    example ``{"gambatte_gb_colorization": "GBC"}``) seeded before the core
    initialises; see :func:`probe_core_options` for reading what a core
    offers.
    """

    def __init__(self, core_path, rom_path, system_dir=None, options=None) -> None:
        # Resolve to an absolute path: dlopen() does not search the working
        # directory for bare filenames like "gambatte_libretro.so".
        self.core_path = Path(core_path).resolve()
        self.rom_path = Path(rom_path)
        # Core options to seed before the core initialises, as text. A core
        # only reads most of its options once, at startup, so these have to be
        # in place before retro_init() rather than poked in afterwards.
        self.options: typing.Dict[str, str] = {
            _text(key): _text(value) for key, value in dict(options or {}).items()
        }
        # Where the core should look for BIOS/firmware files, i.e. what it is
        # told when it asks RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY. None means
        # the core is given no system directory at all, which is fine for a
        # BIOS-free core and is what libretro.py would do on its own (it
        # hands out a throwaway temporary directory).
        self.system_dir = Path(system_dir) if system_dir is not None else None
        self._pressed: frozenset = frozenset()
        self._session = None
        self._video = None
        self._path_driver = None
        self._audio_buffer = None
        self._joypad_state_cls = None
        self.started = False

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Load the core and the ROM. Raises EmulatorError on failure."""
        if self.started:
            return
        if not self.core_path.is_file():
            raise EmulatorError(f"Libretro core not found: {self.core_path}")
        if not self.rom_path.is_file():
            raise EmulatorError(f"ROM not found: {self.rom_path}")
        if self.rom_path.stat().st_size < MIN_ROM_SIZE:
            self._log_start_failure("ROM file is too small to be a game")
            raise EmulatorError("The file is too small to be a ROM.")

        # libretro.py is imported here rather than at module scope so that the
        # cog still loads, and every command still answers, on an install
        # where the dependency is missing or broken -- the player gets this
        # sentence instead of the cog failing to import at all.
        try:
            import libretro
            from libretro import JoypadState
        except Exception as exc:  # ImportError or environment issues
            raise EmulatorError(
                f"libretro.py could not be loaded: {exc}. Reinstall the cog, "
                "or install it manually with `pip install libretro.py`."
            ) from exc

        self._joypad_state_cls = JoypadState
        try:
            video = _make_video_driver()
        except Exception as exc:
            raise EmulatorError(
                f"This libretro.py is not one the cog can drive: {exc}"
            ) from exc
        path_driver = self._make_path_driver(libretro)
        log_driver = _make_log_driver()

        def input_generator():
            while True:
                yield self._current_joypad()

        encoded_options = _encode_options(self.options)
        try:
            builder_defaults = getattr(libretro, "defaults", None)
            if builder_defaults is not None:
                # libretro.py <= 0.6.x: SessionBuilder API.
                builder = (
                    builder_defaults(str(self.core_path))
                    .with_content(str(self.rom_path))
                    .with_input(input_generator)
                    .with_video(video)
                )
                if path_driver is not None and hasattr(builder, "with_paths"):
                    # Overrides the TempDirPathDriver that defaults() installs,
                    # whose system directory is a throwaway temp folder.
                    builder = builder.with_paths(path_driver)
                if log_driver is not None and hasattr(builder, "with_log"):
                    builder = builder.with_log(log_driver)
                if encoded_options and hasattr(builder, "with_options"):
                    # Seeds a DictOptionDriver with these values, which it
                    # keeps when the core registers its own definitions. An
                    # unknown key, or a value the core does not offer, is
                    # ignored and the core sees its own default instead -- so
                    # a setting left over from an older build of a core can
                    # never stop a game from starting.
                    builder = builder.with_options(encoded_options)
                session = builder.build()
            else:
                # libretro.py >= 0.7: Session constructor API.
                kwargs = {"input": input_generator, "video": video}
                if path_driver is not None:
                    kwargs["path"] = path_driver
                if log_driver is not None:
                    kwargs["log"] = log_driver
                if encoded_options:
                    kwargs["options"] = encoded_options
                try:
                    session = libretro.Session(
                        str(self.core_path), str(self.rom_path), **kwargs
                    )
                except TypeError:
                    # A libretro.py whose Session takes no options= argument.
                    # The game matters more than the setting, so start it
                    # anyway and say why the setting did nothing.
                    if "options" not in kwargs:
                        raise
                    log.warning(
                        "This libretro.py does not accept core options at "
                        "startup, so %s of them were ignored.",
                        len(kwargs.pop("options")),
                    )
                    session = libretro.Session(
                        str(self.core_path), str(self.rom_path), **kwargs
                    )
            session.__enter__()
        except Exception as exc:
            self._log_start_failure(exc)
            if "Failed to load game" in str(exc):
                # libretro.py raises this bare RuntimeError when the core's
                # retro_load_game() returns false, i.e. the core rejected the
                # content (corrupt file, HTML page saved as a ROM, ...).
                raise EmulatorError(
                    "The core could not load this ROM. It may be corrupt, or "
                    "for a different console than its file extension says."
                ) from exc
            raise EmulatorError(f"Failed to start the core: {exc}") from exc

        self._session = session
        self._video = video
        self._path_driver = path_driver
        self.started = True
        self._audio_buffer = self._find_audio_buffer(session)

    @staticmethod
    def _find_audio_buffer(session):
        """
        The live sample array libretro.py appends to, if we can empty it.

        Checked once, on a buffer that is still empty, so the per-frame drain
        never has to guess.
        """
        try:
            buffer = session.audio.buffer
            del buffer[:]
        except Exception:
            log.debug(
                "This libretro.py exposes no clearable audio buffer; memory "
                "use may grow during long sessions.",
                exc_info=True,
            )
            return None
        return buffer

    def _drain_audio(self) -> None:
        """
        Throw away the audio the session has accumulated.

        libretro.py's ArrayAudioDriver appends every sample the core produces
        to an ``array("h")`` that nothing ever empties: about 176 KiB per
        emulated second at 44.1 kHz stereo, so a channel that plays for an
        hour would be sitting on 600 MiB of audio nobody can hear. The cog
        posts silent clips, so the samples are dropped as they arrive.
        """
        buffer = self._audio_buffer
        if buffer is None:
            return
        try:
            del buffer[:]
        except Exception:
            # Whatever this is, it is not the array we thought it was.
            self._audio_buffer = None

    def _make_path_driver(self, libretro):
        """
        Build the driver that answers the core's directory questions.

        A libretro core that needs firmware asks the frontend for its "system
        directory" (``RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY``) and looks for
        the BIOS there. libretro.py exposes that through its path driver:
        ``ExplicitPathDriver(system=...)``, handed to ``with_paths()`` on the
        0.6.x SessionBuilder or to the ``path=`` argument of the 0.7+
        ``Session`` constructor. Left alone, libretro.py points every core at
        a fresh temporary directory, so a BIOS could never be found.

        Returns None (and logs) if anything about it fails: every core this
        cog ships is BIOS-free, so an old or unusual libretro.py should still
        play games rather than refuse to start.
        """
        if self.system_dir is None:
            return None
        driver_cls = getattr(libretro, "ExplicitPathDriver", None)
        if driver_cls is None:
            log.warning(
                "This libretro.py has no ExplicitPathDriver, so cores cannot "
                "be told where to find BIOS files."
            )
            return None
        try:
            root = self.system_dir.parent
            # libretro.py 0.6.0 requires all four of these to be set: it calls
            # os.makedirs() on each unconditionally, and makedirs(None) throws.
            # They are passed as str, not Path, because 0.6.0's PathLike branch
            # does fsencode(value.encode()) and a pathlib.Path has no .encode().
            directories = {
                "system": self.system_dir,
                "assets": root / "assets",
                "save": root / "save",
                "playlist": root / "playlist",
            }
            for path in directories.values():
                path.mkdir(parents=True, exist_ok=True)
            return driver_cls(
                corepath=str(self.core_path),
                **{key: str(path) for key, path in directories.items()},
            )
        except Exception as exc:
            log.warning(
                "Could not point the core at the system directory %s (%s); "
                "a core that needs a BIOS will not find one.",
                self.system_dir,
                exc,
            )
            return None

    @property
    def system_directory(self) -> typing.Optional[str]:
        """
        The system directory this session actually reports to the core.

        Read back from libretro.py rather than from what was asked for, so a
        caller (or a test) can confirm the path really reached the core.
        """
        for source in (self._session, self._path_driver):
            value = getattr(source, "system_directory", None) or getattr(
                source, "system_dir", None
            )
            if isinstance(value, bytes):
                return value.decode("utf-8", "replace")
            if isinstance(value, str):
                return value
        return None

    def _log_start_failure(self, exc) -> None:
        """Log everything useful for diagnosing a start failure."""
        rom_exists = self.rom_path.is_file()
        rom_size: object = None
        head = b""
        if rom_exists:
            try:
                rom_size = self.rom_path.stat().st_size
                with open(self.rom_path, "rb") as rom_file:
                    head = rom_file.read(MIN_ROM_SIZE)
            except OSError:
                pass
        hint = ""
        if head.lstrip()[:1] == b"<":
            hint = "; the ROM looks like an HTML page, not a game"
        elif rom_exists and isinstance(rom_size, int) and rom_size < MIN_ROM_SIZE:
            hint = "; the ROM is far too small to be a game"
        log.error(
            "Failed to start the emulator: %s (core=%s, rom=%s, rom_exists=%s, rom_size=%s%s)",
            exc,
            self.core_path,
            self.rom_path,
            rom_exists,
            rom_size,
            hint,
        )

    def stop(self) -> None:
        """Unload the game and free the core. Safe to call more than once."""
        session, self._session = self._session, None
        self._video = None
        self._path_driver = None
        self._audio_buffer = None
        self.started = False
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass

    close = stop

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    # -- Timing -------------------------------------------------------------

    @property
    def av_info(self):
        """The core's reported audio/video info, or None before it starts."""
        if self._video is None:
            return None
        try:
            return self._video.system_av_info
        except Exception:
            return None

    @property
    def fps(self) -> float:
        """
        The frame rate this core actually runs at.

        Gambatte reports 59.727, fceumm 60.100 and a PAL core 50.0, so
        anything that converts between seconds and frames has to ask rather
        than assume 60.
        """
        av_info = self.av_info
        try:
            rate = float(av_info.timing.fps)
        except Exception:
            return DEFAULT_FPS
        # A core that has not finished initialising can report 0.
        return rate if 1.0 <= rate <= 1000.0 else DEFAULT_FPS

    @property
    def aspect_ratio(self) -> float:
        """
        The width:height ratio the picture is meant to be shown at.

        Console pixels are usually not square: the NES reports 1.306 and the
        SNES 1.333 for a 256x224 frame, so drawing them at a flat 2x makes
        everything look tall and thin.
        """
        av_info = self.av_info
        try:
            ratio = float(av_info.geometry.aspect_ratio)
        except Exception:
            ratio = 0.0
        if ratio > 0.0:
            return ratio
        try:
            shot = self._video.screenshot()
            return shot.width / shot.height
        except Exception:
            return 4 / 3

    def frames_for_ms(self, milliseconds: float) -> int:
        """How many emulated frames last roughly this long, at least one."""
        return frame_count(self.fps, float(milliseconds) / 1000.0)

    def frames_for_seconds(self, seconds: float) -> int:
        return frame_count(self.fps, seconds)

    def clip_frames(self, seconds: float = CLIP_SECONDS) -> int:
        """How many emulated frames a clip of this length covers here."""
        return clip_frame_count(self.fps, seconds)

    def capture_step(self, clip_fps: int = CLIP_FPS) -> int:
        """How many emulated frames one picture of a clip covers here."""
        return capture_step(self.fps, clip_fps)

    def input_budget(self, frames: int, clip_fps: int = CLIP_FPS) -> int:
        """The last frame of a clip a button may still be released on."""
        return input_budget(self.fps, frames, clip_fps)

    # -- Input and emulation ------------------------------------------------

    def _current_joypad(self):
        return self._joypad_state_cls(**{name: True for name in self._pressed})

    def _require_started(self) -> None:
        if not self.started:
            raise EmulatorError("The emulator is not running.")

    @staticmethod
    def _check_button(button: str) -> str:
        button = str(button).lower()
        if button not in BUTTONS:
            raise EmulatorError(f"Unknown button {button!r}; expected one of {BUTTONS}")
        return button

    def advance(self, frames: int = 1) -> None:
        """Run the core for the given number of frames."""
        self._require_started()
        try:
            for _ in range(max(0, frames)):
                self._session.run()
        except Exception as exc:
            raise EmulatorError(f"The core crashed while running: {exc}") from exc
        finally:
            # Every frame, so the audio libretro.py hoards never outgrows one
            # frame's worth; see _drain_audio().
            self._drain_audio()

    def press(self, button: str, hold_frames: int = 12, release_frames: int = 40) -> None:
        """
        Hold a button for ``hold_frames`` frames, release it, then run
        ``release_frames`` more frames so the game visibly responds.
        """
        button = self._check_button(button)
        self._require_started()
        self._pressed = frozenset({button})
        try:
            self.advance(hold_frames)
        finally:
            self._pressed = frozenset()
        self.advance(release_frames)

    # -- Save states --------------------------------------------------------

    def save_state(self) -> bytes:
        """
        Serialize the whole machine (CPU, RAM, video, audio) into bytes.

        The blob is only meaningful to the same core, but it does not depend
        on the core *instance*: it can be handed to a freshly started
        emulator running the same ROM, which is how a session resumes after
        the emulator has been freed.
        """
        self._require_started()
        try:
            core = self._session.core
            size = core.serialize_size()
            if not size:
                raise EmulatorError("This core does not support save states.")
            buffer = bytearray(size)
            if not core.serialize(buffer):
                raise EmulatorError("The core refused to write a save state.")
        except EmulatorError:
            raise
        except Exception as exc:
            raise EmulatorError(f"The save state could not be created: {exc}") from exc
        return bytes(buffer)

    def load_state(self, data: bytes) -> None:
        """
        Restore a blob from :meth:`save_state` into the running core.

        A frame is run afterwards because the video driver still holds the
        picture from before the restore; without it a screenshot or clip
        would start on a stale frame.
        """
        self._require_started()
        if not data:
            raise EmulatorError("The save state is empty.")
        try:
            core = self._session.core
            size = core.serialize_size()
            if size and len(data) != size:
                raise EmulatorError(
                    f"The save state is {len(data)} bytes but this core "
                    f"expects {size}; it was probably made with a different "
                    "core or game."
                )
            if not core.unserialize(bytes(data)):
                raise EmulatorError("The core refused to load the save state.")
        except EmulatorError:
            raise
        except Exception as exc:
            raise EmulatorError(f"The save state could not be loaded: {exc}") from exc
        self.advance(1)

    # -- Core options -------------------------------------------------------

    @property
    def _option_driver(self):
        """The running session's option driver, or None if it has none."""
        if self._session is None:
            return None
        try:
            return self._session.options
        except Exception:
            return None

    def option_definitions(self) -> typing.Dict[str, dict]:
        """
        Everything this core has told us about its own settings, as text.

        See :func:`describe_definitions` for the shape. Empty if the core has
        registered nothing (or this libretro.py exposes no option driver),
        which is not the same as the core having no options.
        """
        driver = self._option_driver
        if driver is None:
            return {}
        try:
            return describe_definitions(driver.definitions)
        except Exception:
            log.debug("Could not read the core's option definitions.", exc_info=True)
            return {}

    def option_value(self, key: str) -> typing.Optional[str]:
        """The value the core would read for ``key`` right now, or None."""
        driver = self._option_driver
        if driver is None:
            return None
        try:
            return _text(driver.variables[_text(key).encode("utf-8")])
        except Exception:
            return None

    def set_option(self, key: str, value: str) -> bool:
        """
        Change a setting on the running core.

        Writing to the option driver raises the "variables have changed" flag,
        which the core notices the next time it polls. Cores differ in how much
        of that they honour mid-game -- a palette usually changes at once, a
        region setting usually waits for a restart -- so the caller should
        treat a True here as "the core has been told", not "the picture
        changed".
        """
        driver = self._option_driver
        if driver is None:
            return False
        try:
            driver.variables[_text(key).encode("utf-8")] = _text(value).encode("utf-8")
        except Exception:
            log.warning("Could not set the core option %s.", key, exc_info=True)
            return False
        return True

    # -- Battery saves (SRAM) -----------------------------------------------
    #
    # A save state is a snapshot of the whole machine and is only loadable by
    # the exact build of the core that wrote it: update the core and every
    # state on disk becomes a size mismatch (see load_state). SRAM is the
    # opposite -- it is the cartridge's own battery-backed memory, the same
    # bytes an original cart would hold, in a format that does not change --
    # so it is kept alongside the state as insurance. The state restores the
    # exact moment; the SRAM restores the player's in-game save.

    def _save_ram(self) -> typing.Optional[memoryview]:
        """A writable view of the cartridge's battery memory, or None."""
        if not self.started:
            return None
        try:
            memory = self._session.core.get_memory(RETRO_MEMORY_SAVE_RAM)
        except Exception:
            log.debug("This core does not expose its save RAM.", exc_info=True)
            return None
        if memory is None:
            return None
        try:
            size = len(memory)
        except Exception:
            return None
        # A cartridge with no battery reports either a NULL pointer (None
        # above) or a zero-length region; both are normal, not errors.
        if size <= 0:
            return None
        if size > MAX_SRAM_SIZE:
            log.warning(
                "The core %s reports %s bytes of save RAM, which is past the "
                "%s byte ceiling; ignoring it.",
                self.core_path.name,
                size,
                MAX_SRAM_SIZE,
            )
            return None
        return memory

    def save_sram(self) -> typing.Optional[bytes]:
        """
        The cartridge's battery save, or None if this game has none.

        None is the normal answer for a cart with no battery (a NES test ROM,
        most Game Boy puzzle games): it means "nothing to keep", not "this
        failed". Never raises.
        """
        memory = self._save_ram()
        if memory is None:
            return None
        try:
            return bytes(memory)
        except Exception:
            log.warning("Could not read the save RAM.", exc_info=True)
            return None

    def load_sram(self, data: bytes) -> bool:
        """
        Write a battery save back into the running game.

        Must be called *after* the game is loaded, because the core only
        allocates its save memory once it knows what cartridge it is holding.
        Returns False when there is nothing to restore into, or when the blob
        does not fit the region the core reports -- neither is worth raising
        over, since the fallback is simply a fresh save file.
        """
        if not data:
            return False
        memory = self._save_ram()
        if memory is None:
            return False
        size = len(memory)
        if len(data) != size:
            # A different revision of the same game, or a cart whose save size
            # depends on a core option. Restoring a prefix would hand the game
            # a half-written save file, which is worse than a clean one.
            log.warning(
                "Not restoring %s bytes of save RAM into a %s byte region.",
                len(data),
                size,
            )
            return False
        try:
            memory[:] = bytes(data)
        except Exception:
            log.warning("Could not restore the save RAM.", exc_info=True)
            return False
        return True

    @property
    def sram_size(self) -> int:
        """How many bytes of battery save this game has, 0 if it has none."""
        memory = self._save_ram()
        return 0 if memory is None else len(memory)

    # -- Video --------------------------------------------------------------

    @staticmethod
    def _pillow():
        """Import Pillow, turning a missing dependency into an EmulatorError."""
        try:
            from PIL import Image
        except Exception as exc:  # ImportError or a broken install
            raise EmulatorError(f"Pillow could not be loaded: {exc}") from exc
        return Image

    def _screenshot(self):
        """
        The driver's current frame, or an EmulatorError if there isn't one.

        Before the core has rendered anything, ArrayVideoDriver has a buffer
        but no dimensions, and raises a bare TypeError deep inside itself.
        """
        try:
            shot = self._video.screenshot()
        except Exception as exc:
            raise EmulatorError(f"No video frame is available yet: {exc}") from exc
        if shot is None:
            raise EmulatorError("No video frame is available yet.")
        return shot

    def output_size(self, scale: int = 2) -> "tuple":
        """
        The size a frame should be shown at, in pixels.

        The height is the console's own height doubled (unless the frame is
        already large), and the width follows from the aspect ratio the core
        reports, so a NES frame comes out 4:3-ish instead of tall and narrow.
        A Game Boy's pixels are square, so 160x144 lands on exactly 320x288.
        """
        self._require_started()
        shot = self._screenshot()
        factor = scale if shot.height <= DOUBLE_UP_TO_HEIGHT else 1
        height = max(1, shot.height * max(1, factor))
        width = max(1, round(height * self.aspect_ratio))
        return width, height

    def _frame_image(self, size=None, *, scale: int = 2, colors: int = 0):
        """
        Grab the current screen as a Pillow image.

        ArrayVideoDriver.screenshot() converts the core's native pixel format
        (RGB565/XRGB8888/RGB1555) into an RGBA byte buffer, so Pillow can read
        it directly. The image is resized with nearest-neighbor so it stays
        crisp pixel art rather than a blurry upscale.

        ``size`` pins the output to an exact size. :meth:`record` uses it so
        that a core which changes resolution part-way through a clip (the SNES
        does, and libretro calls that SET_GEOMETRY) cannot produce frames of
        two different sizes, which no animation format allows.
        """
        Image = self._pillow()
        shot = self._screenshot()
        image = Image.frombuffer(
            "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
        ).convert("RGB")
        if colors:
            # Quantizing before the resize is a quarter of the work at 2x,
            # and a nearest-neighbor resize of a P-mode image keeps the
            # palette indices intact.
            image = image.quantize(colors=colors)
        if size is None:
            size = self.output_size(scale=scale)
        if tuple(size) != image.size:
            image = image.resize(tuple(size), Image.NEAREST)
        return image

    def screenshot(self, scale: int = 2) -> bytes:
        """Return the current screen as PNG bytes."""
        self._require_started()
        buffer = io.BytesIO()
        self._frame_image(scale=scale).save(buffer, format="PNG")
        return buffer.getvalue()

    def record(
        self,
        frames: "int | None" = None,
        *,
        scale: int = 2,
        fps: int = CLIP_FPS,
        presses: "typing.Iterable | None" = None,
        clip_format: str = DEFAULT_CLIP_FORMAT,
    ) -> bytes:
        """
        Run the core for ``frames`` frames and return the clip as image bytes.

        ``presses`` is a sequence of ``(button, start_frame, hold_frames)``
        triples, scheduled against the frames *of this recording*, so the clip
        shows the game reacting to the input rather than only its end state.
        Overlapping entries are fine: they are simply held at the same time.
        A press scheduled at frame 0 goes down *before* the first emulated
        frame of the clip, so the first picture the player sees is already the
        game responding.

        Roughly every ``core_fps / fps``-th emulated frame is captured, so the
        default one second (60 emulated Game Boy frames) at 15 fps is a 15
        picture clip. Identical consecutive frames cost almost nothing -- the
        encoder merges them and adds their durations together -- so a game
        sitting on a static screen produces a handful of kilobytes, and a
        short clip of one can legitimately come back out as a single frame
        holding the whole clip's duration.

        A press is fitted to the recording by the caller (see
        :func:`input_budget` and ``RetroView.press_plan``); what happens here
        is only the final safety clamp, which keeps a press inside the clip
        but does not promise the release will be *seen*.

        Note that this is the *only* thing that advances the emulation: the
        console is frozen between one clip and the next.
        """
        self._require_started()
        clip_format = str(clip_format).upper()
        if clip_format not in CLIP_FORMATS:
            raise EmulatorError(
                f"Unknown clip format {clip_format!r}; expected one of {CLIP_FORMATS}"
            )
        core_fps = self.fps
        if frames is None:
            frames = self.clip_frames(CLIP_SECONDS)
        frames = max(1, int(frames))
        step = capture_step(core_fps, fps)

        # frame index -> buttons that go down / come up on that frame.
        down: dict = {}
        up: dict = {}
        for button, start, hold in presses or ():
            button = self._check_button(button)
            start = max(0, min(int(start), frames - 1))
            end = max(start + 1, min(start + int(hold), frames))
            down.setdefault(start, set()).add(button)
            up.setdefault(end, set()).add(button)

        # GIF cannot store the full colour range, so quantize on the way in.
        # Lossless WebP is exact, so quantizing it would only lose quality.
        colors = GIF_COLORS if clip_format == "GIF" else 0

        images = []
        # Taken from the first captured frame, then held for the rest of the
        # clip: a core that changes resolution part-way through must not
        # produce frames of two different sizes. It cannot be worked out
        # before the loop, because a core that has just been loaded has not
        # rendered anything yet.
        size = None
        held: set = set()
        # One duration per captured picture rather than one for the clip.
        # They are all ``step`` frames long except possibly the last, which
        # covers however many emulated frames were left: a 0.5 second Game
        # Boy clip is 30 frames, which is seven whole pictures and an eighth
        # covering two frames. Giving that last one a full 67ms made the clip
        # play 7% longer than the half second it emulated.
        durations: typing.List[int] = []
        try:
            for index in range(frames):
                if index in up:
                    held -= up[index]
                if index in down:
                    held |= down[index]
                self._pressed = frozenset(held)
                self.advance(1)
                if index % step == 0:
                    if size is None:
                        size = self.output_size(scale=scale)
                    images.append(self._frame_image(size, colors=colors))
                    covered = min(step, frames - index)
                    durations.append(max(1, round(1000 * covered / core_fps)))
        finally:
            self._pressed = frozenset()

        if not images:
            raise EmulatorError("No video frames were captured.")
        return self._encode(images, durations, clip_format)

    @staticmethod
    def _encode(images, duration_ms, clip_format: str) -> bytes:
        """Turn a list of same-sized Pillow images into one animation."""
        return encode_animation(images, duration_ms, clip_format)


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
                quality=100,
                # method=1 is the sweet spot: method>=5 costs 20-370x the
                # time for no measurable saving, and method=6 takes
                # minutes. minimize_size is worth ~20% on this content.
                method=1,
                minimize_size=True,
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


def _make_log_driver():
    """
    Keep the core's own chatter out of the bot's log.

    libretro.py's default log driver builds a logger called ``libretro``,
    forces it to DEBUG, and staples a StreamHandler onto it, so every line a
    core prints lands on the bot's stderr at the core's own level. Gambatte
    alone emits a dozen INFO lines per boot.

    Worse, most of those lines are useless. ``retro_log_printf_t`` is a C
    *variadic* function, and ctypes cannot express varargs for a callback:
    libretro.py binds it as ``CFUNCTYPE(None, retro_log_level, c_char_p)``, so
    only the format string ever arrives and the arguments are gone for good.
    That is where ``[Gambatte] %s`` in the bot's log comes from, and no log
    driver can recover it -- the data never crosses the boundary. Those lines
    carry nothing, so they are dropped outright.

    Everything that survives is logged at DEBUG (bar the first few errors),
    under ``red.robloach.retro.core``, so turning core logging back on is one
    logging config change away.

    Returns None if this libretro.py has no usable log driver to subclass, in
    which case the caller leaves logging alone.
    """
    try:
        from libretro import UnformattedLogDriver
    except Exception:
        log.debug("This libretro.py has no UnformattedLogDriver to build on.", exc_info=True)
        return None

    class _CoreLogDriver(UnformattedLogDriver):
        def __init__(self) -> None:
            # Passing a logger is what stops the base class attaching its own
            # StreamHandler (and its unbounded record-keeping filter) to a
            # global "libretro" logger.
            super().__init__(logger=core_log)
            self._warned = 0

        # 0.6.x calls this with (level, fmt, *args); 0.7+ with (level, fmt).
        def log(self, level, fmt: bytes, *args) -> None:
            try:
                message = bytes(fmt).decode("utf-8", "replace").strip()
            except Exception:
                return
            if not message or FORMAT_SPECIFIER.search(message):
                return
            severity = logging.DEBUG
            if getattr(level, "name", "") == "ERROR" and self._warned < MAX_CORE_WARNINGS:
                self._warned += 1
                severity = logging.WARNING
            core_log.log(severity, "%s", message)

    try:
        return _CoreLogDriver()
    except Exception:
        log.debug("Could not build the core log driver.", exc_info=True)
        return None


def _make_video_driver():
    """
    An ArrayVideoDriver that tolerates SET_GEOMETRY before av_info arrives.

    Several cores (snes9x, nestopia, quicknes) change their geometry while
    the core is still initialising, at which point libretro.py 0.6.0's
    geometry setter dereferences a ``_system_av_info`` that is still None and
    spews an ``Exception ignored on calling ctypes callback`` traceback to
    stderr. Nothing downstream needs the value -- the screenshot is sized
    from the dimensions passed to each video refresh -- so it is dropped.
    """
    from libretro import ArrayVideoDriver

    class _TolerantArrayVideoDriver(ArrayVideoDriver):
        @property
        def geometry(self):
            return ArrayVideoDriver.geometry.fget(self)

        @geometry.setter
        def geometry(self, value) -> None:
            if self._system_av_info is None:
                log.debug("Ignoring SET_GEOMETRY before the core reported av_info.")
                return
            ArrayVideoDriver.geometry.fset(self, value)

    return _TolerantArrayVideoDriver()


def _main() -> int:
    """
    Tiny CLI for smoke-testing:

        python retro/emulator.py CORE ROM OUT.png [FRAMES]
        python retro/emulator.py CORE ROM OUT.webp [FRAMES]
        python retro/emulator.py CORE ROM OUT.gif [FRAMES]

    A ``.webp`` or ``.gif`` output records the frames as an animated clip; any
    other extension runs the frames and writes a single PNG of the final
    screen.

    Setting ``RETRO_SYSTEM_DIR`` points the core's system (BIOS) directory at
    that folder and prints back whatever libretro.py reports for it, which is
    how the wiring is checked in CI.
    """
    import os
    import sys
    import time

    if len(sys.argv) < 4:
        print(__doc__)
        print("Usage: python retro/emulator.py CORE ROM OUT.png|OUT.webp|OUT.gif [FRAMES]")
        return 1
    out_path = Path(sys.argv[3])
    suffix = out_path.suffix.lower()
    clip_format = {".webp": "WEBP", ".gif": "GIF"}.get(suffix)
    system_dir = os.environ.get("RETRO_SYSTEM_DIR") or None

    started = time.perf_counter()
    with RetroEmulator(sys.argv[1], sys.argv[2], system_dir=system_dir) as emulator:
        if system_dir:
            print(f"system directory: {emulator.system_directory}")
        # 0 for a cartridge with no battery, which is normal; see save_sram().
        print(f"save ram: {emulator.sram_size} bytes")
        print(f"core options: {len(emulator.option_definitions())}")
        if len(sys.argv) > 4:
            frames = int(sys.argv[4])
        elif clip_format:
            frames = emulator.clip_frames(CLIP_SECONDS)
        else:
            frames = 120
        if clip_format:
            payload = emulator.record(frames, clip_format=clip_format)
        else:
            emulator.advance(frames)
            payload = emulator.screenshot()
        fps = emulator.fps
    elapsed = time.perf_counter() - started

    out_path.write_bytes(payload)
    print(
        f"Wrote {len(payload)} bytes of {clip_format or 'PNG'} after {frames} "
        f"frames ({fps:.2f} fps) to {out_path} in {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
