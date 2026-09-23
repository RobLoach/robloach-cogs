"""
Where the cog keeps things, and how much of the disk it may use.

One directory per cog class name (Red decides that; see retro/migration.py),
with a folder inside it for each kind of file: ``cores/``, ``roms/``,
``states/`` (save states and battery saves, plus one previous generation of
each) and ``system/`` (the libretro system directory, i.e. BIOS files).

Everything in here is either a path, a read, an atomic write, or a deletion.
Nothing in here talks to Discord.

Every one of those writes and deletions also keeps the running total the disk
budget is judged against up to date, because the alternative -- measuring the
whole directory again -- is a full recursive walk, and the budget is checked
on every single download. See "Measuring without walking" below.
"""

import asyncio
import logging
import os
import re
import threading
import time
import typing
from pathlib import Path

from redbot.core.data_manager import cog_data_path

from . import archives
from .abc import MixinMeta

log = logging.getLogger("red.robloach.retro")

# Cached ROMs (and their save states) are keyed by game, so a channel can
# switch between games and keep each one's progress. This caps how many
# cached ROMs a single channel keeps on disk -- the ROMs only, never the
# saves; see _prune_cached_games.
MAX_CACHED_GAMES_PER_CHANNEL = 5

# -- The disk budget -----------------------------------------------------------
#
# Everything this cog stores lives under one directory: the cores, the cached
# ROMs (up to 32 MiB each, five per channel), the save states and battery
# saves with one previous generation each, and any BIOS files the owner has
# installed. The per-channel ROM cache bounds one channel; nothing bounded the
# total, so a bot in fifty channels had no ceiling at all.
#
# The cores are the fixed cost and they are bigger on disk than the download
# suggests: the seven of them are about 4.5 MiB of zips from the buildbot and
# 31 MiB unpacked (measured on linux/x86_64, 2026-09; genesis_plus_gx alone is
# 12 MiB). That is what this budget has to account for, so it is the unpacked
# figure quoted here and in `[p]retroset download`.
#
# 1 GiB is the default: about thirty times the cores, room for something like
# a hundred ordinary cartridges with their saves, and small enough that a VPS
# with a 20 GB disk cannot be filled by a Discord channel. The owner can
# change it, and 0 means "no limit" for somebody who would rather watch it
# themselves.
DEFAULT_DISK_BUDGET_MB = 1024
# A ceiling on the setting itself, so a typo cannot ask for an exabyte.
MAX_DISK_BUDGET_MB = 1024 * 1024

# -- Measuring without walking -------------------------------------------------
#
# The budget needs one number: how many bytes the data directory holds. The
# honest way to get that number is _data_usage -- rglob the tree and stat
# every file -- and _make_room used to ask for it on every incoming download
# and every BIOS install. That is O(everything the cog has ever stored), it
# grows with the number of channels (five cached ROMs and four save files
# each), and it lands on the path a player is waiting on: starting a game.
# On a networked data directory it is the difference between a game starting
# and a game seeming not to.
#
# So the total is kept as a running number instead. Every write and every
# delete this module performs adjusts it (_note_usage_change), which is free
# -- those calls already know how many bytes they moved -- and _make_room
# then answers from memory.
#
# The catch is that this module is not the only thing that can change the
# directory. An owner can drop a core in by hand, delete a ROM with `rm`, or
# restore a backup over the top of it; other modules in this cog delete saves
# without coming through here (see _note_usage_change for the list). So the
# cached number is treated as an estimate that must never be the reason a
# download is refused, with two rules that bound how wrong it may be:
#
#   * **the margin.** The cache is only trusted for decisions that are
#     comfortably inside the budget. The moment `cached + incoming` comes
#     within USAGE_CACHE_MARGIN of the limit, the directory is measured for
#     real -- so every *refusal*, and every prune, is made on a fresh walk.
#     One ROM's worth of slack: a full-size cached ROM is the largest single
#     thing an ordinary download adds, so this keeps a whole download's worth
#     of room between the last cached decision and the limit.
#   * **the interval.** Even deep inside the budget the cache is thrown away
#     after USAGE_CACHE_SECONDS, so drift from hand-edits has a bounded life
#     rather than lasting until the cog is reloaded. Five minutes: long
#     enough that a channel working through a pile of games pays for one
#     walk rather than twenty, short enough that "I deleted some ROMs by
#     hand" is believed almost immediately.
#
# Which direction of error does that leave? Only downwards, and deliberately:
#
#   * an *over*count (the cache thinks the disk is fuller than it is, which
#     is what every unaccounted deletion elsewhere in the cog produces) can
#     never refuse a download, because a total close enough to the limit to
#     refuse anything is re-measured first. It costs one extra walk.
#   * an *under*count (something appeared out of band) can let the directory
#     creep past the budget, but only by as much as appeared out of band
#     since the last walk, and only for as long as the margin hides it --
#     the next near-the-limit decision or the next interval measures it and
#     the pruner catches up.
#
# That is the right way round: a bot that is 30 MiB over its budget for five
# minutes is a bot nobody notices, while a bot that refuses to start a game
# because of a number it made up is the whole cog broken.
USAGE_CACHE_SECONDS = 300.0
USAGE_CACHE_MARGIN = 32 * 1024 * 1024

# The running total is adjusted from whichever thread did the write --
# _write_atomic and the pruners are handed to asyncio.to_thread all over the
# cog -- and `total += delta` is three bytecodes, so two threads finishing a
# write at once can lose one of them. One module-level lock rather than a
# per-instance one: it is held for a couple of integer operations, there is
# normally exactly one cog, and a lock that has to be created lazily on a
# mixin with no __init__ has a race of its own.
_usage_lock = threading.Lock()

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

# Where `[p]retrosaves rollback` parks the live file while it swaps the two
# generations (see SavesMixin._rollback_saves). Only ever on disk for the
# instants between two renames -- unless the process dies there, which is why
# _sweep_partial_writes knows how to put a stranded one back at the next load.
ROLLBACK_SUFFIX = ".rollback"


class StorageMixin(MixinMeta):
    """The data directory: paths, reads, atomic writes, and the budget."""

    #: Bytes the whole data directory held at the last full measurement, or
    #: None when it has never been measured. Class attributes rather than
    #: __init__ assignments because the mixin has no __init__ of its own (the
    #: same reason CoresMixin._cores_scan_cache is one); the first write
    #: creates the instance attribute, so two cogs in one process -- which
    #: the tests build -- never share a total.
    _usage_cached_total: typing.Optional[int] = None
    #: ``time.monotonic()`` when that measurement was taken. Monotonic, not
    #: wall clock: a bot that runs for months will see the system clock
    #: stepped, and a cache that believes it is from the future never expires.
    _usage_cached_at: float = 0.0
    #: Every byte this module has been told it *added*, summed and never
    #: reset. Only the difference between two readings is ever used, to catch
    #: writes that land while a measurement is walking the tree; see
    #: _data_usage. Deletions are deliberately not in here -- see there.
    _usage_added: int = 0

    # -- Files --------------------------------------------------------------

    def _data_dir(self, name: str) -> Path:
        """
        The folder for one kind of file, made the first time it is asked for.

        This sits under every path helper, so it is on the hottest paths the
        cog has -- every press that autosaves builds four save paths through
        it. It used to ``mkdir(parents=True, exist_ok=True)`` on every call
        (and ``cog_data_path`` mkdirs the root on every call of its own),
        which is a fistful of syscalls per path for directories that exist
        for the life of the install; the paths already created are remembered
        on the instance instead, so the common case is a dictionary lookup.

        The memo can lie: a test, or somebody tidying the disk, can delete a
        directory the instance remembers creating. That must never cost a
        save, so :meth:`_write_atomic` re-makes the parent directory itself
        as its own safety net -- on the write path only, where one extra
        syscall is noise. The read paths already treat a missing directory
        as "nothing there" everywhere they look.
        """
        try:
            dirs = self._data_dirs
        except AttributeError:
            dirs = self._data_dirs = {}
        path = dirs.get(name)
        if path is None:
            path = cog_data_path(self) / name
            path.mkdir(parents=True, exist_ok=True)
            dirs[name] = path
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

    def _write_atomic(self, path: Path, data: bytes, keep_backup: bool = False) -> None:
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

        ``keep_backup`` is also what decides whether the write is *flushed*
        to the platter before the rename. It is the saves that ask for it and
        only the saves that are worth it: a save state is the one file here
        that exists nowhere else -- a ROM re-downloads, a core re-installs,
        but the exact moment a channel had reached does not come back -- and a
        state is a few hundred kilobytes, written every
        SAVE_STATE_EVERY_PRESSES presses and when a game sleeps, against a
        clip that costs a second of emulation to encode. The directory entry
        is deliberately *not* flushed as well: doubling the syncs would buy
        only the difference between "the rename is durable now" and "the
        rename is durable at the next commit", and either way the file at
        ``path`` is a whole save state rather than half of one.

        The temporary file is removed on every way out that is not a
        successful rename. Nothing else can: every pruner in this cog skips a
        ``.tmp`` (it is not a game and not a save), while ``_data_usage``
        counts one -- so a 32 MiB ROM write that died on a full disk used to
        take 32 MiB of the disk budget with it, permanently, and the only cure
        was somebody deleting the file by hand. See _sweep_partial_writes for
        the ones left behind by a process that was killed outright.

        This is also where most of the cog's bytes enter the data directory:
        Retro.py writes cached ROMs and saves through it, saves.py writes
        imported ones, cores.py writes the cores. So it is where the disk
        budget's running total is kept up to date, and every one of those
        callers gets that for free -- which is why this is an instance method
        rather than the classmethod it used to be. Only a *successful* write
        counts: the temporary file is transient, and the accounting is the
        difference the directory is actually left holding, which with
        ``keep_backup`` is the new file minus the generation that just fell
        off the end rather than the whole write.
        """
        temporary = path.with_suffix(path.suffix + ".tmp")
        # Both stats before anything moves, because after the renames there
        # is no way to ask what was there. Two stats against a recursive
        # walk of the entire data directory is the trade this is making.
        live_before = self._file_size(path)
        backup_before = self._file_size(self._backup_path(path)) if keep_backup else 0
        rotated = False
        try:
            # The one place the directory is guaranteed rather than assumed:
            # _data_dir memoizes what it has created, and a directory deleted
            # out from under a running cog must cost a couple of syscalls
            # here, on the write path, rather than the save.
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "wb") as handle:
                handle.write(data)
                if keep_backup:
                    handle.flush()
                    os.fsync(handle.fileno())
            if keep_backup:
                try:
                    # replace(), not a copy: a rename is atomic and costs
                    # nothing, so the previous generation is never
                    # half-written either.
                    path.replace(self._backup_path(path))
                    rotated = True
                except FileNotFoundError:
                    # Nothing to rotate. The first save of a new game.
                    pass
            temporary.replace(path)
            # What the directory holds now, minus what it held before. With a
            # rotation the old live file survives as the backup and the old
            # backup is the thing that went; without one the file at `path`
            # was simply overwritten. `rotated` rather than "the live file had
            # a size", so a zero-byte save is accounted for like any other.
            after = len(data) + (live_before if rotated else backup_before)
            self._note_usage_change(after - (live_before + backup_before))
        except BaseException:
            # BaseException, not OSError: a cancelled task unwinding through
            # here leaves exactly the same orphan behind as a failed write,
            # and the orphan is the thing being cleaned up.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Could not clean up the partial write %s; it will count "
                    "against the disk budget until the next cog load.",
                    temporary,
                    exc_info=True,
                )
            raise

    def _sweep_partial_writes(self) -> int:
        """
        Clean up after writes a killed process left half-done. Blocking.

        Returns how many bytes were reclaimed. _write_atomic cleans up after
        itself now, but it cannot clean up after ``SIGKILL``, a power cut, or
        any version of this cog that shipped before it did -- and an orphan
        is invisible to everything except the disk budget, which counts it.
        So the data directory is swept once, at load.

        Two kinds of leftover, with opposite cures:

        * a ``.tmp`` is deleted. It is never a file anybody can use: not a
          playable ROM (_cached_rom skips it), not a save (_stored_slugs and
          both pruners skip it), and never read back by anything.
        * a ``.rollback`` is *recovered* -- see :meth:`_recover_rollback`.
          It is a real save that `[p]retrosaves rollback` parked between two
          renames, and if the process died there it may be the only copy of
          the channel's newest progress, so deleting it would be the sweep
          destroying the very thing the swap was built to protect.

        Load is also the one moment when no write of this cog's can be in
        flight, so nothing living can be touched out from under itself. It is
        for that reason the right moment to take the disk budget's baseline
        measurement too, and this is the only walk of the whole directory the
        cog makes as a matter of course -- so the sweep finishes by
        remeasuring, which costs one more walk at load and buys the running
        total a starting point that owes nothing to whatever happened while
        the bot was off. (The namespace migration moves files in just before
        this runs; measuring here rather than earlier is what makes those
        bytes count.)

        Never raises: it is on the cog load path and a cog that will not load
        is worse than a few megabytes nobody can account for.
        """
        freed = 0
        found = 0
        try:
            root = cog_data_path(self)
            orphans = list(root.rglob("*.tmp"))
            stranded = list(root.rglob("*" + ROLLBACK_SUFFIX))
        except OSError:
            log.warning("Could not sweep the Retro data directory.", exc_info=True)
            return 0
        for path in stranded:
            reclaimed = self._recover_rollback(path)
            if reclaimed:
                freed += reclaimed
                found += 1
        for path in orphans:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = int(path.stat().st_size)
                path.unlink()
            except OSError:
                log.warning(
                    "Could not delete the abandoned partial write %s",
                    path,
                    exc_info=True,
                )
                continue
            freed += size
            self._note_usage_change(-size)
            found += 1
        if found:
            log.info(
                "Reclaimed %s from %s abandoned partial write(s) left by an "
                "earlier run.",
                self._humanize_bytes(freed),
                found,
            )
        # The baseline, after the deletions above rather than before them.
        # _measure_usage never raises either, so this keeps the promise in
        # the docstring: whatever the disk says, the cog still loads.
        self._measure_usage()
        return freed

    def _recover_rollback(self, path: Path) -> int:
        """
        Put one stranded ``.rollback`` file back into its save set. Blocking.

        ``SavesMixin._rollback_saves`` swaps a live save with its previous
        generation through three renames: (1) live -> ``<name>.rollback``,
        (2) ``.bak`` -> live, (3) ``<name>.rollback`` -> ``.bak``. Each
        rename is atomic, so a crash can only land *between* them, and each
        gap identifies itself at the next load by which ordinary slot is
        empty:

        * **after (1)**: the live slot is empty and the spare holds the
          *newest* generation. It goes back to the live slot -- the rollback
          never happened, which is the honest reading of a swap that did not
          finish, and nothing is lost.
        * **after (2)**: the backup slot is empty; the swap has effectively
          happened and the spare holds what used to be live. It goes into
          the backup slot, finishing the swap, which is what the crashed
          command was told it did.
        * **both slots occupied**: no crash point of the swap leaves this
          shape, so the spare is a leftover from before this recovery
          existed, orphaned and then played past -- both generations it
          could have belonged to have been written since. It is deleted,
          and those are the only bytes this returns as reclaimed.

        Whichever gap it was, both generations survive; until this runs the
        worst a boot sees is one missing slot, which the restore chain
        answers by falling through to the next thing it has. Never raises,
        for the same reason as the sweep around it.
        """
        try:
            if path.is_symlink() or not path.is_file():
                return 0
            name = path.name[: -len(ROLLBACK_SUFFIX)]
            if not name:
                # A file literally called ".rollback" belongs to nothing the
                # cog wrote; leave it rather than crash the load sweep on it.
                return 0
            live = path.with_name(name)
            backup = self._backup_path(live)
            if not live.exists():
                path.replace(live)
                log.info("Recovered %s from an interrupted rollback.", live)
                return 0
            if not backup.exists():
                path.replace(backup)
                log.info(
                    "Finished an interrupted rollback: %s is the previous "
                    "generation again.",
                    backup,
                )
                return 0
            size = int(path.stat().st_size)
            path.unlink()
            self._note_usage_change(-size)
            return size
        except OSError:
            log.warning(
                "Could not recover the stranded rollback file %s",
                path,
                exc_info=True,
            )
            return 0

    def _read_file(self, path: Path) -> typing.Optional[bytes]:
        """The contents of one save file, or None if it is not usable."""
        try:
            if not path.is_file():
                return None
            return path.read_bytes() or None
        except OSError:
            log.warning("Could not read the Retro save file %s", path, exc_info=True)
            return None

    @staticmethod
    def _file_size(path: Path) -> int:
        """
        How many bytes a file that may not be there holds. Never raises.

        A missing file and an unreadable one both measure zero, because every
        caller is asking the same question -- "how much of the disk budget
        does this account for" -- and the answer for something that cannot be
        stat()ed is "nothing we can prove".
        """
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    def _discard(self, path: Path) -> None:
        """Delete a save file that no core will load. Never raises."""
        size = self._file_size(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete the unusable save %s", path, exc_info=True)
            return
        self._note_usage_change(-size)

    @staticmethod
    def _humanize_bytes(size: typing.Optional[int]) -> str:
        """``131072`` -> ``128.0 KiB``. Exact bytes for the small ones."""
        if not size:
            return "0 bytes"
        if size < 1024:
            return f"{size:,} bytes"
        for unit in ("KiB", "MiB"):
            size /= 1024.0
            if size < 1024:
                return f"{size:,.1f} {unit}"
        return f"{size / 1024.0:,.1f} GiB"

    def _prune_cached_games(
        self, channel_id: int, keep_slug: str
    ) -> typing.List[str]:
        """
        Drop the oldest cached ROMs for a channel, keeping every save.

        Files are keyed by slug so every game a channel plays keeps its own
        save and can be resumed later, but a channel that works through a
        pile of ROMs should not keep them all forever.

        This is the count-based half of the cache policy: the
        MAX_CACHED_GAMES_PER_CHANNEL most recently played games in a channel
        keep their cached ROM, and a game that falls off the end loses the
        ROM *only*. Its save state and battery save (and the previous
        generation of each) stay exactly where they are -- the same rule the
        disk budget's pruner follows (_prune_roms_for_budget), and the same
        promise the cog makes out loud everywhere it mentions making room: a
        ROM re-downloads, a save does not come back, so no save is ever
        deleted to make room. Starting the game again by name or URL picks
        the saves straight back up. (This pruner used to take all four save
        files with it, which quietly destroyed exactly the progress that
        messaging said was safe.)

        Returns the ROM filenames that were deleted, so the caller can drop
        the session and Resume-button records that pointed at them; see
        ``Retro._forget_pruned_roms``. The records still go even though the
        saves stay: resuming is impossible without the ROM, and a record
        whose cached ROM has gone can only apologise when it is clicked.
        """
        deleted: typing.List[str] = []
        try:
            roms = list(self._roms_dir().glob(f"{channel_id}-*"))
        except OSError:
            return deleted
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
            size = self._file_size(path)
            try:
                # The ROM and nothing else. The game's four save files are
                # deliberately left where they are; see the docstring.
                path.unlink(missing_ok=True)
                deleted.append(path.name)
            except OSError:
                log.warning("Could not prune the cached ROM %s", path, exc_info=True)
                continue
            self._note_usage_change(-size)
        return deleted

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
    #     would be a budget that could be walked past. (Measure, yes; walk
    #     the whole directory on every download, no -- the total is kept
    #     running instead, and the rules for when it is trusted are under
    #     "Measuring without walking" at the top of this file.)
    #   * when it is full, prune *cached ROMs* and nothing else. A ROM is
    #     re-downloadable and a save is not, so a save is never deleted to
    #     make room for somebody else's download. If pruning ROMs is not
    #     enough, the download is refused and says so.

    def _note_usage_change(self, delta: int) -> None:
        """
        Tell the running total that this module just moved ``delta`` bytes.

        Positive for bytes written, negative for bytes deleted, and cheap on
        purpose: every caller already has the number in its hand, and the
        only other way to learn it is to walk the whole directory again.

        Nothing happens until the directory has been measured once. There is
        no number to adjust before that, and building one out of the deltas
        alone would start a bot that already has 31 MiB of cores on disk at
        "zero bytes used" -- an *under*count, which is the direction that can
        actually overrun the budget.

        What this cannot see is anything the cog does outside this module:
        `[p]retrosaves delete` and the save-import path unlink files in
        retro/saves.py, `[p]retroset bios remove` unlinks one in
        retro/Retro.py, and an owner with a shell can do whatever they like.
        Every one of those cog paths is a *deletion*, so they leave the total
        too high, which the near-the-limit rescan turns into one extra walk
        rather than a wrong answer. Additions from outside -- a core dropped
        in by hand -- are what the staleness interval is for. The reasoning
        for both is under "Measuring without walking" at the top of this file.
        """
        if not delta:
            return
        with _usage_lock:
            if delta > 0:
                # Additions only: see the comment in _data_usage about what
                # this reading is used for.
                self._usage_added += delta
            total = self._usage_cached_total
            if total is not None:
                # Clamped at zero: deleting a file nobody counted (one that
                # arrived before the last measurement was taken, say) must
                # not be able to drive the total negative and make the cog
                # think it has room it does not have.
                self._usage_cached_total = max(0, total + delta)

    def _data_usage(self) -> typing.Dict[str, int]:
        """
        How many bytes each part of the data directory holds. Blocking.

        Keyed by the top-level folder (``roms``, ``states``, ``cores``,
        ``system``) with anything loose at the root under ``other``, plus a
        ``total``. Symlinks are counted as nothing rather than followed:
        nothing here creates one, and following one would let a link into
        somebody's home directory look like the cog's own usage.

        This is the one place the tree is really walked, so it is also where
        the running total is refreshed -- which means the owner-facing usage
        report (``Retro._usage_report``) pays off any accumulated drift as a
        side effect of somebody asking what the bot is using.
        """
        root = cog_data_path(self)
        usage: typing.Dict[str, int] = {"total": 0}
        # Read before the walk and again after it. A write that finishes
        # while the walk is in progress may or may not have been seen by it,
        # so the bytes added meanwhile are added back on the way out: that
        # counts such a write once or twice, never zero times. Twice is an
        # overcount, which costs one extra walk and cannot refuse anybody's
        # download; missing it altogether would be an undercount, which is
        # the one thing the budget cannot afford. Deletions are left out of
        # that reading for the mirror-image reason -- subtracting a file the
        # walk had already skipped would undercount.
        added = self._usage_added
        try:
            entries = list(root.rglob("*"))
        except OSError:
            log.warning("Could not measure the Retro data directory.", exc_info=True)
            # Deliberately without touching the cache: a directory that
            # cannot be read says nothing about how full it is, and leaving
            # the last real measurement in place is better than replacing it
            # with a zero that would let every download through.
            return usage
        for path in entries:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = int(path.stat().st_size)
            except OSError:
                continue
            first = path.relative_to(root).parts[0]
            key = "other" if first == path.name else first
            usage[key] = usage.get(key, 0) + size
            usage["total"] += size
        with _usage_lock:
            self._usage_cached_total = usage["total"] + max(0, self._usage_added - added)
            self._usage_cached_at = time.monotonic()
        return usage

    def _measure_usage(self) -> int:
        """
        Walk the directory, refresh the running total, return it. Blocking.

        The never-raises wrapper around :meth:`_data_usage`, for the two
        callers that only want the number and cannot afford an exception:
        the cog load sweep and the budget check.
        """
        try:
            return self._data_usage().get("total", 0)
        except Exception:
            log.exception("Could not measure the Retro data directory.")
            # Zero, which is the answer that lets the download through. A
            # measurement that raises is a broken data directory, and the
            # cog refusing to start games because it cannot count them would
            # be a worse failure than going over the budget.
            return 0

    @staticmethod
    def _usage_margin(budget: int) -> int:
        """
        How much room the cached total is not allowed to decide inside.

        One cached ROM's worth (USAGE_CACHE_MARGIN), so the last decision
        taken on remembered numbers still leaves a whole download's room
        before the limit -- except on a budget small enough that a fixed
        32 MiB would swallow it whole, where a quarter of the budget keeps
        the same shape at a smaller scale. A tiny budget therefore measures
        for real nearly every time, which is exactly right: the closer the
        limit is to the files, the less a remembered number is worth.
        """
        return min(USAGE_CACHE_MARGIN, budget // 4)

    async def _usage_total(self, incoming: int, budget: int) -> int:
        """
        What the directory holds, measured for real only when it matters.

        The running total is trusted when the decision it is about to make is
        comfortably inside the budget and the number is not stale; otherwise
        the directory is walked (in a thread -- it is the one blocking thing
        on the start path). So a refusal, and the prune that precedes it, are
        always made against the real disk.
        """
        total = self._usage_cached_total
        if (
            total is not None
            and time.monotonic() - self._usage_cached_at < USAGE_CACHE_SECONDS
            and total + incoming + self._usage_margin(budget) <= budget
        ):
            return total
        return await asyncio.to_thread(self._measure_usage)

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
            self._note_usage_change(-size)
            deleted.append(name)
        return freed, deleted

    async def _make_room(
        self,
        incoming: int,
        keep_rom: typing.Optional[str] = None,
        *,
        prefix: str = "",
    ) -> typing.Tuple[bool, str]:
        """
        Check the budget, prune cached ROMs if that helps, and report.

        Returns ``(there is room, the sentence to say)``. The sentence is
        worth saying either way: a refusal has to explain itself, and a
        download that only fitted because five cached ROMs were thrown away
        should say so rather than silently deleting other channels' caches.

        ``prefix`` is the bot's real command prefix, for the commands the
        refusal points at. Red only rewrites ``[p]`` in a *docstring*, so a
        sent string has to be given the prefix by whoever is sending it --
        every caller here has a ``ctx`` and passes ``ctx.clean_prefix``. The
        default names the commands without one rather than printing a `[p]`
        nobody can type.
        """
        budget = await self._disk_budget()
        if not budget:
            return True, ""
        # Not a walk of the data directory every time somebody starts a game:
        # the running total answers the easy cases, and anything close enough
        # to the limit to prune or refuse measures the disk for real first.
        # See "Measuring without walking" at the top of this file.
        total = await self._usage_total(incoming, budget)
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
            # The saves for those games stay exactly where they are -- that is
            # this pruner's whole rule -- but the session and Resume-button
            # *pointers* at the deleted files do not: a button that can only
            # apologise is not worth a Config entry for the life of the
            # install. Starting the game again by name re-downloads the ROM
            # and picks the saves straight back up. See
            # ``Retro._forget_pruned_roms``.
            await self._forget_pruned_roms(deleted)
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
            f"with `{prefix}retrosaves delete <game>`, or ask the bot owner "
            f"to raise `{prefix}retroset diskbudget`."
        )
        return False, " ".join(lines)
