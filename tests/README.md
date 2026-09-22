# Tests

```bash
pip install -r ../requirements-dev.txt
pytest                      # the fast suite, which is the default: ~9s
pytest -m emulator          # real cores and real ROMs: ~29s, or ~17s with -n 2
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
| fast | console tables, button layouts, emoji, the per-console press line, zip handling, the version metadata, the `RetroCog` -> `Retro` migration, the whole cog driven against fakes (`[p]retrosaves`, `[p]retroreset` and the Undo button included), the session-record lifecycle (the channel and guild listeners, and bounded growth), the shared restore chain, the clip arithmetic and the fast frame grab (`press_plan`, `input_budget`, `capture_plan`, `preroll_budget`, `clip_size` and the rest of `retro/clips.py`: plain functions of a frame rate, a framebuffer or a frame size, so no core is needed), property tests | nothing (more of it runs with `discord.py` installed; a few tests that read a clip's frames back want Pillow) |
| `-m emulator` | real libretro cores: clips at every length from 0.2s to 4s, timing (playback really does match emulated time, fractions included), clip-boundary continuity, the size each console is posted at, resetting a core, save states, battery saves, core options, BIOS directory, deliberately corrupted ROMs (`test_malformed_roms.py`), and the save export/import round trip, the Undo round trip and the `[p]retroreset` round trip through the cog | `libretro.py`, Pillow, cores and ROMs (`test_saves_roundtrip.py` and `test_malformed_roms.py` also want `discord.py`) |
| `-m network` | every core systems.py recommends is still on the libretro buildbot | `RETRO_TEST_NETWORK=1` and the internet |

`pytest -m emulator -n 2` roughly halves the slow half (29s to 17s here). It
has to be `-n`, i.e. separate processes: one libretro core may be loaded per
process. The fast suite is *slower* under `-n`, so it is left serial.

Almost all of the slow half is Pillow encoding real WebP, so the clips the
tests record are deliberately as short as the assertion allows -- a test
about which picture a clip *opens* on proves the same thing with 0.4 second
clips as with two second ones. The one test that is genuinely about length
keeps it: `test_a_clip_plays_for_as_long_as_it_emulated` runs at every clip
length up to four seconds. Don't shorten that one.

`-m redbot` marks the few tests that need the real Red-DiscordBot (command
permission metadata, the assembled cog's `__cog_commands__`, the set of
Discord events it really registers a listener for, and the two
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

`tests/test_mixins.py` also pins the command surface, the Config keys and
the listeners. The first two are already on other people's disks, so a
command or a settings key that a refactor quietly drops is their data gone;
the listeners are pinned because losing one is *invisible* -- nothing fails,
the records they exist to delete simply start accumulating for ever again.

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
answers privately, so the assertion there is `["response.send_message"]` and
an untouched message.

**Saying who pressed which button rides on that same edit.** Every action
writes one line of `content` -- `Rob pressed A.`, `Rob pressed ⬅️.`,
`Rob waited.`, `Rob undid the last press.`, `Rob reset the game.` -- and the
tests in the "Who did it" section of `test_cog_session.py` check it three
ways:

* `test_every_action_says_who_did_it` is table-driven over `ATTRIBUTED`, one
  row per action, each row saying how to perform it and what the message must
  then read. A press, Wait and ×3 arrive as `interaction.user`;
  `[p]retroreset` is a *command* and arrives as `ctx.author`, so its row goes
  through the command and reads the edit off the message
  (`fakes.RetroEnv.message_edit`) rather than off an interaction log;
* `test_no_action_can_notify_anybody` re-runs the same table with a display
  name of `@everyone **<@111…>**` and asserts *both* guards -- that no
  mention syntax survives in the string, and that the edit carries an
  `allowed_mentions` with `everyone`, `users`, `roles` and `replied_user` all
  false. The snapshot exposes them as `allowed_mentions` and `pings_nobody`;
* `test_the_attribution_still_rides_on_the_one_edit` keeps the call log at
  `["response.defer", "edit_original_response"]`, so the attribution cannot
  quietly buy itself a second edit.

Alongside those: `test_a_presser_with_no_usable_name_still_gets_a_line`
covers a plain `User` (no `guild_permissions`, which is what somebody who has
left the guild looks like) and an object with nothing nameable on it at all,
which must fall back to `Pressed A.` rather than raise or emit a line
starting with a space.

The wording and the sanitising are unit-level and live in `test_view.py`: the
per-console table over all eight consoles (`PRESS_LINES`, held against the
`Button` entries in `retro/systems.py` -- which is the point, since the
RetroPad field a button maps to is frequently not what the console calls it,
a Genesis `C` being RetroPad `a`), the one-voice table (`ACTION_LINES`,
checked against `RetroView.ACTION_NOTES`), and `SANITISED` /
`HOSTILE_NAMES`, which are every shape of display name that would otherwise
change the shape of the message -- markdown, backticks, start-of-line
markdown, masked links, mass mentions, zero-width characters, newlines and a
200 character name.

**One control is allowed to be greyed out at any moment: Undo**, with an
empty history (which is every session's first moment, and every session's
state after a restart). So `fakes.pressable()` -- and the
`any_disabled`/`all_disabled` keys of the interaction snapshot -- leave it
out, while `fakes.playable()` keeps it for the tests that are about it.
Without that split, "a press does not grey the controls out" quietly becomes
"there was something to undo".

**×3 used to be the second one, and is not any more**: on a clip too short
for two taps it is not drawn at all, so whenever it is on the message it is
live. `fakes.button()` therefore returns `None` for a control the view does
not have, and the presence/absence table is
`test_the_repeat_button_is_drawn_only_when_it_can_do_something` in
`test_view.py` -- `REPEAT_BY_LENGTH` × all eight consoles, from the 0.2s
settings floor to the 15s ceiling, checking the tap count, whether the button
exists, its label, that the layout still fits Discord's grid, and that Wait
and Undo have not moved.
`test_changing_the_clip_length_adds_and_removes_the_button_in_place` walks a
live view down to 0.2s and back up, because a Discord action row is ordered
by insertion and simply re-adding the button would land it after Undo.

The matching statement for the *content* of a clip is
`test_one_clip_carries_on_from_the_last_with_no_frames_lost` in
`test_emulator.py`: it records two consecutive clips off a real Game Boy,
then rewinds the save state and emulates the same frames one at a time, and
requires the first clip's last picture and the second clip's first picture to
be adjacent emulated frames. `tests/test_clips.py` makes the same statement
about `capture_plan` at every clip length and frame rate, with no core.

**A clip plays for exactly as long as it emulated**, and that rule is
unconditional -- see `test_a_clip_plays_for_as_long_as_it_emulated`, whose
docstring states it and says why it was nearly weakened. A clip *opens* on
pictures where the press has not visibly landed yet (the hold is 160ms and a
game reacts more slowly), and on a game that sits still until it is prodded
the opening picture really was the previous clip's closing picture over again.
Trimming those pictures out of the recording would have made playback shorter
than the span it covers, so it was measured and rejected; what the fix turned
out to be is a bounded **pre-roll**, which plays the press out before the
recording starts rather than dropping anything from it.

Four tests carry that, and between them they are the whole argument:

* `test_the_preroll_opens_a_clip_on_the_first_picture_the_press_changed`
  records each responsive probe twice from one save state -- once with the
  pre-roll switched off, which is the "before" column -- and requires the
  opening repeat to go from at least one picture to none, with every picture
  the capture plan asked for still photographed both times;
* `test_a_completely_static_screen_still_produces_a_whole_clip` is the case
  the bound protects and the case a trim would have destroyed: three screens
  where *every* picture is unchanged, which a trim would have reduced to a
  17ms flash. The pre-roll spends its bound, finds nothing, and records the
  full clip -- byte-for-byte the clip it would have recorded with no pre-roll
  at all. It `skip`s rather than fails if a ROM turns out not to be static
  under the core build in use;
* `test_the_hold_is_honoured_in_full_and_released_before_the_last_picture`
  reads the input back off the frames the core really saw, by wrapping
  `advance`, and pins that the pre-roll did not shorten, lengthen or move the
  press;
* `test_the_preroll_leaves_the_seam_with_no_repeat_and_no_gap` does two
  presses in a row and requires the pre-roll's own frames to be pictures the
  player had already seen, so that skipping them loses nothing.

`test_a_clip_with_no_input_in_it_has_no_preroll_at_all` is the other side of
it: Wait, Undo, a boot and `[p]retroreset` record with no schedule, so they
photograph from their first frame as they always did. The bound itself is
plain arithmetic and is covered with no core at all in `test_clips.py`
(`preroll_budget`), including the rule that stops it running through one of
the repeat button's taps -- which `test_view.py` checks across the whole
clip-length x hold settings grid.

`roms/libbet.gb` was added to `fetch_assets.py` for this: Libbet and the
Magic Floor (zlib) is the only ROM here with the shape the bug was reported
against, a Game Boy screen that sits completely still until it is prodded and
then takes a couple of frames to react. uCity animates every frame (so it is
the control -- the pre-roll must do nothing to it) and dmg-acid2 never changes
at all. Libbet also ignores the d-pad on that screen, which gives the
must-not-flash case from the same ROM. One thing it is *not* used for is
consecutive clips: pressing A there makes gambatte dupe a frame, and
libretro.py 0.11.x raises out of its own environment callback when it sees one
(0.6.0 does not), so the seam test uses nestest and a SNES homebrew instead.
That is an upstream regression rather than anything to do with clips -- a
recording with the pre-roll switched off fails in exactly the same place.

## What the cog holds on to

`tests/test_leaks.py` is the file for "after N of these have come and gone,
is N still in memory?". It is in the fast suite and needs no core. The
container it was written for is not the cog's own: discord.py keeps every
persistent view in a store keyed by message id, filled by `Client.add_view`
*and* by every send or edit that carries a view, and emptied by nothing
except `View.stop()` -- there is no `bot.remove_view`. So every game a
channel plays used to leave a whole `RetroView` reachable for the life of the
process -- and in those days each one held a replay buffer of up to 8 MiB,
which is what made it worth chasing. `Retro._release_view` is the fix and
these tests are the proof; they fail if it is taken out.

Four of its sections are worth knowing about by name:

* **section 2, the footage a session holds**, which is now zero. The replay
  buffer went with the Replay button and the single `last_clip` that
  replaced it went too (nothing in the cog read it: `_show` is handed the
  clip it posts). So the assertions use `fakes.footage_bytes(view)` -- every
  bytes-like attribute except the compressed undo history -- rather than
  naming an attribute, because the thing being guarded against is a clip
  coming back under a new name.
* **section 5b, the silent failures that turn the safety machinery off.**
  Three handlers that each disabled something this file is about and said
  nothing: `RetroEmulator.stop` swallowing a failed unload (so a *running*
  core with nothing pointing at it looked exactly like a free
  MAX_LIVE_EMULATORS slot), `_drain_audio` answering one bad `del` by
  switching itself off for the rest of the session (restoring the whole
  176 KiB-per-emulated-second audio leak), and `probe_core_options` skipping
  `retro_deinit` for a `retro_init` that raised half way through. The tests
  drive `retro/emulator.py` through `load_standalone` with stand-ins, so
  they need no core -- the two `probe_core_options` ones need libretro.py
  importable and skip without it. The deliberate asymmetry is asserted as
  well: deinit is *not* called when `retro_init` was never reached, because
  the libretro API does not define that and libretro.py will make the call
  regardless.
* **section 9, the session records in Config**, which is about the growth
  that was not in memory at all: the per-channel `session` and `retired`
  records were written and never deleted, so a bot rebuilt a `RetroView`
  for every channel that had *ever* played, on every load. It covers the
  listeners, the load-time skip, the after-ready sweep and the "N channels
  in, nothing left behind" count -- and, just as hard, the semantics that
  make dropping a record safe: a record is a pointer, the saves are not, and
  the saves are kept. Each of those paths also has to *free the core* of a
  channel that was still playing, which is section 5's other half: the view
  leaves `cog.sessions`, and `_evict_locked` looks nowhere else.
* **section 10, the disk budget's own leak.** `_write_atomic` writes
  `<name>.tmp` and renames it; every pruner skips a `.tmp` and `_data_usage`
  counts one, so an orphan is budget nothing can reclaim. These cover the
  cleanup on a failed write, a failed rename and a cancellation, the sweep
  of the ones an earlier run left behind, and which writes pay for an
  `fsync` (the saves do, a re-downloadable ROM does not).

Three conventions in there worth knowing:

* the fake bot is given a **real** `discord.ui.view.ViewStore`, because a
  fake of the container under test can only ever agree with itself;
* `drop_the_test_doubles(env)` clears what `tests/fakes.py` recorded before
  the `weakref` check, because `FakeMessage` keeps the kwargs it was sent
  with (view included) and a real `discord.Message` does not;
* `FakeBot.ready` is False for the tests about what the cog may conclude
  from an empty channel cache. Red loads its cogs *before* the bot connects,
  so `bot.get_channel` answering None is not evidence of anything until
  `wait_until_red_ready()` has returned -- acting on it earlier would delete
  every record on every restart.

## Where the clip on a message is

A session holds no footage, so a test that wants to see the picture a player
is looking at reads it off the edit that carried it:

* `interaction.clip()` for a button press, which edits the *interaction*
  (`FakeInteraction.snapshot` keeps the attachment's bytes under `"clip"`);
* `retro.shown_clip(view)` for the paths that edit the message itself --
  `[p]retroreset` through `RetroView.show_clip`, and the first clip of a
  game, which arrives as the `file=` of the send.

That is closer to what the feature promises than an attribute was, which is
why the real-core Undo and `[p]retroreset` comparisons in
`test_saves_roundtrip.py` go through it.

## Getting the cores and ROMs

```bash
python tests/fetch_assets.py            # ./test-assets, or $RETRO_TEST_ASSETS
RETRO_TEST_ASSETS=./test-assets pytest -m emulator
```

The script pulls the cores from the libretro buildbot and the ROMs from
their authors' releases (dmg-acid2 and uCity are MIT-licensed, Libbet and
the Magic Floor is zlib, nestest is the standard NES test ROM). Nothing it downloads is committed. Two assets
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
  and `test_saves_roundtrip.py` for the places the cog *and* a real core are
  needed at once (it puts `RetroEmulator` back over the fake): the save
  export/import round trip, and Undo. `test_malformed_roms.py` does the same
  for deliberately corrupted cartridges, at both levels -- the emulator on
  its own, and `[p]retro` with a ROM no core will take. `test_packaging.py`
  covers the cog as Red's Downloader sees it, `retro/version.py` included --
  that module loads standalone too, so the version can be checked with
  neither Red nor `discord.py` installed.
* **A mistake that is invisible until somebody reads a message gets a
  source-level test.** `test_packaging.py` has two: no `.py` file may carry
  a version literal of its own, and no string literal outside a *docstring*
  may contain `[p]`. Red substitutes `[p]` in a command's help text and
  nowhere else, so `[p]retro <name>` in a string the cog builds and sends
  reaches the channel exactly as written -- an instruction to type a prefix
  nobody has. A command passes `ctx.clean_prefix`; a reply with no context
  (a button click) asks `Retro._prefix_for`, which is why `FakeBot` answers
  `get_valid_prefixes` with the same `!` that `FakeContext.clean_prefix`
  hands out. The behavioural halves are in `test_cog_content.py`,
  `test_cog_saves.py` and `test_resume.py`.
* **A corrupted ROM is a regression test, not a fuzzer.** Everything in
  `test_malformed_roms.py` is seeded, so a failure is reproducible, and the
  sixteen runs it makes cost about 1.5s in total. What it asserts for every
  one of them is the invariant -- a clean run or an `EmulatorError`, inside
  a time budget so a hang fails rather than hanging the suite, and never a
  loaded core left behind -- rather than that any particular seed breaks any
  particular core. Two tests *are* about a specific failure mode, and they
  say so in their names: without one of those, nothing would exercise the
  wrapping in `RetroEmulator.advance` at all. The measured hit rates per
  core are in that file's docstring; if a core update moves them,
  re-measure the seeds rather than deleting the test.
* **Comparing two save states is not a byte comparison.** Gambatte's state
  carries a four-byte `time` field that a cartridge with no real-time clock
  never initialises, so two states serialized from an identical machine come
  out four bytes apart as soon as anything in the process has allocated in
  between -- an `await` is enough. `test_saves_roundtrip.py` therefore
  compares the console's work RAM, the cartridge's battery RAM and a hash of
  the picture (all three of which *are* exactly the emulated machine) and
  allows the state itself `STATE_SLACK` bytes of difference. Four out of
  182,530, measured.
* **`FakeEmulator.reset()` is a power cycle, not test bookkeeping.** The
  fake's frame counter *is* its machine state (its save state carries it), so
  `reset()` puts it back to zero and leaves `sram` alone, exactly as
  `retro_reset` does to a real cartridge. What used to be called
  `FakeEmulator.reset()` -- forget every instance, clear the per-test
  settings -- is `FakeEmulator.reset_all()`, because a classmethod of that
  name would shadow the real method on every instance.
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
