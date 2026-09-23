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
| fast | console tables, button layouts, emoji, the per-console press line, zip handling, the version metadata, the `RetroCog` -> `Retro` migration, the whole cog driven against fakes (`[p]retrosaves`, `[p]retroreboot`, `[p]retroend`, the press queue and the Undo button included), the session-record lifecycle (the channel and guild listeners, and bounded growth), the shared restore chain, the clip arithmetic and the fast frame grab (`press_plan`, `input_budget`, `capture_plan`, `clip_size` and the rest of `retro/clips.py`: plain functions of a frame rate, a framebuffer or a frame size, so no core is needed), property tests | nothing (more of it runs with `discord.py` installed; a few tests that read a clip's frames back want Pillow) |
| `-m emulator` | real libretro cores: clips at every length from 0.2s to 4s, timing (playback really does match emulated time, fractions included), clip-boundary continuity, the size each console is posted at, resetting a core, save states, battery saves, core options, BIOS directory, deliberately corrupted ROMs (`test_malformed_roms.py`), and the save export/import round trip, the Undo round trip and the `[p]retroreboot` round trip through the cog | `libretro.py`, Pillow, cores and ROMs (`test_saves_roundtrip.py` and `test_malformed_roms.py` also want `discord.py`) |
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

The controller does the same thing one level down. `retro/RetroView.py` was
2,700 lines with about a third of them not about the view at all, and five
modules took that third: `retro/timing.py` (how long a button is held, how
many taps fit in a clip, how long an edit waits before it may replace the one
on screen), `retro/text.py` (every line the session writes, and the
sanitising a display name goes through before it can be one),
`retro/restore.py` (`Progress` and the save state -> previous state ->
in-game save chain), `retro/permissions.py` (`may_manage`) and
`retro/session.py` (booting a core, emulating a press, an undo or a reboot,
encoding the clip, and the undo history). `RetroView`
re-exports every name they took, so `retro.viewmod.<name>` still works in the
tests that use it; `tests/test_view.py`'s `MOVED` table holds that promise,
and `RetroEnv` also offers `timingmod`, `textmod`, `restoremod`,
`permissionsmod` and `sessionmod` for a test that would rather say where the
thing lives.

`retro/session.py` is the one that is not plain functions: it is
`SessionMixin`, which `RetroView` inherits, and everything on it **blocks,
runs in the cog's single emulator worker thread and assumes the emulator lock
is held** (`_encode` is the deliberate exception and runs with the lock
given back -- see that module's docstring, which is where the rule is
written down). Two consequences for the tests:

* it moved *methods*, so `MOVED_METHODS` in `test_view.py` makes the same
  "same object, not a copy" promise for `RetroView.capture_press` and its
  fifteen neighbours that `MOVED` makes for module-level names;
* **patch `retro.sessionmod`, not `retro.viewmod`, for anything the mixin's
  own code looks up.** A re-export is a second binding of the same object:
  `SessionMixin._trim_history` reads `MAX_UNDO_BYTES` out of its own
  module's globals, so lowering `viewmod.MAX_UNDO_BYTES` changes a name
  nothing reads and the test passes while bounding nothing. `sessionmod`
  exists for exactly that, and `history_is_consistent()` reads the same
  binding the code does.

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

**And a queued press obeys it too**, which is the thing to watch when
touching the queue: a press that was taken down while somebody else's was
running is edited onto its *own* deferred interaction when its turn comes, so
"one press, one edit" is a statement about every press rather than about
whichever one won the race. The section "Overlapping presses are queued, not
dropped" in `test_cog_session.py` is all of it:

* `test_the_running_press_says_what_is_queued_behind_it` -- the running
  press's single edit carries `*Queued: ⬅️*`, and the queued press then
  makes its own `["response.defer", "edit_original_response"]`;
* `test_two_simultaneous_presses_both_happen_one_edit_each` -- which used to
  assert that the loser produced *nothing at all*. That was the bug;
* `test_a_queued_press_runs_against_the_state_it_was_queued_behind` -- order,
  read off `FakeEmulator.frame` (its frame counter *is* its machine state, so
  "which state did this press see" is answerable exactly);
* `test_the_queue_is_bounded_and_says_how_deep` and
  `test_one_person_gets_one_waiting_press_however_fast_they_click` -- the two
  rules that keep the latency and the fairness;
* `test_the_queue_is_discarded_when_the_game_moves_somewhere_else`,
  parametrised over hibernate / retire / reboot / undo / replaced, plus
  `test_a_discard_says_so_once_on_the_next_line`. A press that vanishes
  silently is the bug the queue exists to fix, so the one case where dropping
  is right has to say so.

## Pacing: a clip is not replaced before it has been watched

A clip takes 42-92ms to make and 1005ms to watch, so a queue drain used to
replace each one after about 5% of it had played. The fix is that the *edit*
waits, for the clip's whole playing time at every clip length; the rule and
the numbers behind it are in the note above `MAX_PACE_SECONDS` in
`retro/timing.py`.

That wait was capped at 1.25 seconds once, which meant every clip longer than
that was replaced part-played and the player was jumped forward over the
difference -- 2.75 seconds of a 4 second clip. `MAX_PACE_SECONDS` is now a
second past the longest clip the setting can ask for, i.e. a guard against a
nonsense figure rather than a policy, and
`test_every_clip_length_is_paced_for_its_whole_playing_time` holds that at
1.5, 2, 4 and 5 seconds as well as at the default.

**The gate spends its time in exactly one place** -- the module-level
`pace_wait` in `retro/RetroView.py` -- and `RetroEnv` swaps that for a
recorder in every cog test. So the whole gate runs (the deadline, the cap, the
floor, the cancellation) while nothing actually waits, and the tests assert on
the delay that was *asked for* rather than on elapsed time:

* `retro.pace_waits` is every delay any view in this test asked for, in order;
* `view.last_pace_seconds` is the last one, and `0.0` for an edit that went
  straight out;
* `retro.real_pacing()` puts the wait that really sleeps back, for the three
  tests that are about waiting. Those use a 0.2 second clip length so the
  whole drain costs tenths of a second.

That is why the fast suite still runs in about the same time it did. Paying
for it would be a real second per press, several hundred times over.

The section "A clip is not replaced before it has been watched" in
`test_cog_session.py` is all of it, and the statements worth knowing are:

* `test_a_press_with_nothing_playing_is_not_held_back` -- the common case, one
  person pressing one button at a time, must stay instant;
* `test_the_queue_drains_with_real_pacing_and_still_finishes` -- four presses
  and four *real* waits, one after another, with an empty queue at the end.
  Every edit now waits longer than it used to at any length above a second,
  so a gate that could fail to come back would fail here first;
* `test_pacing_never_holds_the_emulator_lock` -- a whole press on *another
  channel*, run from inside the wait itself. There is one libretro core for
  the whole bot, and a recent bug was a lock held across Discord calls, so
  "only the edit waits" is checked by having somebody else get in;
* `test_teardown_never_waits_on_pacing` -- parametrised over sleep, end,
  reboot, eviction and unload, with the *real* wait, so "it was cut short" is
  a measurement. Each of those calls `view.cancel_pacing()` before it reaches
  for the view's lock, because a press that is holding its edit back is
  holding that lock;
* `test_a_paced_press_still_makes_exactly_one_edit` -- waiting is not
  something anybody can see, so it must not cost a second edit or a second
  response.

The arithmetic underneath is in `test_clips.py` (`clip_plan` and
`playback_seconds`: how long a clip plays for, knowable before there is a clip
to measure) and the identity between that arithmetic and the bytes is asserted
against a real core in `test_emulator.py` -- the durations in the WebP's ANMF
chunks add up to exactly what the cog paced against.

**The line above the clip starts with the game**, so almost every content
assertion goes through `fakes.RetroEnv.line(view, text)` rather than comparing
a bare sentence. That helper *spells the format out* instead of reading it
back off the view -- a test that agreed with whatever `RetroView._line` did
would assert nothing -- and the literal form is pinned once, against a known
game, in `test_the_line_names_the_game_before_anything_else`:
`**ucity** · Tester pressed A.`, one middle dot throughout.
`test_the_header_never_says_the_session_is_asleep` covers the other side:
the header is the game's name whatever state the session is in. A `· asleep`
mark used to go on it, and pinning its absence is what stops it coming back
-- the wake press's *Woke up where you left off.* is the whole of what gets
said now.

The console used to sit between the two (`**ucity** · Game Boy — ...`) and the
queue listing used to name the presser (`*Queued: Ada ⬅️*`). Both are gone from
the line; `Pending.who` is still captured at the moment of the click, and
`test_a_queued_press_from_somebody_unnameable_still_reads` is what would
notice if it stopped being.

**Saying who pressed which button rides on that same edit.** Every action
writes one line of `content` -- `Rob pressed A.`, `Rob pressed ⬅️.`,
`Rob waited.`, `Rob undid the last press.`, `Rob reset the game.` -- and the
tests in the "Who did it" section of `test_cog_session.py` check it three
ways:

* `test_every_action_says_who_did_it` is table-driven over `ATTRIBUTED`, one
  row per action, each row saying how to perform it and what the message must
  then read. A press, Wait and ×3 arrive as `interaction.user`;
  `[p]retroreboot` is a *command* and arrives as `ctx.author`, so its row goes
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

**No control is allowed to be greyed out any more**, so
`fakes.CONDITIONAL_CONTROLS` is an empty tuple. Both entries it used to have
went for the same reason -- a present, dead, unexplained control reads as
broken:

* **Undo**, whenever the history was empty (which is every session's state
  after a bot restart, since the history is memory only). It is always
  enabled now and a click with nothing to undo is answered privately; a
  *disabled* Discord button cannot be clicked, so that explanation used to be
  unreachable by the very person looking at the dead control. See
  `test_the_undo_button_stays_clickable_with_nothing_to_undo`;
* **×3**, on a clip too short for two taps, which is not drawn at all --
  plus one line on the press that hides it, because a control that silently
  disappears reads as removed just as surely
  (`test_retroset_cliplength_reaches_live_sessions_and_their_buttons`).

`fakes.pressable()` still filters by `CONDITIONAL_CONTROLS`, so the next
control that can have nothing to do has to argue with that comment first.
`fakes.button()` returns `None` for a control the view does not have, and the
×3 presence/absence table is
`test_the_repeat_button_is_drawn_only_when_it_can_do_something` in
`test_view.py` -- `REPEAT_BY_LENGTH` × all eight consoles, from the 0.2s
settings floor to the 5s ceiling, checking the tap count, whether the button
exists, its label, that the layout still fits Discord's grid, and that Wait
and Undo have not moved.
`test_changing_the_clip_length_adds_and_removes_the_button_in_place` walks a
live view down to 0.2s and back up, because a Discord action row is ordered
by insertion and simply re-adding the button would land it after Undo.

The matching statement for the *content* of a clip is
`test_one_clip_carries_on_from_the_last_with_no_frames_lost` in
`test_emulator.py`: it records two consecutive clips off a real Game Boy,
then rewinds the save state and emulates the same frames one at a time, and
requires the first clip's last picture to be its window's final emulated frame
and the second clip's first picture to be one capture step into the next
window. `tests/test_clips.py` makes the same statement about `capture_plan` at
every clip length and frame rate, with no core.

**A recorded clip plays for exactly as long as it emulated**, and that rule is
unconditional of the recording -- see
`test_a_clip_plays_for_as_long_as_it_emulated`, whose docstring states it and
says why it was nearly weakened.

One thing shortens a clip afterwards, and it does it to playback rather than
to emulation: `clips.trim_repeated_opening` drops the opening pictures that
are byte-identical to the still the previous clip left in the channel, so a
clip always opens on something new even on a core as slow to react as mgba.
The three rules that make it safe are in `test_clips.py`, under "None of the
previous clip is shown in the new one" -- the final picture is never dropped,
a clip is never emptied, and **a clip of a screen where nothing moved is left
completely alone**. That last one is a regression that has already shipped:
trimming it leaves the single picture the first rule obliges it to keep and
plays a 1005ms clip as a 17ms flash. `test_cog_session.py` pins the session
half -- who remembers the still (a 16 byte hash, never the pixels), that only
a *successful* edit promotes it, what the pacing gate then waits for, and
every teardown path that has to forget it.

**A clip starts exactly one emulated frame after the last one ended.** A
picture is taken *after* an emulated frame, so the last picture of a clip is
its window's final frame and the next clip picks the console up on the next
frame of it -- no repeat, no gap, and no way for the console to run ahead of
what has been posted. The next clip's first *picture* is one capture step
further on, because `capture_plan` photographs the end of each span so that a
press scheduled on frame 0 has had time to land; the frames in between are
emulated and counted in that picture's duration, never skipped.

Both halves have been got wrong. A bounded **pre-roll** ran a clip's opening
press out unphotographed until the picture stopped being the one the previous
clip had left in the channel: it put 1 to 16 frames into every seam and, on
the static screens this cog is actually played on, opened the clip on the
repeated picture anyway. Removing it left the report standing, because the
capture cadence still started on frame 0 -- one emulated frame after the
button went down, which is the one frame on which nothing can have happened
yet. Both are written up with their measurements in the seam block in
`retro/clips.py` and in section 3a of `test_emulator.py`.

Six tests carry it, and between them they are the whole argument:

* `test_the_seam_between_two_clips_is_exactly_one_emulated_frame` is the
  statement itself, over every probe and both with a press and without one.
  Two clips are recorded back to back, then the same window is rewound and
  emulated one frame at a time with identical input, and every picture of
  both clips has to be its reference frame -- so the first clip ends on frame
  N and the second opens on N+step. It also counts the frames the recordings
  emulated (by wrapping `advance`), which is the half a frozen screen cannot
  satisfy by accident;
* `test_a_clip_opens_on_the_game_already_reacting_to_the_press` is the fix for
  the reported stutter, per core: it measures how long the core really takes
  to answer the button (frame by frame, off the core itself) and then requires
  the second clip's opening picture to differ from the picture the first one
  finished on. `PRESS_LATENCY` above it carries the per-core numbers -- 1 for
  µCity, 2 for nestest, 4 for snes9x, 11 for mgba -- and mgba is deliberately
  skipped rather than asserted, since eleven frames is nearly three pictures
  and one repeated opening picture is still honest there;
* `test_the_clip_frame_rate_does_not_move_the_seam` answers the question the
  report asked ("is there something we can do in the framerate?") at 10, 15,
  20 and 60 fps: `capture_plan` photographs the final frame at every cadence,
  so the picture left in the channel is where the next clip resumes whatever
  the rate. Raising the rate takes the shutter *earlier* into the window,
  which is the wrong direction for reaction latency;
* `test_a_moving_game_never_repeats_a_picture_across_the_seam` is the
  pictures half, on the one probe that animates by itself;
* `test_a_completely_static_screen_still_produces_a_whole_clip` is the cost,
  and the case a trim would have destroyed: screens where *every* picture is
  unchanged still get the full clip, every picture the plan asked for, and
  durations adding up to the whole second. It presses once first, so the
  screen is where a *second* press finds it, and `skip`s rather than fails if
  a ROM turns out not to be static under the core build in use;
* `test_a_games_own_reaction_latency_is_shown_rather_than_skipped` pins the
  trade in the other direction: Libbet answers Start on its third frame, and
  the clip holds the unchanged picture for exactly the pictures whose frames
  fall before that -- no more (nothing is dragged out) and no fewer (nothing
  is hidden). At the default step of four those three frames fit inside the
  opening picture's own duration, so the count is zero; on a slower core it
  would not be, and the arithmetic is the same either way.

`test_the_hold_is_honoured_in_full_and_released_before_the_last_picture` reads
the input back off the frames the core really saw and pins that the window is
the clip and nothing else, and
`test_a_clip_with_no_input_in_it_covers_its_window_from_the_first_frame` is
Wait, Undo, a boot and `[p]retroreboot`, which always resumed on the very next
frame.
The arithmetic underneath all of it is covered with no core at all in
`test_clips.py` (`test_the_seam_between_two_clips_is_exactly_one_emulated_frame`
and `test_the_clip_frame_rate_cannot_move_the_seam`, over every frame rate,
clip length and cadence), and `test_view.py` checks that every tap of the
repeat button lands inside the clip that records it across the whole
clip-length x hold settings grid.

`roms/libbet.gb` was added to `fetch_assets.py` for this: Libbet and the
Magic Floor (zlib) is the only ROM here with the shape the bug was reported
against, a Game Boy screen that sits completely still until it is prodded and
then takes a couple of frames to react. uCity animates every frame (so it is
the control -- the one probe whose clips never repeat a picture across a seam)
and dmg-acid2 never changes at all. Libbet also ignores the d-pad on that
screen, which gives the must-not-flash case from the same ROM, and that is how
the seam tests drive it: pressing A there makes gambatte dupe a frame, and
libretro.py 0.11+ raises out of its own environment callback when it sees one
(0.6.0 does not), so consecutive clips of Libbet under A come back as "the
core crashed while running". That is an upstream regression rather than
anything to do with clips.

## What the cog holds on to

`tests/test_leaks.py` is the file for "after N of these have come and gone,
is N still in memory?". It is in the fast suite and needs no core. The
container it was written for is not the cog's own: discord.py keeps every
persistent view in a store keyed by message id, filled by `Client.add_view`
*and* by every send or edit that carries a view, and emptied by nothing
except `View.stop()` -- there is no `bot.remove_view`. So every game a
channel plays used to leave a whole `RetroView` reachable for the life of the
process, each one holding a stack of compressed save states.
`Retro._release_view` is the fix and these tests are the proof; they fail if
it is taken out.

Four of its sections are worth knowing about by name:

* **section 2, the footage a session holds**, which is zero: a clip is
  built, uploaded and let go of inside one press. The assertions use
  `fakes.footage_bytes(view)` -- every bytes-like attribute except the
  compressed undo history -- rather than naming an attribute, because the
  thing being guarded against is a clip being kept under *any* name.
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
  `[p]retroreboot` through `RetroView.show_clip`, and the first clip of a
  game, which arrives as the `file=` of the send.

That is closer to what the feature promises than an attribute was, which is
why the real-core Undo and `[p]retroreboot` comparisons in
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
(`roms/snes_rotzoom.sfc`) and a Game Boy RPG (`roms/gb-rpg.gb`, for the
one-press-one-tile test). Layout:

```
$RETRO_TEST_ASSETS/cores/gambatte_libretro.so
$RETRO_TEST_ASSETS/roms/ucity.gbc
```

`RETRO_TEST_ASSETS` (or `./test-assets`) is the **only** place the fixtures
look, so `RETRO_TEST_ASSETS=$(mktemp -d) pytest` is an honest "no assets
here" run in which every test that needs one skips. There used to be a
handful of `/tmp` fallbacks as well; they are gone, because `assets.core()`
hands what it finds straight to `dlopen` and `/tmp` is world-writable.

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
  source-level test.** `test_packaging.py` has four: every command the cog
  publishes is named in `retro/README.md`, every README heading a *sent*
  string points at exists, no `.py` file carries a version literal of its
  own, and no string literal outside a *docstring* contains `[p]`. Red substitutes `[p]` in a command's help text and
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
* **Four kinds of test are deliberately not here, and should not come back.**
  Each of them used to be, in quantity, and none of them can fail for a
  reason anybody wants to know about:
  * **assertions on the prose of a README or of `info.json`.** A wording
    change is not a regression, and a test that turns one into a failure
    teaches people to stop improving the wording. What is checked instead is
    mechanical and derived: that every command the cog publishes is named in
    `retro/README.md`, and that every README heading a sent string points at
    exists. Nothing checks *what a sentence says*.
  * **"the removed feature is still removed".** Lists of `assert not
    hasattr(module, "MAX_REPLAY_BYTES")` cannot fail unless somebody
    deliberately re-adds the name, at which point they are in the way rather
    than a warning. The exception worth keeping is a removal whose return
    would be *broken* rather than merely unwanted, which is why
    `test_systems.py` still refuses the two consoles this layout cannot
    render. A feature that is gone is proved gone by the behaviour tests of
    what replaced it.
  * **restatements of a definition.** `PRESSED_NOTE == ACTION_NOTES["press"][1]`
    is the line above it in the source.
  * **assertions on a dependency's internals.** `inspect.getsource` of a
    private Red module, or the exact text of a figure the libretro buildbot
    republishes nightly, breaks on an unrelated upgrade and says nothing
    about this cog. Ask the dependency a question instead (see
    `test_reds_confirmview_is_the_shape_the_cog_drives_it_as`).
* **An invariant gets one home.** The undo history's -- `history_bytes` is
  exactly the sum of the deque, and both bounds hold -- was spelled out in
  four files, which is three places for it to be spelled out differently. It
  is `fakes.history_is_consistent(view)` now.
* **A test that needs time to pass should say so, not sleep.** Use an
  `Event` for "the other coroutine got there" and `os.utime` for "this file
  is older than that one"; `time.sleep` in a test is wall clock every
  developer pays on every run for something that can be stated exactly.
* One libretro core can be loaded per process. Build emulators through the
  `emu` fixture, which stops the previous one for you, and never run the
  emulator tests in parallel.
