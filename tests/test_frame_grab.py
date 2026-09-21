"""The fast frame grab, against libretro.py's own screenshot().

``retro.clips.fast_frame_image`` decodes an ArrayVideoDriver's framebuffer
with Pillow instead of letting libretro.py convert it a pixel at a time. It is
about a hundred times faster and it reaches into libretro.py's private
attributes to do it, so what matters is that it produces *exactly* the same
bytes and that it stands down cleanly when it cannot.

These tests need libretro.py and Pillow but no core and no ROM: the driver is
driven directly with synthetic frames, which is what makes it possible to
cover every pixel format and every rotation (including the ones no core here
has a ROM for) in the fast suite. That is deliberate -- this is the check that
would notice a libretro.py release renaming ``_frame`` or changing its layout,
and it is worth nothing if it only runs when the slow assets are present.

tests/test_emulator.py has the same assertion against real cores.
"""

import logging
import random
from array import array

import pytest

from .loader import load_standalone

C = load_standalone("retro_clips_for_frame_grab", "clips.py")

pytest.importorskip("libretro", reason="the frame grab tests need libretro.py")
pytest.importorskip("PIL", reason="the frame grab tests need Pillow")

# The top level names, which is the most stable surface libretro.py has --
# retro/emulator.py imports ArrayVideoDriver from here too.
from libretro import ArrayVideoDriver, PixelFormat, Rotation  # noqa: E402
from PIL import Image  # noqa: E402

#: A frame size chosen to be awkward: both dimensions odd, neither a multiple
#: of anything, and unequal, so a transposed image cannot accidentally match.
WIDTH, HEIGHT = 17, 11

FORMATS = [PixelFormat.RGB565, PixelFormat.XRGB8888, PixelFormat.RGB1555]


def make_driver(pixel_format, rotation=Rotation.NONE, seed=1, pad_pixels=5):
    """
    An ArrayVideoDriver holding one frame of random pixels.

    ``pad_pixels`` is the slack at the end of each row: real cores hand
    libretro a pitch wider than the picture (gambatte's 160 pixel frame
    arrives with a 512 byte pitch, i.e. 96 pixels of padding), and reading
    that padding as picture would be the obvious way to get this wrong.
    """
    driver = ArrayVideoDriver()
    driver._pixel_format = pixel_format
    driver._rotation = rotation
    pitch = (WIDTH + pad_pixels) * pixel_format.bytes_per_pixel
    rnd = random.Random(seed)
    data = bytes(rnd.randrange(256) for _ in range(pitch * HEIGHT))
    driver._frame = array("B", data)
    driver.refresh(memoryview(data), WIDTH, HEIGHT, pitch)
    return driver


def official_image(driver):
    """What ``_frame_image`` used to build, straight from screenshot()."""
    shot = driver.screenshot()
    return Image.frombuffer(
        "RGBA", (shot.width, shot.height), bytes(shot.data), "raw", "RGBA", 0, 1
    ).convert("RGB")


@pytest.fixture(autouse=True)
def forget_logged_fallbacks():
    """Each test starts with nothing logged, so "logged once" is testable."""
    C._SLOW_GRAB_LOGGED.clear()
    yield
    C._SLOW_GRAB_LOGGED.clear()


# -- 1. The tables --------------------------------------------------------------


@pytest.mark.parametrize("bits", [5, 6])
def test_the_expansion_table_turns_pillow_s_rounding_into_libretro_s(bits):
    # Pillow stretches a `bits`-wide channel by scaling (c * 255 // high);
    # libretro.py replicates the high bits. The table has to map one onto the
    # other for every value the channel can hold.
    table = C._channel_expansion_table(bits)
    assert len(table) == 256
    high = (1 << bits) - 1
    for value in range(high + 1):
        pillow = value * 255 // high
        libretro = (value << (8 - bits)) | (value >> (2 * bits - 8))
        assert table[pillow] == libretro, (bits, value)


def test_the_expansion_table_is_the_identity_everywhere_else():
    # Only the 32 (or 64) values a decoded channel can actually hold are
    # remapped; applying the table to anything else must not move it.
    table = C._channel_expansion_table(5)
    reachable = {value * 255 // 31 for value in range(32)}
    for value in range(256):
        if value not in reachable:
            assert table[value] == value, value


def test_the_tables_cover_every_pixel_format_libretro_defines():
    # A new pixel format in libretro.py must show up as a *fallback*, not as
    # a wrong picture, so this is really a check that the keys line up.
    names = {f.name for f in PixelFormat}
    assert set(C.FAST_RAW_MODES) == names
    assert set(C.FAST_POINT_TABLES) == names
    assert C.FAST_POINT_TABLES["XRGB8888"] is None, "8 bits a channel needs no expansion"
    for name in ("RGB565", "RGB1555"):
        assert len(C.FAST_POINT_TABLES[name]) == 768, "one 256 entry run per channel"


# -- 2. Pixel identity ----------------------------------------------------------


@pytest.mark.parametrize("pixel_format", FORMATS, ids=[f.name for f in FORMATS])
@pytest.mark.parametrize("rotation_name", sorted(C.FAST_ROTATIONS))
def test_the_fast_grab_is_byte_identical_to_libretro_s_own(pixel_format, rotation_name):
    rotation = getattr(Rotation, rotation_name)
    driver = make_driver(pixel_format, rotation)
    fast = C.fast_frame_image(driver, Image)
    assert fast is not None, "the fast path should handle this frame"
    official = official_image(driver)
    assert fast.size == official.size
    assert fast.tobytes() == official.tobytes()
    assert not C._SLOW_GRAB_LOGGED, "nothing should have fallen back"


@pytest.mark.parametrize("pixel_format", FORMATS, ids=[f.name for f in FORMATS])
def test_the_fast_grab_is_identical_with_no_row_padding_either(pixel_format):
    # pitch == width * bytes_per_pixel, which is what a core that pads
    # nothing hands over.
    driver = make_driver(pixel_format, pad_pixels=0)
    fast = C.fast_frame_image(driver, Image)
    assert fast is not None
    assert fast.tobytes() == official_image(driver).tobytes()


@pytest.mark.parametrize("pixel_format", FORMATS, ids=[f.name for f in FORMATS])
def test_the_fast_grab_ignores_the_padding_at_the_end_of_each_row(pixel_format):
    # Two drivers whose pictures are identical and whose padding differs must
    # produce the same image, or the pitch is being read as picture.
    plain = make_driver(pixel_format, pad_pixels=0)
    padded = ArrayVideoDriver()
    padded._pixel_format = pixel_format
    padded._rotation = Rotation.NONE
    row = WIDTH * pixel_format.bytes_per_pixel
    pitch = row + 40
    buffer = bytearray()
    source = bytes(plain._frame)
    for y in range(HEIGHT):
        buffer += source[y * row : (y + 1) * row]
        buffer += b"\xa5" * 40
    padded._frame = array("B", bytes(buffer))
    padded.refresh(memoryview(bytes(buffer)), WIDTH, HEIGHT, pitch)

    assert C.fast_frame_image(padded, Image).tobytes() == C.fast_frame_image(plain, Image).tobytes()


def test_a_ninety_degree_rotation_falls_back_rather_than_guessing():
    # libretro.py 0.6.0 starts a 90 degree rotation at (width - 4) rather
    # than (width - 1), so its output is shifted by three rows and wraps.
    # Reproducing that is not worth it and silently fixing it would change
    # what the cog posts, so the official path keeps the frame.
    driver = make_driver(PixelFormat.RGB565, Rotation.NINETY)
    assert C.fast_frame_image(driver, Image) is None
    assert any("NINETY" in reason for reason in C._SLOW_GRAB_LOGGED)


# -- 3. The guards --------------------------------------------------------------


def test_a_driver_that_has_not_rendered_anything_declines_quietly():
    driver = ArrayVideoDriver()
    driver._frame = array("B", b"\x00" * 64)
    assert C.fast_frame_image(driver, Image) is None
    assert C.fast_frame_size(driver) is None
    # Before the first frame there is nothing wrong, so nothing is logged.
    assert not C._SLOW_GRAB_LOGGED


def test_a_driver_with_no_framebuffer_at_all_declines():
    assert C.fast_frame_image(ArrayVideoDriver(), Image) is None
    assert C.fast_frame_image(object(), Image) is None


def test_an_unknown_pixel_format_falls_back():
    driver = make_driver(PixelFormat.RGB565)

    class Invented:
        name = "RGB10A2"
        bytes_per_pixel = 4

    driver._pixel_format = Invented()
    assert C.fast_frame_image(driver, Image) is None
    assert any("RGB10A2" in reason for reason in C._SLOW_GRAB_LOGGED)


def test_a_renamed_private_attribute_falls_back():
    # The whole hazard of this optimisation in one test: libretro.py is free
    # to rename any of these, and when it does the cog must get slow, not
    # wrong.
    for attribute in ("_frame", "_last_width", "_last_height", "_last_pitch", "_pixel_format"):
        driver = make_driver(PixelFormat.RGB565)
        assert C.fast_frame_image(driver, Image) is not None, attribute
        delattr(driver, attribute)
        assert C.fast_frame_image(driver, Image) is None, attribute


def test_a_framebuffer_shorter_than_the_frame_falls_back():
    driver = make_driver(PixelFormat.RGB565)
    driver._frame = array("B", bytes(driver._frame)[:-4])
    assert C.fast_frame_image(driver, Image) is None
    assert any("byte framebuffer" in reason for reason in C._SLOW_GRAB_LOGGED)


def test_a_pitch_too_small_for_the_width_falls_back():
    driver = make_driver(PixelFormat.RGB565)
    driver._last_pitch = WIDTH  # half of what RGB565 needs
    assert C.fast_frame_image(driver, Image) is None
    assert any("too small" in reason for reason in C._SLOW_GRAB_LOGGED)


def test_a_zero_sized_frame_declines():
    for width, height, pitch in ((0, HEIGHT, 44), (WIDTH, 0, 44), (WIDTH, HEIGHT, 0)):
        driver = make_driver(PixelFormat.RGB565)
        driver._last_width, driver._last_height, driver._last_pitch = width, height, pitch
        assert C.fast_frame_image(driver, Image) is None


def test_the_fallback_is_logged_once_at_debug_and_not_once_a_frame(caplog):
    driver = make_driver(PixelFormat.RGB565, Rotation.NINETY)
    with caplog.at_level(logging.DEBUG, logger="red.robloach.retro.emulator"):
        for _ in range(20):
            assert C.fast_frame_image(driver, Image) is None
    lines = [r for r in caplog.records if "framebuffer directly" in r.message]
    assert len(lines) == 1, [r.message for r in lines]
    assert lines[0].levelno == logging.DEBUG


# -- 4. The cheap frame size ----------------------------------------------------


@pytest.mark.parametrize("pixel_format", FORMATS, ids=[f.name for f in FORMATS])
@pytest.mark.parametrize("rotation_name", sorted(r.name for r in Rotation))
def test_the_cheap_frame_size_agrees_with_screenshot(pixel_format, rotation_name):
    # output_size() used to pay for a whole screenshot() to read one number
    # off it, which is why recording fifteen pictures converted sixteen
    # frames. Whatever fast_frame_size answers has to be what screenshot()
    # would have said, sideways rotations included.
    driver = make_driver(pixel_format, getattr(Rotation, rotation_name))
    shot = driver.screenshot()
    assert C.fast_frame_size(driver) == (shot.width, shot.height)


def test_an_unknown_rotation_has_no_cheap_frame_size():
    driver = make_driver(PixelFormat.RGB565)

    class Invented:
        name = "FORTY_FIVE"

    driver._rotation = Invented()
    assert C.fast_frame_size(driver) is None
