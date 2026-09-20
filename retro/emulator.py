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
    "CLIP_FORMATS",
    "DEFAULT_CLIP_FORMAT",
    "clip_extension",
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
CLIP_SECONDS = 5
CLIP_FPS = 15

# Bounds for the configurable clip length.
MIN_CLIP_SECONDS = 1
MAX_CLIP_SECONDS = 15

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


def clip_extension(clip_format: str = DEFAULT_CLIP_FORMAT) -> str:
    """``"WEBP"`` -> ``".webp"``."""
    return f".{str(clip_format).lower()}"


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
        emulator.stop()
    """

    def __init__(self, core_path, rom_path, system_dir=None) -> None:
        # Resolve to an absolute path: dlopen() does not search the working
        # directory for bare filenames like "gambatte_libretro.so".
        self.core_path = Path(core_path).resolve()
        self.rom_path = Path(rom_path)
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
                session = builder.build()
            else:
                # libretro.py >= 0.7: Session constructor API.
                kwargs = {"input": input_generator, "video": video}
                if path_driver is not None:
                    kwargs["path"] = path_driver
                if log_driver is not None:
                    kwargs["log"] = log_driver
                session = libretro.Session(
                    str(self.core_path),
                    str(self.rom_path),
                    **kwargs,
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
        return max(1, round(self.fps * float(milliseconds) / 1000.0))

    def frames_for_seconds(self, seconds: float) -> int:
        return max(1, round(self.fps * float(seconds)))

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

        Roughly every ``core_fps / fps``-th emulated frame is captured, so the
        default five seconds at 15 fps is a 75 frame clip. Identical
        consecutive frames cost almost nothing, so a game sitting on a static
        screen produces a handful of kilobytes.
        """
        self._require_started()
        clip_format = str(clip_format).upper()
        if clip_format not in CLIP_FORMATS:
            raise EmulatorError(
                f"Unknown clip format {clip_format!r}; expected one of {CLIP_FORMATS}"
            )
        core_fps = self.fps
        if frames is None:
            frames = self.frames_for_seconds(CLIP_SECONDS)
        frames = max(1, int(frames))
        fps = max(1, min(round(core_fps), int(fps)))
        step = max(1, round(core_fps / fps))
        duration_ms = max(1, round(1000 * step / core_fps))

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
        finally:
            self._pressed = frozenset()

        if not images:
            raise EmulatorError("No video frames were captured.")
        return self._encode(images, duration_ms, clip_format)

    @staticmethod
    def _encode(images, duration_ms: int, clip_format: str) -> bytes:
        """Turn a list of same-sized Pillow images into one animation."""
        buffer = io.BytesIO()
        try:
            if clip_format == "WEBP":
                images[0].save(
                    buffer,
                    format="WEBP",
                    save_all=True,
                    append_images=images[1:],
                    duration=duration_ms,
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
                # clip too fast. No loop= argument on purpose: Pillow only
                # writes the looping extension when one is given.
                #
                # optimize=True made these GIFs 16-40% *bigger* (the frames
                # are already palette images), as well as slower.
                images[0].save(
                    buffer,
                    format="GIF",
                    save_all=True,
                    append_images=images[1:],
                    duration=max(10, round(duration_ms / 10) * 10),
                    optimize=False,
                )
        except Exception as exc:
            raise EmulatorError(f"The clip could not be encoded: {exc}") from exc
        return buffer.getvalue()


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
        if len(sys.argv) > 4:
            frames = int(sys.argv[4])
        elif clip_format:
            frames = emulator.frames_for_seconds(CLIP_SECONDS)
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
