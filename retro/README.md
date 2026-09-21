# Discord Red: Retro Cog

Play retro console games together in Discord. Attach a ROM and control the game
with a controller built out of Discord buttons. Every press posts a short
animated clip of the next second of gameplay, so you see the game react
instead of a still frame. Anyone in the channel can play, making it a fun
social feature.

Eleven consoles are supported out of the box, from the Atari 2600 to the Super
Nintendo. Emulation is provided by
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

The emulator cores (about 5 MiB for all eleven consoles, none of which need a
BIOS) start downloading in the background as soon as the cog loads. To do it
now, or to check on it:

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
- `[p]retrosaves [game]` (aliased `saves`) lists what this channel has saved, or shows one game in detail. See [Managing saves](#managing-saves).
- `[p]retrosaves export <game>` posts a game's battery save as a file to keep. `export state <game>` or `export both <game>` sends the save state too.
- `[p]retrosaves import <game>` installs an attached `.srm`/`.sav` (and optionally a `.state`), checked against the real core first.
- `[p]retrosaves reset <game>` drops the save state, so the game restarts from the last in-game save.
- `[p]retrosaves delete <game>` wipes both halves of a game's save, after asking.
- `[p]retroset download` (owner) downloads every supported core for your platform from the [libretro buildbot](https://buildbot.libretro.com). `[p]retroset download <core>` fetches or refreshes just one.
- `[p]retroset autodownload [true|false]` (owner) controls whether missing cores are fetched automatically when the cog loads. On by default.
- `[p]retroset game add|remove|list` (owner) manages the games anyone can start by name.
- `[p]retroset coreoptions [core] [key] [value]` (owner, aliased `coreopts`) reads and changes a core's own settings. See below.
- `[p]retroset bios add|list|remove` (owner) manages BIOS files for cores that need one. See below.
- `[p]retroset timeout <minutes>` (owner) sets how long a game idles before it sleeps.
- `[p]retroset cliplength <seconds>` (owner) sets how much play each clip shows. The default is 1 second; anything from 0.2 to 15 works, fractions included (`0.8` is a real answer).
- `[p]retroset hold <milliseconds>` (owner) sets how long a button is held when someone presses it. The default is 160. It is a ceiling: a clip too short to show the button coming back up holds it for less.
- `[p]retroset settings` (owner) shows the current configuration, including the system directory and any BIOS files in it.

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
nothing but the ROM is needed — and the whole set is about 5 MiB.

| Console | Core | File extensions |
| --- | --- | --- |
| Game Boy / Color | `gambatte` | `.gb` `.gbc` `.dmg` |
| Game Boy Advance | `mgba` | `.gba` |
| Nintendo Entertainment System | `fceumm` | `.nes` `.unf` `.unif` |
| Super Nintendo | `snes9x` | `.smc` `.sfc` `.swc` `.fig` `.bs` `.st` |
| Sega Genesis / Mega Drive | `genesis_plus_gx` | `.md` `.mdx` `.smd` `.gen` `.68k` `.sgd` |
| Sega Master System / Game Gear | `genesis_plus_gx` | `.sms` `.gg` `.sg` |
| Atari 2600 | `stella2014` | `.a26` `.mvc` |
| PC Engine / TurboGrafx-16 | `mednafen_pce_fast` | `.pce` |
| WonderSwan | `mednafen_wswan` | `.ws` `.wsc` `.pc2` |
| Neo Geo Pocket | `mednafen_ngp` | `.ngp` `.ngc` `.ngpc` `.npc` |
| Virtual Boy | `mednafen_vb` | `.vb` `.vboy` |

`.bin` is deliberately not accepted: three of these consoles claim it, so there
is no way to tell them apart. Rename an Atari 2600 ROM to `.a26` instead. CD
formats (`.cue`, `.iso`, `.chd`) and Famicom Disk System images (`.fds`) are not
supported, because they need disc images or a BIOS. ROMs are capped at 32 MiB.
A `.zip` containing any of the above is unpacked automatically.

Each console shows its own controls, with the names printed on its own
controller: the Genesis gets **A B C** (and **X Y Z** and **Mode**), the Atari
2600 gets **Fire**, **Select** and **Reset**, the PC Engine gets **I** through
**VI** and **Run**, and the Neo Geo Pocket's **A** and **B** are the right way
round rather than swapped.

## The controller

The buttons are laid out like the console's own pad rather than as a list: the
d-pad is a cross on the left, the face buttons sit to its right, and Start and
Select share the bottom row with **Wait**, **×3** and **Replay**. A Game Boy
looks like this, where `·` is a greyed-out spacer that holds the column open:

```
·  ⬆️
⬅️  ⬇️  ➡️   B  A
Start  Select   ⏩ Wait   A ×3   🔁 Replay
```

Consoles with more buttons grow upwards and sideways into the same shape — the
Game Boy Advance and Virtual Boy put **L** and **R** on the top row where the
shoulder buttons really are, the Super Nintendo adds a **Y X / B A** block, and
the six-button Genesis and PC Engine keep their real two-by-three face cluster:

```
·  ⬆️   X  Y  Z
⬅️  ⬇️  ➡️
·  ·   A  B  C
Mode  Start   ⏩ Wait   B ×3   🔁 Replay
```

Discord allows five rows of five components, and the widest layout (the Super
Nintendo) uses four rows and nineteen buttons, so there is room to spare.

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
rather than held in memory. A channel keeps the five most recent of them; if
the ROM cache for one has since been pruned, the button says so instead of
failing, and the game's save is still there for `[p]retro <name>` to pick up.

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
while costing three times as long to record. Measured on a Raspberry Pi 5
with Pokémon Red in the overworld: a one second clip is 60 emulated frames
captured as 15 pictures (6–12 once identical ones are merged), 5–8 KiB and
0.41–0.45 seconds of work; four seconds was 239 frames, 60 pictures and
1.43–1.50 seconds. Playback matches emulated time either way — a one second
clip plays for 1.005s and a 0.5 second one for 0.502s.

A clip in which nothing moved at all — a title screen, a menu, a game waiting
for you — is written as a single still frame of a few hundred bytes, which is
exactly as informative and much likelier at a second than it was at four.
Replay still counts it as the second of play it stood for.

**Replay** shows the last **15 seconds**, not just the last clip. Each session
keeps its most recent clips in memory, and pressing Replay decodes them and
stitches them into one animation, oldest first, ending on the moment you just
played. The button says how much it holds — `Replay 12s`, or `Replay 0.4s` on
very short clips — so it never promises more than it has. Fifteen seconds is
reachable at every clip length now: the clip cap used to be eight, which at
a one second clip meant Replay could only ever hold eight seconds of the
fifteen it advertised. The buffer holds as many clips as fifteen seconds
takes — 76 at the 0.2s floor, 16 at a second, 5 at four seconds — bounded by
8 MiB of memory it has never come close to. Fifteen seconds of Game Boy play
in the buffer is 31 KiB at a second a clip and 81 KiB at 0.2s, and stitching
it costs 0.18–0.40 seconds and produces a 13–25 KiB animation. The hard
ceiling is 300 frames, which is more pictures than fifteen seconds needs at
any clip length and keeps even a pathological, fully-changing SNES picture
under seven seconds.

The buffer is **memory only**. That is a deliberate trade: clips are worthless
the moment the session moves on, and writing them would multiply the cog's
storage by the number of channels for a button most people press once. So after
a bot restart there is nothing to replay yet, and the button is greyed out and
says so until the next press refills it.

The controls grey out the moment you press a button and come back when the new
clip is ready, so you can tell the bot heard you.

Nothing but the clip is posted: no status card, no caption. The buttons say what
they do. A line of text only appears when there is something to say, such as the
game having gone to sleep or a save state that could not be restored, and it is
cleared again by the next press.

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
  only then is a tap dropped: 0.5s does two, and below about 0.45s only one
  would fit, which is what the confirm button already does, so the button
  greys itself out and says `A ×1` rather than lying. Its label always counts
  the taps it will really do.

## Permissions

Playing needs only **Attach Files** (plus the usual Read Messages / Send
Messages) — the clip is a plain attachment and the controls are buttons, so no
embed is involved. `[p]retrosaves export` asks for the same **Attach Files**,
because it posts the save file itself. **Embed Links** is used by one
owner-only command, `[p]retroset settings`.

Among the people in a channel, playing and looking at saves are open to
everybody; destroying a save is not. `[p]retrostop`, `[p]retrosaves reset`,
`[p]retrosaves delete` and `[p]retrosaves import` all want the person who
started the game, **Manage Messages**, or the bot owner.

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
  [Snes9x](https://github.com/libretro/snes9x), [Genesis Plus GX](https://github.com/libretro/Genesis-Plus-GX),
  [Stella](https://github.com/libretro/stella2014-libretro) and
  [Mednafen](https://github.com/libretro/beetle-wswan-libretro) core teams
- [retrobrews](https://retrobrews.github.io/), for collecting the homebrew
- [µCity](https://github.com/AntonioND/ucity) by Antonio Niño Díaz, the worked
  example throughout these docs

## License

GPL-3.0-or-later. See the [LICENSE](../LICENSE) at the root of this repository.
