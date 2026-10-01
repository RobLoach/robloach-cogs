# Retro — design notes

How the cog works and why, with the measurements behind each decision. The
manual is [retro/README.md](../retro/README.md); what was tried and **reversed**
is [HISTORY.md](HISTORY.md), kept separately so nobody re-attempts it.

Measurements are on a Raspberry Pi 5, **at the defaults of the time** (mostly a
1s clip and a 160ms hold; they are now 1.6s and 80ms). They are left as
measured, because what they show is the comparison between the columns.

## The seam between two clips

**A clip starts exactly one emulated frame after the last one ended.** A
picture is taken *after* an emulated frame, so a clip's last picture is its
window's final frame and the next clip picks the console up on the very next
one. Press, clip, press, clip is one unbroken run: nothing emulated twice,
nothing run past unaccounted for.

**A clip photographs the end of each sampling span, not its start** — frames
4, 8 … 60 of a one second Game Boy clip rather than 1, 5 … 57, 60. That matters
because a game takes a moment to react, and the opening picture would otherwise
be the state one frame after the button went down, on which nothing can have
happened yet. First emulated frame whose picture differs at all:

| Core / ROM | Button | First frame that differs |
| --- | --- | --- |
| gambatte / µCity | ↓ | 1 (it animates every frame anyway) |
| fceumm / nestest | ↓ | 2 |
| snes9x / rotozoom | A | 4 |
| mgba / homebrew | A | 11 |

Sampling the end of the span gives the console a whole picture's worth of
emulation — four frames, 67ms — to answer the button before the shutter. It
costs no game time and skips no frame. Opening pictures that are the previous
clip's still over again:

| Core / ROM | Button | Before | Now |
| --- | --- | --- | --- |
| fceumm / nestest | ↓ | 1 of 16 | **0 of 15** |
| gambatte / Libbet | Start | 1 of 16 | **0 of 15** |
| gambatte / µCity | ↓ | 0 of 16 | 0 of 15 |
| fceumm / nestest | Start | 16 of 16 | 15 of 15 |
| gambatte / Libbet | ↓ (ignored) | 16 of 16 | 15 of 15 |
| gambatte / dmg-acid2 | A | 16 of 16 | 15 of 15 |

**The bottom three rows must stay that way.** They are games that do not answer
the button at all, and a game's reaction latency is *shown* rather than skipped.
The encoder merges identical pictures into one stored frame and adds their
durations, so it is a held picture rather than a stutter — and it is the truth
about the game. Escaping it would mean skipping forward until the picture
changes, which is [what the pre-roll did](HISTORY.md#the-pre-roll).

**Repeats of the previous clip's still are dropped**, with their durations, so
a clip opens on something new. Two rules keep that safe: the final picture is
never dropped (it is where the next clip resumes), and a clip in which nothing
moved is left whole — trimming that is how a 1005ms clip once played as a 17ms
flash. This drops *playback*, never emulation.

[The frame rate is not a lever on any of this.](HISTORY.md#the-frame-rate-as-a-lever)

## The controller

Laid out like the console's own pad. A Game Boy, where `·` is a spacer:

```
·  ⬆️
⬅️  ⬇️  ➡️   B  A
Start  Select  ⏳ Wait   A ×3   ↩️ Undo
```

Bigger consoles grow into the same shape — the GBA puts **L**/**R** on top, the
SNES adds **Y X / B A**, the Genesis and PC Engine keep their six-button cluster.

- **⏳ Wait** — one clip's worth of time with no input.
- **↩️ Undo** — steps back one press, eight deep. Kept on disk, so it survives a
  restart and a sleep. Updating a core empties it.
- **A ×3** — repeats your **last press** several times in one clip, so `⬅️`
  then `⬅️ ×3` walks four tiles. Labelled with the real tap count, and
  **hidden rather than greyed out** when only one fits:

| Clip length | Taps | Button |
| --- | --- | --- |
| 0.2s (floor) – 0.25s | 1 | not shown |
| 0.26s – 0.45s | 2 | `A ×2` |
| 0.46s – 5s (ceiling) | 3 | `A ×3` |

There is no Stop or Reset button. Sleeping, rebooting and ending a game are
commands, because each is destructive to everybody's game.

### What a press does

Records the next **1.6 seconds** (`[p]retroset cliplength`) and posts it as an
animated WebP that plays once. The button is held **80ms**
(`[p]retroset hold`). Between presses the console is frozen, so a game left
overnight is where you left it.

The message carries the clip and one line:

> **µCity** · Rob pressed A. *Queued: ⬅️*

Names are plain text and can never notify anyone.

**Presses queue rather than drop**, five deep, first come first served — one
person may hold every slot. Every waiting press is shown. The queue is thrown
away by anything that moves the game elsewhere (sleep, reboot, undo, an idle
timeout, another channel taking the emulator), and the message says how many
went.

**A clip is never replaced before it has finished playing.** A press that
arrives when nothing is playing is still instant.

## Clip size and format

Each console is posted at a size that suits it rather than a flat 2×. Discord
scales to the message column anyway, so doubling a TV console was work thrown
away. One second of real motion, end to end:

| Console | Was | Now | Time | Bytes |
| --- | --- | --- | --- | --- |
| Game Boy | 320×288, 47ms | 320×288, 47ms | — | — |
| Game Boy Advance | 480×320, 83ms | 480×320, 86ms | +3% | +19% (of 0.6 KiB) |
| NES | 585×448, 169ms | 293×224, 75ms | −55% | −7% |
| Super Nintendo | 597×448, 383ms | 299×224, 173ms | −55% | −21% |
| Genesis | 585×448, 116ms | 293×224, 65ms | −44% | −23% |
| Master System | 585×384, 61ms | 293×192, 34ms | −44% | −22% |

Still nearest-neighbour and still corrected for non-square pixels; a console
wider than its corrected width is doubled instead, so nothing is ever shrunk.

**WebP, always.** On a 4 second clip: 24.4 KiB against 53.2 KiB of GIF on a
moving Game Boy screen, 248 KiB against 998 KiB on the SNES. It is also
pixel-exact where GIF must quantize to 64 colours, and it stores durations in
milliseconds rather than centiseconds — enough to hold the 67ms a frame really
lasts.

## Pacing

A clip costs far less to make than to watch, and once the queue removed the
human from between two presses that stopped being harmless:

| clip | produced in | plays for | was on screen for |
| --- | --- | --- | --- |
| 1 | 41 ms | 1005 ms | 68 ms |
| 2 | 67 ms | 1005 ms | 54 ms |
| 3 | 42 ms | 1005 ms | 42 ms |

Each was replaced after about **5%** of it had played. The seam is exact, but
nobody was ever shown it. So the *edit* now waits for the clip it replaces to
finish:

| | 4 clips (one press plus a full queue of three) | each clip on screen |
| --- | --- | --- |
| before | 207 ms | 42–68 ms of 1005 ms |
| after | 3.08 s | 1006–1007 ms |

The wait is for the **whole** clip at every length. A clip cut off part-played
is not merely a shorter pause — the next clip picks up after the truncated
one's last *emulated* frame, so you are jumped over footage that was made and
then painted over. That was [the 1.25 second
cap](HISTORY.md#the-125-second-pacing-cap), and it is gone.

Four things keep the waiting cheap:

- **a press with nothing playing is still instant** — the wait is a deadline,
  not a delay, so time already spent emulating counts against it;
- **nothing else waits.** Only the edit is held; the core was handed back
  before it. The one core is held for *emulation and nothing else* — encoding,
  saving and posting all happen without it;
- **the wait is bounded** at a second past the longest clip the setting allows;
- **nothing that takes the game away waits at all** — sleep, end, reboot, undo,
  an idle timeout, eviction. Undo and reboot do not pace their own clips
  either: those replace a picture of something that has stopped having
  happened.

## The press queue

A press holds the session for about a second, and a click arriving in that
second used to be acknowledged and silently forgotten. With two or three people
playing, *most* clicks land there — so the controller felt intermittently dead.

Four rules:

- **at most as many wait as fit in about five seconds.** Each is a whole clip
  of latency, and a queued press is emulated against a state its author has not
  seen — so the depth is a *time budget*, not a count: five presses at a one
  second clip, three at the 1.6s default, one at the 5s ceiling. A flat count
  meant the wait grew with the setting, which is what it is for.
- **first come, first served, and one person may hold every slot.** Taking
  turns is what the depth cap already does; a
  [per-person limit](HISTORY.md#one-waiting-press-per-person) broke the
  commonest way one person plays. A press with no room is dropped silently: the
  queue is already on the message, and the moment somebody is clicking fastest
  is the worst moment to answer every click.
- **every waiting press is visible**, as a suffix on the line the running press
  is already rewriting, so it costs **no extra edit**. Consecutive presses by
  one person read as one run: `*Queued: Rob ⬆️⬇️⬇️*`.
- **the queue is intent, never work.** A waiting entry is a button name and a
  deferred interaction; nothing touches the emulator until its turn.

**The queue is thrown away** whenever the game stops being what those presses
were aimed at — sleep, reboot, end, undo, idle timeout, eviction, switching
games. Undo is the sharpest case: those presses were queued against the state
the undo just put back, so running them would undo the undo one button at a
time. The one case where dropping is right says so, once.

**Undo is not queueable.** "One press back" queued three deep means undoing a
press its author never saw.

## Undo

Every press takes a save state before it changes anything. This is cheap enough
to be free:

| Console | Save state | Compressed | Ratio |
| --- | --- | --- | --- |
| Game Boy | 182,530 | 15,780 | 8.6% |
| NES | 13,758 | 870 | 6.3% |
| Game Boy Advance | 528,448 | 6,298 | 1.2% |
| Super Nintendo | 823,407 | 12,606 | 1.5% |
| Genesis | 1,036,288 | 19,524 | 1.9% |

A full eight-deep history is 124 KiB on the Game Boy, and compressing one costs
about a millisecond inside a press that already spends tens. Capped by bytes as
well as count (2 MiB), because those are *today's* cores.

**Kept on disk**, in one `.undo` file beside the save state, so a message that
has outlived a restart still undoes. **Read lazily** — on wake, or on a click,
never at load, which would hold up to 2 MiB *per channel* for games nobody may
touch again. Only ever read into an *empty* history, which is what makes it
safe from both places at once.

Written after the state has landed, so the two describe the same game. An
emptied history *removes* the file, so "no file" and "nothing to undo" stay the
same thing. A history that will not parse is worth one empty Undo button and
nothing else.

**Wiping progress wipes the history**, or Undo walks straight back into what
was thrown away — and on import, silently undoes the import. Two defences: a
`[p]retrosaves` command that pauses a live game clears it, and a game with no
live session has the file removed by whatever deletes its saves (`SavePaths`
lists every file, and the delete walks it).

Two details: an undo **costs one clip of emulated time**, because it records
forwards from the restored state rather than freezing at it — freezing would
leave the clip a second ahead of the game. And it **writes to disk
immediately**, because the state on disk is easily newer than the one just
restored.

## The ×3 button

**It repeats your last press**, several times in one clip — so `⬅️` then
`⬅️ ×3` walks four tiles, and `A` then `A ×3` clears a text box in one round
trip instead of three.

It used to repeat the console's confirm button and nothing else, which covered
text boxes and menus but not the commonest intent on a controller: walking.
A d-pad has four directions, and four more components is more than the Super
Nintendo has room for (19 of Discord's 25 already), so the one button follows
the last press instead.

It starts on the **confirm** button — a session nobody has pressed has no last
press, and a text box is what somebody reaches for it for first. **Wait, Undo
and a reboot do not move it**: Wait presses nothing, and the other two put the
game somewhere the last press no longer describes.

The label follows on the same edit the press was already making, so `⬅️ ×3` is
on screen the moment somebody walks — it costs no extra edit. The *name* is the
button's caption rather than its label, because the d-pad has no labels, only
arrows, and `⬅️ ×3` says which way far better than `LEFT ×3` would.

A click is dispatched from the **view's** current target rather than from the
button that was clicked: a stale copy on a message Discord has not re-rendered
carries whatever it was labelled with when that message was drawn, and
repeating a direction from four presses ago is not what the label somebody is
looking at says.

**Hidden, not greyed out, when only one tap fits.** One tap is exactly what the
confirm button one row over does, and a present, dead, unexplained control
[reads as broken](HISTORY.md#the-greyed-out-3-button). It says one line when it
goes, on an edit that was happening anyway; coming back says nothing.

| Clip length | Taps | The button |
| --- | --- | --- |
| 0.2s – 0.25s | 1 | not shown |
| 0.26s – 0.45s | 2 | `A ×2` |
| 0.46s – 5s | 3 | `A ×3` |

Those boundaries move with the hold: a tap costs its hold plus the gap after
it, so halving the hold from 160ms to 80ms moved them down from 0.46s and
0.73s.

**Wait and Undo never move** — the row reserves space for all three whether or
not the third is drawn.

## The controller's shape

Discord allows five rows of five. What each console uses:

| Console | Components | Rows |
| --- | --- | --- |
| Game Boy / Color, NES | 12 | 3 |
| Game Boy Advance | 14 | 3 |
| Super Nintendo | 19 | 4 |
| Sega Genesis | 18 | 4 |
| PC Engine | 18 | 4 |
| Master System, Game Gear | 11 | 3 |
| Neo Geo Pocket | 11 | 3 |

The worst case is the SNES at 19 over four rows, leaving a spare row. All three
controls fit beside every console's bottom row.

Every emoji the cog can put on a button is checked at import, because a
character that is not a real emoji is a `400 Invalid Form Body` that takes out
the whole command — that has happened in production. (Wait's hourglass
[replaced a fast-forward symbol](HISTORY.md#-for-wait), which was a lie.)

## One edit per press

**A press makes exactly one edit of the message**, which swaps the clip in and
redraws the buttons together. A Discord client re-renders a message on *any*
edit, and re-rendering restarts the attached animation — so a second edit
replays the clip already there. That is [what two edits per
press](HISTORY.md#two-edits-per-press) looked like from the channel.

The cost is real and unavoidable: instant "your click landed" feedback is gone,
because anything that shows something either edits this message or posts
another one. Clicks during a press are queued instead, so nobody sees *This
interaction failed*.

A sleeping session [does not advertise itself](HISTORY.md#the--asleep-marker);
the press that wakes it says so, on the edit it was making anyway.

## Rebooting

`[p]retroreboot` reboots the running game, as if you flipped its power switch.

**A command, not a button**, because it throws away everybody's
progress-in-flight — the same reason sleep and end are commands, and it has the
same permission check.

| | what it does |
| --- | --- |
| `[p]retroreboot` | reboots the game **now**. Nothing on disk is deleted. |
| `[p]retrosaves dropstate <game>` | deletes a save **state file**, so the game next starts from its last in-game save. Touches no running game. |

Three decisions inside it:

- **Nothing on disk is written.** A reset does not save, so the `.state` still
  holds the moment before it — a reset must not overwrite a good save with a
  title screen. It becomes the saved game only when the rebooted game saves of
  its own accord.
- **The in-game save is untouched.** `retro_reset` is the reset line, not a new
  cartridge.
- **A reboot is an undo point.** The state it is about to throw away is pushed
  onto the history first, so one click of Undo puts the player back. Nothing
  else in this cog can lose as much in one command.

## The dropdown, and slash commands

The **dropdown** on `[p]retro list` re-runs the ordinary command: it copies the
message, writes `[p]retro <name>` into it as the person who clicked, and asks
Red to invoke that. So there is one start path, with the same checks and
cooldowns — charged to **whoever clicked**, which is very often not whoever ran
the command. A component click goes through none of a command's gates on its
own; re-invoking is what gets them all back without a second copy to drift.

Deliberately **not persistent**: it expires after three minutes. A picker
surviving a restart would be a dropdown, a week later, in a channel that has
played three other things since.

**Two commands are hybrids**, and the reason is autocomplete — it does not
exist for prefix commands, and these are the two places where not knowing what
to type is the actual problem. `/retro play` rather than `/retro` because
Discord will not let a *group* be invoked, only its subcommands; without a
fallback there would be no slash way to start a game at all.

Every completion answers from what is already known — the installed cores, the
cached option definitions. **Never from a probe**: a probe dlopens a core on
the one emulator thread whatever is playing is using, and Discord allows about
three seconds. None of them can fail loudly either, because an autocomplete
that raises shows an empty box with no clue why.

The rest of `[p]retroset` is prefix-only. It is all owner-only, and a dozen
slash commands one person can run would clutter everybody's menu.

## Reporting which build is loaded

Twice a puzzling answer turned out to be a bot running an older build —
memorably `[p]retroset cliplength 0.8` replying *must be an integer*, on a copy
that predated fractional lengths. Three facts, because only together are they
honest:

- **the version** `info.json` declares — the single source of truth, read by
  `retro/version.py`, with no version literal in any `.py` to drift from;
- **the commit**, read straight out of `.git` with no subprocess, and only one
  directory up from the package so another repo's commit is never reported;
- **the fingerprint of the loaded code** — a hash of every `retro/*.py` *as
  they were when the cog loaded*. `git pull` without `[p]reload retro`
  deliberately does not change it, which is exactly the situation worth proving.

Nothing here can fail a command: every piece degrades to "not shown", and
`version.py` imports nothing but the standard library.

## Measuring a press

Three questions kept being answered by hand: how long a press takes, which part
is expensive, and whether the event loop is keeping up. The third is the one
production actually needed — a click Discord sent more than three seconds ago
answers `10062 Unknown interaction`, and every edit through that token then
answers `10015`, so the clip is lost. Telling a slow bot from a slow network
needs the bot measured.

`retro/metrics.py` keeps **count, total and worst per name** — four floats,
whatever the traffic. No history: a ring buffer would answer nicer questions
and grow with use, and nothing here is worth a megabyte of a bot's memory.
Nothing is persisted, because the question is "is this bot healthy *now*".

Three names, and the split is the point — a single "the press took 900ms"
cannot tell them apart, and which one is large is the whole diagnosis:

- **core wait** — somebody else's press, since there is one core;
- **emulate** — this press's own core time;
- **encode** — the expensive step, which needs no core and so runs with the
  lock already handed back.

**Event loop lag** is one coroutine sleeping a known time and seeing how long
that really took. Anything above zero is the loop busy elsewhere. A sample past
three seconds is logged at warning, because a click arriving in a stall that
long is one the player sees fail.

Every call site is on the press path, so none of it can raise: a metric that
can fail a press is worse than no metric. The timer records a block that threw,
too — a press that took four seconds and *then* failed is exactly the one worth
knowing about.

## What the cog forgets

| | what it is | when it goes |
| --- | --- | --- |
| the **session** and **Resume** records | a pointer: *this message was playing this game* | by itself, as soon as it cannot resume anything |
| the **save state** and **in-game save** | the player's progress, keyed by channel **and game** | only when somebody asks |

Because progress is keyed by channel and game rather than by message, dropping
a record loses the button and nothing else. Records go when the cached ROM was
pruned, the channel was deleted, the bot left the server, or the bot can no
longer see the channel — the last judged only once actually connected, because
Red loads cogs before logging in and at that moment every channel looks deleted.

**The saves for a deleted channel are deliberately kept.** From inside the bot,
an archived thread, a channel briefly lost sight of and a deleted one look
identical; deleting somebody's progress on that evidence is not a trade worth
making. The opposite mistake costs a few hundred kilobytes.

**One restore chain, two doors.** Starting a game and waking one run the same
function — save state, then in-game save, then the beginning — so they cannot
drift apart.
