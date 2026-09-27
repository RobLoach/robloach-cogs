# Retro — design notes

Why the cog is built the way it is: the decisions, the measurements behind
them, and the attempts that were made and reversed.

This is **not** the manual. [retro/README.md](../retro/README.md) tells you how
to use the cog and is written for somebody playing or running it; everything
here is for somebody changing the code, and most of it is here because getting
it wrong once was expensive. Several sections describe behaviour that no longer
exists — that is the point of keeping them.

Measurements are on a Raspberry Pi 5 unless stated otherwise, and were taken
at **the defaults of the time** — for most of this document a one second clip
and a 160ms hold. The defaults are now 1.6s and 80ms (see
`CLIP_SECONDS` in retro/clips.py and `DEFAULT_HOLD_MS` in retro/timing.py);
the numbers below are left exactly as measured rather than rescaled, because
what they were taken to show is the comparison between the columns.

## The seam between two clips

Both halves have been got wrong, and the history is worth keeping because
every attempt was aimed at the same report.

**First, the pre-roll.** A press does not show up the instant the button goes
down — the hold was 160ms then and a game takes a moment longer than that to
react —
so on a game that sits still until it is prodded (an overworld, a menu, a text
box, which is most of what this cog is played on) the clip's opening picture
was byte-identical to the one the previous clip had left in the channel. That
was reported as "the clip seems to replay a bit from the previous clip", and
the answer was a bounded **pre-roll**: the button went down and the console
ran, *unphotographed*, until the picture stopped being the held one.

It did not fix the report, and it cost the seam to not fix it. Four
consecutive one second clips per row, at the default hold, on a Raspberry Pi
5 — emulated frames between one clip's last picture and the next clip's first
("seam"), and whether that first picture is the held one again:

| Core / ROM | Button | Seam, with the pre-roll | Again? | Seam, without | Again? |
| --- | --- | --- | --- | --- | --- |
| gambatte / µCity | ← | 1, 1, 16 | no, no, yes | **1, 1, 1** | no, no, yes |
| gambatte / Libbet | ↓ (ignored) | 16, 1, 16 | yes, no, yes | **1, 1, 1** | yes |
| fceumm / nestest (a menu) | Start | 16, 16, 16 | yes | **1, 1, 1** | yes |
| fceumm / nestest | ↓ | 2, 2, 2 | no | **1, 1, 1** | yes |
| gambatte / dmg-acid2 | A | 16, 16, 16 | yes | **1, 1, 1** | yes |
| any of the above | (no press) | 1, 1, 1 | — | **1, 1, 1** | — |

The seam was `1 + the frames the pre-roll used`, exactly, in every row. And on
the three static rows — the ones this cog is actually played on — the pre-roll
ran its whole 15 frame bound, found no change, opened the clip on the repeated
picture *anyway*, and charged 251ms of game time per press for it. So the
pre-roll is gone, and one clip resumes on the frame after the last one
everywhere.

**Second, the opening picture itself.** Removing the pre-roll left the report
standing, because a clip still opened on frame 0 of its window — the state one
emulated frame after the button went down, which is the one frame on which
nothing can have happened yet. Measured as the first emulated frame whose
picture differs at all, with the button held from frame 0:

| Core / ROM | Button | First frame that differs |
| --- | --- | --- |
| gambatte / µCity | ↓ | 1 (it animates every frame anyway) |
| fceumm / nestest | ↓ | 2 |
| snes9x / rotozoom | A | 4 |
| mgba / homebrew | A | 11 |

So on everything but an already-moving game, the opening picture was the
previous clip's closing picture over again, held for a whole 67ms.

A clip now photographs the **end** of each sampling span rather than its
start: frames 4, 8 … 60 of a one second Game Boy clip instead of 1, 5 … 57,
60. That gives the console a whole picture's worth of emulation — four frames,
67ms — to answer the button before the shutter, and it costs no game time and
skips no frame, which is exactly the difference between it and the pre-roll.
The frames before the first picture are emulated and counted in that picture's
duration. As a bonus the ragged tail is gone: a one second clip is fifteen
even pictures instead of sixteen with a 50ms and a 17ms one on the end.

Measured the same way as the table above, after one press so the screen is
where a *second* press finds it — opening pictures that are the held one
again, out of the sixteen the old cadence took and the fifteen this one does:

| Core / ROM | Button | Before | Now |
| --- | --- | --- | --- |
| fceumm / nestest | ↓ | 1 of 16 | **0 of 15** |
| gambatte / Libbet | Start | 1 of 16 | **0 of 15** |
| gambatte / µCity | ↓ | 0 of 16 | 0 of 15 |
| fceumm / nestest | Start | 16 of 16 | 15 of 15 |
| gambatte / Libbet | ↓ (ignored) | 16 of 16 | 15 of 15 |
| gambatte / dmg-acid2 | A | 16 of 16 | 15 of 15 |

**What that still trades away.** The bottom three rows are games that do not
answer the button at all, and they must stay that way: the only way to escape
a repeated opening picture there is to skip forward until the picture changes,
which on a screen that never changes is either a jump (what the pre-roll did)
or an unbounded wait (worse). So a game's own reaction latency is *shown*
rather than skipped. The encoder merges a run of identical pictures into one
stored frame and adds their durations together, so that is a held picture
rather than a stutter — and it is the truth about the game.

A core slower than one picture can still open on a repeat: mgba's eleven
frames is nearly three pictures, so a Game Boy Advance clip may hold its
opening picture once or twice before the game answers, and so can any console
on a game that takes its time about a text box.

**Those repeats are dropped from the clip, and only those.** An opening
picture that is byte-identical to the still the previous clip left sitting in
the channel is the viewer looking at the same frame twice, so it is left out
of the clip along with the time it would have been shown for. Two rules keep
that safe, and both are what a previous attempt at this got wrong:

* the clip's **final** picture is never dropped — it is the exact state the
  next clip carries on from — so a clip is never emptied;
* a clip in which **nothing** moved is left completely alone. The game really
  has not moved and the clip saying so for its whole length is the honest
  answer. Trimming that case down to the one picture the rule above obliges
  it to keep is how a 1005ms clip once played as a 17ms flash.

Nothing is skipped to achieve it: this drops *playback*, never emulation.
Every frame of the window is still run, once, in order, and the clip still
ends on the window's final frame, so the seam above is untouched.

**The frame rate is not a lever on any of this**, which is the first thing
tried. A clip photographs its window's final frame whatever the sampling
cadence is, so the still left in the channel is where the next clip resumes at
10, 15, 20 and 60 fps alike — measured against all five rows above. What the
rate *does* change is how long the opening picture waits for the console to
answer, and it changes it the wrong way: a higher rate is a sharper animation
and an earlier shutter. That is why the answer was the sampling phase and not
the frame rate.

A clip with no input in it — **Wait**, **Undo**, a boot, `[p]retroreboot` —
always resumed on the very next frame and is unchanged.

**A clip plays for as long as it emulated, minus any opening you were already
looking at.** That is the whole of the exception, and it is worth stating
precisely because the rule used to have none. The recording itself still
covers its window start to finish with nothing dropped off either end: a
picture stands for the frames ending on it, and the durations tile the window
with no hole and no overlap. The only thing that ever comes off is a run of
opening pictures identical to the still already on the message, and that time
is not lost to you — the message edit is held back until the clip on screen
has played in full, so you really did spend it looking at that very picture.

It is emphatically not "as much of the clip as had something new in it". A
clip of a game that never moves is a full-length clip of a game that never
moves.

`tests/test_emulator.py` holds all of it against real cores: the timing rule,
the tables above, the static screen that must still get a whole clip, the
opening picture that must show the game already reacting, and the adjacency of
two presses in a row (proved by rewinding and emulating the same window a
frame at a time). If a press still feels slow to land,
`[p]retroset hold` is the dial
— a shorter hold reacts sooner, at the risk of a game not noticing the press
at all below about 100ms.

## Clip size and format

**Each console is posted at a size that suits it** rather than at a flat 2x.
Discord scales an attached image down to the message column anyway, so
doubling a TV console was work thrown away: a Game Boy frame is 160×144 and
genuinely needs the double (320×288, as it always was), but a NES or SNES
frame is already 256×224 and is posted at 293×224 and 299×224. Measured on a
Raspberry Pi 5, one second of real motion, recording a whole clip end to end:

| Console | Was | Now | Time | Bytes |
| --- | --- | --- | --- | --- |
| Game Boy | 320×288, 47ms | 320×288, 47ms | — | — |
| Game Boy Advance | 480×320, 83ms | 480×320, 86ms | +3% | +19% (of 0.6 KiB) |
| NES | 585×448, 169ms | 293×224, 75ms | −55% | −7% |
| Super Nintendo | 597×448, 383ms | 299×224, 173ms | −55% | −21% |
| Genesis | 585×448, 116ms | 293×224, 65ms | −44% | −23% |
| Master System | 585×384, 61ms | 293×192, 34ms | −44% | −22% |

The picture is still scaled with nearest-neighbour and still corrected for
the console's own non-square pixels, so it is the same crisp, correctly
proportioned pixel art — there is simply less of it to encode and upload. A
console whose frame is wider than the corrected width (a Genesis in its
320-pixel mode) is doubled instead, so nothing is ever *shrunk*.

A clip in which nothing moved at all — a title screen, a menu, a game waiting
for you — is written as a single still frame of a few hundred bytes, which is
exactly as informative and much likelier at a second than it was at four.

**A session keeps no footage at all.** The clip a press records is encoded,
uploaded as the message's attachment, and dropped. There is nowhere else it
lives, which is why the message is the only place to look for it. The only
thing a session holds is its **Undo** history, which is capped in both
directions (see below).

Clips are **animated WebP**, always. Measured on a 4 second clip, WebP is
24.4 KiB against a 53.2 KiB GIF on a moving Game Boy screen and 248 KiB
against 998 KiB on the Super Nintendo — and it is pixel-exact, where GIF has
to be quantized down to 64 colours, and it stores frame durations in
milliseconds rather than centiseconds, so it can hold the 67 ms a frame of a
15 fps clip actually lasts.

## Pacing: one clip at a time

**A clip is not replaced until it has had its playing time on screen.** A clip
costs far less to make than it does to watch, and once the queue above removed
the human from between two presses that stopped being harmless. Measured on
gambatte running Libbet, one second clips, on a Raspberry Pi 5 — three presses
back to back, as a queue drains them:

| clip | produced in | plays for | was on screen for |
| --- | --- | --- | --- |
| 1 | 41 ms | 1005 ms | 68 ms |
| 2 | 67 ms | 1005 ms | 54 ms |
| 3 | 42 ms | 1005 ms | 42 ms |

Each clip was replaced after about **5%** of it had played. The seam between
two clips is exact — the section above is all about making the last picture of
one clip the state the next one starts from — but nobody was ever shown it,
because the animation never reached that picture before the next clip landed
on top of it. What that looks like from the channel's side is the picture
lurching, and it was reported as the clip "going back a bit".

So the **edit** now waits for the clip it is replacing to finish playing — all
of it, at every clip length. The same drain, unchanged in every other respect:

| | 4 clips (one press plus a full queue of three) | each clip on screen |
| --- | --- | --- |
| before | 207 ms | 42–68 ms of 1005 ms |
| after | 3.08 s | 1006–1007 ms |

**The wait used to be capped at 1.25 seconds, and that cap was itself a
stutter.** It is gone. A clip cut off part-played is not merely a shorter
pause: the next clip picks the console up one frame after the truncated one's
last *emulated* frame, not after the last frame you saw, so you were jumped
forward over footage that was emulated, encoded, uploaded and then painted
over. And because the gap grew with the clip length, it was exactly the
complaint that a long `cliplength` still stuttered:

| cliplength | plays for | old wait | you never saw |
| --- | --- | --- | --- |
| 1 s (default) | 1.005 s | 1.005 s | — |
| 1.5 s | 1.507 s | 1.25 s | 0.26 s |
| 2 s | 1.993 s | 1.25 s | 0.74 s |
| 4 s | 4.003 s | 1.25 s | **2.75 s** |
| 5 s (max) | 5.008 s | 1.25 s | 3.76 s |

What that costs is the drain, and it is said plainly rather than hidden: a
full queue is three presses behind the one running, so draining it takes
about three clips — three seconds at the default (unchanged: 1.005 was always
under the old cap), twelve at four seconds, fifteen at the five second
ceiling. That is the right way round. A clip nobody is allowed to finish
watching is emulation, encoding and upload spent on frames no human ever
sees, and both numbers in that product are already yours: `cliplength` says
how long a press is worth watching, and the queue depth says how many presses
may be lined up. Every waiting press is listed on the message while it waits,
so a channel that has queued fifteen seconds of play can see that it has.

Four things keep the waiting from costing anything it should not:

* **a press that arrives when nothing is playing is still instant.** One
  person pressing a button and watching the result — which is most play — is
  untouched: 42 ms, exactly as before. The wait is a deadline rather than a
  delay, so the time the press spent emulating, encoding and uploading counts
  against it;
* **nothing else waits.** Only the edit is held back; the emulation and the
  encoding have already happened, and the single libretro core has already
  been given back, so another channel can start a game or press a button
  during the wait. That is the rule everywhere, not just here: the one core
  is held for **emulation and nothing else**. Turning the frames into a clip
  is the most expensive step of a press and needs no core, so it happens with
  the core already handed on; so does writing the save state every third
  press, so does posting the first message of a game, and so does the edit
  that tells another channel its game went to sleep. Each of those used to be
  done while holding the core, which meant one channel's slow Discord request
  or fsync was latency charged to every other channel's presses;
* **a wait is still bounded**, at a second past the longest clip the setting
  can ask for. Nothing a session can produce comes near it — a 5 second clip
  plays for 5.008 — so it is a guard against a nonsense number rather than
  something to tune. At the 0.2 second minimum a wait is 0.2 seconds and
  pacing is effectively free;
* **nothing that takes the game away waits at all.** `[p]retrosleep`,
  `[p]retroend`, `[p]retroreboot`, **Undo**, an idle timeout, another channel
  taking the emulator and unloading the cog all cut a wait short the moment
  they start. **Undo** and `[p]retroreboot` do not pace their *own* clips
  either: those replace a picture of something that has just stopped having
  happened, and holding the correction back to finish showing it would be
  showing you the very thing you asked to take away.

It is not a setting. The number that matters — how long a clip plays for — is
already `[p]retroset cliplength`, and the bound exists to stop a nonsense
value from holding an edit for an hour rather than to be tuned alongside it.

**And none of the previous clip is shown in the new one.** Pacing gets the
clip on screen watched in full; it does not by itself stop the next clip from
*opening* on the very same picture, because the shutter falls a quarter of a
second into the window and some games are slower than that to react — mgba
takes eleven frames to draw a button press. Any opening picture that is
byte-identical to the still already on the message is therefore left out of
the clip, with its duration, so a clip always opens on something you have not
just been looking at. The final picture is never dropped and a clip in which
nothing moved is left whole; see "A clip plays for as long as it emulated,
minus any opening you were already looking at" above. It costs nothing — the
comparison is sixteen bytes per picture, made on the thread that was about to
spend tens of milliseconds encoding the clip anyway, and no extra emulation
at all.

## The press queue

**A press that lands while somebody else's is being emulated is queued, not
dropped.** A press holds the session for about a second, and a click that
arrived during that second used to be acknowledged to Discord and then
silently forgotten. In a channel with two or three people playing, *most*
clicks land in that second — so the controller felt intermittently dead: press
a direction, nothing happens, press it again.

Four rules, and each of them is there for a reason:

* **at most five presses wait.** Each one is a whole clip of latency, and a
  queued press is emulated against a game state its author has not seen yet.
  Five waiting plus the one running is about ten seconds at the 1.6 second
  default — the last person to click waits that long to see what their press
  did. It was three, on the reasoning that four seconds was the limit of
  "I pressed that"; five is a deliberate trade for the thing people actually
  do with a d-pad, which is tap it several times in a row. A depth that
  cannot hold a whole run refuses the tail of it. Note that it multiplies
  with the clip length, since every edit waits out the clip it replaces: a
  full drain is about five clips, so five seconds at the default and
  twenty-five at the five second ceiling;
* **first come, first served, and one person may hold every slot.** There
  used to be a one-waiting-press-per-person rule, on the theory that it made
  a group take turns. What it actually did was break the commonest way one
  person plays: a direction is rarely pressed once, so walking four tiles is
  four clicks in a row — and the second, third and fourth were all refused
  because the first was still waiting. The controller went back to feeling
  dead for exactly the person using it most. The depth cap above is what
  bounds the queue now, and it bounds it the same way whoever is clicking:
  five waiting is five waiting, and somebody who fills all five only ever
  costs themselves the wait for their own presses to play. **A press with no
  room left is quietly dropped**: the click is acknowledged so Discord never
  shows "interaction failed", and nothing else is said. The queue is already
  on the message for anybody who looks, and the moment somebody is clicking
  fast enough to fill it is the worst possible moment to answer every click
  with a message of its own;
* **every waiting press is visible.** An input nobody can see is an input that
  feels lost, which is the whole complaint. The queue is listed as a suffix on
  the very line the running press is already rewriting, so it costs **no extra
  edit** — an ephemeral "your press is queued" would be a second message per
  click (removed once already for being spam) and any further edit of this
  message would re-render the attachment and visibly rewind the clip. The
  listing names a person and then their buttons in order, closed up, so that a
  run reads as one intent rather than as several unrelated things:

  > **Pokemon** · Rob pressed ⬆️. *Queued: Rob ⬆️⬇️⬇️*

  Consecutive presses by one person are one run; somebody else starts a new
  one (`*Queued: Rob ⬆️⬆️, Ada ⬅️*`), and the order is never rearranged,
  because the order is what the queue is;
* **the queue is intent, never work.** A waiting entry is a button name and a
  deferred interaction. Nothing touches the emulator until its turn comes and
  the session's lock is taken again for it, so the one-core-at-a-time rule is
  untouched.

When its turn comes, a queued press makes exactly the same **single edit** an
immediate one does, through its own deferred interaction — a component defer
leaves `edit_original_response` available for the next fifteen minutes. So
"one press, one visible change" holds for every press rather than for
whichever one won the race.

**The queue is thrown away** whenever the game stops being the thing those
presses were aimed at: `[p]retrosleep`, `[p]retroreboot`, `[p]retroend`,
**Undo**, an idle timeout, another channel taking the emulator, or the channel
switching games. Replaying a queued direction into a different game state is
worse than dropping it, and Undo is the sharpest case — those presses were
queued against the state the undo has just put *back*, so running them would
undo the undo one button at a time. Because a press that vanishes silently is
the bug this whole mechanism fixes, the one case where dropping is right says
so out loud, once, on the next line the session writes:

> **µCity** · Rob undid the last press. *2 queued presses dropped*

**Undo is deliberately not queueable.** A click that arrives while a press is
being emulated is dropped rather than taken down: "one press back" queued
several presses deep means undoing a press its author never saw. It is
acknowledged and nothing more — the press already running posts its clip a
moment later, which answers the question by itself, so there is nothing worth
whispering about.

**A press that lands while the game is being undone, rebooted or put to sleep
is dealt with rather than left in the queue.** Those three take the session's
lock without going through the press path, and a click landing while they hold
it is taken down like any other. An undo runs them afterwards (they are
ordinary next presses); a reboot and a sleep drop them, which is what both of
those commands already promise, and say how many went. Left alone, one such
entry sat in the queue with nothing coming to run it, which made every later
click queue behind it for ever — a controller that answered nothing at all
until the game hibernated. A press that finds a queue with no runner now
starts one itself, so the state cannot persist even if some future path
forgets.

## Undo

This is cheap enough to be free. A save state takes well under a millisecond
to make, and a state is almost all zeroes, so it compresses enormously —
measured on the real cores on a Raspberry Pi 5:

| Console | Save state | Compressed | Ratio |
| --- | --- | --- | --- |
| Game Boy | 182,530 | 15,780 | 8.6% |
| NES | 13,758 | 870 | 6.3% |
| Game Boy Advance | 528,448 | 6,298 | 1.2% |
| Super Nintendo | 823,407 | 12,606 | 1.5% |
| Genesis | 1,036,288 | 19,524 | 1.9% |

A full eight-deep history of real play is **124 KiB** on the Game Boy, 99 KiB
on the SNES and 152 KiB on the Genesis, and compressing one costs about a
millisecond inside a press that already spends tens of them recording a clip.
The history is capped by bytes as well as by count (2 MiB), because the sizes
above are what *today's* cores cost and a
count alone bounds nothing.

It is **read lazily** — when a game is woken, or when somebody actually clicks
Undo, never at load. Reading every stored channel's history when the cog loads
would hold up to 2 MiB *per channel* for as long as the cog is loaded, for
games nobody may ever touch again; this way the memory is proportional to what
is being played rather than to what has ever been played. It is only ever read
into an *empty* history, which is what makes it safe to do from both of those
places at once: a session that already has undo points in memory has newer ones
than the file.

The file is written next to the save state and only once that state has landed,
so the two always describe the same game — a history saved beside a state that
failed to write would step back into a past the next boot never reaches. An
emptied history *removes* the file rather than writing an empty one, so "no
file" and "nothing to undo" stay the same thing on disk and a history that has
just been cleared cannot come back on the next restart. Nothing here can cost
anybody their game: a history that will not write, or will not parse, is worth
exactly one empty Undo button.

**Wiping progress wipes the history with it.** Every entry is a save state from
*after* the moment being discarded, so a history left behind would be an Undo
button that walks straight back into what was just thrown away — and on
`[p]retrosaves import`, one that silently undid the import. There are two
defences: a `[p]retrosaves` command that pauses a live game clears the history
as it pauses (and the save that follows removes the file), and a game with no
live session has its file removed by whatever deletes the rest of its saves.
`SavePaths` in `retro/storage.py` lists every file a game's progress lives in
and `[p]retrosaves delete` walks it, so the undo history cannot be the one
thing left behind pointing at saves that are gone.

Two deliberate details:

* **An undo costs one clip's worth of emulated time**, exactly as pressing
  Wait does, because it records forwards from the restored state rather than
  freezing at it. Restoring and then rewinding again would leave the clip on
  the message a second *ahead* of the game, and the next press would play
  that second again — which is precisely the "the clip jumps backwards when I
  press a button" problem described below. A game that is frozen between
  presses can afford the second.
* **An undo writes the save state to disk immediately** instead of waiting
  for the next automatic save. The state on disk is easily *newer* than the
  one Undo just restored, so without that a restart or a sleep straight
  afterwards would quietly put the undone press back.

There is no `[p]retro` command for it, deliberately: the button is where the
misclick happened, the history it pops only exists in that session's memory,
and `[p]retrosaves rollback` is already the durable, on-disk version of the
same idea — it goes back to the previous *save state generation* for a game,
which is the answer when the in-memory history is gone.

`rollback` used to be aliased **`undo`**, and that alias is gone. It was the
worst collision in the cog: somebody who liked the Undo button and typed the
word got a command that threw away *several* presses' worth of save instead of
one, on disk. Three more aliases went with it for the same reason — `export`'s
`download` (because `[p]retroset download` installs emulators), `import`'s
`restore` (because "restore" is what the whole cog calls putting a game back
on boot) and `[p]retroset bios`'s `system` (because "system" is the directory
those files go in, and also what a player calls a console).

## The ×3 button

**It is hidden, not greyed out, when only one tap fits.** One tap is not a
repeat at all — it is exactly what the confirm button one row over already
does — so rather than drawing a dead button, the row is drawn without it. A
present, dead, unexplained control reads as broken: the greyed-out version of
this button was reported as the feature having been *removed from the cog*.
It comes back by itself on the next press as soon as the clip is long enough,
because the controls are redrawn on every press, so `[p]retroset cliplength`
corrects it either way without restarting anything.

**And it says so when it goes.** A control that silently disappears reads as
removed just as surely as a greyed-out one does, so the press on which it
vanishes carries one line — on an edit that was happening anyway:

> The **A x3** button is hidden while clips are this short — only one tap
> fits. A longer `cliplength` brings it back.

Coming back says nothing: the button is right there saying what it does.

| Clip length | Taps | The button |
| --- | --- | --- |
| 0.2s (the floor) | 1 | not shown |
| 0.4s | 1 | not shown |
| 0.45s | 1 | not shown |
| 0.46s | 2 | `A ×2` |
| 0.5s | 2 | `A ×2` |
| 0.72s | 2 | `A ×2` |
| 0.73s | 3 | `A ×3` |
| 1s (the default) | 3 | `A ×3` |
| 5s (the ceiling) | 3 | `A ×3` |

**Wait and Undo never move.** The controls row reserves space for all three of
them whether or not the third is drawn, so on every one of the nine consoles
the row is the same shape at every clip length and simply has one fewer button
in it — a Game Boy is `Start Select Wait A ×3 Undo` at a second and
`Start Select Wait Undo` at a fifth of one. `[p]retroset cliplength` and
`[p]retroset settings` both say so when the length you have chosen is short
enough to hide it.

`[p]retroset hold` changes the numbers above, because the taps have to fit
inside the clip alongside the hold; whatever you set, the label and the line
on the message count the same real taps.

## The controller's shape

Discord allows five rows of five components, and this is what each console
uses once the controls are added:

| Console | Components | Rows |
| --- | --- | --- |
| Game Boy / Color, NES | 12 | 3 |
| Game Boy Advance | 14 | 3 |
| Super Nintendo | 19 | 4 |
| Sega Genesis | 18 | 4 |
| PC Engine | 18 | 4 |
| Master System, Game Gear | 11 | 3 |
| Neo Geo Pocket | 11 | 3 |

Three controls fit beside every console's bottom row, so none of them needs a
row of its own. The worst case is the Super Nintendo at 19 components over
four rows, leaving six components and a spare row in hand.

There is no **Stop** and no **Reset** button. Putting a game to sleep
(`[p]retrosleep`), rebooting it (`[p]retroreboot`) and finishing with it
(`[p]retroend`) are all commands, because each of them is destructive to a
game everybody in the channel is playing and none of them should be one
mis-tap away.

**⏳ Wait** used to be drawn with ⏩, the fast-forward symbol, which was
simply a lie: nothing is sped up and nothing is skipped — the button lets one
clip's worth of time pass with no input at all. An hourglass says that. (Every
emoji the cog can put on a button is listed and checked at import time, because
a character that is not a real Unicode emoji is a `400 Invalid Form Body` on
the send and takes out the whole command; that has happened in production.)

## Rebooting, and one edit per press

**It is a command and not a button, deliberately.** A reboot throws away the
progress-in-flight of everybody in the channel, which is exactly the reason
`[p]retrosleep` is not a button either, and it has the same permission check:
only the person who **started** the game, anybody with **Manage Messages**,
and the bot owner can run it. Everyone else is told so and nothing happens.

**It was called `[p]retroreset`** (which still works). The pair
`[p]retroreset` / `[p]retrosaves reset` were one word apart and opposite in
effect, so each help text needed a bolded disclaimer about the other and a
reply carried a third. Renaming both to what they do — reboot the console
now, versus delete a save file — retired all three disclaimers:

| | what it does |
| --- | --- |
| `[p]retroreboot` | reboots the game **now**. Nothing on disk is deleted. |
| `[p]retrosaves dropstate <game>` | deletes a save **state file**, so the game next starts from its last in-game save. Touches no running game. |

Three decisions inside it, and the reasons for them:

* **Nothing on disk is written.** A reset does not save, so the `.state` file
  still holds the moment *before* the reset — a reset is not allowed to
  quietly overwrite a good save state with a title screen. It becomes the
  saved game only when the rebooted game saves of its own accord: the next
  automatic save (within three presses) or the next time it sleeps, both of
  which also rotate the pre-reset state into the previous generation that
  `[p]retrosaves rollback` can bring back.
* **The in-game save is not touched at all.** That is the save the
  player made from inside the game, and resetting a real console never wiped
  one — `retro_reset` is the reset line, not a new cartridge. Use
  `[p]retrosaves delete` if that is really what you want.
* **A reboot is an undo point.** The state it is about to throw away is pushed
  onto the session's history first, so one click of **↩️ Undo** puts the
  player back where they were. Nothing else in this cog can lose as much in
  one command, and the undo machinery exists for exactly that class of
  mistake; it costs a sub-millisecond save state and some tens of kilobytes.
  The older undo points are left alone — they came from the same core and the
  same ROM, so a second click still means "one press further back".

**A press changes the message exactly once.** The controls used to grey
themselves out the instant you clicked and come back with the new clip, which
was two edits of one message — and a Discord client re-renders a message from
scratch on *any* edit to it. Re-rendering restarts whatever animation is
already attached, so that first edit played the **previous** clip again from
its first frame, and a moment later the new clip replaced it. What that looked
like from the outside was the game jumping backwards a few frames every time
somebody pressed a button.

So a press is now acknowledged silently and makes a single edit, which swaps
the clip in and redraws the buttons together. The cost is real and there is no
way round it: the instant "your click landed" feedback is gone, because
anything that shows you something either edits this message (and rewinds the
clip) or posts a second one (an ephemeral "still emulating" notice, which was
tried and removed for being spam). Clicks that arrive while a press is being
emulated are **queued** rather than dropped, and nobody ever sees *This
interaction failed*; see [The press queue](#the-press-queue). A click that lands
while the game is being rebooted by `[p]retroreboot` is dropped, because the
queue goes with the reboot.

The clip and **one line** of text are all that is posted: no status card, no
caption. The line always names the game, then says who pressed which button
unless there is something more important to say — the game having
gone to sleep and come back, or a save state that could not be restored — and
the next press rewrites it either way. See
[What a press does](../retro/README.md#what-a-press-does) in the manual.

**A sleeping session does not advertise itself.** The header used to gain a
`· asleep` mark for as long as there was no core loaded. It was accurate and
unhelpful: sleeping is an implementation detail of fitting one emulator
across every channel, the controls stay live throughout, and the next press
wakes the game with no more ceremony than any other press — so labelling a
working controller "asleep" only invites somebody to think it is broken, or
that they have to do something to it first. The one moment worth explaining
is the wake itself, which is the longest wait in the cog (a core to load, a
save state to restore), and the press that causes it says *Woke up where you
left off.* on the edit it was making anyway.

## Starting a game from the dropdown

Picking from it **re-runs the ordinary command**. The dropdown copies the message
it is attached to, writes `[p]retro <name>` into it as the person who clicked,
and asks Red to invoke that. So there is exactly one way a game is ever started
by name, with the same checks, the same two cooldowns and the same
`max_concurrency` — and all of them charged to **whoever clicked**, which matters
because that is very often not whoever ran the command. A component click goes
through none of a command's gates on its own (the same problem the **Resume**
button has, and it solves it the same way), and re-invoking is what gets every
one of them back without a second copy of the start path existing to drift.

It is deliberately **not** a persistent view: it stops working after three
minutes and greys itself out. A picker that survived a restart would be a
dropdown, a week later, in a channel that has played three other things since —
and clicking it would replace whatever is playing now. A bot with no saved games
gets no dropdown at all, because Discord refuses a select with no options and
would take the whole reply down with it.

## Slash commands and autocomplete

It is `/retro play` rather than `/retro` because **Discord does not let a group
be invoked, only its subcommands**. `[p]retro` is a group (it has `[p]retro
list`), so without a fallback subcommand there would be no slash way to start a
game at all — `/retro` would offer nothing but `/retro list`. The prefix form is
untouched: `[p]retro ucity` and `[p]retro <url>` still work exactly as they did.

**`/retroset coreoptions`** autocompletes all three of its arguments: the
installed cores, that core's option keys, and the values the option will accept
(with the core's own default marked, and `reset`). libretro options are very
nearly all enumerations and the core declares them, so this is the one place a
completion can offer the whole correct set rather than a guess at it.

Every one of these answers from what the cog already knows — the installed
cores, and the option definitions cached in Config. Never from a probe: a probe
dlopens a core on the one emulator thread whatever is being played is using, and
Discord allows an autocomplete about three seconds. So a core whose options have
never been read offers nothing, and the command's own reply is what explains
that. None of them can fail loudly either: an autocomplete that raises shows an
empty box with no clue why, so a Config that will not answer degrades to no
suggestions and everything can still be typed by hand.

The rest of `[p]retroset` is deliberately **prefix-only**
(`with_app_command=False`). The whole group is owner-only, and publishing a
dozen slash commands that only one person in the server can run would put them
in everybody's slash menu for nothing.

## Reporting which build is loaded

Three separate facts, because only together are they honest:

* **the version** is what `info.json` declares. That file is the *single
  source of truth* — `retro/version.py` reads it, and there is no version
  literal in any `.py` for it to drift from (a test in
  `tests/test_packaging.py` checks both halves of that). It is still a number
  somebody has to remember to bump, which is why it is not the only thing
  shown. The scheme is `MAJOR.MINOR.PATCH`: a feature bumps the minor, a fix
  the patch. An install from before this existed reports `0.0.0+unknown`.
* **the commit**, when the cog is installed from a git checkout — which is
  the normal case for Red's Downloader, since it clones the repo. It is read
  straight out of `.git` (HEAD, then a loose ref or `packed-refs`): no `git`
  binary is needed and no subprocess is started, so there is nothing to hang
  on. The search goes up exactly one directory from the package, so a bot
  whose data folder happens to live inside some *other* repository is never
  told a commit that has nothing to do with this cog. No `.git`, an
  unreadable one, or a ref that resolves to nothing: the line is simply not
  printed.
* **the fingerprint of the loaded code**, which is the part that cannot go
  stale. It is a hash of every `retro/*.py` *as they were when the cog was
  loaded*, so two bots showing the same fingerprint really are running the
  same code — and `git pull` without `[p]reload retro` deliberately does
  **not** change it, because that is exactly the situation worth being able
  to prove.

`[p]retroset settings` leads with a one-line version of the same thing.
Nothing here can fail a command: every piece of it degrades to "not shown"
rather than raising, and `retro/version.py` imports nothing but the standard
library so it works on the most broken install there is.

## What the cog forgets, and what it never does

Two kinds of thing are stored per channel, and the difference between them is
the whole of this section:

| | what it is | when it goes |
| --- | --- | --- |
| the **session** and **Resume** records | a pointer: *this message, in this channel, was playing this game* | by itself, as soon as it cannot resume anything |
| the **save state** and **in-game save** | the player's progress, keyed by channel **and game** | only when somebody asks (`[p]retrosaves delete`) |

Because progress is keyed by channel and game rather than by message,
**dropping a record loses the button and nothing else**. If the channel's game
was still awake it is saved and its emulator freed first, exactly as going to
sleep would — a forgotten channel must not leave a core running, since only one
may be loaded at a time. Starting the game again by name re-downloads the ROM
and restores from the save exactly as it always did, so the cog does it
automatically in four cases:

* **the cached ROM it named was pruned**, by the disk budget or by the
  per-channel cap of five games. A Resume button without its ROM can only
  apologise. **The save it points at is kept either way**: the cap drops the
  cached ROM and nothing else, so a sixth game does not cost the oldest one
  its progress, and `[p]retro <name>` re-downloads the ROM and picks the save
  straight back up. It used to take all four save files with it, which was
  the one place the promise below was not kept;
* **the channel or thread was deleted**;
* **the bot left the server** (or was thrown out of it);
* **the bot can no longer see the channel at all**, which is the same thing
  noticed a restart later. Nothing is judged until the bot is actually
  connected, because Red loads its cogs before logging in and at that moment
  every channel that exists looks deleted.

Before this, nothing was ever deleted: a bot rebuilt a session for every
channel that had *ever* played, on every load, for the life of the install.

**The saves for a deleted channel are deliberately kept.** They are small
next to a cached ROM, the disk budget already prunes ROMs (and never a save),
and from inside the bot an archived thread, a channel it has briefly lost
sight of and a channel that was really deleted look identical — deleting
somebody's progress on that evidence is not a trade worth making. The
opposite mistake costs a few hundred kilobytes, which `[p]retroset diskbudget`
counts and reports under *saves*.

**One restore chain, two doors.** Starting a game and waking a sleeping one
run the same function — save state, then in-game save, then the beginning —
and the same code decides what to say afterwards and throws away a state the
core would not take. They used to be two copies kept in step by the tests; now
they cannot drift apart, and the suite holds the two against each other for
every outcome.

## Naming: dropstate, reboot, and the aliases that went

`dropstate` used to be called `[p]retrosaves reset`, one word away from
`[p]retroreset` and opposite in effect — one deleted a file and touched no
running game, the other rebooted a running game and deleted no file. Each help
text carried a bolded disclaimer about the other, and there was a third in a
reply, which is what wrong names look like. Both were renamed for what they
do; both old names still work.

## The RetroCog → Retro rename

The cog's Python class used to be called `RetroCog` and is now simply `Retro`,
which is the name that appears in `[p]help` and `[p]cog list`. Red derives
**both** of a cog's storage locations from that class name — `Config` keys
every setting and session by it, and the data folder is `<data>/cogs/<class
name>/ ` — so the rename would otherwise have orphaned every downloaded core,
cached ROM, save state, in-game save, BIOS file, saved game and live session.

It does not. The first time the renamed cog loads it migrates itself:

- the old `RetroCog` data folder's contents are moved into the new `Retro`
  one, entry by entry, so an interrupted move simply finishes next time;
- any recorded core path that pointed into the old folder is repointed, once
  the file is really at the other end of it;
- the old settings and every channel's session record are copied across, but
  **only** if the new namespace has never been written to, so a copy can never
  overwrite something newer;
- a marker is stored so none of it ever runs twice.

Anything that cannot be done is logged and skipped rather than raised: a
read-only disk or an unavailable Config costs the migration, never the cog
load, and the old data is left exactly where it is so it can be moved by hand.
If both folders already have the same thing in them, the new one wins and the
old copy is left alone with a line in the log saying so.

Two things deliberately did **not** change, because both are baked into things
that already exist: the `Config` identifier integer (which keys all the stored
data) and the `libretro` prefix on every button's `custom_id` (which is how
Discord routes a click on a message this cog has already posted).
