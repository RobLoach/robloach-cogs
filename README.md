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

```
[p]repo add robloach-cogs https://github.com/RobLoach/robloach-cogs
[p]cog install robloach-cogs retro
[p]load retro
```

## Development

```
pip install -r requirements-dev.txt
pytest                      # the fast suite, the default: ~10s
pytest -m emulator          # the real-core half: ~30s, or ~16s with -n 2
ruff check .
```

## License

Copyright (C) 2025-2026 Rob Loach. GPL-3.0-or-later; see [LICENSE](LICENSE).
