# Retro

Play retro console games together in Discord. Attach a ROM and control it with
buttons; every press posts a clip of the next second and a half of play. Anyone
in the channel can play.

Nine consoles, NES to Game Boy Advance, emulated with
[libretro.py](https://github.com/JesseTG/libretro.py). Games save themselves and
resume where they left off, across restarts.

> Only use ROMs you have the rights to. Free homebrew:
> <https://retrobrews.github.io/>

<img src="../assets/screenshot.png" alt="A Game Boy game in a Discord channel: a clip of µCity's title screen, the line &quot;ucity · RobLoach reset the game.&quot;, and a row of controller buttons." width="492">

Why it works the way it does: [docs/DESIGN.md](../docs/DESIGN.md).

## Contents

- [Getting started](#getting-started)
- [Starting a game](#starting-a-game)
- [Consoles](#consoles)
- [The controller](#the-controller)
- [Saving and sleeping](#saving-and-sleeping)
- [Managing saves](#managing-saves)
- [Settings](#settings)
- [Core options](#core-options)
- [BIOS files](#bios-files)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Permissions](#permissions)
- [ROM URLs](#rom-urls)
- [Upgrading](#upgrading)

## Getting started

```
[p]repo add robloach-cogs https://github.com/RobLoach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

Needs Python 3.11. Nothing to configure: the seven cores (4.5 MiB) download in
the background on first load. `[p]retroset download` does it now,
`[p]retroset settings` shows where they went.

Then play. [µCity](https://github.com/AntonioND/ucity) is a good first game:

```
[p]retro https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc
```

Save it so anyone can start it by name:

```
[p]retroset game add ucity https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc
[p]retro ucity
```

## Starting a game

```
[p]retro ucity            a game saved by name
[p]retro <url>            a direct link to a ROM
[p]retro                  with a ROM or .zip attached
[p]retro                  nothing running: lists what you can start
```

`/retro play game:` autocompletes over the saved games. `[p]retro list` shows
the same list, and both offer a dropdown to pick from. With a game already
running, a bare `[p]retro` brings it back.

**Attachments** — a raw ROM or a `.zip`, up to 32 MiB. You can type a name as
well: `[p]retro sonic` with a compilation attached picks `Sonic.md` out of it
rather than whatever sorts first. Folders inside the archive are fine; nothing
is unpacked using paths from the archive.

## Consoles

Chosen from the file extension. Every core here is BIOS-free.

| Console | Core | Extensions |
| --- | --- | --- |
| Game Boy / Color | `gambatte` | `.gb` `.gbc` `.dmg` |
| Game Boy Advance | `mgba` | `.gba` |
| Nintendo Entertainment System | `fceumm` | `.nes` `.unf` `.unif` |
| Super Nintendo | `snes9x` | `.smc` `.sfc` `.swc` `.fig` |
| Sega Genesis / Mega Drive | `genesis_plus_gx` | `.md` `.mdx` `.smd` `.gen` `.68k` `.sgd` |
| Sega Master System | `genesis_plus_gx` | `.sms` `.sg` |
| Sega Game Gear | `genesis_plus_gx` | `.gg` |
| PC Engine / TurboGrafx-16 | `mednafen_pce_fast` | `.pce` |
| Neo Geo Pocket | `mednafen_ngp` | `.ngp` `.ngc` `.ngpc` `.npc` |

**`.bin` is refused.** It says nothing about which console a ROM is for — Atari
cartridges, Mega Drive ROMs, CD tracks and BIOS dumps are all `.bin`. Rename a
Genesis ROM to `.md`. CD images (`.cue`, `.iso`, `.chd`) and BIOS-dependent
formats (`.fds`, `.bs`, `.st`) are not supported; each says so when you try it.

Each console shows its own button names — the Genesis **A B C**, the Master
System **1**, **2** and **Pause**, the PC Engine **I**–**VI** and **Run**.

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
- **A ×3** — taps the confirm button several times in one clip. Labelled with
  the real tap count, and **hidden rather than greyed out** when only one fits:

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

## Saving and sleeping

Progress is written automatically — every few presses, on sleep, on unload —
beside a cached copy of the ROM. Writes are atomic, so a crash leaves the
previous save rather than half of a new one.

A game **sleeps** after 10 minutes idle (`[p]retroset timeout`), on
`[p]retrosleep`, or at shutdown. Sleeping frees the emulator and keeps the
controls live; the next press wakes it where it was, in about 25ms.

**One game runs at a time across the whole bot** — a libretro core has global
state. Starting one elsewhere saves and sleeps the other, and both channels are
told.

**A replaced game keeps a ▶️ Resume button**, which survives a restart. A
channel keeps one per cached game, five at most.

### Two kinds of save

A **save state** is the exact moment, down to the frame; it only loads on the
same build of the same core. An **in-game save** is what you saved from inside
the game — the cartridge's battery memory, a `.srm` any emulator can read.

Both are written together, because **updating a core invalidates every save
state**. On waking, the state is preferred; if it is gone or refused, the game
boots fresh with the in-game save in place and says so. Cartridges with no
battery are normal, not a failure.

### What is deleted

| | | |
| --- | --- | --- |
| **session** and **Resume** records | a pointer to a message | dropped automatically once they can resume nothing |
| **save state** and **in-game save** | your progress | only when you ask |

Records are dropped when the cached ROM is pruned, the channel is deleted, or
the bot leaves the server. **The saves are kept either way** — starting the
game again re-downloads the ROM and picks the save straight back up.

## Managing saves

```
[p]retrosaves                          this channel's saved games
[p]retrosaves <game>                   one game in detail
[p]retrosaves export <game>            post the in-game save as a file
[p]retrosaves export state|both <game> send the save state too
[p]retrosaves import <game>            install an attached save
[p]retrosaves dropstate <game>         delete the save state only
[p]retrosaves rollback <game>          back one save-state generation
[p]retrosaves delete <game>            wipe both halves, after asking
```

- **`dropstate`** keeps the in-game save: "restart from my last in-game save".
  **`delete`** wipes both. Neither touches the cached ROM.
- **`rollback`** is the durable, on-disk version of Undo.
- **Importing** is checked before anything is written — the file has to be named
  like a save, be inside the size ceilings (1 MiB / 16 MiB), and actually fit
  the cartridge, which is tested on the real core.
- **A game being played is saved and slept first**, and its Undo history
  dropped; otherwise the next press would write the old files straight back.

Listing and `export` are open to the channel. `dropstate`, `delete` and
`import` are not — see [Permissions](#permissions).

## Settings

All owner-only and bot-wide.

| Setting | Default | What it does |
| --- | --- | --- |
| `[p]retroset cliplength <seconds>` | 1.6 | Play per clip. 0.2–5, fractional. |
| `[p]retroset hold <milliseconds>` | 80 | How long a button is held. A short clip holds for less. |
| `[p]retroset timeout <minutes>` | 10 | Idle time before a game sleeps. |
| `[p]retroset diskbudget <MiB>` | 1024 | Disk the cog may use. `0` is no limit. |
| `[p]retroset autodownload [true\|false]` | on | Fetch missing cores on load. |
| `[p]retroset allowprivateurls [true\|false]` | **off** | Let ROM URLs reach your network. See [ROM URLs](#rom-urls). |

**No save is ever deleted to make room** — cached ROMs go, oldest first.

Clip length multiplies: every edit waits out the clip it replaces, so a full
queue of five is five whole clips — eight seconds at the default, twenty-five
at the ceiling.

## Core options

Every core has its own settings. Changes apply whenever that core loads a game.

```
[p]retroset coreoptions                                  list the cores
[p]retroset coreoptions gambatte                         list its options
[p]retroset coreoptions gambatte gb_colorization         explain one
[p]retroset coreoptions gambatte gb_colorization GBC     set it
[p]retroset coreoptions gambatte gb_colorization reset   put the default back
```

`/retroset coreoptions` autocompletes all three arguments, which is much easier
than remembering libretro's own names.

Keys work with or without the core's prefix, or as any unambiguous ending.
`reset` clears an override (not `default` or `none` — some cores use those as
real values). Values are checked against what the core accepts.

Reading a core's options can mean loading it, so a running game is slept first.
Results are remembered. Some cores (FCEUmm) declare nothing until a ROM is
loaded; theirs become listable once somebody plays one.

## BIOS files

Every core above is BIOS-free — most people never need this.

```
[p]retroset bios add                     (with a file or .zip attached)
[p]retroset bios add <url>
[p]retroset bios add <filename>          (attached, renaming it)
[p]retroset bios list
[p]retroset bios remove <filename>
```

A `.zip` installs everything in it, keeping its folder layout — cores disagree
about whether firmware sits at the top or in a subfolder, and this satisfies
both. `<filename>` renames a single file to the exact name a core looks for.

Archive paths are validated, never rewritten: absolute paths, `..`, deep
nesting, dotfiles, symlinks and device nodes are refused. Capped at 64 MiB, 250
files, 16 MiB each.

**This cog ships no firmware and downloads none.** BIOS images are copyrighted;
supplying one you may use is up to you.

## Command reference

**Anyone in the channel:**

| Command | What it does |
| --- | --- |
| `[p]retro [name\|url]` | Start a game, or bring back the channel's. Also `/retro play`. |
| `[p]retro list` | The saved games and the playable consoles. |
| `[p]retrosaves [game]` | What this channel has saved, or one game in detail. |
| `[p]retrosaves list` | The listing on its own. |
| `[p]retrosaves info <game>` | One game in detail. |
| `[p]retrosaves export <game>` | Post a save as a file. |

**The game's starter, Manage Messages, or the bot owner:**

| Command | What it does |
| --- | --- |
| `[p]retrosleep` | Save and sleep. The controls keep working. |
| `[p]retroend` | Finish with it: saved, freed, a ▶️ Resume button left. |
| `[p]retroreboot` | Reboot the running game. Nothing on disk is deleted, and it is an undo point. |
| `[p]retrosaves import <game>` | Install an attached save. |
| `[p]retrosaves dropstate <game>` | Delete the save state. |
| `[p]retrosaves rollback <game>` | Back one save-state generation. |
| `[p]retrosaves delete <game>` | Wipe a game's save. |

**Owner only:**

| Command | What it does |
| --- | --- |
| `[p]retroset download [core]` | Download the cores, or refresh one. |
| `[p]retroset autodownload [true\|false]` | See [Settings](#settings). |
| `[p]retroset game add <name> <url>` | Save a game so anyone can start it by name. |
| `[p]retroset game remove <name>` | Forget one. |
| `[p]retroset game list` | List them. |
| `[p]retroset coreoptions [core] [key] [value]` | See [Core options](#core-options). |
| `[p]retroset bios add\|list\|remove` | See [BIOS files](#bios-files). |
| `[p]retroset cliplength <seconds>` | See [Settings](#settings). |
| `[p]retroset hold <milliseconds>` | See [Settings](#settings). |
| `[p]retroset timeout <minutes>` | See [Settings](#settings). |
| `[p]retroset diskbudget [megabytes]` | Disk cap; with no argument, what is using it. |
| `[p]retroset allowprivateurls [true\|false]` | See [ROM URLs](#rom-urls). |
| `[p]retroset settings` | The current configuration. |
| `[p]retroset version` | "Am I running the new code?" |
| `[p]retrodiagnose [true]` | "Does this install actually work?" |

## Troubleshooting

**`[p]retrodiagnose`** — what this install can actually do, as opposed to what
it is configured to do. Reports the build, the installed libretro.py and
Pillow, whether a video driver can be made, each core, storage against its
budget, and what is running. Plain text, for pasting into an issue.
`[p]retrodiagnose true` loads every core as well.

**`[p]retroset version`** — the declared version, the commit, and a fingerprint
of the code that was actually loaded. `git pull` without `[p]reload retro` does
not change the fingerprint, which is exactly the situation worth proving.

**A ROM will not start.** Web pages, truncated downloads and unknown extensions
are refused before any core sees them. Past that it is the core's own
complaint, in one plain sentence. Either way the emulator is freed.

**A press did nothing.** Check `[p]retrodiagnose`. If clips arrive but feel slow
to land, `[p]retroset hold` is the dial — shorter reacts sooner, at the risk of
a game not noticing the press below about 100ms.

## Permissions

Playing needs **Attach Files** (plus Read/Send Messages). **Embed Links** is
used by `[p]retroset settings` alone.

Playing and looking at saves are open to everybody. Destroying progress is not:
`[p]retrosleep`, `[p]retroend`, `[p]retroreboot`, `[p]retrosaves dropstate`,
`[p]retrosaves delete` and `[p]retrosaves import` all want the game's starter,
**Manage Messages**, or the bot owner.

## ROM URLs

`[p]retro <url>` makes the bot fetch a URL a channel member chose, which is a
server-side request forgery waiting to happen. Every fetch goes through one
guard:

- the scheme must be `http` or `https`;
- every address the hostname resolves to must be public — loopback, private,
  link-local and reserved ranges are refused, so nobody can aim the bot at your
  router or at a cloud metadata service;
- the connection goes to the address just checked, and every redirect is
  re-checked.

Refusals, declined connections and timeouts all answer with **the same
sentence**: telling them apart is what a port scanner wants. The real reason
goes to the log.

`[p]retroset allowprivateurls true` turns the guard off **for everybody**. It
exists for a bot on your own LAN. If the library is reachable from the internet,
prefer `[p]retroset game add` with the guard left on.

## Upgrading

```
[p]repo update robloach-cogs
[p]cog update retro
[p]reload retro
```

All three, in that order. `[p]cog update` compares against what the repo was
last fetched at, not GitHub, so without the first it has nothing to install —
and **without the reload the bot is still running the old code**. Check with
`[p]retroset version`.

Settings, cores, cached ROMs, saves, BIOS files and live sessions all survive.
Games that were playing come back on their next button press.

See [CHANGELOG.md](../CHANGELOG.md) for what changed.

## Credits

- [libretro.py](https://github.com/JesseTG/libretro.py) by Jesse Talavera
- [libretro](https://www.libretro.com/) and the RetroArch buildbot
- The Gambatte, mGBA, FCEUmm, Snes9x, Genesis Plus GX and Mednafen core teams
- [retrobrews](https://retrobrews.github.io/), for collecting the homebrew
- [µCity](https://github.com/AntonioND/ucity) by Antonio Niño Díaz

## License

GPL-3.0-or-later. See the [LICENSE](../LICENSE).
