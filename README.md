# robloach-cogs

Experimental Cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).

## Cogs

| Cog | Description |
| --- | --- |
| [Retro](retro) | Play retro console games in Discord |

## Installation

```
[p]repo add robloach-cogs https://github.com/robloach/robloach-cogs
[p]cog install robloach-cogs <list of cogs>
[p]load <list of cogs>
```

## Development

```
pip install -r requirements-dev.txt
pytest                      # the fast suite, which is the default: a few seconds
pytest -m emulator          # the real-core half
pytest -m "not network"     # everything this machine can run
ruff check .
```

The slow tests drive real libretro cores; `python tests/fetch_assets.py`
downloads the cores and freely-distributable ROMs they need, and they skip
without them. See [tests/README.md](tests/README.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).