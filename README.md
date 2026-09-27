# robloach-cogs

Cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).

| Cog | Description |
| --- | --- |
| [Retro](retro) | Play retro console games in Discord |

## Retro

Attach a ROM and play a retro console game in a channel, with a controller
built out of Discord buttons. Every press posts a clip of the next second of
play and redraws the controls, on one message, with one edit.

<img src="assets/screenshot.png" alt="A Game Boy game running in a Discord channel: a clip of µCity's title screen, a line reading &quot;ucity · RobLoach reset the game.&quot;, and a row of controller buttons underneath — a d-pad, B and A, Start, Select, Wait, A x3 and Undo." width="492">

Nine consoles from the NES to the Game Boy Advance, picked automatically from
the file extension, each with its own controller layout. Games save themselves
and pick up where they left off, even after the bot restarts. Undo steps back
through the last eight presses. Anyone in the channel can play.

```
[p]repo add robloach-cogs https://github.com/RobLoach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

Needs Python 3.10 or newer (Red itself allows 3.8.1+; this cog does not).

**[Read the Retro manual →](retro)** for how to use it, or
**[docs/DESIGN.md](docs/DESIGN.md)** for why it works the way it does.

## Development

```
pip install -r requirements-dev.txt
pytest                      # the fast suite, which is the default: ~10s
pytest -m emulator          # the real-core half: ~30s, or ~16s with -n 2
pytest -m "not network"     # everything this machine can run
ruff check .
```

The slow tests drive real libretro cores; `python tests/fetch_assets.py`
downloads the cores and freely-distributable ROMs they need, and they skip
without them. See [tests/README.md](tests/README.md).

Red-DiscordBot is deliberately **not** in `requirements-dev.txt`: the suite
runs against `tests/stubs` without it, which is how CI runs the fast job and
how a bare checkout works. Installing the real Red unlocks a further 18 tests
that pin the command surface and the slash-command tree — worth doing before
changing either, and worth running the suite both ways afterwards, because
each configuration hides bugs the other catches.

## License

Copyright (C) 2025-2026 Rob Loach.

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.
