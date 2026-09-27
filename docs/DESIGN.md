# Retro — design notes

Why the cog is built this way: the decisions, the measurements behind them, and
the attempts that were reversed. Not the manual — that is
[retro/README.md](../retro/README.md).

Measurements are on a Raspberry Pi 5, **at the defaults of the time** (mostly a
1s clip and a 160ms hold; they are now 1.6s and 80ms). They are left as
measured, because what they show is the comparison between the columns.

## Contents

- [The seam between two clips](#the-seam-between-two-clips)
- [Clip size and format](#clip-size-and-format)
- [Pacing](#pacing)
- [The press queue](#the-press-queue)
- [Undo](#undo)
- [The ×3 button](#the-3-button)
- [The controller's shape](#the-controllers-shape)
- [One edit per press](#one-edit-per-press)
- [Rebooting](#rebooting)
- [The dropdown, and slash commands](#the-dropdown-and-slash-commands)
- [Reporting which build is loaded](#reporting-which-build-is-loaded)
- [What the cog forgets](#what-the-cog-forgets)
- [Naming](#naming)
- [The RetroCog → Retro rename](#the-retrocog--retro-rename)

## The seam between two clips

One report — *"the clip seems to replay a bit from the previous clip"* — and
two wrong answers before the right one.

**First, the pre-roll.** A game takes a moment to react to a press, so on a
game that sits still until prodded the clip's opening picture was identical to
the one the previous clip left on screen. The fix tried was a bounded
*pre-roll*: run the console unphotographed until the picture changed.

It did not fix the report, and it broke the seam to not fix it. Emulated frames
between one clip's last picture and the next clip's first, and whether that
first picture is the held one again:

| Core / ROM | Button | Seam, with the pre-roll | Again? | Seam, without | Again? |
| --- | --- | --- | --- | --- | --- |
| gambatte / µCity | ← | 1, 1, 16 | no, no, yes | **1, 1, 1** | no, no, yes |
| gambatte / Libbet | ↓ (ignored) | 16, 1, 16 | yes, no, yes | **1, 1, 1** | yes |
| fceumm / nestest (a menu) | Start | 16, 16, 16 | yes | **1, 1, 1** | yes |
| fceumm / nestest | ↓ | 2, 2, 2 | no | **1, 1, 1** | yes |
| gambatte / dmg-acid2 | A | 16, 16, 16 | yes | **1, 1, 1** | yes |
| any of the above | (no press) | 1, 1, 1 | — | **1, 1, 1** | — |

The seam was `1 + the frames the pre-roll used`, exactly. On the static rows —
what this cog is mostly played on — it ran its whole 15-frame bound, found no
change, opened on the repeated picture anyway, and charged 251ms of game time
per press for it. Gone.

**Second, the opening picture.** A clip still opened on frame 0 of its window:
one frame after the button went down, the one frame on which nothing can have
happened. First emulated frame whose picture differs at all:

| Core / ROM | Button | First frame that differs |
| --- | --- | --- |
| gambatte / µCity | ↓ | 1 (it animates every frame anyway) |
| fceumm / nestest | ↓ | 2 |
| snes9x / rotozoom | A | 4 |
| mgba / homebrew | A | 11 |

So a clip now photographs the **end** of each sampling span rather than its
start — frames 4, 8 … 60 instead of 1, 5 … 57, 60. That gives the console a
whole picture's worth of emulation to answer the button, costs no game time and
skips no frame, which is the difference between it and the pre-roll. Opening
pictures that are the held one again:

| Core / ROM | Button | Before | Now |
| --- | --- | --- | --- |
| fceumm / nestest | ↓ | 1 of 16 | **0 of 15** |
| gambatte / Libbet | Start | 1 of 16 | **0 of 15** |
| gambatte / µCity | ↓ | 0 of 16 | 0 of 15 |
| fceumm / nestest | Start | 16 of 16 | 15 of 15 |
| gambatte / Libbet | ↓ (ignored) | 16 of 16 | 15 of 15 |
| gambatte / dmg-acid2 | A | 16 of 16 | 15 of 15 |

**The bottom three rows must stay that way.** They are games that do not answer
the button at all; the only escape is to skip forward until the picture
changes, which is either a jump (the pre-roll) or an unbounded wait. A game's
reaction latency is *shown* rather than skipped, and the encoder merges
identical pictures into one stored frame — so it is a held picture, not a
stutter, and it is the truth about the game.

**Repeats of the previous clip's still are dropped**, with their durations, so
a clip opens on something new. Two rules keep that safe: the final picture is
never dropped (it is where the next clip resumes), and a clip in which nothing
moved is left whole — trimming that is how a 1005ms clip once played as a 17ms
flash. This drops *playback*, never emulation.

**The frame rate is not a lever**, which was the first thing tried. A clip
photographs its window's final frame at any cadence, so the seam is identical
at 10, 15, 20 and 60 fps. A higher rate only brings the shutter *earlier*,
which is the wrong direction.

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

**The wait was capped at 1.25s, and that cap was itself a stutter.** A clip cut
off part-played is not just a shorter pause: the next clip picks up one frame
after the truncated one's last *emulated* frame, so you were jumped forward
over footage that was made and then painted over.

| cliplength | plays for | old wait | you never saw |
| --- | --- | --- | --- |
| 1 s (default) | 1.005 s | 1.005 s | — |
| 1.5 s | 1.507 s | 1.25 s | 0.26 s |
| 2 s | 1.993 s | 1.25 s | 0.74 s |
| 4 s | 4.003 s | 1.25 s | **2.75 s** |
| 5 s (max) | 5.008 s | 1.25 s | 3.76 s |

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

- **at most five wait.** Each is a whole clip of latency, and a queued press is
  emulated against a state its author has not seen. It multiplies with clip
  length: a full drain is five clips.
- **first come, first served, and one person may hold every slot.** There was a
  one-per-person rule, on the theory it made a group take turns. It broke the
  commonest way one person plays — walking four tiles is four clicks, and the
  last three were refused because the first was still waiting. A press with no
  room is dropped silently: the queue is already on the message, and the moment
  somebody is clicking fastest is the worst moment to answer every click.
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

Taps the confirm button several times in one clip, so a text box takes one
round trip instead of three.

**Hidden, not greyed out, when only one tap fits.** One tap is exactly what the
confirm button one row over does. A present, dead, unexplained control reads as
broken — the greyed-out version of this button was reported as the feature
having been *removed*. It says one line when it goes, on an edit that was
happening anyway; coming back says nothing.

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

**⏳ Wait** was drawn with ⏩ once, which was a lie: nothing is sped up or
skipped. Every emoji the cog can put on a button is checked at import, because
a character that is not a real emoji is a `400 Invalid Form Body` that takes
out the whole command — that has happened in production.

## One edit per press

The controls used to grey themselves out on click and come back with the clip:
two edits. **A Discord client re-renders a message on any edit**, and
re-rendering restarts the attached animation — so the first edit replayed the
*previous* clip from frame zero, and a moment later the new one replaced it.
From the outside, the game jumped backwards on every press.

So a press is acknowledged silently and makes a single edit. The cost is real
and unavoidable: instant "your click landed" feedback is gone, because anything
that shows something either edits this message (rewinding the clip) or posts
another one (an ephemeral notice, tried and removed for being spam). Clicks
during a press are queued instead, so nobody sees *This interaction failed*.

**A sleeping session does not advertise itself.** The header gained a
`· asleep` mark once. It was accurate and unhelpful: sleeping is an
implementation detail, the controls stay live, and labelling a working
controller "asleep" only invites somebody to think it is broken. The one moment
worth explaining is the wake, and the press that causes it says so.

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

## Naming

`dropstate` was `[p]retrosaves reset`, one word from `[p]retroreset` and
opposite in effect: one deleted a file and touched no running game, the other
rebooted a running game and deleted no file. Each help text carried a bolded
disclaimer about the other. Both were renamed for what they do, and all three
disclaimers retired.

`rollback` was aliased **`undo`** — the worst collision in the cog, since
somebody who liked the Undo button and typed the word got a command that threw
away several presses' worth of save, on disk. Gone, with three more for the
same reason: `export`'s `download`, `import`'s `restore`, and
`[p]retroset bios`'s `system`.

## The RetroCog → Retro rename

Red derives **both** storage locations from the cog's class name — Config keys
every setting by it, and the data folder is `<data>/cogs/<class name>/`. So
renaming the class would have orphaned every core, ROM, save and session.

The first load after the rename migrates itself: the old data folder's contents
move entry by entry (so an interrupted move finishes next time), recorded core
paths are repointed once the file is really at the other end, and the old
settings are copied **only** if the new namespace has never been written to.

A half-done move does **not** record itself as done: one entry that could not be
moved used to strand that entry permanently, and is now retried on the next
load. Anything that cannot be done is logged and skipped rather than raised.

Two things deliberately did not change, because both are baked into things that
already exist: the Config identifier integer, and the `libretro` prefix on every
button's `custom_id`, which is how Discord routes a click on a message already
posted.
