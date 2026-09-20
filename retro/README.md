# Discord Red: Retro Cog

Play retro console games together in Discord. Attach a ROM and control the game
with a controller built out of Discord buttons. Every press posts a short
animated clip of the next few seconds of gameplay, so you see the game react
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
- `[p]retroset download` (owner) downloads every supported core for your platform from the [libretro buildbot](https://buildbot.libretro.com). `[p]retroset download <core>` fetches or refreshes just one.
- `[p]retroset autodownload [true|false]` (owner) controls whether missing cores are fetched automatically when the cog loads. On by default.
- `[p]retroset core <path>` (owner) points the cog at a libretro core that is already on the machine. The console is read from the filename, so keep the buildbot name (`snes9x_libretro.so`).
- `[p]retroset game add|remove|list` (owner) manages the games anyone can start by name.
- `[p]retroset coreoptions [core] [key] [value]` (owner, aliased `coreopts`) reads and changes a core's own settings. See below.
- `[p]retroset bios add|list|remove` (owner) manages BIOS files for cores that need one. See below.
- `[p]retroset timeout <minutes>` (owner) sets how long a game idles before it sleeps.
- `[p]retroset cliplength <seconds>` (owner) sets how much play each clip shows. The default is 4.
- `[p]retroset hold <milliseconds>` (owner) sets how long a button is held when someone presses it. The default is 160.
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
switches. Starting the same one again just resumes it. Each game a channel
plays keeps its own save, so you can switch back and forth.

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

> This game's save state could not be used (the emulator core was updated), so
> it started from the title screen — but your in-game save survived. Load it
> from the game's own menu to carry on.

Plenty of cartridges have no battery at all — nestest, dmg-acid2, most
puzzle games. That is normal, not a failure: nothing is written, no empty file
is left behind, and if a save state is ever lost for one of those the channel
is told the game simply started over.

## Playing

Each press records the next four seconds (configurable with
`[p]retroset cliplength`) and posts them as a lossless animated WebP. The clip
plays through once and stops rather than looping forever, so a busy channel
isn't full of flickering images — press **Replay** to watch the last clip again.
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
character crosses two tiles for one press. Tune it with `[p]retroset hold`. The
**×3** button taps the console's confirm button three times in one clip, so text
boxes and menus take one round trip instead of three.

## Permissions

Playing needs only **Attach Files** (plus the usual Read Messages / Send
Messages) — the clip is a plain attachment and the controls are buttons, so no
embed is involved. **Embed Links** is used by one owner-only command,
`[p]retroset settings`.

## BIOS files

Every core listed above is BIOS-free, so most people never need this. It exists
so the bot owner can add a core that *does* need firmware.

Cores ask the frontend for its **system directory** and look for firmware there.
This cog keeps one inside its own data folder and points every core at it;
`[p]retroset settings` shows the path and lists what is in it.

```
[p]retroset bios add <filename>          (with the file attached)
[p]retroset bios add <filename> <url>
[p]retroset bios list
[p]retroset bios remove <filename>
```

`<filename>` is the exact name the core looks for, so it has to match what that
core documents. A `.zip` is unpacked: a member matching `<filename>` wins,
otherwise the only file inside is used. Filenames are validated — no folders, no
traversal — and the file is capped at 16 MiB.

**This cog ships no firmware, never downloads any on its own, and names none.**
Console BIOS images are copyrighted; supplying a copy you are entitled to use is
entirely up to you.

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
