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
pytest                      # the fast suite, which is the default: ~10s
pytest -m emulator          # the real-core half: ~30s, or ~16s with -n 2
pytest -m "not network"     # everything this machine can run
ruff check .
```

The slow tests drive real libretro cores; `python tests/fetch_assets.py`
downloads the cores and freely-distributable ROMs they need, and they skip
without them. See [tests/README.md](tests/README.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).