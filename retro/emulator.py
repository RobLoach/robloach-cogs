"""
Standalone Game Boy emulator built on libretro.py.

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
from pathlib import Path

__all__ = [
    "RetroEmulator",
    "EmulatorError",
    "BUTTONS",
    "MIN_ROM_SIZE",
    "FRAMES_PER_SECOND",
    "CLIP_SECONDS",
    "CLIP_FRAMES",
    "GIF_FPS",
]

log = logging.getLogger("red.robloach.retro.emulator")

# Game Boy buttons, named after JoypadState fields.
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")

# A Game Boy ROM is at least 0x150 bytes: the cartridge header ends at 0x14F,
# and Gambatte's retro_load_game rejects anything smaller.
MIN_ROM_SIZE = 0x150

# The Game Boy renders 60 frames a second, so a four second clip is 240
# emulated frames. Sampling every 4th frame gives a 15 fps GIF of 60 frames,
# each shown for 1000/15 = 67ms. GIF only stores durations in hundredths of a
# second, so that is rounded to 70ms and 4 seconds of play takes 4.2 seconds
# to watch; rounding down instead would play the clip 10% too fast.
FRAMES_PER_SECOND = 60
CLIP_SECONDS = 4
CLIP_FRAMES = FRAMES_PER_SECOND * CLIP_SECONDS
GIF_FPS = 15

# The Game Boy palette is tiny (4 shades on DMG, a few dozen on GBC), so
# quantizing to this many colours is effectively lossless and keeps the GIF
# small. Discord's attachment limit is 8 MiB; a 320x288 clip is well under it.
GIF_COLORS = 64


class EmulatorError(RuntimeError):
    """Raised when the emulator cannot be started or run."""


class RetroEmulator:
    """
    Wraps a libretro Game Boy core (e.g. Gambatte) and a loaded ROM.

    Usage::

        emulator = RetroEmulator("gambatte_libretro.so", "game.gb")
        emulator.start()
        emulator.advance(120)
        emulator.press("start", hold_frames=8, release_frames=40)
        png_bytes = emulator.screenshot()
        gif_bytes = emulator.record(press=("a", 8))
        state = emulator.save_state()
        emulator.stop()
    """

    def __init__(self, core_path, rom_path) -> None:
        # Resolve to an absolute path: dlopen() does not search the working
        # directory for bare filenames like "gambatte_libretro.so".
        self.core_path = Path(core_path).resolve()
        self.rom_path = Path(rom_path)
        self._pressed: frozenset = frozenset()
        self._session = None
        self._video = None
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
            self._log_start_failure("ROM file is smaller than a Game Boy ROM header")
            raise EmulatorError("The file is too small to be a Game Boy ROM.")

        try:
            import libretro
            from libretro import ArrayVideoDriver, JoypadState
        except Exception as exc:  # ImportError or environment issues
            raise EmulatorError(f"libretro.py could not be loaded: {exc}") from exc

        self._joypad_state_cls = JoypadState
        video = ArrayVideoDriver()

        def input_generator():
            while True:
                yield self._current_joypad()

        try:
            builder_defaults = getattr(libretro, "defaults", None)
            if builder_defaults is not None:
                # libretro.py <= 0.6.x: SessionBuilder API.
                session = (
                    builder_defaults(str(self.core_path))
                    .with_content(str(self.rom_path))
                    .with_input(input_generator)
                    .with_video(video)
                    .build()
                )
            else:
                # libretro.py >= 0.7: Session constructor API.
                session = libretro.Session(
                    str(self.core_path),
                    str(self.rom_path),
                    input=input_generator,
                    video=video,
                )
            session.__enter__()
        except Exception as exc:
            self._log_start_failure(exc)
            if "Failed to load game" in str(exc):
                # libretro.py raises this bare RuntimeError when the core's
                # retro_load_game() returns false, i.e. the core rejected the
                # content (corrupt file, HTML page saved as a ROM, ...).
                raise EmulatorError(
                    "The core could not load this ROM. It may be corrupt or "
                    "not a real Game Boy game."
                ) from exc
            raise EmulatorError(f"Failed to start the core: {exc}") from exc

        self._session = session
        self._video = video
        self.started = True

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
            hint = "; the ROM looks like an HTML page, not a Game Boy ROM"
        elif rom_exists and isinstance(rom_size, int) and rom_size < MIN_ROM_SIZE:
            hint = "; the ROM is smaller than a Game Boy ROM header"
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

    # -- Input and emulation ------------------------------------------------

    def _current_joypad(self):
        return self._joypad_state_cls(**{name: True for name in self._pressed})

    def _require_started(self) -> None:
        if not self.started:
            raise EmulatorError("The emulator is not running.")

    def advance(self, frames: int = 1) -> None:
        """Run the core for the given number of frames (60 frames ~ 1 second)."""
        self._require_started()
        try:
            for _ in range(max(0, frames)):
                self._session.run()
        except Exception as exc:
            raise EmulatorError(f"The core crashed while running: {exc}") from exc

    def press(self, button: str, hold_frames: int = 8, release_frames: int = 40) -> None:
        """
        Hold a button for ``hold_frames`` frames, release it, then run
        ``release_frames`` more frames so the game visibly responds.
        """
        button = str(button).lower()
        if button not in BUTTONS:
            raise EmulatorError(f"Unknown button {button!r}; expected one of {BUTTONS}")
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

    def _frame_image(self, scale: int = 2, colors: int = 0):
        """
        Grab the current screen as a Pillow image.

        ArrayVideoDriver.screenshot() converts the core's native pixel format
        (RGB565/XRGB8888/RGB1555) into an RGBA byte buffer, so Pillow can read
        it directly. The image is upscaled with nearest-neighbor so the
        160x144 screen is legible in Discord. When ``colors`` is set the image
        is quantized to a palette first: that is a quarter of the work at
        scale 2, and a nearest-neighbor resize of a P-mode image keeps the
        palette indices intact.
        """
        Image = self._pillow()
        shot = self._video.screenshot()
        if shot is None:
            raise EmulatorError("No video frame is available yet.")

        image = Image.frombuffer(
            "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
        ).convert("RGB")
        if colors:
            image = image.quantize(colors=colors)
        if scale > 1:
            image = image.resize(
                (shot.width * scale, shot.height * scale), Image.NEAREST
            )
        return image

    def screenshot(self, scale: int = 2) -> bytes:
        """Return the current screen as PNG bytes."""
        self._require_started()
        buffer = io.BytesIO()
        self._frame_image(scale=scale).save(buffer, format="PNG")
        return buffer.getvalue()

    def record(
        self,
        frames: int = CLIP_FRAMES,
        *,
        scale: int = 2,
        fps: int = GIF_FPS,
        press: "tuple | None" = None,
    ) -> bytes:
        """
        Run the core for ``frames`` frames and return the clip as GIF bytes.

        ``press`` is an optional ``(button, hold_frames)`` pair. The button is
        held down for the first ``hold_frames`` frames *of the recording* and
        released afterwards, so the GIF shows the game reacting to the press
        instead of only its end state.

        Roughly every ``60 / fps``-th emulated frame becomes a GIF frame, so
        the default 240 frames at 15 fps is a 4 second, 60 frame clip.
        Identical consecutive frames are merged by the encoder, so a game
        sitting on a static screen produces a handful of kilobytes.
        """
        self._require_started()
        frames = max(1, int(frames))
        fps = max(1, min(FRAMES_PER_SECOND, int(fps)))
        step = max(1, round(FRAMES_PER_SECOND / fps))
        # GIF durations are stored in centiseconds, so round to 10ms here
        # instead of letting the encoder truncate and speed the clip up.
        duration_ms = max(10, round(1000 * step / FRAMES_PER_SECOND / 10) * 10)

        release_at = 0
        if press is not None:
            button, hold_frames = press
            button = str(button).lower()
            if button not in BUTTONS:
                raise EmulatorError(f"Unknown button {button!r}; expected one of {BUTTONS}")
            release_at = max(0, min(int(hold_frames), frames))
            if release_at:
                self._pressed = frozenset({button})

        images = []
        try:
            for index in range(frames):
                if release_at and index == release_at:
                    self._pressed = frozenset()
                self.advance(1)
                if index % step == 0:
                    images.append(self._frame_image(scale=scale, colors=GIF_COLORS))
        finally:
            self._pressed = frozenset()

        if not images:
            raise EmulatorError("No video frames were captured.")

        buffer = io.BytesIO()
        try:
            # No loop= argument on purpose: Pillow only writes the NETSCAPE
            # looping extension when one is given, and loop=0 would mean
            # "loop forever". Without it the clip plays exactly once and then
            # holds on its last frame, which is what the Replay button is for.
            images[0].save(
                buffer,
                format="GIF",
                save_all=True,
                append_images=images[1:],
                duration=duration_ms,
                optimize=True,
            )
        except Exception as exc:
            raise EmulatorError(f"The clip could not be encoded: {exc}") from exc
        return buffer.getvalue()


def _main() -> int:
    """
    Tiny CLI for smoke-testing:

        python -m libretro.emulator CORE ROM OUT.png [FRAMES]
        python -m libretro.emulator CORE ROM OUT.gif [FRAMES]

    A ``.gif`` output records the frames as an animated clip; any other
    extension runs the frames and writes a single PNG of the final screen.
    """
    import sys
    import time

    if len(sys.argv) < 4:
        print(__doc__)
        print("Usage: python -m libretro.emulator CORE ROM OUT.png|OUT.gif [FRAMES]")
        return 1
    out_path = Path(sys.argv[3])
    animated = out_path.suffix.lower() == ".gif"
    default_frames = CLIP_FRAMES if animated else 120
    frames = int(sys.argv[4]) if len(sys.argv) > 4 else default_frames

    started = time.perf_counter()
    with RetroEmulator(sys.argv[1], sys.argv[2]) as emulator:
        if animated:
            payload = emulator.record(frames)
        else:
            emulator.advance(frames)
            payload = emulator.screenshot()
    elapsed = time.perf_counter() - started

    out_path.write_bytes(payload)
    kind = "GIF" if animated else "PNG"
    print(
        f"Wrote {len(payload)} bytes of {kind} after {frames} frames to "
        f"{out_path} in {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
