# Retro — what was tried and reversed

Things that are **no longer true**, kept so nobody re-attempts them. Each was
built, measured, and taken out again.

How it works now is [DESIGN.md](DESIGN.md); how to use it is
[retro/README.md](../retro/README.md).

Measurements are on a Raspberry Pi 5, at the defaults of the time — mostly a
one second clip and a 160ms hold. They are left as measured.

## The pre-roll

One report — *"the clip seems to replay a bit from the previous clip"* — got
two wrong answers before the right one.

A game takes a moment to react to a press, so on a game that sits still until
prodded, a clip's opening picture was identical to the one the previous clip
left on screen. The fix tried was a bounded **pre-roll**: run the console
*unphotographed* until the picture changed.

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
change, opened on the repeated picture *anyway*, and charged 251ms of game time
per press for it.

**Why it cannot come back:** on a screen that never changes, the only way to
escape a repeated opening picture is to skip forward until the picture changes,
which is either a jump (this) or an unbounded wait (worse). A game's reaction
latency is shown rather than skipped. The answer was the sampling *phase*
instead — see [the seam](DESIGN.md#the-seam-between-two-clips).

## The frame rate as a lever

The first thing tried on the same report, and it does nothing. A clip
photographs its window's final frame at any cadence, so the seam is identical
at 10, 15, 20 and 60 fps. What the rate *does* change is how long the opening
picture waits for the console to answer — and it changes it the wrong way: a
higher rate means a sharper animation and an **earlier** shutter.

## The 1.25 second pacing cap

When the edit started waiting for the clip it replaces to finish playing, that
wait was capped at 1.25s. The cap was itself a stutter: a clip cut off
part-played is not merely a shorter pause, because the next clip picks up one
frame after the truncated one's last *emulated* frame. You were jumped forward
over footage that was emulated, encoded, uploaded and then painted over.

| cliplength | plays for | old wait | you never saw |
| --- | --- | --- | --- |
| 1 s | 1.005 s | 1.005 s | — |
| 1.5 s | 1.507 s | 1.25 s | 0.26 s |
| 2 s | 1.993 s | 1.25 s | 0.74 s |
| 4 s | 4.003 s | 1.25 s | **2.75 s** |
| 5 s (max) | 5.008 s | 1.25 s | 3.76 s |

Because the gap grew with the clip length, this was exactly the complaint that
a long `cliplength` *still* stuttered. The bound is now a second past the
longest clip the setting allows — a guard against a nonsense value rather than
a policy.

## One waiting press per person

The queue had a per-person limit, on the theory that it made a roomful of
people take turns without a scheduler.

What it actually did was break the commonest way one person plays: a direction
is rarely pressed once, so walking four tiles is four clicks in a row — and the
second, third and fourth were all refused because the first was still waiting.
The controller went back to feeling dead for exactly the person using it most.

The depth cap does the same job without that: five waiting is five waiting
whoever queued them, and somebody who fills all five only costs themselves the
wait for their own presses.

## Two edits per press

The controls used to grey themselves out the instant you clicked, and come back
with the new clip. Two edits of one message.

**A Discord client re-renders a message on any edit**, and re-rendering
restarts whatever animation is attached. So the first edit replayed the
*previous* clip from frame zero, and a moment later the new clip replaced it.
From the outside, the game jumped backwards a few frames on every press — which
is exactly how it was reported.

The instant "your click landed" feedback is gone and cannot come back: anything
that shows something either edits this message (rewinding the clip) or posts a
second one. An ephemeral "still emulating" notice was tried for that and
removed for being spam — a message per click, at the moment people click most.

## The `· asleep` marker

The message header gained a `· asleep` mark for as long as a session had no
core loaded. It was accurate and unhelpful: sleeping is an implementation
detail of fitting one emulator across every channel, the controls stay live
throughout, and the next press wakes the game with no ceremony. Labelling a
working controller "asleep" only invites somebody to think it is broken.

The one moment worth explaining is the wake itself — the longest wait in the
cog — and the press that causes it says so on the edit it was making anyway.

## The greyed-out ×3 button

The repeat button used to grey itself out when only one tap fitted in the clip.
It was **reported as the feature having been removed from the cog** — a
present, dead, unexplained control reads as broken.

It is hidden instead, and says one line on the press that takes it away. The
**Undo** button went the other way for the same reason: it used to grey out
when there was nothing to undo, which meant the person looking at the dead
control could never click it to find out why. It stays clickable and answers
privately.

## ⏩ for Wait

The Wait button was drawn with the fast-forward symbol, which was simply a lie:
nothing is sped up and nothing is skipped — it lets one clip's worth of time
pass with no input. An hourglass says that.

(Every emoji the cog can put on a button is checked at import time, because a
character that is not a real Unicode emoji is a `400 Invalid Form Body` on the
send and takes out the whole command. That has happened in production.)

## Names that collided

`[p]retrosaves dropstate` was `[p]retrosaves reset` — one word from
`[p]retroreset` and opposite in effect: one deleted a file and touched no
running game, the other rebooted a running game and deleted no file. Each help
text carried a bolded disclaimer about the other, and a reply carried a third.
Both were renamed for what they do, and all three disclaimers retired.

`[p]retrosaves rollback` was aliased **`undo`** — the worst collision in the
cog, since somebody who liked the Undo button and typed the word got a command
that threw away several presses' worth of save, on disk. Three more went for
the same reason: `export`'s `download` (because `[p]retroset download` installs
emulators), `import`'s `restore` (because that is what the cog calls putting a
game back on boot), and `[p]retroset bios`'s `system` (because that is the
directory, and also what a player calls a console).

`[p]retroset core <path>` is gone entirely — cores are *detected* now, by
scanning the folder, rather than registered by hand.

## The RetroCog → Retro rename

A one-off migration that has already run on every install that needed it.

Red derives **both** storage locations from the cog's class name — Config keys
every setting by it, and the data folder is `<data>/cogs/<class name>/`. So
renaming the class would have orphaned every core, ROM, save and session.

The first load after the rename migrates itself: the old data folder's contents
move entry by entry (so an interrupted move finishes next time), recorded core
paths are repointed once the file is really at the other end, and the old
settings are copied **only** if the new namespace has never been written to.

A half-done move does **not** record itself as done: one entry that could not be
moved used to strand that entry permanently, and is retried on the next load
instead. Anything that cannot be done is logged and skipped rather than raised.

Two things deliberately did not change, because both are baked into things that
already exist: the Config identifier integer, and the `libretro` prefix on every
button's `custom_id`, which is how Discord routes a click on a message already
posted.

## Defaults that moved

| | Was | Now | Why |
| --- | --- | --- | --- |
| clip length | 4s → 1s | **1.6s** | Four seconds was mostly the game sitting still after the press had played out, at three times the cost to record. One second was a turn's round trip; 1.6 gives a move room to land. |
| button hold | 400ms → 160ms | **80ms** | 400ms outlasted a Game Boy walk cycle (16 frames), so one press walked two tiles. 80ms reacts sooner — but nothing is measured below 100ms, so a game that polls rarely may miss it. |
| clip length ceiling | 15s | **5s** | A 15 second clip is one nobody sits through, and it cost memory and encode time on every press. |
| queue depth | 3 → **5** | | Three assumed four seconds was the limit of "I pressed that". Five holds a whole run of d-pad taps, which is what people actually do. |
