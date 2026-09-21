# Tests

```bash
pip install -r ../requirements-dev.txt
pytest                      # the fast suite, which is the default: ~8s
pytest -m emulator          # real cores and real ROMs: ~26s, or ~15s with -n 2
pytest -m "not network"     # both of the above, i.e. everything this machine can run
pytest -m network           # the buildbot check; also needs RETRO_TEST_NETWORK=1
```

**A plain `pytest` is the fast suite.** `pyproject.toml` puts
`-m "not emulator and not network"` in `addopts`, because running the tests
is something you do twenty times an hour and the real-core half costs five
times as much as the rest put together. A `-m` on the command line replaces
that one (pytest's `-m` holds a single value and the command line is applied
after `addopts`), so each line above means exactly what it says -- and the
jobs in `.github/workflows/test.yml`, which all pass their own `-m`, still
run everything between them.

`pytest` works on a bare checkout. Anything it cannot run -- a missing core,
no `libretro.py`, no `discord.py` -- **skips** with a reason rather than
failing.

## The three halves

| | what it covers | needs |
| --- | --- | --- |
| fast | console tables, button layouts, emoji, zip handling, the version metadata, the `RetroCog` -> `Retro` migration, the whole cog driven against fakes (`[p]retrosaves` and the Undo button included), the shared restore chain, the clip arithmetic and the fast frame grab (`press_plan`, `input_budget`, `capture_plan`, `clip_size` and the rest of `retro/clips.py`: plain functions of a frame rate, a framebuffer or a frame size, so no core is needed), property tests | nothing (more of it runs with `discord.py` installed; the stitched-replay tests want Pillow) |
| `-m emulator` | real libretro cores: clips at every length from 0.2s to 4s, timing (playback really does match emulated time, fractions included), clip-boundary continuity, the size each console is posted at, stitched replays, save states, battery saves, core options, BIOS directory, and the save export/import round trip and the Undo round trip through the cog | `libretro.py`, Pillow, cores and ROMs (`test_saves_roundtrip.py` also wants `discord.py`) |
| `-m network` | every core systems.py recommends is still on the libretro buildbot | `RETRO_TEST_NETWORK=1` and the internet |

`pytest -m emulator -n 2` roughly halves the slow half (26s to 15s here). It
has to be `-n`, i.e. separate processes: one libretro core may be loaded per
process. The fast suite is *slower* under `-n`, so it is left serial.

Almost all of the slow half is Pillow encoding real WebP, so the clips the
tests record are deliberately as short as the assertion allows -- a test
about *which end* a replay is trimmed from proves the same thing with 0.4
second clips as with two second ones. The tests that are genuinely about
length keep it: `test_a_clip_plays_for_as_long_as_it_emulated` runs at every
clip length up to four seconds, `test_buffered_clips_stitch_back_into_one_animation`
records sixteen seconds so the fifteen second window has something to trim,
and `test_stitching_fifteen_seconds_is_quick_enough_to_do_on_a_button_press`
has to keep stitching a real fifteen seconds or it is measuring nothing.
Don't shorten those three.

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

## What a button press is allowed to do to the message

A press must cause **exactly one** edit of the Discord message, and
`tests/test_cog_session.py` is where that is enforced -- the fakes record
every interaction call, so the assertion is on the list of calls rather than
on a screenshot:

* `test_a_press_makes_exactly_one_edit_to_the_message` -- the call log is
  `["response.defer", "edit_original_response"]` and nothing else, the one
  edit carries the new `.webp`, and the buttons come out enabled;
* `test_nothing_is_edited_while_the_press_is_being_emulated` -- checked from
  inside a wrapped `run_press`, because the bug was an edit that changed *no
  attachment* and still restarted the one already there, so "only one clip is
  uploaded" was never the statement that mattered;
* `test_a_press_no_longer_greys_the_controls_out` -- the trade, pinned, so an
  intermediate "greyed out" edit cannot come back by accident.

**Undo obeys the same rule**, and `test_undo_makes_exactly_one_edit_to_the_message`
and `test_nothing_is_edited_while_the_undo_is_being_emulated` say so. An undo
with an empty history is the one click that makes *no* edit at all: it
answers privately, like Replay with an empty buffer, so the assertion there
is `["response.send_message"]` and an untouched message.

Three controls are allowed to be greyed out at any moment, because each of
them can have nothing to do: **Replay** with an empty buffer, **×3** on a
clip too short for two taps, and **Undo** with an empty history (which is
every session's first moment, and every session's state after a restart). So
`fakes.pressable()` -- and the `any_disabled`/`all_disabled` keys of the
interaction snapshot -- leave those three out, while `fakes.playable()` keeps
them for the tests that are about them. Without that split, "a press does not
grey the controls out" quietly becomes "there was something to replay".

The matching statement for the *content* of a clip is
`test_one_clip_carries_on_from_the_last_with_no_frames_lost` in
`test_emulator.py`: it records two consecutive clips off a real Game Boy,
then rewinds the save state and emulates the same frames one at a time, and
requires the first clip's last picture and the second clip's first picture to
be adjacent emulated frames. `tests/test_clips.py` makes the same statement
about `capture_plan` at every clip length and frame rate, with no core.

## What the cog holds on to

`tests/test_leaks.py` is the file for "after N of these have come and gone,
is N still in memory?". It is in the fast suite and needs no core. The
container it was written for is not the cog's own: discord.py keeps every
persistent view in a store keyed by message id, filled by `Client.add_view`
*and* by every send or edit that carries a view, and emptied by nothing
except `View.stop()` -- there is no `bot.remove_view`. So every game a
channel plays used to leave a whole `RetroView`, replay buffer included,
reachable for the life of the process. `Retro._release_view` is the fix and
these tests are the proof; they fail if it is taken out.

Two conventions in there worth knowing:

* the fake bot is given a **real** `discord.ui.view.ViewStore`, because a
  fake of the container under test can only ever agree with itself;
* `drop_the_test_doubles(env)` clears what `tests/fakes.py` recorded before
  the `weakref` check, because `FakeMessage` keeps the kwargs it was sent
  with (view included) and a real `discord.Message` does not.

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

* Put it where it belongs: `test_systems.py`, `test_archives.py`,
  `test_clips.py` and `test_frame_grab.py` import nothing but the standard
  library plus what the module under test needs (they load it directly, via
  `tests/loader.py`,
  which puts it in a synthetic package so a relative import of a sibling
  still resolves without running `retro/__init__.py`); `test_view.py` and
  `test_cog_*.py` use
  the `retro` fixture, which is a real `Retro` wired to the fakes in
  `tests/fakes.py`; `test_migration.py` and `test_resume.py` use the same
  fixture for the data migration and the Resume button; `test_restore.py`
  holds the two callers of the save state -> battery save -> cold boot chain
  against each other; `test_emulator.py` is for things that need a real core,
  and `test_saves_roundtrip.py` for the two places the cog *and* a real core
  are needed at once (it puts `RetroEmulator` back over the fake): the save
  export/import round trip, and Undo. `test_packaging.py` covers the cog as
  Red's Downloader sees it, `retro/version.py` included -- that module loads
  standalone too, so the version can be checked with neither Red nor
  `discord.py` installed.
* **Comparing two save states is not a byte comparison.** Gambatte's state
  carries a four-byte `time` field that a cartridge with no real-time clock
  never initialises, so two states serialized from an identical machine come
  out four bytes apart as soon as anything in the process has allocated in
  between -- an `await` is enough. `test_saves_roundtrip.py` therefore
  compares the console's work RAM, the cartridge's battery RAM and a hash of
  the picture (all three of which *are* exactly the emulated machine) and
  allows the state itself `STATE_SLACK` bytes of difference. Four out of
  182,530, measured.
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
