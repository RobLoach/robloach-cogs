#!/usr/bin/env python3
"""Fetch the cores and ROMs the slow tests need.

    python tests/fetch_assets.py                   # cores + the free ROMs
    python tests/fetch_assets.py --cores gambatte  # just one core
    python tests/fetch_assets.py --dest /tmp/ra    # somewhere else

Everything lands in ``$RETRO_TEST_ASSETS`` (or ``./test-assets``) as::

    cores/gambatte_libretro.so
    roms/ucity.gbc

Nothing downloaded here is committed: the cores are 4-12 MiB platform
binaries from the libretro buildbot, and the ROMs are fetched from their
authors' own releases. Every ROM below is freely redistributable -- MIT
licensed homebrew or a public test ROM -- and anything that is not (a
commercial game, for the Pokemon walk-cycle test) has to be supplied by
hand; the tests that need it skip when it is absent.

Stdlib only, so it runs before anything is installed.
"""

import argparse
import io
import os
import platform
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

BUILDBOT = "https://buildbot.libretro.com/nightly"


def buildbot_directory():
    """The buildbot path for this machine, matching Retro._buildbot_url."""
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        arch = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "i686": "x86",
            "aarch64": "aarch64",
            "arm64": "aarch64",
            "armv7l": "armhf",
        }.get(machine)
        if arch is None:
            raise SystemExit(f"No libretro buildbot builds for linux/{machine}.")
        return f"{BUILDBOT}/linux/{arch}/latest", "_libretro.so"
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"{BUILDBOT}/apple/osx/{arch}/latest", "_libretro.dylib"
    if sys.platform in ("win32", "cygwin"):
        arch = "x86_64" if machine in ("amd64", "x86_64") else "x86"
        return f"{BUILDBOT}/windows/{arch}/latest", "_libretro.dll"
    raise SystemExit(f"No libretro buildbot builds for {sys.platform}.")

#: filename -> (url, what it is). "required" ones are what CI needs.
ROMS = {
    "dmg-acid2.gb": (
        "https://github.com/mattcurrie/dmg-acid2/releases/download/v1.0/dmg-acid2.gb",
        "a Game Boy PPU test ROM (MIT); no battery save",
        True,
    ),
    "ucity.gbc": (
        "https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc",
        "a Game Boy Color city builder (MIT); 128 KiB battery save",
        True,
    ),
    "libbet.gb": (
        "https://github.com/pinobatch/libbet/releases/download/v0.08/libbet.gb",
        "Libbet and the Magic Floor (zlib); a static Game Boy screen that "
        "answers A and ignores everything else",
        True,
    ),
    "nestest.nes": (
        "https://raw.githubusercontent.com/christopherpow/nes-test-roms/master/other/nestest.nes",
        "the standard NES CPU test ROM",
        False,
    ),
}

#: Assets that cannot be downloaded and are simply skipped without.
BY_HAND = {
    "snes_rotzoom.sfc": "any small SNES homebrew or test ROM, for the busy-clip test",
    "pokemon.gb": "a Game Boy RPG of your own, for the one-press-one-tile test",
}


def core_names():
    """The cores systems.py recommends, without importing Red."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "retro_systems_fetch", REPO_ROOT / "retro" / "systems.py"
    )
    systems = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(systems)
    return sorted(systems.CORES)


def download(url, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def fetch_core(core, destination, base, suffix):
    filename = f"{core}{suffix}"
    target = destination / "cores" / filename
    if target.is_file():
        print(f"  have {filename}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = download(f"{base}/{filename}.zip")
    except urllib.error.URLError as error:
        print(f"  FAILED {filename}: {error}")
        return False
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next(e for e in archive.namelist() if e.endswith(filename))
        target.write_bytes(archive.read(member))
    print(f"  got  {filename} ({target.stat().st_size // 1024} KiB)")
    return True


def fetch_rom(name, destination):
    url, description, required = ROMS[name]
    target = destination / "roms" / name
    if target.is_file():
        print(f"  have {name}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(download(url))
    except urllib.error.URLError as error:
        print(f"  {'FAILED' if required else 'skipped'} {name}: {error}")
        return not required
    print(f"  got  {name} ({target.stat().st_size // 1024} KiB) -- {description}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        default=os.environ.get("RETRO_TEST_ASSETS") or str(REPO_ROOT / "test-assets"),
        help="where to put them (default: $RETRO_TEST_ASSETS or ./test-assets)",
    )
    parser.add_argument(
        "--cores",
        default="all",
        help="comma separated core names, 'all' (default) or 'none'",
    )
    parser.add_argument("--no-roms", action="store_true", help="cores only")
    args = parser.parse_args(argv)

    destination = Path(args.dest).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Assets directory: {destination}")

    ok = True
    if args.cores != "none":
        base, suffix = buildbot_directory()
        wanted = core_names() if args.cores == "all" else args.cores.split(",")
        print(f"Cores ({len(wanted)}) from {base}:")
        for core in wanted:
            ok &= fetch_core(core.strip(), destination, base, suffix)

    if not args.no_roms:
        print("ROMs:")
        for name in ROMS:
            ok &= fetch_rom(name, destination)
        for name, note in BY_HAND.items():
            if not (destination / "roms" / name).is_file():
                print(f"  absent {name}: supply {note}")

    print(f"\nRun the slow tests with:\n  RETRO_TEST_ASSETS={destination} pytest -m emulator")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
