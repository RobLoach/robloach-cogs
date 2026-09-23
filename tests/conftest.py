"""Shared fixtures: import paths, the Red stub, and the core/ROM assets.

Three things have to be arranged before any test in here can import the cog:

* the repository root goes on ``sys.path``, so ``import retro.Retro``
  works from a bare checkout with nothing installed;
* ``redbot`` has to be importable, because ``retro/__init__.py`` and
  ``retro/Retro.py`` import it at module level. The real Red is used when
  it is installed and ``tests/stubs`` is used when it is not;
* the emulator tests need real libretro cores and real ROMs, which are far
  too large (and, for the cores, far too platform-specific) to commit. They
  are found through ``RETRO_TEST_ASSETS`` and the tests skip without them.

See tests/README.md.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Red, for real if it is installed and stubbed if it is not. find_spec rather
# than an import, so a real Red is not paid for at collection time.
HAS_REAL_RED = importlib.util.find_spec("redbot") is not None
if not HAS_REAL_RED:
    sys.path.insert(0, str(TESTS_DIR / "stubs"))

HAS_DISCORD = importlib.util.find_spec("discord") is not None
HAS_LIBRETRO = importlib.util.find_spec("libretro") is not None
HAS_PILLOW = importlib.util.find_spec("PIL") is not None


# -- Assets -------------------------------------------------------------------
#
# Layout of an assets directory (see tests/fetch_assets.py, which builds one):
#
#     $RETRO_TEST_ASSETS/
#         cores/gambatte_libretro.so
#         cores/fceumm_libretro.so
#         ...
#         roms/ucity.gbc
#         roms/dmg-acid2.gb
#         ...
#
# Files may also sit directly in the root, which is what a "just unzip the
# buildbot downloads here" directory looks like.

class Assets:
    """Where the cores and ROMs are, if they are anywhere.

    Only ``$RETRO_TEST_ASSETS`` (or ``test-assets/`` in the checkout) is ever
    searched, and that is deliberate: ``core()`` hands what it finds straight
    to ``dlopen``, so a search path anybody on the machine can write to --
    ``/tmp``, which this used to fall back to -- is arbitrary code execution
    during a test run.
    """

    def __init__(self, roots):
        self.roots = list(roots)

    # -- lookup
    def core(self, name):
        """The path to this platform's ``<name>_libretro.<so|dylib|dll>``."""
        filenames = [f"{name}_libretro{suffix}" for suffix in (".so", ".dylib", ".dll")]
        for root in self.roots:
            for filename in filenames:
                for candidate in (root / "cores" / filename, root / filename):
                    if candidate.is_file():
                        return candidate
        return None

    def rom(self, name):
        """The path to a ROM by its logical name, or None."""
        for root in self.roots:
            for candidate in (root / "roms" / name, root / name):
                if candidate.is_file():
                    return candidate
        return None

    # -- lookup, or skip
    def need_core(self, name):
        path = self.core(name)
        if path is None:
            pytest.skip(f"the {name} core is not in RETRO_TEST_ASSETS")
        return str(path)

    def need_rom(self, name):
        path = self.rom(name)
        if path is None:
            pytest.skip(f"{name} is not in RETRO_TEST_ASSETS")
        return str(path)


@pytest.fixture(scope="session")
def assets():
    """Where this machine keeps the cores and ROMs the slow tests need."""
    configured = os.environ.get("RETRO_TEST_ASSETS")
    root = Path(configured).expanduser() if configured else REPO_ROOT / "test-assets"
    return Assets([root])


@pytest.fixture(scope="session")
def gambatte(assets):
    return assets.need_core("gambatte")


@pytest.fixture(scope="session")
def ucity(assets):
    """uCity: an MIT-licensed GBC game whose cartridge has a 128 KiB battery."""
    return assets.need_rom("ucity.gbc")


@pytest.fixture(scope="session")
def dmg_acid2(assets):
    """dmg-acid2: a plain Game Boy test ROM with no battery save at all."""
    return assets.need_rom("dmg-acid2.gb")


#: Seconds of boot that put Libbet on the screen the clip seam tests want.
#: Measured on gambatte under libretro.py 0.6.0, 0.11.x and 0.12 alike: from
#: 3.5s to 5s the picture is frozen for 131 frames (2.2 seconds) with no input
#: and with any button but A or Start, and A or Start changes it on the
#: *third* frame. So one ROM covers both shapes the seam has to be right for
#: -- a static screen that answers a press after a delay, whose latency the
#: clip now shows rather than skipping, and a static screen that ignores the
#: press altogether, which must still produce a full-length clip.
LIBBET_BOOT_SECONDS = 4


@pytest.fixture(scope="session")
def libbet(assets):
    """Libbet and the Magic Floor: a zlib-licensed Game Boy homebrew.

    The only ROM here with the shape the seam reports were about (see the
    seam block in retro/clips.py): a screen that sits completely still until a
    button is pressed, and then takes a couple of frames to react. uCity
    animates every frame and dmg-acid2 never changes at all, so neither of
    them is that case.
    """
    return assets.need_rom("libbet.gb")


# -- The cog, wired to fakes --------------------------------------------------


@pytest.fixture
def retro(tmp_path, monkeypatch):
    """A Retro running on fake Discord, fake Config and a fake emulator.

    Everything it touches lives under ``tmp_path``, so tests never share a
    data directory and can run in any order.
    """
    pytest.importorskip("discord", reason="the cog tests need discord.py")
    from .fakes import RetroEnv

    return RetroEnv(tmp_path, monkeypatch)


# -- Marker handling ----------------------------------------------------------


def pytest_runtest_setup(item):
    """Skip, rather than fail, when a test's prerequisites are missing."""
    for marker in item.iter_markers():
        if marker.name == "emulator":
            if not HAS_LIBRETRO:
                pytest.skip("libretro.py is not installed")
            if not HAS_PILLOW:
                pytest.skip("Pillow is not installed")
        elif marker.name == "network":
            if not os.environ.get("RETRO_TEST_NETWORK"):
                pytest.skip("set RETRO_TEST_NETWORK=1 to check the buildbot")
        elif marker.name == "redbot":
            if not HAS_REAL_RED:
                pytest.skip("needs the real Red-DiscordBot, not tests/stubs")
