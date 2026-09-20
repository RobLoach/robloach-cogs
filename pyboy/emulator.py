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
from pathlib import Path

__all__ = ["GameBoyEmulator", "EmulatorError", "BUTTONS"]

# Game Boy buttons, named after JoypadState fields.
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")


class EmulatorError(RuntimeError):
    """Raised when the emulator cannot be started or run."""


class GameBoyEmulator:
    """
    Wraps a libretro Game Boy core (e.g. Gambatte) and a loaded ROM.

    Usage::

        emulator = GameBoyEmulator("gambatte_libretro.so", "game.gb")
        emulator.start()
        emulator.advance(120)
        emulator.press("start", hold_frames=8, release_frames=40)
        png_bytes = emulator.screenshot()
        emulator.stop()
    """

    def __init__(self, core_path, rom_path) -> None:
        self.core_path = Path(core_path)
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
            raise EmulatorError(f"Failed to start the core: {exc}") from exc

        self._session = session
        self._video = video
        self.started = True

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

    # -- Video --------------------------------------------------------------

    def screenshot(self, scale: int = 2) -> bytes:
        """
        Return the current screen as PNG bytes.

        ArrayVideoDriver.screenshot() converts the core's native pixel format
        (RGB565/XRGB8888/RGB1555) into an RGBA byte buffer, so Pillow can read
        it directly. The image is upscaled with nearest-neighbor so the
        160x144 screen is legible in Discord.
        """
        self._require_started()
        from PIL import Image

        shot = self._video.screenshot()
        if shot is None:
            raise EmulatorError("No video frame is available yet.")

        image = Image.frombuffer(
            "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
        )
        if scale > 1:
            image = image.resize(
                (shot.width * scale, shot.height * scale), Image.NEAREST
            )
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def _main() -> int:
    """Tiny CLI for smoke-testing: python -m pyboy.emulator CORE ROM OUT.png [FRAMES]"""
    import sys

    if len(sys.argv) < 4:
        print(__doc__)
        print("Usage: python -m pyboy.emulator CORE ROM OUT.png [FRAMES]")
        return 1
    frames = int(sys.argv[4]) if len(sys.argv) > 4 else 120
    with GameBoyEmulator(sys.argv[1], sys.argv[2]) as emulator:
        emulator.advance(frames)
        png = emulator.screenshot()
    Path(sys.argv[3]).write_bytes(png)
    print(f"Wrote {len(png)} bytes after {frames} frames to {sys.argv[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
