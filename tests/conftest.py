"""Shared fixtures: import paths, the Red stub, and the core/ROM assets.

Three things have to be arranged before any test in here can import the cog:

* the repository root goes on ``sys.path``, so ``import retro.RetroCog``
  works from a bare checkout with nothing installed;
* ``redbot`` has to be importable, because ``retro/__init__.py`` and
  ``retro/RetroCog.py`` import it at module level. The real Red is used when
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

#: Logical ROM name -> paths this machine may already have it at. These are
#: only consulted when RETRO_TEST_ASSETS is unset, so pointing that variable
#: at an empty directory really does produce a run with no assets.
LEGACY_ROMS = {
    "ucity.gbc": ("/tmp/dl-ucity.gbc", "/tmp/pyboy-smoke/ucity.gbc"),
    "dmg-acid2.gb": ("/tmp/pyboy-smoke/dmg-acid2.gb", "/tmp/roms/dmg-acid2.gb"),
    "nestest.nes": ("/tmp/roms/nestest.nes",),
    "snes_rotzoom.sfc": ("/tmp/roms/snes_rotzoom.sfc",),
    "pokemon.gb": ("/tmp/pyboy-smoke/dl-pokemon.gb",),
}

LEGACY_CORE_DIRS = ("/tmp/coretest",)


class Assets:
    """Where the cores and ROMs are, if they are anywhere."""

    def __init__(self, roots, core_dirs, legacy_roms):
        self.roots = list(roots)
        self.core_dirs = list(core_dirs)
        self.legacy_roms = dict(legacy_roms)

    # -- lookup
    def core(self, name):
        """The path to this platform's ``<name>_libretro.<so|dylib|dll>``."""
        filenames = [f"{name}_libretro{suffix}" for suffix in (".so", ".dylib", ".dll")]
        for root in self.roots:
            for filename in filenames:
                for candidate in (root / "cores" / filename, root / filename):
                    if candidate.is_file():
                        return candidate
        for directory in self.core_dirs:
            for filename in filenames:
                candidate = Path(directory) / filename
                if candidate.is_file():
                    return candidate
        return None

    def rom(self, name):
        """The path to a ROM by its logical name, or None."""
        for root in self.roots:
            for candidate in (root / "roms" / name, root / name):
                if candidate.is_file():
                    return candidate
        for candidate in self.legacy_roms.get(name, ()):
            if Path(candidate).is_file():
                return Path(candidate)
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
    if configured:
        # An explicit assets directory is the whole story: no /tmp fallbacks,
        # so `RETRO_TEST_ASSETS=/empty pytest` is an honest "no assets" run.
        return Assets([Path(configured).expanduser()], [], {})
    return Assets([REPO_ROOT / "test-assets"], LEGACY_CORE_DIRS, LEGACY_ROMS)


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


# -- The cog, wired to fakes --------------------------------------------------


@pytest.fixture
def retro(tmp_path, monkeypatch):
    """A RetroCog running on fake Discord, fake Config and a fake emulator.

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
