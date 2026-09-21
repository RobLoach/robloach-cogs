# Discord Red: Retro Cog

Play retro console games together in Discord. Attach a ROM and control the game
with a controller built out of Discord buttons. Every press posts a short
animated clip of the next second of gameplay, so you see the game react
instead of a still frame. Anyone in the channel can play, making it a fun
social feature.

Eight consoles are supported out of the box, from the Nintendo Entertainment
System to the Game Boy Advance. Emulation is provided by
[libretro.py](https://github.com/JesseTG/libretro.py) running cores from the
[libretro buildbot](https://buildbot.libretro.com).

Games remember where you were. A channel's game keeps playing across days,
cog reloads, and bot restarts — nothing is ever "timed out and lost".

Only use ROMs you have the rights to. There are hundreds of free, legal
homebrew games for these consoles at **<https://retrobrews.github.io/>** — no
copyrighted ROMs.

## Getting started

Install and load the cog:

```
[p]repo add robloach-cogs https://github.com/robloach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

The seven emulator cores that cover all eight consoles — about 4.5 MiB to
download, 31 MiB once unpacked, none of them needing a BIOS — start
downloading in the background as soon as the cog loads. To do it now, or to
check on it:

```
[p]retroset download
[p]retroset settings
```

**There is nothing to configure.** Cores are *detected*, not registered: the
cog scans its own `cores` folder every time it needs one, so a core that is
simply there — downloaded by `[p]retroset download`, fetched automatically on
load, or dropped in by hand while the bot was off — is playable immediately, as
long as it keeps its buildbot filename (`snes9x_libretro.so`,
`gambatte_libretro.dll`). `[p]retroset settings` prints the folder.

There used to be a `[p]retroset core <path>` command for pointing the cog at a
core by hand, and it is gone. A path recorded by it (or by hand, for a core
that lives somewhere else entirely, such as a RetroArch install) is still
honoured, and `[p]retroset settings` marks such a core as coming from
elsewhere.

Then play. Any of these works:

```
[p]retro https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc
[p]retro                      (with a ROM, or a .zip, attached to the message)
[p]retro ucity                (once it has been saved by name, below)
```

[µCity](https://github.com/AntonioND/ucity) is a complete open source city
builder for the Game Boy Color, and makes a good first game. Save it so anyone
can start it by name:

```
[p]retroset game add ucity https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc
[p]retro ucity
```

## Commands

- `[p]retro [name|url]` starts a game from a saved name, a URL, or a ROM attached to the message. `.zip` files are unpacked for you. With no arguments it brings back the game already going in the channel.
- `[p]retrostop` saves the game and puts it to sleep. The controls keep working — pressing any button wakes it up again. There is no Stop button under the screen; this command is how a game is stopped.
- `[p]retroreset` reboots the game that is running, as if you had flipped its power switch. There is no Reset button under the screen either, deliberately — see [Resetting a game](#resetting-a-game). Not to be confused with `[p]retrosaves reset`, which deletes a save state *file*.
- `[p]retrosaves [game]` (aliased `saves`) lists what this channel has saved, or shows one game in detail. See [Managing saves](#managing-saves).
- `[p]retrosaves export <game>` posts a game's battery save as a file to keep. `export state <game>` or `export both <game>` sends the save state too.
- `[p]retrosaves import <game>` installs an attached `.srm`/`.sav` (and optionally a `.state`), checked against the real core first.
- `[p]retrosaves reset <game>` drops the save state, so the game restarts from the last in-game save.
- `[p]retrosaves rollback <game>` (aliased `undo`) goes back to the previous save-state generation — the durable, on-disk version of the **Undo** button.
- `[p]retrosaves delete <game>` wipes both halves of a game's save, after asking.
- `[p]retroset download` (owner) downloads every supported core for your platform from the [libretro buildbot](https://buildbot.libretro.com). `[p]retroset download <core>` fetches or refreshes just one.
- `[p]retroset autodownload [true|false]` (owner) controls whether missing cores are fetched automatically when the cog loads. On by default.
- `[p]retroset game add|remove|list` (owner) manages the games anyone can start by name.
- `[p]retroset coreoptions [core] [key] [value]` (owner, aliased `coreopts`) reads and changes a core's own settings. See below.
- `[p]retroset bios add|list|remove` (owner) manages BIOS files for cores that need one. See below.
- `[p]retroset diskbudget [megabytes]` (owner, aliased `disk`/`budget`) caps what the whole cog may use on disk, and with no argument reports what is using it. The default is 1024 MiB; `0` is no limit. **No save is ever deleted to make room** — cached ROMs are, oldest first.
- `[p]retroset allowprivateurls [true|false]` (owner) lets ROM URLs point inside your own network. Off, and best left off: see [ROM URLs](#rom-urls).
- `[p]retroset timeout <minutes>` (owner) sets how long a game idles before it sleeps.
- `[p]retroset cliplength <seconds>` (owner) sets how much play each clip shows. The default is 1 second; anything from 0.2 to 15 works, fractions included (`0.8` is a real answer).
- `[p]retroset hold <milliseconds>` (owner) sets how long a button is held when someone presses it. The default is 160. It is a ceiling: a clip too short to show the button coming back up holds it for less.
- `[p]retroset settings` (owner) shows the current configuration, including the build that is loaded, the system directory and any BIOS files in it.
- `[p]retroset version` (owner) answers **“am I running the new code?”** — the declared version, the commit it was installed from, and a fingerprint of the source that was actually loaded. See [Which build is this?](#which-build-is-this).

## Attaching a ROM

Attach the file to the message you run `[p]retro` with and leave the arguments
off. Both a raw ROM and a `.zip` work, up to 32 MiB.

For a `.zip`, the cog looks inside and takes the first file whose extension is
one of the consoles below, in alphabetical order, and says which one it picked
if there is more than one. Folders inside the archive are fine. Nothing is ever
unpacked using the paths stored in the archive, the uncompressed size is checked
against the 32 MiB limit before anything is read, and a corrupt or
password-protected archive gets a plain explanation rather than a stack trace.

## Consoles

The console is chosen from the ROM's file extension. Every core is BIOS-free —
nothing but the ROM is needed. Seven cores cover the eight consoles
(`genesis_plus_gx` runs two of them): about 4.5 MiB of zips from the
buildbot, 31 MiB unpacked, which is the figure that counts against
`[p]retroset diskbudget`.

| Console | Core | File extensions |
| --- | --- | --- |
| Game Boy / Color | `gambatte` | `.gb` `.gbc` `.dmg` |
| Game Boy Advance | `mgba` | `.gba` |
| Nintendo Entertainment System | `fceumm` | `.nes` `.unf` `.unif` |
| Super Nintendo | `snes9x` | `.smc` `.sfc` `.swc` `.fig` `.bs` `.st` |
| Sega Genesis / Mega Drive | `genesis_plus_gx` | `.md` `.mdx` `.smd` `.gen` `.68k` `.sgd` |
| Sega Master System / Game Gear | `genesis_plus_gx` | `.sms` `.gg` `.sg` |
| PC Engine / TurboGrafx-16 | `mednafen_pce_fast` | `.pce` |
| Neo Geo Pocket | `mednafen_ngp` | `.ngp` `.ngc` `.ngpc` `.npc` |

`.bin` is deliberately not accepted: it is the one extension that says nothing
about which console a ROM is for — Mega Drive ROMs, Atari cartridges, raw CD
tracks and BIOS dumps are all `.bin` — so a Genesis ROM has to be renamed to
`.md` instead. CD formats (`.cue`, `.iso`, `.chd`) and Famicom Disk System
images (`.fds`) are not supported, because they need disc images or a BIOS.
ROMs are capped at 32 MiB. A `.zip` containing any of the above is unpacked
automatically.

Each console shows its own controls, with the names printed on its own
controller: the Genesis gets **A B C** (and **X Y Z** and **Mode**), the Master
System gets **1**, **2** and **Pause**, the PC Engine gets **I** through **VI**
and **Run**, and the Neo Geo Pocket's **A** and **B** are the right way round
rather than swapped.

## The controller

The buttons are laid out like the console's own pad rather than as a list: the
d-pad is a cross on the left, the face buttons sit to its right, and the three
controls — **Wait**, **×3** and **Undo** — sit on the end of the bottom row.
A Game Boy looks like this, where `·` is a greyed-out spacer that holds the
column open:

```
·  ⬆️
⬅️  ⬇️  ➡️   B  A
Start  Select  ⏩ Wait   A ×3   ↩️ Undo
```

Consoles with more buttons grow upwards and sideways into the same shape — the
Game Boy Advance puts **L** and **R** on the top row where the shoulder
buttons really are, the Super Nintendo adds a **Y X / B A** block, and
the six-button Genesis and PC Engine keep their real two-by-three face cluster:

```
·  ⬆️   X  Y  Z
⬅️  ⬇️  ➡️
·  ·   A  B  C
Mode  Start  ⏩ Wait   B ×3   ↩️ Undo
```

Discord allows five rows of five components, and this is what each console
uses once the controls are added:

| Console | Components | Rows |
| --- | --- | --- |
| Game Boy / Color, NES | 12 | 3 |
| Game Boy Advance | 14 | 3 |
| Super Nintendo | 19 | 4 |
| Sega Genesis | 18 | 4 |
| PC Engine | 18 | 4 |
| Master System / Game Gear | 11 | 3 |
| Neo Geo Pocket | 11 | 3 |

Three controls fit beside every console's bottom row, so none of them needs a
row of its own. The cluster was four wide while there was a **Replay** button,
which was one too many to share a row with Start and Select — so six of the
eight consoles had their controls on a separate row and the widest three used
all five of Discord's action rows. Removing Replay gave every console one
component and, on six of them, one whole row back: the worst case is now the
Super Nintendo at 19 components over four rows, leaving six components and a
spare row in hand.

There is no **Stop** button and no **Reset** button. Both are commands
(`[p]retrostop`, `[p]retroreset`), because both are destructive to a game
everybody in the channel is playing and neither should be one mis-tap away.

## Core options

Every libretro core has its own settings — the console region, sound quality,
the colour palette a Game Boy game is tinted with. Gambatte has 32 of them,
FCEUmm 44, Genesis Plus GX 62. `[p]retroset coreoptions` reads and changes
them, and the changes are applied every time that core loads a game.

```
[p]retroset coreoptions                                        list the cores
[p]retroset coreoptions gambatte                               list its options
[p]retroset coreoptions gambatte gb_colorization               explain one
[p]retroset coreoptions gambatte gb_colorization GBC           set it
[p]retroset coreoptions gambatte gb_colorization reset         put the default back
```

A listing looks like this, paginated with arrows when it does not fit in one
message:

```
**`gambatte`** — Game Boy
32 option(s), read from the core itself.

**gb_colorization** = `GBC` **(changed)**
  `gambatte_gb_colorization` • `disabled`, `auto`, `GBC`, `SGB`, `internal`, `custom`
**gbc_color_correction** = `GBC only` *(default)*
  `gambatte_gbc_color_correction` • `disabled`, `GBC only`, `always`
```

**Keys** may be given with or without the core's prefix, so
`gambatte_gb_colorization` and `gb_colorization` both work, as does any
unambiguous ending of a key (`colorization` alone is refused, because Gambatte
has two options ending that way, and the two are listed for you). Prefixes are
not guessed: FCEUmm names its options `fceumm_region` but the Neo Geo Pocket
core names its one option `ngp_language`, not `mednafen_ngp_language`.

**`reset`** is the value that clears an override and puts the core's own
default back. It is not `default` or `none`, because some cores use those as
real values.

**Values** are checked against what the core says it accepts, and an invalid
one is refused with the list of valid ones:

```
[p]retroset coreoptions fceumm region Mars
→ `Mars` is not something the `fceumm` core accepts for `fceumm_region`.
  Valid values: `Auto`, `NTSC`, `PAL`, `Dendy`. Use `reset` to put the
  default (`Auto`) back.
```

**Reading a core's options can mean loading it**, and only one core may be
loaded at a time, so a game that is running is saved and put to sleep first —
exactly as starting a game in another channel would. Everything discovered is
remembered, so later listings are instant.

Most cores declare their options before any game is loaded, so asking is
enough. **Some do not**: FCEUmm declares *nothing* until a ROM is in, and then
declares 44. When that happens the cog says so rather than pretending the core
has no options, and the options become listable the moment somebody plays a
NES game — every session that starts adds what its core reports to what is
already known. A setting made before then is stored unchecked, with a warning:
libretro validates every key and value against the core's own list and
silently uses its default for anything it does not recognise, so a typo wastes
your time but cannot break a game.

`[p]retroset settings` lists whatever is currently overridden.

## Saving and sleeping

The cog never throws a game away. Progress is written to a save state
automatically — every few presses, whenever a game goes to sleep, and when the
cog is unloaded — next to a cached copy of the ROM.

Cores are never allowed to hoard: the audio libretro.py accumulates is dropped
every frame (it would otherwise grow by about 176 KiB per emulated second, since
nothing plays it), and everything a core prints goes to the
`red.robloach.retro.core` logger at DEBUG rather than onto the bot's console.

A game goes to sleep after 10 minutes without input (configurable with
`[p]retroset timeout`), when somebody runs `[p]retrostop`, or when the bot shuts
down. Sleeping frees the emulator but keeps the controls live: the next button
press wakes the game up exactly where it was, even if the bot has restarted in
between.

One game runs at a time across the whole bot. A libretro core is a shared
library with global state, so two emulators running at once would corrupt each
other's games; when a second channel starts playing, the first channel's game is
saved and put to sleep, and both channels are told so. Waking back up takes
about 25ms, so nobody notices.

Starting a *different* game in a channel banks the current game's progress and
switches. Each game a channel plays keeps its own save, so you can switch back
and forth freely.

**Starting a game again always picks up its save.** Whether the game is still
the channel's (in which case its existing message is simply brought back), or
was replaced five games ago, running `[p]retro <name>` restores that channel's
save state for it rather than cold-booting over the top — the same save
state → battery save → fresh chain a sleeping game is woken with, and a plain
one-line notice when the state could not be used.

**A replaced game keeps a Resume button.** When a channel moves on, the old
message's controls are swapped for a single **▶️ Resume**, and its text says so:

> Replaced by **µCity**. **Pokemon** was saved — press Resume to come back to it.

Pressing it starts that game again in that channel, right on that message: the
game it replaces is saved and retired in turn (and gets a Resume button of its
own), anything live elsewhere is hibernated first, and the save state comes
back. It keeps working after a bot restart, because what it needs is stored
rather than held in memory. A channel keeps one per cached game — five at the
very most — and a button whose cached ROM has been cleaned up is forgotten
along with it, rather than left there to apologise when somebody clicks it.

### What the cog forgets, and what it never does

Two kinds of thing are stored per channel, and the difference between them is
the whole of this section:

| | what it is | when it goes |
| --- | --- | --- |
| the **session** and **Resume** records | a pointer: *this message, in this channel, was playing this game* | by itself, as soon as it cannot resume anything |
| the **save state** and **battery save** | the player's progress, keyed by channel **and game** | only when somebody asks (`[p]retrosaves delete`), or when the per-channel game cap drops that game entirely |

Because progress is keyed by channel and game rather than by message,
**dropping a record loses the button and nothing else**. Starting the game
again by name re-downloads the ROM and restores from the save exactly as it
always did, so the cog does it automatically in four cases:

* **the cached ROM it named was pruned**, by the disk budget or by the
  per-channel cap of five games. A Resume button without its ROM can only
  apologise;
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

### Battery saves, as insurance

A save state is a snapshot of the whole machine, and it is only ever loadable
by the same build of the same core: **update a core and every save state it
wrote stops fitting**, which would strand a sleeping game. So the cartridge's
own battery save — its SRAM, the thing an original cart kept your file in — is
written alongside the state, on exactly the same schedule, as a `.srm` file
that any emulator can read.

When a game wakes up, the save state is preferred: it brings back the precise
moment, mid-jump if that is where you were. If the state is missing or the
core has since been updated and rejects it, the game is booted fresh with the
battery save poured back in, and the channel is told:

> This game's save state could not be used (most likely the emulator core was
> updated), so it started from the title screen — but your in-game save
> survived. Load it from the game's own menu to carry on.

Plenty of cartridges have no battery at all — nestest, dmg-acid2, most
puzzle games. That is normal, not a failure: nothing is written, no empty file
is left behind, and if a save state is ever lost for one of those the channel
is told the game simply started over.

**One restore chain, two doors.** Starting a game and waking a sleeping one
run the same function — save state, then battery save, then the beginning —
and the same code decides what to say afterwards and throws away a state the
core would not take. They used to be two copies kept in step by the tests; now
they cannot drift apart, and the suite holds the two against each other for
every outcome.

## Managing saves

`[p]retrosaves` (aliased `saves`) is a window onto exactly those files:
nothing else is stored, and nothing is kept per user.

```
[p]retrosaves                          list this channel's saved games
[p]retrosaves <game>                   one game in detail
[p]retrosaves list
[p]retrosaves info <game>
[p]retrosaves export <game>            post the battery save as a file
[p]retrosaves export state|both <game> send the save state too
[p]retrosaves import <game>            install an attached save file
[p]retrosaves reset <game>             drop the save state only
[p]retrosaves delete <game>            wipe both halves, after asking
```

A listing looks like this, paginated with arrows when it does not fit:

```
**3 game(s)** in this channel, 2 with saved progress, 434.6 KiB in total.

**µCity** — `ucity` • Game Boy • **playing now**
  save state 178.3 KiB, 2 minutes ago • battery save 128.0 KiB, 2 minutes ago
**Tobu Tobu Girl** — `tobu` • Game Boy • resumable
  save state 178.3 KiB, 3 days ago
```

`info` adds the console and core, whether the cached ROM is still there, and —
the part people actually want — what starting the game right now would do:
from its save state, from the title screen with the in-game save in place, or
from the very beginning.

**`reset` and `delete` are different things, deliberately.** `reset` throws
away the save state and keeps the cartridge's battery save, which is
"restart from my last in-game save".
`delete` wipes both, which is "start this game completely fresh": it takes the
player's in-game save with it, and asks for a yes first. Neither touches the
cached ROM, so the game still starts instantly afterwards.

**Exporting** posts the `.srm` as a plain attachment, so a player can keep it
or load it in another emulator. The save state is only sent when asked for,
because it is several times larger and only ever loads on the same build of
the same core. Anything over what the server accepts as an attachment is named
and skipped rather than failing the command.

**Importing** is checked hard before anything is written. The file has to be
named like a save (`.srm`/`.sav`, or `.state`), be inside the size ceilings
(1 MiB for a battery save, 16 MiB for a state), and then actually fit: the
game is booted on the real core and the files are offered to it, so a battery
save for the wrong cartridge comes back as

> That battery save is 8.0 KiB (8,192 bytes) but **µCity** has 128.0 KiB
> (131,072 bytes) of save memory.

and a state from another emulator comes back as the core's own complaint,
never a stack trace. Importing a battery save on its own also removes the
existing save state, because a state is restored *before* SRAM is looked at
and would otherwise be put back over the import.

**A game that is being played right now is saved and put to sleep first.** A
running emulator holds the authoritative copy of both saves and writes them
out on its next automatic save, so deleting or replacing the files under it
would be undone by the very next button press. Every command here that changes
a file hibernates the session first and says that it did; its controls stay
live, and the next press starts it from whatever the command left behind.

They also **drop the session's Undo history**, for the same reason one step
further out: an undo restores a state from memory and writes it straight back
to disk, so without that, `[p]retrosaves delete` — "start this game
completely fresh" — would be one button press away from coming back.

**Who may do what.** Listing, `info` and `export` are open to the channel,
like playing. `reset`, `delete` and `import` are limited to the person who
started the game, anybody with **Manage Messages**, and the bot owner — the
same three who may `[p]retrostop` someone else's game.

## Playing

Each press records the next **second** of play (configurable with
`[p]retroset cliplength`, 0.2–15 and fractional) and posts it as a lossless
animated WebP. The clip plays through once and stops rather than looping
forever, so a busy channel isn't full of flickering images.

A second is the default because a turn is a round trip: press, wait for the
clip, watch it, press again. It used to be four seconds, and three of those
were usually the game sitting still after the press had already played out —
while costing three times as long to record. A one second Game Boy clip is 60
emulated frames captured as 16 pictures (fewer once identical ones are
merged), a few kilobytes, and about 47ms of work on a Raspberry Pi 5;
four seconds is 239 frames and 61 pictures. Playback matches emulated time
either way — a one second clip plays for 1.004s and a 0.5 second one for
0.502s.

**One clip carries on exactly where the last one stopped.** A clip
photographs every fourth emulated frame *and* its own final frame, so the
picture it finishes on — the one left sitting in the channel, since a clip
plays through once and holds its last frame — is the precise state the next
press continues from. It used to stop on the last frame that happened to fall
on the sampling cadence, three frames (about 50ms) before the end of what it
had already emulated, so every press began with a small invisible jump.

**A clip starts where the last one ended.** A press does not show up on
screen the instant the button goes down — the hold is 160ms (ten frames) and
a game takes a moment longer than that to react, while a clip's first picture
is taken after a single emulated frame. On a game that animates by itself
that first picture already looks different from the one the previous clip left
in the channel, so the clip visibly moves on. On a game that sits still until
it is prodded — an overworld, a menu, a text box, which is most of what this
cog is played on — the first picture was *byte-identical* to the previous
clip's last one, so a new clip opened by replaying a moment the player had
already been looking at. That was reported, accurately, as "the clip seems to
replay a bit from the previous clip".

So a clip that opens with a press in it now runs a short **pre-roll** first:
the button goes down and the console runs, unphotographed, until the picture
is no longer the one the last clip finished on — and only then does the
recording start. Measured on real cores at the default hold, opening pictures
identical to the held one, out of the sixteen a one second clip takes:

| Core / ROM | Button | Before | After | Pre-roll |
| --- | --- | --- | --- | --- |
| gambatte / µCity | ← | 0 of 16 | 0 of 16 | none |
| genesis_plus_gx / homebrew | Start | 0 of 16 | 0 of 16 | none |
| gambatte / Libbet | A | 1 of 16 | **0** | 2 frames |
| fceumm / nestest (a menu) | Start | 1 of 16 | **0** | 1 frame |
| snes9x / homebrew | A | 1 of 16 | **0** | 3 frames |
| mgba / GBA homebrew | A | 3 of 16 | **0** | 10 frames |
| gambatte / Libbet | ↓ (ignored) | 16 of 16 | 16 of 16 | 15 (the bound) |
| gambatte / dmg-acid2 | A | 16 of 16 | 16 of 16 | 15 (the bound) |

µCity is the control: it animates every frame, so its clips never opened on a
repeat and nothing about them changed. The worst real case is ten frames, on a
game that reacts to the button coming *up* rather than going down — which is
exactly the length of the hold. The clips get slightly smaller rather than
bigger (a NES menu 1564 → 1418 bytes, the GBA homebrew 1098 → 614) because
what came off the front was a picture the encoder was storing, and a press
costs 0.4–1.6ms more to record.

**The pre-roll is bounded at a quarter of a second** (15 frames on a Game Boy,
a quarter of a default clip). The last two rows above are why: on a screen
that never changes at all — a paused game, a menu, a button the game ignores —
there is no first difference to find, so the pre-roll runs out its bound and
then records the clip exactly as it would have: full length, every picture,
the whole second, and byte for byte the same file. That costs 8–18ms and a
quarter of a second of game time, out of the 33–80ms a clip takes end to end.

The rule that was tried before this and rejected was a *trim* of the clip's
leading duplicate pictures, and on those rows it wants to trim everything and
leave the single picture it is obliged to keep — a 1005ms clip played as a
17ms flash. A bound cannot do that, because a pre-roll throws nothing away
that the clip recorded.

The press itself is not rearranged to pay for this. The pre-roll's frames are
emulated with the button held on exactly the frames it would have been held on
anyway, so the console sees the same input on the same frames as it did before
the pre-roll existed; all that changes is which frames get photographed. The
hold is therefore honoured in full, and the guarantee that the button is up
before the clip's last picture holds with *more* room than before rather than
less. A clip with no input in it — **Wait**, **Undo**, a boot, `[p]retroreset`
— has no pre-roll at all: nothing was pressed, so there is nothing whose
effect to wait for, and skipping frames because a paused game has not moved
would throw away the only thing Wait does.

**A clip plays for exactly as long as it emulated** — start to finish, with
nothing dropped off either end. Not "about as long", and not "as much of it as
had something new in it". A second of clip is a second of console. That rule
survived the fix above intact, which is the main reason the fix is a pre-roll
and not a trim: the pre-roll happens in *front* of the recording, so the same
number of frames go in and the same number of frames' worth of picture
durations come out.

`tests/test_emulator.py` holds all of it against real cores:
`test_a_clip_plays_for_as_long_as_it_emulated` for the timing rule,
`test_the_preroll_opens_a_clip_on_the_first_picture_the_press_changed` for the
table above, `test_a_completely_static_screen_still_produces_a_whole_clip` for
the case the bound protects, and
`test_the_preroll_leaves_the_seam_with_no_repeat_and_no_gap` for two presses
in a row. If a press still feels slow to land, `[p]retroset hold` is the dial
— a shorter hold reacts sooner, at the risk of a game not noticing the press
at all below about 100ms.

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
lives, which is why the message is the only place to look for it.

That took two goes. There used to be a **Replay** button that stitched the
last fifteen seconds of play back into one animation, and with it a
per-session buffer of recent clips bounded at **8 MiB**; removing the button
removed the buffer and left a single clip per session behind, and nothing
read *that* either — the one edit a press makes is handed the clip it is
about to post. So it has gone too. Measured on the real cores, fifteen
seconds of play at the default clip length:

| Console | Buffer (15 × 1s) | One clip | Now |
| --- | --- | --- | --- |
| Game Boy (µCity) | 13.5 KiB | 0.7 KiB | 0 |
| NES (nestest) | 21.8 KiB | 1.4 KiB | 0 |
| Super Nintendo | 153.9 KiB | 12.4 KiB | 0 |
| Genesis | 111.2 KiB | 2.0 KiB | 0 |

At the 0.2s clip floor the buffer held 76 clips, 36.1 KiB of them. The
*press* is no faster for any of it in a way anybody can feel: the bookkeeping
the buffer needed on every press — copy, append, three caps to re-check, a
button label to rewrite — measured 6.1 µs against a press that spends 37 ms
recording a clip, so about 0.015% of it. What was really saved is the memory,
the 0.18–0.40 s of decoding and re-encoding a Replay click cost, and a
component on every console's controls. The only thing a session holds now is
its **Undo** history, which is capped in both directions (see below).

**Who pressed which button is written on the message.** Every action replaces
the one line above the clip with a sentence naming both — in one voice for all
five of them:

> Rob pressed A.
> Rob pressed ⬅️.
> Rob pressed A ×3.
> Rob waited.
> Rob undid the last press.
> Rob reset the game.

The *button* name comes from the console's own button table, so a Genesis
press reads `Rob pressed C.` where the RetroPad would have called that button
A, and the Neo Geo Pocket's A and B are the right way round. The d-pad has no
labels, so it names itself with its arrow. `[p]retroreset` is a command rather
than a button and is attributed the same way, from whoever ran it.

The *person* is their server display name — their nickname in this server if
they have one, otherwise their global display name — as **plain text**. If
there is nobody to name (which should not happen, but a line is not worth
losing over it) the same sentence is used impersonally: `Pressed A.`,
`Waited.`

**It can never notify anybody.** A ping on every button press, from everybody
in the channel, would make the cog unusable in any channel people are actually
in — so there are two independent guards and either one alone would be enough:

* the name is escaped so that **no mention syntax is emitted at all**. Every
  markdown character is backslashed (including `#`, `-`, `>` and `+`, which
  are markdown only at the start of a line — which is exactly where a name
  sits, so a nickname of `# hello` would otherwise have rendered the line as a
  header), every `<` is escaped so no `<@id>` can form, and `@everyone` and
  `@here` get a zero-width space wedged into them. Invisible characters
  (zero-width spaces, the byte-order mark, right-to-left overrides) are
  dropped, whitespace is collapsed so one line stays one line, and the name
  is capped at 32 characters — Discord's own nickname limit, so a real name is
  never cut — so no one player can take over the line;
* every edit that carries the line also carries an `allowed_mentions` that
  suppresses **everyone, users, roles and the replied-to user**, so even a
  line that somehow did contain a live mention could not deliver one.

It costs nothing: the line rides on the single edit that already carries the
clip, so a press is still exactly one edit of the message. Anything more
important wins — the resumed note, or a save state that could not be used —
and the next press always rewrites the whole line, so it can never go stale.

### Undo

**↩️ Undo steps the game back one press.** Playing a game a second at a time
makes a misclick the most annoying thing that can happen: you press a
direction, wait for the clip, and find you walked into the wrong room. So
every press takes a save state of the machine *before* it changes anything,
and Undo puts the newest one back and records a fresh clip so the channel can
see where it landed.

It reaches back **eight presses**, and Wait and **×3** count as presses — they
move the game on, so they are things to step back from. Pressing Undo eight
times walks the game back eight presses; there is no redo.

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

**The history is in memory only** — and it is cheap to lose, because the real
save state is on disk either way. So a bot restart empties it: the button greys itself out, and a
click that gets through anyway says

> There is nothing to undo yet. Undo steps back through the last 8 presses,
> and that history is kept in memory only — so it is empty until somebody
> presses something, and the bot has restarted since the last press here. The
> game itself is exactly where you left it.

rather than failing. A game going to *sleep* is different: the session object
survives, and a save state can be loaded into any instance of the same core
build, so Undo still reaches back across a sleep — waking the game restores
the moment it fell asleep at, and the undo steps back from there. If a core
has been **updated** in the meantime it will refuse the old state; that
empties the history, says so in one line, and leaves the game exactly as it
was.

**The undo's own clip replaces the undone press's** on the message, so what
the channel is left looking at is where the game actually is. (This used to
be a larger job: the replay buffer had to rewind in step with the game, or a
stitched replay would show somebody walking into a room they were not in.)

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
and `[p]retrosaves rollback` (aliased **`[p]retrosaves undo`**) is already
the durable, on-disk version of the same idea — it goes back to the previous
*save state generation* for a game, which is the answer when the in-memory
history is gone.

### Resetting a game

`[p]retroreset` reboots the game that is playing in the channel, as if you had
flipped its power switch. The clip on its message shows the game booting, and
the message says *Rob reset the game.* — named from whoever ran the command,
in the same voice a press is named in.

**It is a command and not a button, deliberately.** A reset throws away the
progress-in-flight of everybody in the channel, which is exactly the reason
`[p]retrostop` is not a button either, and it has the same permission check:
only the person who **started** the game, anybody with **Manage Messages**,
and the bot owner can run it. Everyone else is told so and nothing happens.

**`[p]retroreset` and `[p]retrosaves reset` are completely different
commands**, which is the one thing to be clear about:

| | what it does |
| --- | --- |
| `[p]retroreset` | reboots the game **now**. Nothing on disk is deleted. |
| `[p]retrosaves reset <game>` | deletes a save **state file**, so the game next starts from its last in-game save. Touches no running game. |

Three decisions inside it, and the reasons for them:

* **Nothing on disk is written.** A reset does not save, so the `.state` file
  still holds the moment *before* the reset — a reset is not allowed to
  quietly overwrite a good save state with a title screen. It becomes the
  saved game only when the rebooted game saves of its own accord: the next
  automatic save (within three presses) or the next time it sleeps, both of
  which also rotate the pre-reset state into the previous generation that
  `[p]retrosaves rollback` can bring back.
* **The cartridge's battery save is not touched at all.** That is the save the
  player made from inside the game, and resetting a real console never wiped
  one — `retro_reset` is the reset line, not a new cartridge. Use
  `[p]retrosaves delete` if that is really what you want.
* **A reset is an undo point.** The state it is about to throw away is pushed
  onto the session's history first, so one click of **↩️ Undo** puts the
  player back where they were. Nothing else in this cog can lose as much in
  one command, and the undo machinery exists for exactly that class of
  mistake; it costs a sub-millisecond save state and some tens of kilobytes.
  The older undo points are left alone — they came from the same core and the
  same ROM, so a second click still means "one press further back".

A sleeping game is woken first (there is no power switch without a core) and
then rebooted. If nothing is playing in the channel, the command says so.

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
emulated are still swallowed silently — nobody ever sees *This interaction
failed* — and the same is true of a click that lands while the game is being
rebooted by `[p]retroreset`.

The clip and **one line** of text are all that is posted: no status card, no
caption. The line says who pressed which button, unless there is something
more important to say — the game having gone to sleep and come back, or a save
state that could not be restored — and the next press rewrites it either way.
Waking a sleeping game says *Resumed where you left off…* alongside the clip
it came back with, instead of naming the presser.

**The game only runs while a clip is being recorded.** Between one press and the
next the console is frozen mid-frame — it is not ticking away in the background,
so nothing can happen to you while nobody is looking, and a game left overnight
is exactly where you left it. That is also why a clip always starts the instant
the button goes down.

A button is held down for 160ms at the start of the clip, directions included.
That is long enough that no game polling its controller a few times a second can
miss it, and short enough that one press is one action: a Game Boy walk cycle is
16 frames (about 270ms), so a longer hold starts a *second* step and the
character crosses two tiles for one press. Tune it with `[p]retroset hold`. In
Pokémon Red, one press of a direction at a one second clip walks exactly one
tile and the step lands around frame 26 of 60, so the clip shows it finish.

Everything scheduled into a clip has to be **released before the clip's last
picture**, or the player never sees what their press did. That makes a short
clip a ceiling on the input inside it, and the ceiling is announced when you
set either value:

* the **hold** is cut to fit. At the 0.2s floor a clip is 12 emulated frames
  and the last one photographed is frame 8, so a 400ms hold becomes about
  134ms — still well over the ~100ms a game needs to notice a press.
* the **×3** button taps as many times as fit. Three 160ms taps 250ms apart
  need 1.4 seconds, so the spacing is squeezed first (218ms at a one second
  clip, 117ms at 0.8s — three taps closer together are still three taps) and
  only then is a tap dropped. Its label always counts the taps it will really
  do, and below two taps **the button is not shown at all** — see below.

### The ×3 button, and when it is there

The **×3** button is the third control, between **Wait** and **↩️ Undo**, and
it taps the console's own confirm button several times in one clip so a text
box or a menu takes one round trip instead of three. It is labelled with that
console's name for the button and the number of taps it will really do: `A ×3`
on a Game Boy, `B ×3` on a Genesis, `1 ×3` on a Master System, `I ×3` on a PC
Engine.

**It is hidden, not greyed out, when only one tap fits.** One tap is not a
repeat at all — it is exactly what the confirm button one row over already
does — so rather than drawing a dead button, the row is drawn without it. It
was greyed out until now, and that turned out to be worse than useless: a
present, dead, unexplained control reads as broken, and it was reported as the
feature having been *removed from the cog*. It comes back by itself on the
next press as soon as the clip is long enough, because the controls are
redrawn on every press, so `[p]retroset cliplength` corrects it either way
without restarting anything.

| Clip length | Taps | The button |
| --- | --- | --- |
| 0.2s (the floor) | 1 | not shown |
| 0.4s | 1 | not shown |
| 0.47s | 1 | not shown |
| 0.48s | 2 | `A ×2` |
| 0.5s | 2 | `A ×2` |
| 0.67s | 2 | `A ×2` |
| 0.68s | 3 | `A ×3` |
| 1s (the default) | 3 | `A ×3` |
| 15s (the ceiling) | 3 | `A ×3` |

**Wait and Undo never move.** The controls row reserves space for all three of
them whether or not the third is drawn, so on every one of the eight consoles
the row is the same shape at every clip length and simply has one fewer button
in it — a Game Boy is `Start Select Wait A ×3 Undo` at a second and
`Start Select Wait Undo` at a fifth of one. `[p]retroset cliplength` and
`[p]retroset settings` both say so when the length you have chosen is short
enough to hide it.

`[p]retroset hold` changes the numbers above, because the taps have to fit
inside the clip alongside the hold; whatever you set, the label and the line
on the message count the same real taps.

## Which build is this?

Twice now a puzzling answer has turned out to be a bot running an older build
than the repository — most memorably `[p]retroset cliplength 0.8` replying
*must be an integer*, on a copy that predated the clip length becoming
fractional. That looks like a bug in the cog and is not one, so the cog can
now answer the question itself:

```
[p]retroset version
```

which answers with the four lines below (the hashes and times are of course
whatever your install really is):

> **Version** `1.1.0`
> **Commit** `2cdc38c1f0a2` on `master`
> **Loaded code** `5fe2e0c2125f`, newest file 2026-09-21 10:00:28
> **Loaded at** 2026-09-21 10:00:32

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

## Permissions

Playing needs only **Attach Files** (plus the usual Read Messages / Send
Messages) — the clip is a plain attachment and the controls are buttons, so no
embed is involved. `[p]retrosaves export` asks for the same **Attach Files**,
because it posts the save file itself. **Embed Links** is used by one
owner-only command, `[p]retroset settings`.

Among the people in a channel, playing and looking at saves are open to
everybody; destroying progress is not. `[p]retrostop`, `[p]retroreset`,
`[p]retrosaves reset`, `[p]retrosaves delete` and `[p]retrosaves import` all
want the person who started the game, **Manage Messages**, or the bot owner.

## ROM URLs

`[p]retro <url>` makes the bot fetch a URL **a channel member chose**, from
wherever the bot is running. That is a server-side request forgery (SSRF)
waiting to happen, so every byte the cog fetches on somebody else's word goes
through one guard (`retro/net.py`):

* the scheme must be `http` or `https`;
* the hostname is resolved, and **every** address it resolves to must be a
  public one. Loopback, private, link-local, unique-local, multicast and
  reserved ranges are all refused — so nobody can aim the bot at
  `http://localhost:8080`, the Docker bridge, your router, or a cloud
  metadata service at `169.254.169.254`, which on most providers hands out
  credentials to anything that asks;
* the connection is made to the address that was just checked, and every
  redirect hop is checked again.

A refusal, a connection that is declined and a request that times out all
come back as **the same sentence**, deliberately: "refused" versus "timed
out" is exactly the difference a port scanner is looking for. The real reason
goes to the bot's log instead.

`[p]retroset allowprivateurls true` turns the guard off, and turning it off
removes the protection **for everybody** — any member who can run `[p]retro`
can then use the bot to probe your network. It exists for a bot you run at
home with a ROM library on your own LAN; the command says so at length before
you use it, and `[p]retroset settings` shows a warning while it is on. If the
library is reachable from the internet at all, prefer `[p]retroset game add`
with the guard left on.

`[p]retroset game add` checks its URL against the same guard while you are
still looking at the command, rather than letting a player discover next week
that it can never be fetched — and because that one is the owner, it says
exactly why.

## Corrupt and unplayable ROMs

A ROM that is not what it claims to be is an ordinary event: a truncated
download, a URL that served an HTML page, a ROM hack that was patched wrong.
None of it can take the bot down.

The obvious cases are refused before any core sees the file — something that
starts with `<` is a web page, anything under 1 KiB is a failed download, and
an extension no console claims is answered with the table above. Past that it
is up to the core, and both of its failure modes end in one plain sentence in
the channel:

* **the core refuses the cartridge** (a scrambled header, a `.gb` that is
  really something else) — *The game could not be started: the core could not
  load this ROM…*;
* **the core accepts it and then falls over** running code that is not a
  game, which is what a corrupted ROM body does about half the time.

Either way the emulator is freed and no half-started session is left behind,
which matters beyond the one game: the bot runs **one** core at a time, so a
core leaked by a failed start would stop every channel playing anything until
the cog was reloaded. `tests/test_malformed_roms.py` drives seeded corrupted
cartridges through both levels — the emulator on its own and `[p]retro` —
and the measurements are in that file's docstring. No hang and no native
crash has ever come out of it.

## BIOS files

Every core listed above is BIOS-free, so most people never need this. It exists
so the bot owner can add a core that *does* need firmware.

Cores ask the frontend for its **system directory** and look for firmware there.
This cog keeps one inside its own data folder and points every core at it;
`[p]retroset settings` shows the path and lists what is in it.

```
[p]retroset bios add                     (with a file or a .zip attached)
[p]retroset bios add <url>
[p]retroset bios add <filename>          (with the file attached, renaming it)
[p]retroset bios add <filename> <url>
[p]retroset bios list
[p]retroset bios remove <filename>
```

**A `.zip` installs everything in it.** Firmware is usually distributed as a
set rather than a single file, so every member of the archive is unpacked into
the system directory, and the folders it had inside the archive are kept:

```
firmware.zip
├── some_bios.bin      →  <system>/some_bios.bin
└── dc/
    ├── dc_boot.bin    →  <system>/dc/dc_boot.bin
    └── dc_flash.bin   →  <system>/dc/dc_flash.bin
```

That layout is deliberate. The system directory is the *root* a core is handed,
and cores disagree about what is in it: most ask for a bare filename at the top
(`disksys.rom`, `scph5501.bin`), but a good few ask for a subfolder of it
(`dc/dc_boot.bin`, `np2kai/FONT.ROM`). Keeping the archive's own layout is the
only thing that satisfies both, and it is the layout firmware sets already come
in. If a core wants a file somewhere else, unzip it yourself and add that one
file — with `<filename>` if it also has to be renamed.

The reply says how many files were installed and how many bytes, and lists a
sample of their paths. `[p]retroset bios list` shows everything, subfolders
included, and `[p]retroset bios remove` takes the path exactly as listed.

`<filename>` is optional and only means anything for a single file: it is the
exact name the core looks for, so it has to match what that core documents.
Leave it off and the file keeps its own name. A zip holding exactly one file
plus a `<filename>` still renames it, the way it always did.

Every path out of an archive is **validated, never rewritten**: absolute paths,
`..`, more than four folders deep, dotfiles, `__MACOSX/`, symlinks, device
nodes and anything outside a small character set are refused and counted, not
mangled into a name the core would never look for. Nothing is ever unpacked
using `ZipFile.extract`; members are read into memory and written to paths this
cog chose. One archive is capped at 64 MiB compressed, 64 MiB uncompressed, 250
files, and 16 MiB per file.

**This cog ships no firmware, never downloads any on its own, and names none.**
Console BIOS images are copyrighted; supplying a copy you are entitled to use is
entirely up to you.

## Upgrading from an earlier version

The cog's Python class used to be called `RetroCog` and is now simply `Retro`,
which is the name that appears in `[p]help` and `[p]cog list`. Red derives
**both** of a cog's storage locations from that class name — `Config` keys
every setting and session by it, and the data folder is `<data>/cogs/<class
name>/ ` — so the rename would otherwise have orphaned every downloaded core,
cached ROM, save state, battery save, BIOS file, saved game and live session.

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

## Credits

- [libretro.py](https://github.com/JesseTG/libretro.py) by Jesse Talavera
- [libretro](https://www.libretro.com/) and the RetroArch buildbot
- The [Gambatte](https://github.com/libretro/gambatte-libretro),
  [mGBA](https://github.com/libretro/mgba), [FCEUmm](https://github.com/libretro/libretro-fceumm),
  [Snes9x](https://github.com/libretro/snes9x), [Genesis Plus GX](https://github.com/libretro/Genesis-Plus-GX)
  and [Mednafen](https://github.com/libretro/beetle-pce-fast-libretro) core teams
- [retrobrews](https://retrobrews.github.io/), for collecting the homebrew
- [µCity](https://github.com/AntonioND/ucity) by Antonio Niño Díaz, the worked
  example throughout these docs

## License

GPL-3.0-or-later. See the [LICENSE](../LICENSE) at the root of this repository.
