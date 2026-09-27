# robloach-cogs

Cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).

## Cogs

| Cog | Description |
| --- | --- |
| [Retro](retro) | Play retro console games in Discord |

<img src="assets/screenshot.png" alt="A Game Boy game running in a Discord channel: a clip of µCity's title screen, a line reading &quot;ucity · RobLoach reset the game.&quot;, and a row of controller buttons underneath — a d-pad, B and A, Start, Select, Wait, A x3 and Undo." width="492">

Every press posts a clip of the next second of play and redraws the controls,
on one message, with one edit. See [the Retro README](retro) for the rest.

## Installation

```
[p]repo add robloach-cogs https://github.com/RobLoach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

Needs Python 3.10 or newer (Red itself allows 3.8.1+; this cog does not).

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

## License

Copyright (C) 2025-2026 Rob Loach.

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.
