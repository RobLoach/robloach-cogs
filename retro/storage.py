"""
Where the cog keeps things, and how much of the disk it may use.

One directory per cog class name (Red decides that; see retro/migration.py),
with a folder inside it for each kind of file: ``cores/``, ``roms/``,
``states/`` (save states and battery saves, plus one previous generation of
each) and ``system/`` (the libretro system directory, i.e. BIOS files).

Everything in here is either a path, a read, an atomic write, or a deletion.
Nothing in here talks to Discord.
"""

import asyncio
import logging
import re
import typing
from pathlib import Path

from redbot.core.data_manager import cog_data_path

from . import archives
from .abc import MixinMeta

log = logging.getLogger("red.robloach.retro")

# Cached ROMs (and their save states) are keyed by game, so a channel can
# switch between games and keep each one's progress. This caps how many of
# them a single channel keeps on disk.
MAX_CACHED_GAMES_PER_CHANNEL = 5

# -- The disk budget -----------------------------------------------------------
#
# Everything this cog stores lives under one directory: the cores (about 4.5 MiB
# for the lot), the cached ROMs (up to 32 MiB each, five per channel), the save
# states and battery saves with one previous generation each, and any BIOS
# files the owner has installed. The per-channel ROM cache bounds one channel;
# nothing bounded the total, so a bot in fifty channels had no ceiling at all.
#
# 1 GiB is the default: a hundred times the cores, room for something like a
# hundred ordinary cartridges with their saves, and small enough that a VPS
# with a 20 GB disk cannot be filled by a Discord channel. The owner can
# change it, and 0 means "no limit" for somebody who would rather watch it
# themselves.
DEFAULT_DISK_BUDGET_MB = 1024
# A ceiling on the setting itself, so a typo cannot ask for an exabyte.
MAX_DISK_BUDGET_MB = 1024 * 1024

# -- The previous generation --------------------------------------------------
#
# An atomic write cannot leave a truncated save behind, but it is perfectly
# capable of writing a *bad* one: a core that has been updated writes a state
# the next build will not read, a game can be saved two frames into a game-over
# screen, and `[p]retrosaves delete` is one confirmation away from a channel's
# only copy. Save states are the thing players actually care about, so every
# successful write rotates the file it replaces to this suffix first, one
# generation deep.
#
# One generation, not five: it doubles the storage for each save (which the
# disk budget above has to account for), and the case it exists for -- "the
# last save is bad, give me the one before it" -- is served by one. Rotation
# is a rename, so it costs nothing and cannot half-happen.
BACKUP_SUFFIX = ".bak"


class StorageMixin(MixinMeta):
    """The data directory: paths, reads, atomic writes, and the budget."""

    # -- Files --------------------------------------------------------------

    def _data_dir(self, name: str) -> Path:
        path = cog_data_path(self) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _roms_dir(self) -> Path:
        return self._data_dir("roms")

    def _system_dir(self) -> Path:
        """
        The folder cores are told to look in for BIOS/firmware files.

        This is what every core gets back from
        ``RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY``. Left to itself libretro.py
        hands each session a throwaway temporary directory, which is deleted
        when the session ends, so nothing could ever be found there.
        """
        return self._data_dir("system")

    def _bios_files(self) -> typing.List[typing.Tuple[str, int]]:
        """
        (relative path, size) for everything in the system directory.

        Walks subfolders, because a firmware set unpacked from a zip keeps the
        folders it had inside it -- some cores look for their firmware in one
        (``dc/dc_boot.bin``) rather than at the root.
        """
        root = self._system_dir()
        found: typing.List[typing.Tuple[str, int]] = []
        try:
            entries = sorted(root.rglob("*"))
        except OSError:
            log.warning("Could not read the Retro system directory.", exc_info=True)
            return []
        for path in entries:
            try:
                if not path.is_file():
                    continue
                found.append((path.relative_to(root).as_posix(), path.stat().st_size))
            except (OSError, ValueError):
                continue
        found.sort(key=lambda entry: entry[0].lower())
        return found

    @staticmethod
    def _bios_name(filename: str) -> typing.Optional[str]:
        """
        A bare, safe filename for the system directory, or None if unusable.

        Cores want an exact filename (``disksys.rom``, ``scph5501.bin``), so
        this validates rather than rewrites: silently turning
        ``../../../.ssh/authorized_keys`` into ``authorized_keys`` would store
        the file under a name the core will never look for, which is worse
        than refusing it.
        """
        name = str(filename).strip().strip('"').strip("'")
        if not name:
            return None
        if "/" in name or "\\" in name or "\x00" in name:
            return None
        if name.startswith("."):
            return None
        if not archives.SAFE_COMPONENT.fullmatch(name):
            return None
        return name

    def _bios_path(self, filename: str) -> typing.Optional[Path]:
        """
        Resolve one entry of ``[p]retroset bios list`` to a path, or None.

        Accepts a subfolder, since the listing shows them, but every component
        is validated and the result is checked against the system directory,
        so nothing typed here can name a file outside it.
        """
        relative = archives.safe_member_path(str(filename).strip().strip('"').strip("'"))
        if relative is None:
            return None
        root = self._system_dir().resolve()
        try:
            target = (root / relative).resolve()
        except OSError:
            return None
        if root not in target.parents:
            return None
        return target

    def _write_bios_files(
        self, files: typing.Sequence["archives.ExtractedFile"]
    ) -> typing.Tuple[typing.List[str], int]:
        """
        Write unpacked firmware into the system directory. Blocking.

        Every path has already been validated by ``archives.safe_member_path``,
        but the result is checked against the system directory once more
        before anything is written: this is the last line between an archive
        and the bot's filesystem, and it costs nothing.
        """
        root = self._system_dir().resolve()
        written: typing.List[str] = []
        total = 0
        for entry in files:
            target = (root / entry.path).resolve()
            if target != root and root not in target.parents:
                log.error(
                    "Refusing to write %r from a BIOS archive: it resolves "
                    "outside the system directory.",
                    entry.member,
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(target, entry.data)
            written.append(entry.path)
            total += len(entry.data)
        return written, total

    def _rom_path(self, rom_filename: str) -> typing.Optional[Path]:
        if not rom_filename:
            return None
        return self._roms_dir() / rom_filename

    def _state_path(self, channel_id: int, slug: str) -> Path:
        return self._data_dir("states") / f"{channel_id}-{slug}.state"

    def _sram_path(self, channel_id: int, slug: str) -> Path:
        """
        Where a game's battery save lives, next to its save state.

        A save state is a snapshot of a particular build of a particular core
        and is rejected outright when that core is updated (see
        RetroEmulator.load_state). SRAM is the cartridge's own battery memory
        in the format the cartridge used, so it survives a core update and can
        be poured into a freshly booted game. ``.srm`` is what RetroArch calls
        these, so a file lifted out of this folder is usable elsewhere.
        """
        return self._data_dir("states") / f"{channel_id}-{slug}.srm"

    @staticmethod
    def _backup_path(path: Path) -> Path:
        """
        Where the generation before this one is kept.

        ``9000-ucity.state`` -> ``9000-ucity.state.bak``, and the same for a
        ``.srm``. Deliberately a suffix on the whole name rather than a
        different extension, so the backup sorts next to the file it belongs
        to and nothing that globs for ``*.state`` can mistake it for a save
        the cog would load on its own.
        """
        return path.with_name(path.name + BACKUP_SUFFIX)

    def _save_paths(
        self, channel_id: int, slug: str
    ) -> typing.Tuple[Path, Path, Path, Path]:
        """``(state, state backup, sram, sram backup)`` for one game."""
        state = self._state_path(channel_id, slug)
        sram = self._sram_path(channel_id, slug)
        return state, self._backup_path(state), sram, self._backup_path(sram)

    @staticmethod
    def _slug(name: str) -> str:
        """A lowercase, filesystem-safe id for a game within a channel."""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()[:48]
        return slug or "game"

    @classmethod
    def _write_atomic(cls, path: Path, data: bytes, keep_backup: bool = False) -> None:
        """
        Replace a file's contents in one step, never leaving half of one.

        Write beside the target and rename, so a crash halfway through cannot
        leave a truncated save state behind.

        ``keep_backup`` additionally rotates whatever was there to
        ``<name>.bak`` first, one generation deep. Both steps are renames
        within one directory, so the only moment either file is missing is
        between two atomic operations, and the restore chain tries the backup
        when the live file is not there (see :func:`RetroView.restore_into`).
        Only the saves ask for this: a cached ROM or a downloaded core is
        re-fetchable and a second copy of it is just disk.
        """
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        if keep_backup:
            try:
                # replace(), not a copy: a rename is atomic and costs nothing,
                # so the previous generation is never half-written either.
                path.replace(cls._backup_path(path))
            except FileNotFoundError:
                # Nothing to rotate. The first save of a new game.
                pass
        temporary.replace(path)

    def _read_file(self, path: Path) -> typing.Optional[bytes]:
        """The contents of one save file, or None if it is not usable."""
        try:
            if not path.is_file():
                return None
            return path.read_bytes() or None
        except OSError:
            log.warning("Could not read the Retro save file %s", path, exc_info=True)
            return None

    def _read_sram(self, channel_id: int, slug: str) -> typing.Optional[bytes]:
        """The stored battery save for a game, or None if there isn't one."""
        return self._read_file(self._sram_path(channel_id, slug))

    @staticmethod
    def _discard(path: Path) -> None:
        """Delete a save file that no core will load. Never raises."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete the unusable save %s", path, exc_info=True)

    @staticmethod
    def _humanize_bytes(size: typing.Optional[int]) -> str:
        """``131072`` -> ``128.0 KiB``. Exact bytes for the small ones."""
        if not size:
            return "0 bytes"
        if size < 1024:
            return f"{size:,} bytes"
        for unit in ("KiB", "MiB", "GiB"):
            size /= 1024.0
            if size < 1024 or unit == "GiB":
                return f"{size:,.1f} {unit}"
        return f"{size:,.1f} GiB"  # pragma: no cover - the loop always returns

    def _prune_cached_games(self, channel_id: int, keep_slug: str) -> None:
        """
        Drop the oldest cached ROM+save sets for a channel.

        Files are keyed by slug so every game a channel plays keeps its own
        save and can be resumed later, but a channel that works through a
        pile of ROMs should not keep them all forever.

        This is the count-based cache policy the cog has always had: the
        MAX_CACHED_GAMES_PER_CHANNEL most recent games in a channel are kept
        whole, and a game that falls off the end takes its saves (and their
        previous generations) with it, so no battery save outlives the ROM it
        belongs to. The *disk budget* below is the other, separate limit, and
        it deliberately never touches a save -- see _prune_roms_for_budget.
        """
        try:
            roms = list(self._roms_dir().glob(f"{channel_id}-*"))
        except OSError:
            return
        entries = []
        for path in roms:
            # The leftovers of an interrupted _write_atomic are not games.
            if path.suffix == ".tmp":
                continue
            try:
                entries.append((path.stat().st_mtime, path.name, path))
            except OSError:
                continue
        # Newest first. The name is in the key so two ROMs written in the same
        # filesystem tick (which happens on a coarse mtime) order predictably
        # rather than by comparing Paths.
        entries.sort(reverse=True)
        prefix = f"{channel_id}-"
        seen = 0
        for _, _, path in entries:
            slug = path.stem[len(prefix):]
            if slug == keep_slug:
                continue
            seen += 1
            if seen < MAX_CACHED_GAMES_PER_CHANNEL:
                continue
            try:
                path.unlink(missing_ok=True)
                for save in self._save_paths(channel_id, slug):
                    save.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not prune the cached ROM %s", path, exc_info=True)

    # -- The disk budget ----------------------------------------------------
    #
    # One number for the whole data directory, because that is the thing that
    # can fill a disk: the per-channel ROM cache bounds one channel, and a bot
    # is in as many channels as it is invited to.
    #
    # Two rules, and the second one is the important one:
    #
    #   * measure everything, including the cores, the BIOS files and the
    #     previous generation of every save. A budget that only counted ROMs
    #     would be a budget that could be walked past.
    #   * when it is full, prune *cached ROMs* and nothing else. A ROM is
    #     re-downloadable and a save is not, so a save is never deleted to
    #     make room for somebody else's download. If pruning ROMs is not
    #     enough, the download is refused and says so.

    def _data_usage(self) -> typing.Dict[str, int]:
        """
        How many bytes each part of the data directory holds. Blocking.

        Keyed by the top-level folder (``roms``, ``states``, ``cores``,
        ``system``) with anything loose at the root under ``other``, plus a
        ``total``. Symlinks are counted as nothing rather than followed:
        nothing here creates one, and following one would let a link into
        somebody's home directory look like the cog's own usage.
        """
        root = cog_data_path(self)
        usage: typing.Dict[str, int] = {"total": 0}
        try:
            entries = list(root.rglob("*"))
        except OSError:
            log.warning("Could not measure the Retro data directory.", exc_info=True)
            return usage
        for path in entries:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = int(path.stat().st_size)
            except OSError:
                continue
            try:
                first = path.relative_to(root).parts[0]
            except ValueError:  # pragma: no cover - rglob stays under root
                first = "other"
            key = "other" if first == path.name else first
            usage[key] = usage.get(key, 0) + size
            usage["total"] += size
        return usage

    async def _disk_budget(self) -> int:
        """The ceiling on the whole data directory in bytes; 0 is no limit."""
        try:
            megabytes = int(await self.config.disk_budget_mb())
        except Exception:
            log.exception("Could not read the Retro disk budget.")
            megabytes = DEFAULT_DISK_BUDGET_MB
        return max(0, min(MAX_DISK_BUDGET_MB, megabytes)) * 1024 * 1024

    def _protected_roms(self) -> typing.Set[str]:
        """
        The cached ROMs the budget may not prune: the ones in play.

        A live or merely sleeping session wakes by re-reading its cached ROM,
        so pruning one out from under a channel would break a game that is on
        screen right now. Everything else is fair game -- its saves stay
        exactly where they are, and starting it again by name or URL picks
        them straight back up.
        """
        return {
            view.rom_filename
            for view in self.sessions.values()
            if view.rom_filename
        }

    def _prune_roms_for_budget(
        self, need: int, keep: typing.Set[str]
    ) -> typing.Tuple[int, typing.List[str]]:
        """
        Delete cached ROMs, oldest first, until ``need`` bytes are free.

        Blocking. Returns (bytes freed, names deleted). Touches nothing but
        ``roms/``: no save state, no battery save, no backup of either. The
        oldest ROM across every channel goes first, which is the same "least
        recently played wins" rule the per-channel cache uses.
        """
        freed = 0
        deleted: typing.List[str] = []
        try:
            entries = []
            for path in self._roms_dir().iterdir():
                if path.name in keep or path.suffix == ".tmp":
                    continue
                try:
                    if not path.is_file():
                        continue
                    entries.append((path.stat().st_mtime, path.name, path))
                except OSError:
                    continue
        except OSError:
            log.warning("Could not read the ROM cache to prune it.", exc_info=True)
            return 0, []
        entries.sort()
        for _, name, path in entries:
            if freed >= need:
                break
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                log.warning("Could not prune the cached ROM %s", path, exc_info=True)
                continue
            freed += size
            deleted.append(name)
        return freed, deleted

    async def _make_room(
        self, incoming: int, keep_rom: typing.Optional[str] = None
    ) -> typing.Tuple[bool, str]:
        """
        Check the budget, prune cached ROMs if that helps, and report.

        Returns ``(there is room, the sentence to say)``. The sentence is
        worth saying either way: a refusal has to explain itself, and a
        download that only fitted because five cached ROMs were thrown away
        should say so rather than silently deleting other channels' caches.
        """
        budget = await self._disk_budget()
        if not budget:
            return True, ""
        usage = await asyncio.to_thread(self._data_usage)
        total = usage.get("total", 0)
        if total + incoming <= budget:
            return True, ""
        keep = self._protected_roms()
        if keep_rom:
            keep.add(keep_rom)
        freed, deleted = await asyncio.to_thread(
            self._prune_roms_for_budget, total + incoming - budget, keep
        )
        if deleted:
            log.info(
                "Pruned %s cached ROM(s) (%s bytes) to stay inside the %s MiB "
                "Retro disk budget.",
                len(deleted),
                freed,
                budget // (1024 * 1024),
            )
        if total - freed + incoming <= budget:
            if not deleted:
                return True, ""
            return True, (
                f"Freed {self._humanize_bytes(freed)} by dropping "
                f"{len(deleted)} cached ROM(s) that nobody is playing, to stay "
                f"inside the bot's {self._humanize_bytes(budget)} storage "
                "budget. Nothing anyone had saved was touched \N{EM DASH} "
                "those games will re-download themselves when somebody starts "
                "them again."
            )
        lines = [
            "The bot has run out of room for that: its game storage is "
            f"{self._humanize_bytes(total - freed)} of the "
            f"{self._humanize_bytes(budget)} it is allowed, and this needs "
            f"another {self._humanize_bytes(incoming)}."
        ]
        if deleted:
            lines.append(
                f"{len(deleted)} cached ROM(s) were dropped to try "
                f"({self._humanize_bytes(freed)}) and it still does not fit."
            )
        lines.append(
            "No save was deleted to make room and none will be. Free some up "
            "with `[p]retrosaves delete <game>`, or ask the bot owner to raise "
            "`[p]retroset diskbudget`."
        )
        return False, " ".join(lines)
