# Discord Red: PyBoy Cog

Play Game Boy games together in Discord. Attach a `.gb` or `.gbc` ROM and
control the game with buttons under the screen. Every press posts an animated
GIF of the next four seconds of gameplay, so you see the game react instead of
a still frame. Anyone in the channel can play, making it a fun social feature.
Emulation is provided by
[libretro.py](https://github.com/JesseTG/libretro.py) running the
[Gambatte](https://github.com/libretro/gambatte-libretro) core.

Games remember where you were. A channel's game keeps playing across days,
cog reloads, and bot restarts — nothing is ever "timed out and lost".

Only use ROMs you have the rights to, such as
[homebrew games](https://itch.io/games/tagged/gameboy) — no copyrighted ROMs.

## Installation

```
[p]repo add robloach-cogs https://github.com/robloach/robloach-cogs
[p]cog install robloach-cogs pyboy
[p]load pyboy
```

## Usage

```
[p]pyboyset download
[p]pyboyset core /path/to/gambatte_libretro.so
[p]pyboyset game add tobu https://example.com/tobu.gb
[p]pyboyset settings
[p]pyboy [name|url]
```

- `[p]pyboy` starts a game from a saved name, a URL, or an attached `.gb`/`.gbc` ROM. With no arguments it brings back the game already going in the channel.
- `[p]pyboystop` saves the game and puts it to sleep. The controls keep working.
- `[p]pyboyset download` (owner) downloads the Gambatte core for your platform from the [libretro buildbot](https://buildbot.libretro.com).
- `[p]pyboyset core <path>` (owner) points the cog at an already-installed Game Boy libretro core.
- `[p]pyboyset game add|remove|list` (owner) manages the games anyone can start by name.
- `[p]pyboyset timeout <minutes>` (owner) sets how long a game idles before it sleeps.
- `[p]pyboyset settings` (owner) shows the current configuration.

## Saving and sleeping

The cog never throws a game away. Progress is written to a save state
automatically — every few presses, whenever a game goes to sleep, and when the
cog is unloaded — next to a cached copy of the ROM.

A game goes to sleep after 10 minutes without input (configurable with
`[p]pyboyset timeout`), when someone presses Stop, or when the bot shuts down.
Sleeping frees the emulator but keeps the controls live: the next button press
wakes the game up exactly where it was, even if the bot has restarted in
between. The Stop button only appears while a game is awake, since a sleeping
one is already stopped.

One game runs at a time across the whole bot. A libretro core is a shared
library with global state, so two emulators running at once would corrupt each
other's games; when a second channel starts playing, the first channel's game
is saved and put to sleep. Waking back up takes about 25ms, so nobody notices.

Starting a *different* game in a channel banks the current game's progress and
switches. Starting the same one again just resumes it. Each game a channel
plays keeps its own save, so you can switch back and forth.

## Playing

Each press records the next four seconds and posts them as an animated GIF.
The clip plays through once and stops rather than looping forever, so a busy
channel isn't full of flickering images — press **Replay** to watch the last
clip again. The controls grey out the moment you press a button and come back
when the new clip is ready, so you can tell the bot heard you.

## Credits

- [libretro.py](https://github.com/JesseTG/libretro.py) by Jesse Talavera
- [Gambatte](https://github.com/libretro/gambatte-libretro) Game Boy core
- [libretro](https://www.libretro.com/) and the RetroArch buildbot
