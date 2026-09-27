# Tests

```bash
pip install -r ../requirements-dev.txt
pytest                      # the fast suite, the default: ~9s
pytest -m emulator          # real cores and ROMs: ~29s, or ~17s with -n 2
pytest -m "not network"     # everything this machine can run
pytest -m network           # the buildbot check; needs RETRO_TEST_NETWORK=1
```

`pyproject.toml` puts `-m "not emulator and not network"` in `addopts`, so a
plain `pytest` is the fast suite. A `-m` on the command line replaces it.

**`pytest` works on a bare checkout.** Anything it cannot run — a missing core,
no `libretro.py`, no `discord.py`, no Red — **skips** with a reason.

## The three halves

| | covers | needs |
| --- | --- | --- |
| fast | console tables, layouts, the press line, zips, version metadata, the migration, the whole cog against fakes, clip arithmetic, property tests | nothing (more runs with `discord.py`; a few want Pillow) |
| `-m emulator` | real cores: clips at every length, timing, the seam, per-console sizes, save states, battery saves, core options, corrupt ROMs, the save/Undo/reboot round trips | `libretro.py`, Pillow, cores and ROMs |
| `-m network` | every core `systems.py` recommends is still on the buildbot | `RETRO_TEST_NETWORK=1` |

`-n 2` roughly halves the slow half. It must be `-n` (separate processes): one
libretro core per process. The fast suite is *slower* under `-n`, so it stays
serial.

Almost all of the slow half is Pillow encoding real WebP, so clips are as short
as the assertion allows. The exception is
`test_a_clip_plays_for_as_long_as_it_emulated`, which is genuinely about
length — don't shorten it.

**`-m redbot`** marks the ~18 tests needing real Red-DiscordBot: command
metadata, `__cog_commands__`, the slash tree, the registered listeners, and the
two `Config`/`cog_data_path` escape hatches the migration rests on. Everything
else runs against `tests/stubs/redbot`, used automatically when Red is absent.

**Run it both ways before pushing.** Each configuration hides bugs the other
catches: with Red installed the stub is never exercised (a missing
`hybrid_group` in it broke CI while every local run passed), and without it the
command surface is unpinned.

## The fakes

`tests/fakes.py` stands in for the three things a unit test cannot have —
Discord, Red's Config, and the emulator. The cog itself is real.

Two rules the harness depends on:

* **A fake must be installed on every module that looks the name up.** A module
  resolves globals in its own namespace, so patching `retro.Retro` alone stops
  intercepting the moment the code moves into a mixin — and the fixture keeps
  passing while testing nothing. That has happened twice here, so `retro.patch`
  installs on every module in `fakes.COG_MODULES` that binds the name and
  *raises* if none do.
* **Constants are re-exported, so reads are fine but patches are not.**
  `retro.cogmod.<NAME>` still reads correctly wherever it was defined. Anything
  *patched* has to be patched where the code looks it up.

**Pacing is swapped, not slept.** The module-level `pace_wait` is replaced by a
recorder, so the whole gate runs — deadline, cap, floor, cancellation — while
nothing waits, and tests assert on the delay *asked for*:

* `retro.pace_waits` — every delay any view asked for, in order;
* `view.last_pace_seconds` — the last one, `0.0` for an edit that went straight
  out;
* `retro.real_pacing()` — puts the real wait back, for the three tests that are
  about waiting. Those use a 0.2s clip so the drain costs tenths of a second.

That is why the fast suite still runs in about ten seconds. Paying for it would
be a real second per press, several hundred times over.

## What is pinned, and where

| Invariant | Where |
| --- | --- |
| A press causes **exactly one** edit — including a queued press, on its own deferred interaction | `test_cog_session.py` |
| Nothing is edited while a press is being emulated | `test_cog_session.py` |
| No control is ever greyed out (`fakes.CONDITIONAL_CONTROLS` is empty) | `test_view.py` |
| A clip plays for exactly as long as it emulated | `test_clips.py`, `test_emulator.py` |
| A clip starts one emulated frame after the last ended | `test_emulator.py` |
| A clip is not replaced before it has been watched, at every length | `test_cog_session.py` |
| The command surface, Config keys and listeners | `test_mixins.py` (`-m redbot`) |
| Session records stay bounded and are dropped when dead | `test_leaks.py` |
| The save state → battery save → cold boot chain, from both callers | `test_restore.py` |

The command surface and Config keys are pinned because they are already on
other people's disks: a key a refactor drops is their data gone. The listeners
are pinned because losing one is *invisible* — nothing fails, the records they
delete just accumulate for ever.

## Getting the cores and ROMs

```bash
python tests/fetch_assets.py            # ./test-assets, or $RETRO_TEST_ASSETS
RETRO_TEST_ASSETS=./test-assets pytest -m emulator
```

Cores come from the libretro buildbot, ROMs from their authors' releases
(dmg-acid2 and µCity are MIT, Libbet zlib, nestest the standard test ROM).
Nothing downloaded is committed. Two assets cannot be fetched and are skipped
without: a SNES homebrew (`roms/snes_rotzoom.sfc`) and a Game Boy RPG
(`roms/gb-rpg.gb`, for the one-press-one-tile test).

```
$RETRO_TEST_ASSETS/cores/gambatte_libretro.so
$RETRO_TEST_ASSETS/roms/ucity.gbc
```

That path is the **only** place the fixtures look, so
`RETRO_TEST_ASSETS=$(mktemp -d) pytest` is an honest "no assets" run. There were
`/tmp` fallbacks once; they are gone, because `assets.core()` hands what it
finds straight to `dlopen` and `/tmp` is world-writable.

## Adding a test

**Put it where it belongs.**

| File | For |
| --- | --- |
| `test_systems.py`, `test_archives.py`, `test_clips.py`, `test_frame_grab.py` | pure modules, loaded directly via `tests/loader.py` — no Red, no discord.py |
| `test_view.py`, `test_cog_*.py` | a real `Retro` wired to the fakes |
| `test_migration.py`, `test_resume.py` | the data migration and the Resume button |
| `test_restore.py` | the two callers of the restore chain, against each other |
| `test_emulator.py` | anything needing a real core |
| `test_saves_roundtrip.py` | the cog *and* a real core at once |
| `test_malformed_roms.py` | deliberately corrupted cartridges, at both levels |
| `test_packaging.py` | the cog as Red's Downloader sees it |

**A mistake invisible until somebody reads a message gets a source-level test.**
`test_packaging.py` has several: every published command is named in
`retro/README.md`, every README heading a *sent* string points at exists, no
`.py` carries a version literal, no sent string contains a literal `[p]`, and
`tests/stubs` defines everything the cog uses from `redbot.core.commands`.

Red substitutes `[p]` in help text and nowhere else, so `[p]retro` in a string
the cog *sends* reaches the channel as written — an instruction to type a
prefix nobody has. A command uses `ctx.clean_prefix`; a button click asks
`Retro._prefix_for`, which is why `FakeBot.get_valid_prefixes` answers with the
same `!` that `FakeContext.clean_prefix` hands out.

**A corrupted ROM is a regression test, not a fuzzer.** Everything in
`test_malformed_roms.py` is seeded, so a failure reproduces. It asserts the
invariant — a clean run or an `EmulatorError`, inside a time budget so a hang
fails rather than hanging the suite, and never a core left loaded — not that
any seed breaks any core. If a core update moves the measured hit rates in that
file's docstring, re-measure rather than delete.

**Comparing two save states is not a byte comparison.** Gambatte's state
carries a four-byte `time` field a cartridge with no RTC never initialises, so
two states from an identical machine differ by four bytes as soon as anything
has allocated in between — an `await` is enough. `test_saves_roundtrip.py`
compares with a tolerance.
