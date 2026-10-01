# Changelog

Versions are the one in `retro/info.json`, which `[p]retroset version` reports.
Upgrading: `[p]repo update robloach-cogs`, `[p]cog update retro`,
`[p]reload retro` — **the reload is not optional**.

## 1.3.0

### Changed defaults — these affect existing installs

- **Clip length 1.6s**, up from 1. `[p]retroset cliplength`.
- **Button hold 80ms**, down from 160. The game reacts earlier in the clip.
  Verified against a real Game Boy cartridge in CI, but nothing had been
  measured below 100ms before — if a console starts dropping presses,
  `[p]retroset hold 160` is what it was.
- The ×3 button now appears at a 0.26s clip length rather than 0.46s.

### Added

- **`[p]retrodiagnose`** — what this install can actually do, as opposed to
  what it is configured to do. Plain text, for pasting into a bug report.
- **Slash commands.** `/retro play game:` autocompletes over the saved games;
  `/retroset coreoptions` autocompletes the core, its keys and its values.
- **A dropdown of saved games** on `[p]retro list`. Picking one re-runs the
  ordinary command, so checks and cooldowns land on whoever clicked.
- **Timings in `[p]retrodiagnose`** — where presses spend their time, and how
  late the event loop is waking up. The second answers "is it the bot or the
  network?" when clicks fail.
- **Undo survives a restart** — the history is kept on disk, read lazily, and
  wiped with the progress it describes.

- **The press queue scales with the clip length.** It was a flat five however
  long clips were, so raising the default to 1.6s silently raised the wait for
  the last queued press from five seconds to eight. It is a time budget now —
  about five seconds whatever the setting.

- **The ×3 button repeats your last press**, not just the confirm button —
  `⬅️` then `⬅️ ×3` walks four tiles. It starts on confirm, and Wait, Undo and
  a reboot do not move it.

### Fixed

- **A press whose clip never appeared.** If the click's acknowledgement missed
  Discord's three-second window every edit through that token failed and the
  clip was dropped. It now falls back to editing the message directly.
- **A half-failed migration no longer records itself as done**, so an entry
  that could not be moved is retried instead of stranded.
- **`min_python_version` is declared**, so Downloader refuses an install that
  cannot work rather than failing on every button press.

### Requirements

- **Python 3.11**, the only version Red (`<3.12`) and libretro.py (`>=3.12`
  from 0.8.0) overlap on.

### Documentation

Split by audience: [the manual](retro/README.md), and
[docs/DESIGN.md](docs/DESIGN.md) for the reasoning, and
[docs/HISTORY.md](docs/HISTORY.md) for what was tried and reversed.

## 1.2.0 and earlier

See `git log`.
