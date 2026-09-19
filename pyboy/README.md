# Discord Red: PyBoy Cog

Play Game Boy games together in Discord. Attach a `.gb` or `.gbc` ROM and
control the game with buttons under the screen. Anyone in the channel can
play, making it a fun social feature. Emulation is provided by
[libretro.py](https://github.com/JesseTG/libretro.py) running the
[Gambatte](https://github.com/libretro/gambatte-libretro) core.

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
[p]pyboyset settings
[p]pyboy [url]
```

- `[p]pyboyset download` (owner) downloads the Gambatte core for your platform from the [libretro buildbot](https://buildbot.libretro.com).
- `[p]pyboyset core <path>` (owner) points the cog at an already-installed Game Boy libretro core.
- `[p]pyboyset settings` (owner) shows the current configuration.
- `[p]pyboy` starts a game from an attached `.gb`/`.gbc` ROM, or from a URL passed as an argument. One game per channel; the session ends with the Stop button or after 10 minutes without input.

## Credits

- [libretro.py](https://github.com/JesseTG/libretro.py) by Jesse Talavera
- [Gambatte](https://github.com/libretro/gambatte-libretro) Game Boy core
- [libretro](https://www.libretro.com/) and the RetroArch buildbot
