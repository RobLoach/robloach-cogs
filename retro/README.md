# Discord Red: Retro Cog

Play retro console games together in Discord. Attach a ROM and control the game
with buttons under the screen. Every press posts a short animated clip of the
next few seconds of gameplay, so you see the game react instead of a still
frame. Anyone in the channel can play, making it a fun social feature.

Eleven consoles are supported out of the box, from the Atari 2600 to the Super
Nintendo. Emulation is provided by
[libretro.py](https://github.com/JesseTG/libretro.py) running cores from the
[libretro buildbot](https://buildbot.libretro.com).

Games remember where you were. A channel's game keeps playing across days,
cog reloads, and bot restarts — nothing is ever "timed out and lost".

Only use ROMs you have the rights to, such as
[homebrew games](https://itch.io/games/tagged/homebrew) — no copyrighted ROMs.

## Installation

```
[p]repo add robloach-cogs https://github.com/robloach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

## Usage

```
[p]retroset download
[p]retroset game add tobu https://example.com/tobu.gb
[p]retroset settings
[p]retro [name|url]
```

- `[p]retro` starts a game from a saved name, a URL, or an attached ROM. With no arguments it brings back the game already going in the channel.
- `[p]retrostop` saves the game and puts it to sleep. The controls keep working.
- `[p]retroset download` (owner) downloads every supported core for your platform from the [libretro buildbot](https://buildbot.libretro.com). `[p]retroset download <core>` fetches or refreshes just one.
- `[p]retroset core <path>` (owner) points the cog at a libretro core that is already on the machine. The console is read from the filename, so keep the buildbot name (`snes9x_libretro.so`).
- `[p]retroset game add|remove|list` (owner) manages the games anyone can start by name.
- `[p]retroset timeout <minutes>` (owner) sets how long a game idles before it sleeps.
- `[p]retroset cliplength <seconds>` (owner) sets how much play each clip shows.
- `[p]retroset hold <milliseconds>` (owner) sets how long a button is held when someone presses it.
- `[p]retroset settings` (owner) shows the current configuration.

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

Each console shows its own controls, with the names printed on its own
controller: the Genesis gets **A B C** (and **X Y Z** and **Mode**), the Atari
2600 gets **Fire**, **Select** and **Reset**, the PC Engine gets **I** through
**VI** and **Run**, and the Neo Geo Pocket's **A** and **B** are the right way
round rather than swapped.

## Saving and sleeping

The cog never throws a game away. Progress is written to a save state
automatically — every few presses, whenever a game goes to sleep, and when the
cog is unloaded — next to a cached copy of the ROM.

A game goes to sleep after 10 minutes without input (configurable with
`[p]retroset timeout`), when someone presses Stop, or when the bot shuts down.
Sleeping frees the emulator but keeps the controls live: the next button press
wakes the game up exactly where it was, even if the bot has restarted in
between. The Stop button only appears while a game is awake, since a sleeping
one is already stopped.

One game runs at a time across the whole bot. A libretro core is a shared
library with global state, so two emulators running at once would corrupt each
other's games; when a second channel starts playing, the first channel's game is
saved and put to sleep, and both channels are told so. Waking back up takes
about 25ms, so nobody notices.

Starting a *different* game in a channel banks the current game's progress and
switches. Starting the same one again just resumes it. Each game a channel
plays keeps its own save, so you can switch back and forth.

## Playing

Each press records the next five seconds (configurable with
`[p]retroset cliplength`) and posts them as a lossless animated WebP. The clip
plays through once and stops rather than looping forever, so a busy channel
isn't full of flickering images — press **Replay** to watch the last clip again.
The controls grey out the moment you press a button and come back when the new
clip is ready, so you can tell the bot heard you.

A button is held down for 200ms at the start of the clip, which is long enough
that no game misses it. Directions are held twice as long, because moving needs
sustained input to actually go anywhere. Both are tuned with
`[p]retroset hold`. The **×3** button taps the console's confirm button three
times in one clip, so text boxes and menus take one round trip instead of three.

## Credits

- [libretro.py](https://github.com/JesseTG/libretro.py) by Jesse Talavera
- [libretro](https://www.libretro.com/) and the RetroArch buildbot
- The [Gambatte](https://github.com/libretro/gambatte-libretro),
  [mGBA](https://github.com/libretro/mgba), [FCEUmm](https://github.com/libretro/libretro-fceumm),
  [Snes9x](https://github.com/libretro/snes9x), [Genesis Plus GX](https://github.com/libretro/Genesis-Plus-GX),
  [Stella](https://github.com/libretro/stella2014-libretro) and
  [Mednafen](https://github.com/libretro/beetle-wswan-libretro) core teams
