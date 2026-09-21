"""
Standalone retro console emulator built on libretro.py.

This module has no Red-DiscordBot or discord.py imports so it can be used and
tested on its own. All methods are synchronous and not thread-safe; async
callers should run them in a single worker thread (e.g. asyncio.to_thread)
and serialize access with a lock.

Supports both the SessionBuilder API of libretro.py <= 0.6.x (the newest
release available on Python 3.11, which Red-DiscordBot requires) and the
Session constructor API of libretro.py >= 0.7.

The clip arithmetic, the animation encoder and the fast frame grab live next
door in retro/clips.py, which needs no libretro at all; every one of their
names is re-exported here, so importing them from this module still works
and still gets the same objects.
"""

import io
import logging
import re
import typing
from pathlib import Path

try:
    # The ordinary case: imported as part of the `retro` package.
    from .clips import (
        _SLOW_GRAB_LOGGED,
        CLIP_FORMATS,
        CLIP_FPS,
        CLIP_SECONDS,
        DEFAULT_CLIP_FORMAT,
        FAST_POINT_TABLES,
        FAST_RAW_MODES,
        FAST_ROTATIONS,
        GIF_COLORS,
        MAX_CLIP_SCALE,
        MAX_CLIP_SECONDS,
        MIN_AFTERMATH_FRAMES,
        MIN_CLIP_FRAMES,
        MIN_CLIP_SECONDS,
        MIN_CLIP_WIDTH,
        PREROLL_SECONDS,
        WEBP_METHOD,
        WEBP_MINIMIZE_SIZE,
        EmulatorError,
        _channel_expansion_table,
        _note_slow_frame_grab,
        _pillow,
        capture_plan,
        capture_step,
        clamp_clip_seconds,
        clip_extension,
        clip_frame_count,
        clip_scale,
        clip_size,
        describe_seconds,
        encode_animation,
        fast_frame_image,
        fast_frame_size,
        format_seconds,
        frame_count,
        input_budget,
        preroll_budget,
    )
except ImportError:  # pragma: no cover - `python retro/emulator.py`, see _main
    # Run as a script, where there is no package to be relative to: the
    # directory holding this file is sys.path[0], so the sibling module is
    # importable by its bare name.
    from clips import (  # type: ignore[no-redef]
        _SLOW_GRAB_LOGGED,
        CLIP_FORMATS,
        CLIP_FPS,
        CLIP_SECONDS,
        DEFAULT_CLIP_FORMAT,
        FAST_POINT_TABLES,
        FAST_RAW_MODES,
        FAST_ROTATIONS,
        GIF_COLORS,
        MAX_CLIP_SCALE,
        MAX_CLIP_SECONDS,
        MIN_AFTERMATH_FRAMES,
        MIN_CLIP_FRAMES,
        MIN_CLIP_SECONDS,
        MIN_CLIP_WIDTH,
        PREROLL_SECONDS,
        WEBP_METHOD,
        WEBP_MINIMIZE_SIZE,
        EmulatorError,
        _channel_expansion_table,
        _note_slow_frame_grab,
        _pillow,
        capture_plan,
        capture_step,
        clamp_clip_seconds,
        clip_extension,
        clip_frame_count,
        clip_scale,
        clip_size,
        describe_seconds,
        encode_animation,
        fast_frame_image,
        fast_frame_size,
        format_seconds,
        frame_count,
        input_budget,
        preroll_budget,
    )

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
    "MIN_CLIP_WIDTH",
    "PREROLL_SECONDS",
    "MAX_CLIP_SCALE",
    "CLIP_FORMATS",
    "DEFAULT_CLIP_FORMAT",
    "MAX_SRAM_SIZE",
    "RETRO_MEMORY_SAVE_RAM",
    "capture_plan",
    "capture_step",
    "clamp_clip_seconds",
    "clip_extension",
    "clip_frame_count",
    "clip_scale",
    "clip_size",
    "describe_definitions",
    "describe_seconds",
    "encode_animation",
    "format_seconds",
    "frame_count",
    "input_budget",
    "preroll_budget",
    "probe_core_options",
    # Split out into retro/clips.py and re-exported here, which is the only
    # reason that split cost nothing downstream. Anything importing these
    # from retro.emulator still gets the same objects.
    "fast_frame_image",
    "fast_frame_size",
    "FAST_POINT_TABLES",
    "FAST_RAW_MODES",
    "FAST_ROTATIONS",
    "WEBP_METHOD",
    "WEBP_MINIMIZE_SIZE",
    "GIF_COLORS",
    "_channel_expansion_table",
    "_note_slow_frame_grab",
    "_pillow",
    "_SLOW_GRAB_LOGGED",
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
# header check: the consoles here range from 8 KiB NES carts upwards, and
# every core does its own validation anyway. Anything under this is a
# truncated download or an error page.
MIN_ROM_SIZE = 1024

# Consoles do not agree on a frame rate (Game Boy 59.73, NES 60.10, PAL
# machines 50), so timings are taken from the core's own av_info at runtime.
# This is only the fallback for a core that reports nothing useful.
DEFAULT_FPS = 60.0

# How big a frame is posted is decided by MIN_CLIP_WIDTH and clip_size() in
# retro/clips.py, which carry the per-console measurements the rule was
# chosen from. This used to be a DOUBLE_UP_TO_HEIGHT cutoff here: everything
# up to 256 pixels tall was drawn at 2x, which doubled the TV consoles for no
# visible gain and 2-4x the encode.

# RETRO_MEMORY_SAVE_RAM, i.e. the cartridge's battery-backed save memory. It is
# 0 in libretro.h and has been since libretro existed, but it is spelled out
# here so the SRAM code reads as something other than a magic number.
RETRO_MEMORY_SAVE_RAM = 0

# A sanity ceiling for a battery save. The largest cartridge SRAM any of these
# consoles ever shipped is 128 KiB (µCity's Game Boy Color cart reports exactly
# that), so anything past a megabyte is a core reporting nonsense and is not
# worth writing to disk on every few button presses.
MAX_SRAM_SIZE = 1024 * 1024

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
        # How many frames the last :meth:`record` ran through before it began
        # photographing -- see PREROLL_SECONDS. Zero for a recording with no
        # input in it (the Wait button, a boot, an undo) and for a game that
        # moves on the very first frame, which is to say for everything the
        # pre-roll was not written for. Read by the tests and logged at debug;
        # nothing in the cog branches on it.
        self.last_preroll_frames = 0

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
            width, height = self._frame_size()
            return width / height
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

    def reset(self) -> None:
        """
        Reboot the machine: libretro's ``retro_reset``, i.e. the power switch.

        The cartridge stays in and its battery memory stays put -- resetting a
        console has never wiped a save file, and ``retro_reset`` does not
        reallocate the save RAM region -- so what this throws away is the
        *moment*, not the player's in-game save. An emulator test holds the
        real core to both halves of that.

        A frame is run afterwards for the same reason :meth:`load_state` runs
        one: the video driver still holds the picture from before the reset,
        so a screenshot or a clip taken immediately would open on a stale
        frame.

        Blocking, like everything else here, so callers run it in a worker
        thread. Raises EmulatorError if the core is not running or refuses.
        """
        self._require_started()
        try:
            self._session.reset()
        except Exception as exc:
            raise EmulatorError(f"The core could not be reset: {exc}") from exc
        self.advance(1)

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

    def _frame_size(self) -> "tuple":
        """
        The core's own frame size in pixels, cheaply if that is possible.

        :func:`fast_frame_size` reads it off the video driver; the fallback is
        a full screenshot(), which is also what raises EmulatorError when the
        core has not rendered anything yet.
        """
        size = fast_frame_size(self._video)
        if size is not None:
            return size
        shot = self._screenshot()
        return shot.width, shot.height

    def output_size(self, scale: int = MAX_CLIP_SCALE) -> "tuple":
        """
        The size a frame should be shown at, in pixels.

        ``scale`` is a *ceiling* on how many times over the frame may be
        drawn, not an instruction: :func:`clip_size` enlarges a frame only
        while the picture would be narrower than a message column, so a Game
        Boy's square pixels land on exactly 320x288 while a NES frame stays at
        its own 224 lines and comes out 293x224 rather than tall and narrow.
        ``scale=1`` therefore means "the console's own resolution", which is
        what a caller wanting a native screenshot asks for.
        """
        self._require_started()
        frame_width, frame_height = self._frame_size()
        return clip_size(frame_width, frame_height, self.aspect_ratio, scale)

    def _frame_image(self, size=None, *, scale: int = MAX_CLIP_SCALE, colors: int = 0):
        """
        Grab the current screen as a Pillow image.

        The core's native pixel format (RGB565/XRGB8888/RGB1555) is decoded
        straight out of the video driver's framebuffer by Pillow -- see
        :func:`fast_frame_image` -- and only if that is not possible does
        ArrayVideoDriver.screenshot() convert it a pixel at a time. Both
        produce the same bytes. The image is then resized with
        nearest-neighbor so it stays crisp pixel art rather than a blurry
        upscale.

        ``size`` pins the output to an exact size. :meth:`record` uses it so
        that a core which changes resolution part-way through a clip (the SNES
        does, and libretro calls that SET_GEOMETRY) cannot produce frames of
        two different sizes, which no animation format allows.
        """
        Image = self._pillow()
        image = fast_frame_image(self._video, Image)
        if image is None:
            shot = self._screenshot()
            image = Image.frombuffer(
                "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
            ).convert("RGB")
        if colors:
            # Quantizing before the resize is cheaper whenever the resize
            # enlarges, and a nearest-neighbor resize of a P-mode image keeps
            # the palette indices intact.
            image = image.quantize(colors=colors)
        if size is None:
            size = self.output_size(scale=scale)
        if tuple(size) != image.size:
            image = image.resize(tuple(size), Image.NEAREST)
        return image

    def _frame_signature(self) -> typing.Optional[bytes]:
        """
        The current frame's pixels, for "has the picture changed?", or None.

        Used once per pre-roll frame (see PREROLL_SECONDS), so it is the fast
        grab or nothing: ArrayVideoDriver.screenshot() converts a frame a
        pixel at a time in Python and costs ~21ms on a Game Boy, which over a
        15 frame pre-roll would be 300ms of the ~50ms a whole clip takes. None
        therefore means *either* "the core has not rendered anything yet" or
        "this libretro.py/pixel format cannot be read cheaply", and the
        caller's answer to both is the same: skip the pre-roll and photograph
        from the first frame, exactly as this did before the pre-roll existed.

        The frame is compared at the core's own resolution, before the
        NEAREST resize and any GIF quantization. That is the stricter
        question of the two and the cheaper one: the posted picture is never
        *smaller* than the frame (see clip_scale), so a resize only ever
        repeats pixels and two frames that differ cannot resize to the same
        picture. A geometry change mid-clip shows up as a different length
        and so reads as a change, which it is.
        """
        Image = self._pillow()
        image = fast_frame_image(self._video, Image)
        return None if image is None else image.tobytes()

    def screenshot(self, scale: int = MAX_CLIP_SCALE) -> bytes:
        """Return the current screen as PNG bytes."""
        self._require_started()
        buffer = io.BytesIO()
        self._frame_image(scale=scale).save(buffer, format="PNG")
        return buffer.getvalue()

    def record(
        self,
        frames: "int | None" = None,
        *,
        scale: int = MAX_CLIP_SCALE,
        fps: int = CLIP_FPS,
        presses: "typing.Iterable | None" = None,
        clip_format: str = DEFAULT_CLIP_FORMAT,
    ) -> bytes:
        """
        Run the core for ``frames`` frames and return the clip as image bytes.

        ``presses`` is a sequence of ``(button, start_frame, hold_frames)``
        triples, scheduled against the frames *of this window*, so the clip
        shows the game reacting to the input rather than only its end state.
        Overlapping entries are fine: they are simply held at the same time.
        A press scheduled at frame 0 goes down *before* the first emulated
        frame, so the first picture the player sees is already the game
        responding.

        Every ``core_fps / fps``-th emulated frame is captured, plus the very
        last one -- see :func:`capture_plan`, which is what makes one clip
        carry on from the previous one with no frames lost in between. The
        default one second (60 emulated Game Boy frames) at 15 fps is a 16
        picture clip. Identical consecutive frames cost almost nothing -- the
        encoder merges them and adds their durations together -- so a game
        sitting on a static screen produces a handful of kilobytes, and a
        short clip of one can legitimately come back out as a single frame
        holding the whole clip's duration.

        A press is fitted to the recording by the caller (see
        :func:`input_budget` and ``RetroView.press_plan``); what happens here
        is only the final safety clamp, which keeps a press inside the window
        but does not promise the release will be *seen*.

        **The pre-roll.** A clip that opens with a press in it does not start
        photographing straight away. The press goes down and the core runs,
        unphotographed, until the picture is no longer the one the previous
        clip left standing in the channel -- for at most
        :func:`preroll_budget` frames -- and only then are the ``frames``
        frames of the clip recorded. See PREROLL_SECONDS for why: one frame
        after a button goes down a game that was sitting still is still
        sitting still, so the clip's opening picture was the previous clip's
        closing picture all over again, and the new clip appeared to replay
        the end of the old one before anything moved.

        Three things that follow from doing it as a pre-roll rather than as a
        trim of the recorded pictures:

        * the clip still plays for exactly as long as it emulated. ``frames``
          frames are recorded and ``frames`` frames' worth of durations are
          written; the pre-roll is emulated in *front* of the recording, not
          dropped out of it;
        * nothing the player had not already seen is skipped. The pre-roll
          stops on the first frame that differs from where the last clip
          finished, and that frame is the clip's own frame 0, so the seam is
          still one unbroken run of pictures;
        * a screen that never changes -- a menu, a paused game, a button the
          game ignores -- costs the bound and then records normally, at full
          length. The trim this replaced turned that case into a 17ms flash.

        The hold starts *in* the pre-roll, which is why the schedule is
        measured against the whole window rather than against the recording:
        the press is the thing that is expected to make the picture change, so
        pre-rolling without it would be waiting for a game to move on its own
        while burning the frames the press needed. Emulation therefore sees
        exactly the input it would have seen with no pre-roll at all, frame
        for frame -- all the pre-roll changes is which frames get
        photographed -- so a press is held for its configured length in total
        and ``input_budget``'s promise that the button is up before the last
        photographed frame holds with room to spare (the release moves
        *earlier* in the recording, never later). A recording with no input
        in it -- the Wait button, a boot, a reset, an undo -- has no pre-roll:
        nothing was pressed, so there is nothing whose effect to wait for.

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

        # frame index -> buttons that go down / come up on that frame, keyed
        # by the frame of the whole window (pre-roll included), which is what
        # the loop below counts with. The clamp is to `frames` rather than to
        # the window, so it stays the same last-ditch guarantee it always was:
        # every button is up before the clip's own last frame, whatever the
        # pre-roll does.
        down: dict = {}
        up: dict = {}
        for button, start, hold in presses or ():
            button = self._check_button(button)
            start = max(0, min(int(start), frames - 1))
            end = max(start + 1, min(start + int(hold), frames))
            down.setdefault(start, set()).add(button)
            up.setdefault(end, set()).add(button)

        # How far the pre-roll may look for the press to make a difference,
        # and the picture it is looking for a difference from -- the one the
        # previous clip left standing in the channel. Both are only worked out
        # for a clip that opens with a press in it; see the docstring, and
        # PREROLL_SECONDS for the measurements. `next_press` protects the
        # repeat button: the pre-roll must not run through a later tap.
        budget = 0
        reference = None
        if down.get(0):
            later = [frame for frame in down if frame > 0]
            budget = preroll_budget(core_fps, frames, min(later) if later else None)
            if budget > 0:
                reference = self._frame_signature()

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
        # One duration per captured picture rather than one for the clip, so
        # the clip plays for exactly as long as it emulated: a picture stands
        # until the next one is taken, which is ``step`` frames for all but
        # the tail. Giving the shorter tail pictures a full 67ms each made a
        # half second clip play 7% slow.
        plan = dict(capture_plan(frames, step))
        durations: typing.List[int] = []
        # `index` counts the clip's own frames, which is what `plan`, the
        # durations and every docstring here are in terms of; `window` counts
        # every frame this call emulates, pre-roll included, which is what the
        # press schedule is in terms of. They differ by exactly the number of
        # frames the pre-roll used, and that is zero for most clips.
        preroll = 0
        index = 0
        window = 0
        self.last_preroll_frames = 0
        try:
            while index < frames:
                if window in up:
                    held -= up[window]
                if window in down:
                    held |= down[window]
                self._pressed = frozenset(held)
                self.advance(1)
                window += 1
                if reference is not None:
                    if preroll < budget and self._frame_signature() == reference:
                        # Still the picture the last clip finished on, so the
                        # player has seen this one: run it out rather than
                        # opening the clip on it.
                        preroll += 1
                        continue
                    # Either the picture moved on -- in which case this frame
                    # is what the clip should open on -- or the bound ran out.
                    reference = None
                covered = plan.get(index)
                if covered is not None:
                    if size is None:
                        size = self.output_size(scale=scale)
                    images.append(self._frame_image(size, colors=colors))
                    durations.append(max(1, round(1000 * covered / core_fps)))
                index += 1
        finally:
            self._pressed = frozenset()
            self.last_preroll_frames = preroll

        if not images:
            raise EmulatorError("No video frames were captured.")
        if preroll:
            log.debug(
                "The clip's pre-roll ran %d of a possible %d frames before the "
                "picture changed.",
                preroll,
                budget,
            )
        return self._encode(images, durations, clip_format)

    @staticmethod
    def _encode(images, duration_ms, clip_format: str) -> bytes:
        """Turn a list of same-sized Pillow images into one animation."""
        return encode_animation(images, duration_ms, clip_format)


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
