# Tests

```bash
pip install -r ../requirements-dev.txt
pytest                      # everything the machine can run
pytest -m "not emulator"    # the fast half: about a second
pytest -m emulator          # real cores and real ROMs: about a minute
```

`pytest` works on a bare checkout. Anything it cannot run -- a missing core,
no `libretro.py`, no `discord.py` -- **skips** with a reason rather than
failing.

## The two halves

| | what it covers | needs |
| --- | --- | --- |
| fast | console tables, button layouts, emoji, zip handling, the whole cog driven against fakes, property tests | nothing (more of it runs with `discord.py` installed) |
| `-m emulator` | real libretro cores: clips, timing, save states, battery saves, core options, BIOS directory | `libretro.py`, Pillow, cores and ROMs |
| `-m network` | every core systems.py recommends is still on the libretro buildbot | `RETRO_TEST_NETWORK=1` and the internet |

`pytest -m emulator -n 2` halves the slow half (40s to 22s here). It has to
be `-n`, i.e. separate processes: one libretro core may be loaded per
process. The fast suite is *slower* under `-n`, so it is left serial.

`-m redbot` marks the few tests that need the real Red-DiscordBot (command
permission metadata). Everything else runs against `tests/stubs/redbot`,
which is used automatically when Red is not installed -- Red is a large
dependency and a controller layout does not need a database.

## Getting the cores and ROMs

```bash
python tests/fetch_assets.py            # ./test-assets, or $RETRO_TEST_ASSETS
RETRO_TEST_ASSETS=./test-assets pytest -m emulator
```

The script pulls the cores from the libretro buildbot and the ROMs from
their authors' releases (dmg-acid2 and uCity are MIT-licensed, nestest is
the standard NES test ROM). Nothing it downloads is committed. Two assets
cannot be fetched and are simply skipped without: a small SNES homebrew
(`roms/snes_rotzoom.sfc`) and a Game Boy RPG (`roms/pokemon.gb`, for the
one-press-one-tile test). Layout:

```
$RETRO_TEST_ASSETS/cores/gambatte_libretro.so
$RETRO_TEST_ASSETS/roms/ucity.gbc
```

With `RETRO_TEST_ASSETS` unset the fixtures also look in a few places this
project has historically kept them (`/tmp/coretest`, `/tmp/roms`); setting
it turns that off, so `RETRO_TEST_ASSETS=$(mktemp -d) pytest` is an honest
"no assets here" run.

## Adding a test

* Put it where it belongs: `test_systems.py` and `test_archives.py` import
  nothing but the standard library (they load the module under test
  directly, via `tests/loader.py`); `test_view.py` and `test_cog_*.py` use
  the `retro` fixture, which is a real `RetroCog` wired to the fakes in
  `tests/fakes.py`; `test_emulator.py` is for things that need a real core.
* Anything slow or core-driven gets `pytest.mark.emulator`, and asks for
  its assets through the `assets` fixture (`assets.need_core("gambatte")`,
  `assets.need_rom("ucity.gbc")`) so it skips instead of failing.
* Assert on behaviour the cog promises, and say why in a comment when the
  reason is not obvious -- most of these tests exist because something
  actually broke once.
* One libretro core can be loaded per process. Build emulators through the
  `emu` fixture, which stops the previous one for you, and never run the
  emulator tests in parallel.
