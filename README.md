# robloach-cogs

Cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).

| Cog | Description |
| --- | --- |
| [Retro](retro) | Play retro console games in Discord |

## Retro

Attach a ROM and play a retro console game in a channel, with a controller
built out of Discord buttons. Every press posts a clip of the next second and a
half of play.

<img src="assets/screenshot.png" alt="A Game Boy game in a Discord channel: a clip of µCity's title screen, the line &quot;ucity · RobLoach reset the game.&quot;, and a row of controller buttons." width="492">

Nine consoles from the NES to the Game Boy Advance, each with its own layout.
Games save themselves and resume across restarts. Undo steps back eight
presses. Anyone in the channel can play.

```
[p]repo add robloach-cogs https://github.com/RobLoach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

Needs **Python 3.11** — the only version Red (`<3.12`) and libretro.py
(`>=3.12` from 0.8.0) overlap on.

**[Manual →](retro)** · **[Design notes](docs/DESIGN.md)** ·
**[Changelog](CHANGELOG.md)**

## Development

```
pip install -r requirements-dev.txt
pytest                      # the fast suite, the default: ~10s
pytest -m emulator          # the real-core half: ~30s, or ~16s with -n 2
ruff check .
```

The slow tests need real cores and ROMs — `python tests/fetch_assets.py` gets
them, and they skip without. See [tests/README.md](tests/README.md).

Red is deliberately **not** in `requirements-dev.txt`: the suite runs against
`tests/stubs` without it, which is how CI's fast job and a bare checkout work.
Installing real Red unlocks 18 more tests that pin the command surface and the
slash tree. Run it both ways before pushing — each configuration hides bugs the
other catches.

## License

Copyright (C) 2025-2026 Rob Loach. GPL-3.0-or-later; see [LICENSE](LICENSE).
