# Changelog

All notable changes to this repository. The version is the one in
`retro/info.json`, which `[p]retroset version` reports; the scheme is
`MAJOR.MINOR.PATCH`, where a feature bumps the minor and a fix the patch.

Upgrading is `[p]repo update robloach-cogs`, `[p]cog update retro`,
`[p]reload retro` — see [Upgrading](retro/README.md#upgrading). **The reload
is not optional**: without it the bot is still running the old code.

## 1.3.0

### Changed defaults — these affect existing installs

Both take effect on any install that has not set them itself. Neither touches
a saved game.

- **Clip length is 1.6 seconds**, up from 1. Long enough to watch a move land
  and for the repeat button to fit all three taps. `[p]retroset cliplength`.
- **Button hold is 80ms**, down from 160. The game reacts earlier in the clip.
  Verified against a real Game Boy cartridge in CI — a five-frame press still
  walks exactly one tile — but nothing had been measured below 100ms before,
  so if a console starts dropping presses, `[p]retroset hold 160` is the value
  it was. See the note above `DEFAULT_HOLD_MS` in `retro/timing.py`.
- The repeat button's thresholds moved with the hold: it now appears at a
  **0.26s** clip length rather than 0.46s, and reaches three taps at 0.46s
  rather than 0.73s.

### Added

- **`[p]retrodiagnose`** — what this install can *actually do*, as opposed to
  what `[p]retroset settings` says it is configured to do: the libraries really
  present, whether this libretro.py can be driven at all, each core, storage
  against its budget, and what is running. `[p]retrodiagnose true` loads every
  core as well. Plain text, for pasting into a bug report.
- **Slash commands.** `/retro play game:` autocompletes over the saved games,
  which is how you find out what a bot can play without running a second
  command first. `/retroset coreoptions` autocompletes the core, its option
  keys, and the values that option accepts.
- **A dropdown of saved games** on `[p]retro list` and on a bare `[p]retro` in
  a channel with nothing running. Picking one re-runs the ordinary command, so
  the cooldowns and checks are charged to whoever clicked.
- **Undo survives a restart.** The history is kept on disk beside the save
  state instead of in memory only, so a message that has outlived a restart
  still undoes. Read lazily, and wiped with the progress it describes.

### Fixed

- **A press whose clip never appeared.** If the click's acknowledgement missed
  Discord's three-second window, every edit through that token failed and the
  clip was dropped — the game had moved and the channel was still looking at
  the previous picture. The edit now falls back to the message itself, which
  does not depend on the interaction token.
- **A half-failed data migration no longer records itself as done.** One entry
  that could not be moved (a locked file, a permissions problem) used to strand
  that entry under the old namespace permanently. It is retried on the next
  load instead.
- **`min_python_version` is declared**, so Downloader refuses an install that
  cannot work rather than failing at runtime on every button press.

### Documentation

- Split by audience: [retro/README.md](retro/README.md) is the manual and is
  73% shorter; [docs/DESIGN.md](docs/DESIGN.md) is the design journal — the
  decisions, the measurements, and the attempts that were reversed.
- A command reference, a troubleshooting section, and an expanded
  [Upgrading](retro/README.md#upgrading).
- A screenshot, and a copyright notice.

### Requirements

- **Python 3.11 is required.** It is the only version a Red bot can run this
  on: Red is `>=3.8.1,<3.12`, and libretro.py needs `>=3.12` from 0.8.0
  onwards, so 0.6.0 is the newest a real install can have and it needs 3.11.

## 1.2.0 and earlier

Not recorded. `git log` is the history before this file existed.
