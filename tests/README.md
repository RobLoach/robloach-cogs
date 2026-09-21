# Tests

```bash
pip install -r ../requirements-dev.txt
pytest                      # everything the machine can run
pytest -m "not emulator"    # the fast half: a few seconds
pytest -m emulator          # real cores and real ROMs: about a minute
```

`pytest` works on a bare checkout. Anything it cannot run -- a missing core,
no `libretro.py`, no `discord.py` -- **skips** with a reason rather than
failing.

## The two halves

| | what it covers | needs |
| --- | --- | --- |
| fast | console tables, button layouts, emoji, zip handling, the `RetroCog` -> `Retro` migration, the whole cog driven against fakes (`[p]retrosaves` included), the shared restore chain, the clip arithmetic and the fast frame grab (`press_plan`, `input_budget` and `retro/clips.py`: plain functions of a frame rate or of a framebuffer, so no core is needed), property tests | nothing (more of it runs with `discord.py` installed; the stitched-replay tests want Pillow) |
| `-m emulator` | real libretro cores: clips at every length from 0.2s to 4s, timing (playback really does match emulated time, fractions included), stitched replays, save states, battery saves, core options, BIOS directory, and the save export/import round trip through the cog | `libretro.py`, Pillow, cores and ROMs (`test_saves_roundtrip.py` also wants `discord.py`) |
| `-m network` | every core systems.py recommends is still on the libretro buildbot | `RETRO_TEST_NETWORK=1` and the internet |

`pytest -m emulator -n 2` halves the slow half (about 64s to 34s here). It
has to be `-n`, i.e. separate processes: one libretro core may be loaded per
process. The fast suite is *slower* under `-n`, so it is left serial.

`-m redbot` marks the few tests that need the real Red-DiscordBot (command
permission metadata, the assembled cog's `__cog_commands__`, and the two
`Config`/`cog_data_path` escape hatches the data migration rests on).
Everything else runs against `tests/stubs/redbot`, which is used
automatically when Red is not installed -- Red is a large dependency and a
controller layout does not need a database.

## The cog is several modules and one class

`Retro` is a cog class assembled from mixins, which is how Red's own
multi-file cogs are built: `retro/storage.py` (paths, atomic writes, the
disk budget), `retro/cores.py` (installing cores, and their own options),
`retro/saves.py` (the `[p]retrosaves` group) and `retro/migration.py` (the
one-off `RetroCog` -> `Retro` move), wired together by `retro/abc.py`. The
clip arithmetic and the animation encoder live in `retro/clips.py`, which
imports no libretro at all, with `retro/emulator.py` re-exporting every name
so nothing downstream had to change.

Two consequences for the tests:

* **`retro.patch(name, value, monkeypatch)`, never `monkeypatch.setattr` on
  one module.** A module looks its globals up in its own namespace, so a
  fake installed on `retro.Retro` alone stops intercepting the moment the
  code that uses it lives in a mixin -- and the fixture keeps passing while
  testing nothing, which this repository has shipped twice. `RetroEnv.patch`
  installs the fake on every module in `fakes.COG_MODULES` that binds the
  name and *raises* if none of them do. `tests/test_mixins.py` proves no
  namespace is left holding the real thing.
* **Constants are re-exported from `retro.Retro`.** `retro.cogmod.<NAME>`
  still means what it always did, because a read of an immutable value does
  not care where it was defined. Anything *patched*, though, has to be
  patched where the code looks it up -- which is what `retro.patch` is for.

`tests/test_mixins.py` also pins the command surface and the Config keys:
both are already on other people's disks, so a command or a settings key
that a refactor quietly drops is their data gone.

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

* Put it where it belongs: `test_systems.py`, `test_archives.py` and
  `test_frame_grab.py` import nothing but the standard library plus what the
  module under test needs (they load it directly, via `tests/loader.py`,
  which puts it in a synthetic package so a relative import of a sibling
  still resolves without running `retro/__init__.py`); `test_view.py` and
  `test_cog_*.py` use
  the `retro` fixture, which is a real `Retro` wired to the fakes in
  `tests/fakes.py`; `test_migration.py` and `test_resume.py` use the same
  fixture for the data migration and the Resume button; `test_restore.py`
  holds the two callers of the save state -> battery save -> cold boot chain
  against each other; `test_emulator.py` is for things that need a real core,
  and `test_saves_roundtrip.py` for the one place the cog *and* a real core
  are needed at once (it puts `RetroEmulator` back over the fake).
* `FakeConfirm` answers Red's `ConfirmView` for the commands that ask before
  destroying something: set `FakeConfirm.reset(answer=False)` to press No, and
  read `FakeConfirm.asked` to prove the question was put at all. `FakeUser(...,
  manage_messages=True)` is a moderator, user `1` is the bot owner, and
  `ctx.uploaded()` gives `{filename: bytes}` for everything a command
  attached.
* The fakes lay the data directory out the way Red does -- one folder per cog
  *class name* under `tmp_path/cogs/` -- so `retro.data` is `.../cogs/Retro`
  and `retro.legacy_data` is `.../cogs/RetroCog`. `Config` is likewise handed
  out per cog name, so a handle fetched under the old name really does see a
  different store.
* Anything slow or core-driven gets `pytest.mark.emulator`, and asks for
  its assets through the `assets` fixture (`assets.need_core("gambatte")`,
  `assets.need_rom("ucity.gbc")`) so it skips instead of failing.
* Assert on behaviour the cog promises, and say why in a comment when the
  reason is not obvious -- most of these tests exist because something
  actually broke once.
* One libretro core can be loaded per process. Build emulators through the
  `emu` fixture, which stops the previous one for you, and never run the
  emulator tests in parallel.
