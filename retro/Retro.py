import asyncio
import io
import logging
import platform
import re
import shutil
import sys
import time
import typing
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_list, pagify
from redbot.core.utils.views import SimpleMenu

from . import archives
from .emulator import (
    CLIP_SECONDS,
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    MIN_ROM_SIZE,
    REPLAY_SECONDS,
    EmulatorError,
    RetroEmulator,
    probe_core_options,
)
from .RetroView import (
    BOOT_SECONDS,
    DEFAULT_HOLD_MS,
    DEFAULT_TIMEOUT_MINUTES,
    MAX_HOLD_MS,
    MIN_HOLD_MS,
    SAVE_STATE_EVERY_PRESSES,
    RetiredView,
    RetroView,
)
from .systems import (
    CORES,
    SYSTEMS,
    core_filename,
    core_name_from_filename,
    system_for_core,
    system_for_extension,
)

log = logging.getLogger("red.robloach.retro")

# A ceiling rather than a console limit: the largest cartridge any of these
# cores runs is a 12 MiB Super Nintendo game, and Discord will not let a bot
# attach more than 25 MiB anyway. URL downloads are not bound by Discord, so
# this is what stops a mistyped link pulling down a disc image.
MAX_ROM_SIZE = 32 * 1024 * 1024
MAX_ROM_SIZE_LABEL = "32 MiB"

# Console firmware is small (a Game Boy boot ROM is 256 bytes, a PlayStation
# BIOS 512 KiB, the largest anyone is likely to install a couple of MiB), so
# this is only here to stop a mistyped URL filling the bot's disk.
MAX_BIOS_SIZE = 16 * 1024 * 1024
MAX_BIOS_SIZE_LABEL = "16 MiB"

BUILDBOT = "https://buildbot.libretro.com/nightly"

# Where to point people who want games they are allowed to play.
HOMEBREW_URL = "https://retrobrews.github.io/"

# The worked example in the help text: a complete, open source SimCity-like
# game for the Game Boy Color, MIT licensed, and a direct release download.
EXAMPLE_GAME = "ucity"
EXAMPLE_ROM_URL = (
    "https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc"
)

# How long to wait before the automatic core download is allowed to try
# again. Without it, a cog that is reloaded in a loop (or a core the buildbot
# has stopped publishing) would hit the buildbot on every single load.
AUTO_DOWNLOAD_COOLDOWN_SECONDS = 6 * 60 * 60

# A libretro core is a shared object with process-global state, so two live
# cores quietly corrupt each other's emulation (it segfaults the bot often
# enough to be obvious). Waking a hibernated session costs about 25ms against
# a multi-second clip, so keeping a single core loaded and evicting the least
# recently used one is effectively free and removes the hazard entirely.
MAX_LIVE_EMULATORS = 1

# How often the background task looks for sessions to put to sleep.
IDLE_CHECK_SECONDS = 60

# Cached ROMs (and their save states) are keyed by game, so a channel can
# switch between games and keep each one's progress. This caps how many of
# them a single channel keeps on disk.
MAX_CACHED_GAMES_PER_CHANNEL = 5

# The value that clears a core option override and puts the core's own default
# back. It has to be a word no core uses as a real value, or setting it would
# be ambiguous: "default" is out (mednafen_wswan offers it) and so is "none"
# (snes9x and genesis_plus_gx both use it), but no option in any of the eleven
# cores this cog installs offers "reset".
OPTION_RESET = "reset"

# How many of an option's allowed values one line of `[p]retroset coreoptions
# <core>` shows before it gives up and points at the single-option view. Some
# cores offer sixty-odd palettes on a single setting.
MAX_LISTED_VALUES = 8

# How long the paginated core option listing stays clickable.
OPTION_MENU_TIMEOUT = 180.0

# The class used to be called RetroCog, and Red derives BOTH of this cog's
# storage locations from the class name: Config.get_conf() keys every setting
# and session by type(self).__name__, and cog_data_path() puts the data
# directory under it. Renaming the class to `Retro` therefore moves every
# downloaded core, cached ROM, save state, battery save and BIOS file, and
# every stored setting, out from under the running bot -- unless the old ones
# are brought across, which is what _migrate_legacy_namespace() does.
#
# This constant is the old name and must never change; the migration is keyed
# on it. (The Config *identifier* integer and RetroView.CUSTOM_ID_PREFIX are
# load-bearing for the same reason and are likewise frozen; see their own
# comments.)
LEGACY_COG_NAME = "RetroCog"

# Red's default (JSON) Config driver keeps a cog's stored settings in a file
# called this, *inside* that cog's own data directory -- so the data directory
# and the Config namespace are not two separate places on disk after all. It is
# therefore excluded from the file move below and migrated by reading it
# through a Config handle instead, which is the only way that also works for
# the Postgres driver, where no such file exists.
CONFIG_STORE_FILENAME = "settings.json"

# How many retired messages one channel keeps a working Resume button on.
# Matches MAX_CACHED_GAMES_PER_CHANNEL, because a Resume button for a game
# whose ROM has been pruned can only apologise.
MAX_RETIRED_PER_CHANNEL = MAX_CACHED_GAMES_PER_CHANNEL

# Firmware sets are distributed as a zip of several files, sometimes with a
# folder per console. These cap what one `[p]retroset bios add` will unpack:
# the archive itself, any single file inside it, everything inside it
# together, and how many files that may be.
MAX_BIOS_ARCHIVE_SIZE = 64 * 1024 * 1024
MAX_BIOS_ARCHIVE_SIZE_LABEL = "64 MiB"
MAX_BIOS_TOTAL_SIZE = 64 * 1024 * 1024
MAX_BIOS_FILES = 250

# How many of the installed files `[p]retroset bios add` names in its reply
# before it stops listing and starts counting.
MAX_LISTED_BIOS_FILES = 12

#: The cog's global settings and their defaults. A module constant rather than
#: a literal inside register_global() because the migration below has to be
#: able to tell an untouched configuration from a real one.
DEFAULT_GLOBALS: typing.Dict[str, typing.Any] = {
    # Kept only so an install from before multi-console support can be
    # migrated into `cores` on load; nothing reads it afterwards.
    "core_path": "",
    # core name -> path on disk, e.g. {"gambatte": "/.../gambatte_libretro.so"}
    # Only needed for a core outside the managed directory now: cores that
    # live in it are found by scanning, so this is a record rather than the
    # source of truth. See _installed_cores().
    "cores": {},
    "session_timeout_minutes": DEFAULT_TIMEOUT_MINUTES,
    "clip_seconds": CLIP_SECONDS,
    "hold_ms": DEFAULT_HOLD_MS,
    "games": {},
    # Fetch whatever cores are missing shortly after the cog loads, so a fresh
    # install can play something without the owner having to find
    # `[p]retroset download` first.
    "auto_download_cores": True,
    # When that last ran, so a reload loop cannot hammer the buildbot.
    "auto_download_attempted_at": 0.0,
    # core name -> {option key: value}: the owner's overrides, applied before
    # the core initialises every time it is loaded.
    # e.g. {"gambatte": {"gambatte_gb_colorization": "GBC"}}
    "core_options": {},
    # core name -> {option key: {"desc", "info", "default", "values"}}: what
    # each core has told us about its own settings, so they can be listed
    # without loading that core. Filled in by the ROM-less probe and extended
    # every time a real session starts, which is how a core like FCEUmm --
    # which registers nothing until a game is loaded -- ever becomes listable.
    # See _definitions_for().
    "core_option_definitions": {},
    # Set once the RetroCog -> Retro move has been done, so it never runs a
    # second time and cannot undo a later change by copying stale data over it.
    "legacy_namespace_migrated": False,
}

#: The per-channel settings and their defaults.
DEFAULT_CHANNEL: typing.Dict[str, typing.Any] = {
    # A session outlives its emulator, so the record of one lives here and is
    # reloaded when the cog (or the whole bot) starts again.
    "session": None,
    # str(message_id) -> session record, for messages whose game has been
    # replaced by another one. They keep a Resume button, which has to keep
    # working across a restart, so what it needs to restart the game is stored
    # rather than held in memory. See _retire() and resume_retired().
    "retired": {},
}


class DownloadError(RuntimeError):
    """A download failed for a reason the person who asked should be told."""


class Retro(commands.Cog):
    """
    Play retro console games together in Discord, emulated with libretro.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config: Config = Config.get_conf(
            self,
            # The sum of the bytes of "robloach-cogs/pyboy", the name this cog
            # was born under. It is what Red keys every stored setting, saved
            # game and hibernated session by, so it is frozen for good: a new
            # number would silently hand every existing install an empty
            # configuration. Same reasoning as CUSTOM_ID_PREFIX in RetroView.
            identifier=114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121,
            force_registration=True
        )
        self.config.register_global(**DEFAULT_GLOBALS)
        self.config.register_channel(**DEFAULT_CHANNEL)
        self.sessions: typing.Dict[int, RetroView] = {}
        # message id -> the single Resume button left on a retired message.
        self.retired: typing.Dict[int, RetiredView] = {}
        # Serializes every core operation across all channels.
        self.emulator_lock: asyncio.Lock = asyncio.Lock()
        self._idle_task: typing.Optional[asyncio.Task] = None
        self._download_task: typing.Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        # First, before anything reads a setting or touches the data folder:
        # on an install that predates the rename, both of those live under the
        # old class name until this has run.
        try:
            await self._migrate_legacy_namespace()
        except Exception:
            # Never fatal. A cog that will not load is worse than a cog that
            # comes up looking like a fresh install, and the old data is still
            # sitting there untouched to be retried or moved by hand.
            log.exception(
                "Failed to migrate the Retro cog's data from its old "
                "%s namespace. The old data has been left alone.",
                LEGACY_COG_NAME,
            )
        try:
            await self._migrate_core_path()
        except Exception:
            log.exception("Failed to migrate the old Libretro core setting.")
        try:
            await self._restore_sessions()
        except Exception:
            log.exception("Failed to restore Libretro sessions.")
        self._idle_task = asyncio.create_task(self._hibernation_loop())
        # Deliberately not awaited: loading the cog must not sit waiting on
        # the libretro buildbot, and a download that fails must not stop the
        # cog coming up. _auto_download_loop() swallows everything.
        self._download_task = asyncio.create_task(self._auto_download_loop())

    async def cog_unload(self) -> None:
        for attribute in ("_idle_task", "_download_task"):
            task = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if task is not None:
                task.cancel()
        for view in list(self.sessions.values()):
            try:
                await self.hibernate(
                    view,
                    "The console was put away. Press a button to pick up "
                    "where you left off.",
                )
            except Exception:
                log.exception("Failed to hibernate a Libretro session on unload.")
            # The buttons stay enabled on the message so the game can be
            # resumed after a reload, but this now-orphaned view object must
            # not answer them; cog_load builds fresh ones.
            view.closed = True
        self.sessions.clear()
        for retired in self.retired.values():
            retired.alive = False
        self.retired.clear()

    # -- The RetroCog -> Retro rename ---------------------------------------
    #
    # Red gives a cog two places to keep things, and names both of them after
    # the cog's Python class:
    #
    #   * Config.get_conf(self, ...) uses type(self).__name__ as the cog name
    #     that keys every stored setting and every channel's session record;
    #   * cog_data_path(self) returns <bot data>/cogs/<class name>/, which is
    #     where this cog puts downloaded cores, cached ROMs, save states,
    #     battery saves and the libretro system (BIOS) directory.
    #
    # Renaming the class from RetroCog to Retro therefore points both of them
    # somewhere empty. Both have an explicit escape hatch -- Config.get_conf's
    # cog_name= and cog_data_path's raw_name= -- so the old locations can
    # still be read, which is what makes a one-off migration possible.
    #
    # The migration is not clever. It moves the data directory's contents
    # entry by entry (so an interrupted run simply finishes next time),
    # rewrites any stored core path that pointed into the old directory,
    # copies the old settings across only when the new namespace has never
    # been written to, and records that it is done. Every step is wrapped: a
    # read-only disk or a Config that will not answer must cost the migration,
    # not the cog.

    def _legacy_data_dir(self) -> Path:
        """
        Where this cog's files lived when the class was called RetroCog.

        Derived from the current directory's parent rather than by asking for
        it: ``cog_data_path(raw_name=...)`` *creates* the directory it names,
        and conjuring an empty RetroCog folder on every fresh install just to
        discover it is empty would be silly.
        """
        return cog_data_path(self).parent / LEGACY_COG_NAME

    def _legacy_config(self) -> Config:
        """
        A handle on the settings stored under the old cog name.

        ``force_registration`` is off and nothing is registered on it, so
        ``all()`` returns exactly what is on disk with no defaults mixed in --
        which is how "this install has old data" is told from "it does not".
        """
        return Config.get_conf(
            None,
            identifier=114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121,
            cog_name=LEGACY_COG_NAME,
            force_registration=False,
        )

    async def _migrate_legacy_namespace(self) -> None:
        """Bring a pre-rename install's files and settings across. Once."""
        try:
            if await self.config.legacy_namespace_migrated():
                return
        except Exception:
            log.exception("Could not read the Retro migration marker.")
            return

        # Read before anything is moved, since moving can remove the folder.
        # It is also the one reliable "was this cog ever run under the old
        # name?" signal: every code path that touches storage goes through
        # cog_data_path(), which creates the folder. Without it, merely asking
        # Config about the old name would conjure an empty folder (and, with
        # the JSON driver, an empty settings.json) on every fresh install.
        try:
            had_legacy = self._legacy_data_dir().is_dir()
        except OSError:
            had_legacy = False

        moved = await asyncio.to_thread(self._migrate_data_directory)
        copied = await self._migrate_config() if had_legacy else 0
        # After the copy, not before: the paths that need rewriting are the
        # ones the copy has just brought across.
        await self._rewrite_core_paths()

        try:
            await self.config.legacy_namespace_migrated.set(True)
        except Exception:
            # Harmless: the migration is idempotent. Moving runs out of things
            # to move, and the config copy refuses to run once the new
            # namespace has anything in it.
            log.exception("Could not record that the Retro migration ran.")
        if moved or copied:
            log.info(
                "Migrated the Retro cog out of its old %s namespace: %s file(s) "
                "or folder(s) moved, %s setting(s) copied.",
                LEGACY_COG_NAME,
                moved,
                copied,
            )

    def _migrate_data_directory(self) -> int:
        """
        Move the old data directory's contents into the new one. Blocking.

        Returns how many top-level entries were moved. Works entry by entry
        rather than moving the directory whole, which is what makes it safe to
        run again after an interrupted attempt, and lets an entry that already
        exists in the new location win instead of being clobbered.
        """
        try:
            new_dir = cog_data_path(self)
            old_dir = self._legacy_data_dir()
        except Exception:
            log.exception("Could not work out where the Retro data folders are.")
            return 0
        if not old_dir.is_dir() or old_dir.resolve() == new_dir.resolve():
            return 0

        moved = 0
        kept = []
        try:
            entries = sorted(old_dir.iterdir())
        except OSError:
            log.warning(
                "Could not read the old %s data folder at %s.",
                LEGACY_COG_NAME,
                old_dir,
                exc_info=True,
            )
            return 0
        for entry in entries:
            if entry.name == CONFIG_STORE_FILENAME:
                # Not a file of ours: it is the JSON driver's copy of the old
                # namespace's *settings*, which _migrate_config() reads
                # properly through Config. Moving it would drop it on top of
                # the new namespace's own store.
                continue
            target = new_dir / entry.name
            if target.exists():
                # Both namespaces have this. The new one is what the cog has
                # been running on, so it wins; the old copy is left where it
                # is rather than merged, deleted or renamed over.
                kept.append(entry.name)
                continue
            try:
                shutil.move(str(entry), str(target))
                moved += 1
            except (OSError, shutil.Error):
                log.warning(
                    "Could not move %s into the Retro data folder; it has "
                    "been left where it is.",
                    entry,
                    exc_info=True,
                )
        if kept:
            log.warning(
                "The new Retro data folder already had %s, so the copies in "
                "%s were left alone. Delete that folder once you are happy.",
                humanize_list([f"`{name}`" for name in kept]),
                old_dir,
            )
        try:
            # Only ever removes an empty directory, so nothing can be lost
            # here even if something above went wrong. A folder holding
            # nothing but the old settings file is left standing on purpose:
            # the settings have been *copied*, not moved, so it is a free
            # backup, and the log line below says it can go.
            old_dir.rmdir()
        except OSError:
            if not kept and old_dir.is_dir():
                log.info(
                    "Everything was moved out of %s; what is left there is a "
                    "backup copy of the old settings and can be deleted.",
                    old_dir,
                )
        return moved

    async def _rewrite_core_paths(self) -> None:
        """
        Point stored core paths at the folder the cores were just moved to.

        The only absolute paths this cog stores are the installed cores'.
        Everything else is a bare filename resolved against the data directory
        at the time of use (ROMs, save states, battery saves, BIOS files), so
        it follows the move on its own.

        A path is only rewritten once the file is really at the other end of
        it: if the move could not happen (a read-only disk, say), the old
        path is still the working one and pointing away from it would break a
        core that is otherwise fine.
        """
        try:
            old_dir = self._legacy_data_dir().resolve()
        except Exception:
            return
        new_dir = cog_data_path(self)
        try:
            async with self.config.cores() as cores:
                for name, raw in list(cores.items()):
                    try:
                        relative = Path(raw).resolve().relative_to(old_dir)
                        moved_to = new_dir / relative
                        if not moved_to.is_file():
                            continue
                    except (OSError, ValueError):
                        continue
                    cores[name] = str(moved_to)
                    log.info(
                        "Repointed the %s core at %s after the data move.",
                        name,
                        cores[name],
                    )
        except Exception:
            log.exception("Could not repoint the stored core paths.")

    async def _migrate_config(self) -> int:
        """
        Copy settings and sessions out of the old cog name's namespace.

        Only when the new one is untouched: an install that has already been
        written to under the new name is the newer truth, and pouring a stale
        copy over it would undo whatever has been done since. Returns how many
        top-level values were copied.
        """
        try:
            current = await self.config.all()
        except Exception:
            log.exception("Could not read the Retro settings.")
            return 0
        try:
            channels = await self.config.all_channels()
        except Exception:
            channels = {}
        if channels or any(
            current.get(key) != value for key, value in DEFAULT_GLOBALS.items()
        ):
            log.info(
                "The Retro cog already has settings of its own, so the older "
                "%s ones were left alone.",
                LEGACY_COG_NAME,
            )
            return 0

        try:
            legacy = self._legacy_config()
            old_globals = await legacy.all()
            old_channels = await legacy.all_channels()
        except Exception:
            log.exception(
                "Could not read the old %s settings; carrying on without them.",
                LEGACY_COG_NAME,
            )
            return 0

        copied = 0
        for key, value in (old_globals or {}).items():
            if key not in DEFAULT_GLOBALS:
                # A setting this version no longer has. Leave it behind rather
                # than writing an unregistered key into the new namespace.
                continue
            try:
                await getattr(self.config, key).set(value)
                copied += 1
            except Exception:
                log.warning(
                    "Could not copy the %s setting across from %s.",
                    key,
                    LEGACY_COG_NAME,
                    exc_info=True,
                )
        for channel_id, data in (old_channels or {}).items():
            for key, value in (data or {}).items():
                if key not in DEFAULT_CHANNEL:
                    continue
                try:
                    await getattr(
                        self.config.channel_from_id(int(channel_id)), key
                    ).set(value)
                    copied += 1
                except Exception:
                    log.warning(
                        "Could not copy channel %s's %s across from %s.",
                        channel_id,
                        key,
                        LEGACY_COG_NAME,
                        exc_info=True,
                    )
        return copied

    # -- Cores --------------------------------------------------------------

    async def _migrate_core_path(self) -> None:
        """Fold the old single `core_path` setting into the core mapping."""
        legacy = await self.config.core_path()
        if not legacy:
            return
        cores = await self.config.cores()
        if cores:
            await self.config.core_path.set("")
            return
        # The old setting only ever pointed at a Game Boy core, but read the
        # filename anyway in case someone had pointed it somewhere else.
        name = core_name_from_filename(legacy) or "gambatte"
        await self.config.cores.set({name: legacy})
        await self.config.core_path.set("")
        log.info("Migrated the old Libretro core setting to the %s core.", name)

    # Cores are found, not configured. The managed cores directory is scanned
    # on every lookup, so a core that is simply *there* -- downloaded by
    # `[p]retroset download`, fetched automatically on load, or dropped in by
    # hand while the bot was off -- is playable without anybody having to
    # register it. There used to be a `[p]retroset core <path>` command for
    # that and there is not any more.
    #
    # The `cores` setting is still read first, because it is the only way to
    # use a core that lives somewhere else entirely (a RetroArch install, say)
    # and because installs made before this change have it filled in. It is
    # still written by the downloader, so an owner can see where a core came
    # from, but nothing depends on it being there.

    def _scan_cores_dir(self) -> typing.Dict[str, Path]:
        """Every core this cog knows sitting in the managed cores directory."""
        found: typing.Dict[str, Path] = {}
        try:
            entries = sorted(self._cores_dir().iterdir())
        except OSError:
            log.warning("Could not read the Retro cores directory.", exc_info=True)
            return found
        for path in entries:
            name = core_name_from_filename(path.name)
            if name is None or name in found:
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            found[name] = path
        return found

    async def _installed_cores(self) -> typing.Dict[str, Path]:
        """Every core that can actually be loaded right now, however it got there."""
        found: typing.Dict[str, Path] = {}
        try:
            configured = await self.config.cores()
        except Exception:
            log.exception("Could not read the recorded Retro core paths.")
            configured = {}
        for name, raw in (configured or {}).items():
            if name not in CORES or not raw:
                continue
            try:
                path = Path(raw)
                if path.is_file():
                    found[name] = path
            except OSError:
                continue
        for name, path in self._scan_cores_dir().items():
            found.setdefault(name, path)
        return found

    async def _core_path(self, core: str) -> typing.Optional[Path]:
        """Where to load one core from, or None if it is not installed."""
        try:
            raw = (await self.config.cores()).get(core)
        except Exception:
            raw = None
        if raw:
            path = Path(raw)
            try:
                if path.is_file():
                    return path
            except OSError:
                pass
        # Not recorded, recorded wrongly, or recorded at a path that has since
        # gone: look where the cog puts them. The exact filename first, since
        # that is what both download paths write, then a scan for a core built
        # for another platform's suffix.
        candidate = self._cores_dir() / core_filename(core)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            pass
        return self._scan_cores_dir().get(core)

    def _cores_dir(self) -> Path:
        return self._data_dir("cores")

    @staticmethod
    def _missing_core_message(prefix: str, system, core: str) -> str:
        return (
            f"No {system.name} core is installed. Ask the bot owner to run "
            f"`{prefix}retroset download {core}`."
        )

    @staticmethod
    def _supported_lines(systems=SYSTEMS) -> typing.List[str]:
        """One line per console, listing the file extensions it accepts."""
        return [
            "- **{}**: {}".format(
                system.name,
                ", ".join(f"`.{extension}`" for extension in system.extensions),
            )
            for system in systems
        ]

    # -- Core options -------------------------------------------------------
    #
    # Every libretro core has its own settings -- FCEUmm has 44, Genesis Plus
    # GX 62 -- keyed by strings the core itself declares. Three things make
    # this awkward enough to be worth spelling out:
    #
    #   * Most cores declare their options from retro_set_environment, which
    #     runs before any content is loaded, so they can be read by loading
    #     the core on its own (see emulator.probe_core_options). Some do not:
    #     FCEUmm declares nothing until a ROM is in, and then declares 44. So
    #     an empty answer means "not known yet", never "this core has none".
    #   * Loading a core to ask it is subject to the same one-at-a-time rule
    #     as playing a game, so the probe takes the cog's emulator lock and
    #     hibernates whatever is running first.
    #   * Everything found is cached in Config, and the cache is extended
    #     every time a session starts, so the list stays instant and FCEUmm's
    #     44 options become listable after the first NES game anyone plays.

    async def _core_options(self, core: str) -> typing.Dict[str, str]:
        """The owner's overrides for one core, as plain text."""
        stored = (await self.config.core_options()).get(core) or {}
        return {str(key): str(value) for key, value in stored.items()}

    async def _set_core_option(
        self, core: str, key: str, value: typing.Optional[str]
    ) -> None:
        """Store (or, with ``value=None``, clear) one override."""
        async with self.config.core_options() as options:
            current = dict(options.get(core) or {})
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
            if current:
                options[core] = current
            else:
                options.pop(core, None)

    async def _remember_definitions(
        self, core: str, definitions: typing.Dict[str, dict]
    ) -> None:
        """
        Fold newly-discovered option definitions into the cache.

        Merged rather than replaced: a ROM-less probe sees a subset of what a
        running game sees, and losing the richer answer to a later, poorer one
        would make the listing flap. A key that a core update has since
        dropped can linger, which is harmless -- libretro.py validates every
        key and value against the core's own list and silently substitutes the
        default for anything it does not recognise.
        """
        if not definitions:
            return
        try:
            async with self.config.core_option_definitions() as store:
                merged = dict(store.get(core) or {})
                merged.update(definitions)
                store[core] = merged
        except Exception:
            # Same reasoning as _save_record: a Config write can fail, and a
            # cache that could not be written must not break the command (or,
            # via _learn_options, the game that was just started).
            log.exception("Could not cache the %s core's option definitions.", core)

    async def _cached_definitions(self, core: str) -> typing.Dict[str, dict]:
        return (await self.config.core_option_definitions()).get(core) or {}

    def _live_emulator_for(self, core: str) -> typing.Optional[RetroEmulator]:
        """A running emulator for this core, if some channel is playing one."""
        for view in self.sessions.values():
            if view.live and view.core == core:
                return view.emulator
        return None

    async def _learn_options(self, core: str, emulator: RetroEmulator) -> None:
        """
        Note down what a just-started core says about itself.

        Called on every successful boot, which is the only way the options of
        a core that declares nothing until a ROM is loaded are ever seen.
        Never raises: this is bookkeeping on the side of somebody's game.
        """
        try:
            definitions = emulator.option_definitions()
        except Exception:
            log.debug("Could not read the %s core's options.", core, exc_info=True)
            return
        if definitions:
            await self._remember_definitions(core, definitions)

    async def _probe_definitions(self, core: str) -> typing.Dict[str, dict]:
        """
        Load the core on its own, with no game, and ask what it offers.

        Respects the one-core-at-a-time rule exactly as starting a game does:
        the emulator lock is held, and anything already running is saved and
        hibernated first. The probe itself is blocking C code, so it runs in a
        worker thread. Returns {} (having logged) if the core cannot be asked.
        """
        core_path = await self._core_path(core)
        if core_path is None:
            return {}
        seeded = await self._core_options(core)
        async with self.emulator_lock:
            # A libretro core has process-global state, so nothing else may be
            # loaded while this one is. _evict_locked() with no exclusion
            # hibernates every live session, saving each one first.
            await self._evict_locked()
            try:
                return await asyncio.to_thread(probe_core_options, core_path, seeded)
            except EmulatorError as error:
                log.warning("Could not probe the %s core for options: %s", core, error)
            except Exception:
                log.exception("Probing the %s core for options failed.", core)
        return {}

    async def _definitions_for(
        self, core: str, probe: bool = True
    ) -> typing.Tuple[typing.Dict[str, dict], str]:
        """
        Everything known about a core's options, and where it came from.

        Tried in order: the session that is running that core right now (the
        richest answer, since a loaded ROM is what makes some cores declare
        anything at all), then the Config cache, then a ROM-less probe.
        """
        async with self.emulator_lock:
            live = self._live_emulator_for(core)
            # Reading the definitions is a copy of structures libretro.py
            # already owns -- no call into the core -- but it is done under
            # the lock anyway so it cannot race a press freeing the emulator.
            definitions = live.option_definitions() if live is not None else {}
        if definitions:
            await self._remember_definitions(core, definitions)
            return definitions, "the game running right now"

        cached = await self._cached_definitions(core)
        if cached:
            return cached, "the last time this core ran"

        if probe:
            probed = await self._probe_definitions(core)
            if probed:
                await self._remember_definitions(core, probed)
                return probed, "the core itself"
        return {}, ""

    @staticmethod
    def _option_values(definition: typing.Optional[dict]) -> typing.List[str]:
        """The values a core will accept for an option, in the core's order."""
        if not definition:
            return []
        return [str(pair[0]) for pair in definition.get("values") or () if pair]

    @classmethod
    def _option_default(cls, definition: typing.Optional[dict]) -> str:
        """
        The value a core uses when nobody has chosen one.

        libretro lets a definition leave ``default_value`` NULL, in which case
        the first of the listed values is the default.
        """
        if not definition:
            return ""
        default = str(definition.get("default") or "")
        if default:
            return default
        values = cls._option_values(definition)
        return values[0] if values else ""

    @classmethod
    def _effective_value(
        cls, definition: typing.Optional[dict], override: typing.Optional[str]
    ) -> str:
        """
        What the core will really read for an option.

        An override that is not one of the core's own values is not an error:
        libretro.py hands the core its default instead. Showing the override
        as though it had taken effect would be a lie, so this resolves it the
        same way the core does.
        """
        if override is None:
            return cls._option_default(definition)
        values = cls._option_values(definition)
        if values and override not in values:
            return cls._option_default(definition)
        return override

    @staticmethod
    def _short_key(core: str, key: str) -> str:
        """``fceumm_sound_quality`` -> ``sound_quality`` for readability."""
        prefix = f"{core}_"
        return key[len(prefix):] if key.startswith(prefix) and len(key) > len(prefix) else key

    @classmethod
    def _resolve_option_key(
        cls,
        core: str,
        definitions: typing.Dict[str, dict],
        text: str,
        prefix: str = "[p]",
    ) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
        """
        Turn what somebody typed into a real option key.

        Returns ``(key, error)``; exactly one of the two is set. Cores name
        their options inconsistently -- FCEUmm uses ``fceumm_region`` but
        mednafen_ngp uses ``ngp_language``, not ``mednafen_ngp_language`` --
        so rather than guessing a prefix this tries the exact key, then
        ``<core>_<key>``, then a unique suffix match among the keys the core
        actually declared. An ambiguous suffix is reported rather than picked.
        """
        wanted = str(text).strip().strip("`").lower()
        if not wanted:
            return None, "No option name was given."
        if not definitions:
            # Nothing to match against; the caller decides whether to allow
            # an unvalidated key through.
            return None, None

        lookup = {key.lower(): key for key in definitions}
        for candidate in (wanted, f"{core.lower()}_{wanted}"):
            if candidate in lookup:
                return lookup[candidate], None

        suffix = f"_{wanted}"
        matches = sorted(
            real for lowered, real in lookup.items() if lowered.endswith(suffix)
        )
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            listed = humanize_list([f"`{cls._short_key(core, m)}`" for m in matches])
            return None, (
                f"`{text}` matches {len(matches)} of the `{core}` core's "
                f"options: {listed}. Use the full key to say which one you mean."
            )
        return None, (
            f"The `{core}` core has no option called `{text}`. Run "
            f"`{prefix}retroset coreoptions {core}` to see the "
            f"{len(definitions)} it does have."
        )

    # -- Session storage ----------------------------------------------------

    async def _restore_sessions(self) -> None:
        """Rebuild hibernated sessions from Config and re-arm their buttons."""
        timeout_minutes = await self.config.session_timeout_minutes()
        clip_seconds = await self.config.clip_seconds()
        hold_ms = await self.config.hold_ms()
        for channel_id, data in (await self.config.all_channels()).items():
            record = data.get("session")
            if record:
                record.setdefault("channel_id", channel_id)
                try:
                    view = RetroView.from_record(
                        self, record, timeout_minutes, clip_seconds, hold_ms
                    )
                except Exception:
                    log.exception(
                        "Ignoring an unreadable Libretro session record for channel %s",
                        channel_id,
                    )
                else:
                    self.sessions[channel_id] = view
                    self._register_view(view)
            # Messages whose game was replaced keep a single Resume button,
            # and it has to survive a restart: that is the whole point of it,
            # since a retired message can sit in a channel for weeks before
            # anybody wants the game back.
            for key, retired_record in (data.get("retired") or {}).items():
                try:
                    retired_record = dict(retired_record)
                    retired_record.setdefault("channel_id", channel_id)
                    retired_record.setdefault("message_id", int(key))
                    self._arm_retired(retired_record)
                except Exception:
                    log.exception(
                        "Ignoring an unreadable retired Retro record for "
                        "channel %s, message %s",
                        channel_id,
                        key,
                    )
        if self.sessions or self.retired:
            log.info(
                "Restored %s hibernated Libretro session(s) and %s Resume "
                "button(s).",
                len(self.sessions),
                len(self.retired),
            )

    def _arm_retired(self, record: dict) -> typing.Optional[RetiredView]:
        """Give one retired message a working Resume button."""
        message_id = record.get("message_id")
        if not message_id:
            return None
        message_id = int(message_id)
        existing = self.retired.pop(message_id, None)
        if existing is not None:
            existing.alive = False
        view = RetiredView(self, record)
        self.retired[message_id] = view
        try:
            self.bot.add_view(view, message_id=message_id)
        except Exception:
            log.exception(
                "Could not register the Resume button for message %s", message_id
            )
        return view

    async def _remember_retired(self, record: dict) -> None:
        """
        Store what a Resume button needs, and forget the oldest ones.

        Bounded per channel for the same reason the ROM cache is: a channel
        that works through a pile of games would otherwise accumulate a
        Config entry per message forever, and a Resume button whose ROM has
        been pruned can only apologise anyway.
        """
        channel_id = int(record.get("channel_id") or 0)
        message_id = record.get("message_id")
        if not channel_id or not message_id:
            return
        try:
            async with self.config.channel_from_id(channel_id).retired() as retired:
                retired[str(int(message_id))] = dict(record)
                while len(retired) > MAX_RETIRED_PER_CHANNEL:
                    # Oldest by last_active, which is when the game was last
                    # actually played rather than when it was retired.
                    oldest = min(
                        retired,
                        key=lambda key: float(
                            (retired[key] or {}).get("last_active") or 0.0
                        ),
                    )
                    dropped = retired.pop(oldest)
                    stale = self.retired.pop(int(oldest), None)
                    if stale is not None:
                        stale.alive = False
                    log.debug(
                        "Forgot the Resume button for %s in channel %s.",
                        (dropped or {}).get("game_name"),
                        channel_id,
                    )
        except Exception:
            log.exception(
                "Could not store the retired Retro session for channel %s.",
                channel_id,
            )

    async def _forget_retired(self, channel_id: int, message_id: int) -> None:
        """Drop one retired record, on the way to putting it back in service."""
        view = self.retired.pop(int(message_id), None)
        if view is not None:
            view.alive = False
        try:
            async with self.config.channel_from_id(int(channel_id)).retired() as retired:
                retired.pop(str(int(message_id)), None)
        except Exception:
            log.exception(
                "Could not forget the retired Retro session %s.", message_id
            )

    async def _forget_retired_slug(self, channel_id: int, slug: str) -> None:
        """
        Drop any Resume button for a game that has just been started again.

        Two messages offering to resume the same game in the same channel
        would fight over one save state, so the older one stands down as soon
        as the game comes back some other way. A click on it after this says
        so rather than starting a second copy; see RetiredView._resume.
        """
        stale: typing.List[int] = []
        try:
            async with self.config.channel_from_id(int(channel_id)).retired() as retired:
                for key, record in list(retired.items()):
                    if (record or {}).get("slug") == slug:
                        retired.pop(key, None)
                        stale.append(int(key))
        except Exception:
            log.exception(
                "Could not tidy the retired Retro records for channel %s.", channel_id
            )
        for message_id in stale:
            view = self.retired.pop(message_id, None)
            if view is not None:
                view.alive = False

    def _register_view(self, view: RetroView) -> None:
        """
        Teach the bot to route this message's button clicks to this view.

        Persistent views need a fixed custom_id on every child and no
        timeout, both of which RetroView guarantees.
        """
        if view.message_id is None:
            return
        try:
            self.bot.add_view(view, message_id=view.message_id)
        except Exception:
            log.exception(
                "Could not register the Libretro controls for message %s",
                view.message_id,
            )

    async def _save_record(self, view: RetroView) -> None:
        """Write the session record. Never raises: it is on every save path."""
        try:
            await self.config.channel_from_id(view.channel_id).session.set(
                view.to_record()
            )
        except Exception:
            # A Config write can fail on a full disk or a locked database. The
            # in-memory session is still correct, and the save state (which is
            # what actually holds the player's progress) is written
            # separately, so this must not abort a hibernate.
            log.exception(
                "Could not store the Retro session record for channel %s.",
                view.channel_id,
            )

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
    def _slug(name: str) -> str:
        """A lowercase, filesystem-safe id for a game within a channel."""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()[:48]
        return slug or "game"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        # Write beside the target and rename, so a crash halfway through
        # cannot leave a truncated save state behind.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def _prune_cached_games(self, channel_id: int, keep_slug: str) -> None:
        """
        Drop the oldest cached ROM+state pairs for a channel.

        Files are keyed by slug so every game a channel plays keeps its own
        save and can be resumed later, but a channel that works through a
        pile of ROMs should not keep them all forever.
        """
        try:
            roms = list(self._roms_dir().glob(f"{channel_id}-*"))
        except OSError:
            return
        entries = []
        for path in roms:
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
        entries.sort(reverse=True)
        prefix = f"{channel_id}-"
        seen = 0
        for _, path in entries:
            slug = path.stem[len(prefix):]
            if slug == keep_slug:
                continue
            seen += 1
            if seen < MAX_CACHED_GAMES_PER_CHANNEL:
                continue
            try:
                path.unlink(missing_ok=True)
                self._state_path(channel_id, slug).unlink(missing_ok=True)
                self._sram_path(channel_id, slug).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not prune the cached ROM %s", path, exc_info=True)

    # -- Emulator lifecycle -------------------------------------------------

    async def run_press(
        self, view: RetroView, field: typing.Optional[str], repeat: int = 1
    ) -> bytes:
        """
        Emulate one press for a session, waking it up first if it was asleep.

        Raises EmulatorError if the session cannot be woken or the core
        fails. Called by the view from its button callbacks.
        """
        async with self.emulator_lock:
            await self._wake_locked(view)
            clip = await asyncio.to_thread(view.run_press, field, repeat)
            view.touch()
            view.press_count += 1
            if view.press_count % SAVE_STATE_EVERY_PRESSES == 0:
                await self._write_state(view)
                await self._save_record(view)
            return clip

    async def hibernate(self, view: RetroView, reason: typing.Optional[str] = None) -> None:
        """Save the game, free the emulator, and keep the controls usable."""
        async with self.emulator_lock:
            await self._hibernate_locked(view, reason)

    async def _retire(self, view: RetroView, reason: str) -> None:
        """
        Save a session and swap its controls for a single Resume button.

        The message is no longer the channel's game, so its controls cannot
        stay live: pressing one would wake a second libretro core, and only
        one may be loaded at a time. But the game is not gone either -- its
        ROM, save state and battery save are all still cached under the
        channel's id -- so the message keeps one button that starts it again
        here, through the ordinary start path. See RetiredView.
        """
        async with self.emulator_lock:
            await self._hibernate_locked(view, None)
        record = view.to_record()
        view.closed = True
        message = await view.resolve_message()
        retired = self._arm_retired(record) if message is not None else None
        if retired is None:
            # No message to put a button on, so there is nothing to resume
            # from; leave the view inert and say nothing more about it.
            view.retire()
            await view.refresh(reason)
            return
        await self._remember_retired(record)
        try:
            await message.edit(content=reason, view=retired)
        except discord.HTTPException:
            log.warning(
                "Could not put a Resume button on the retired Retro message "
                "in channel %s.",
                view.channel_id,
                exc_info=True,
            )

    async def resume_retired(self, retired: RetiredView, interaction) -> None:
        """
        Start a retired message's game again, in its own channel.

        Called from the Resume button. This goes through exactly the same
        gates a `[p]retro <name>` would: whatever else is live is saved and
        hibernated first (only one core at a time), the channel's current game
        -- if it has one -- is retired and gets a Resume button of its own,
        and the game comes back from its save state, then its battery save,
        then from the beginning.

        The message being clicked is the one that is brought back to life, so
        the game reappears where it was rather than as a new post further down
        the channel.
        """
        record = dict(retired.record)
        channel_id = int(record.get("channel_id") or retired.channel_id or 0)
        game_name = record.get("game_name") or "that game"

        rom_path = self._rom_path(record.get("rom_filename") or "")
        if rom_path is None or not rom_path.is_file():
            # The cache is bounded, so a game a channel has not touched in a
            # while really can be gone. Say so; the save state is very
            # probably still there, so starting it again by name will pick it
            # back up (see _start_session).
            await self._whisper_interaction(
                interaction,
                f"The cached ROM for **{game_name}** has been cleaned up, so "
                "this button cannot start it. Start it again with "
                f"`[p]retro <name or url>` \N{EM DASH} its save is still here, "
                "and it will pick up where it left off.",
            )
            return

        core = record.get("core") or ""
        core_path = await self._core_path(core)
        if core_path is None:
            await self._whisper_interaction(
                interaction,
                f"The emulator core **{game_name}** needs (`{core}`) is not "
                "installed any more, so it cannot be started.",
            )
            return

        try:
            await interaction.response.edit_message(
                content=f"Starting **{game_name}** again\N{HORIZONTAL ELLIPSIS}",
                view=retired,
            )
        except discord.HTTPException:
            log.warning("Could not acknowledge a Resume click.", exc_info=True)

        view = RetroView.from_record(
            self,
            record,
            await self.config.session_timeout_minutes(),
            await self.config.clip_seconds(),
            await self.config.hold_ms(),
        )
        message_id = int(retired.message_id or getattr(interaction.message, "id", 0) or 0)
        view.message_id = message_id or None
        view.message = getattr(interaction, "message", None)

        state, sram, notice = self._saved_progress(channel_id, view.slug)
        emulator = RetroEmulator(
            core_path,
            rom_path,
            system_dir=self._system_dir(),
            options=await self._core_options(view.core),
        )
        # Booted *before* anything is taken away from the channel, so a core
        # that will not come up costs nothing: whatever was playing is merely
        # hibernated (which its own buttons undo) rather than retired.
        try:
            async with self.emulator_lock:
                # One core at a time, here as everywhere else.
                await self._evict_locked(exclude=view)
                clip = await asyncio.to_thread(view._boot, emulator, state, sram)
        except Exception as error:
            log.warning(
                "Could not resume %s in channel %s: %s", view.slug, channel_id, error
            )
            try:
                await asyncio.to_thread(emulator.stop)
            except Exception:
                log.exception("Could not stop a failed resume's emulator.")
            view.closed = True
            # Put the Resume button back so the click was not destructive.
            self._arm_retired(record)
            await self._restore_retired_message(interaction, retired, record, error)
            return

        # It is up, so the channel really does change hands now. The game it
        # replaces is retired exactly as starting a different game by name
        # would retire it, and gets a Resume button of its own.
        previous = self.sessions.get(channel_id)
        if previous is not None and previous is not view:
            await self._retire(
                previous,
                f"Replaced by **{game_name}**. **{previous.game_name}** was "
                "saved \N{EM DASH} press Resume to come back to it.",
            )
            self.sessions.pop(channel_id, None)
        self.sessions[channel_id] = view

        view.emulator = emulator
        view.last_clip = clip
        view.touch()
        self._settle_boot(view, state, notice)
        await self._forget_retired(channel_id, message_id)
        await self._forget_retired_slug(channel_id, view.slug)
        try:
            view.message = await interaction.edit_original_response(
                content=view._content(),
                attachments=[view._clip_file(clip)],
                view=view,
            )
        except discord.HTTPException as error:
            log.exception(
                "Discord rejected the resumed Retro message in channel %s.",
                channel_id,
            )
            await self._whisper_interaction(interaction, self._http_error_message(error))
        # Whatever happened to the message, the session is real and this
        # message now drives it, so route its clicks here from now on.
        self._register_view(view)
        await self._learn_options(view.core, emulator)
        await self._save_record(view)

    async def _restore_retired_message(
        self,
        interaction,
        retired: RetiredView,
        record: dict,
        error: BaseException,
    ) -> None:
        """Put a failed Resume back the way it was, and say what went wrong."""
        reason = self._friendly_error(error) or "The game could not be started."
        try:
            await interaction.edit_original_response(
                content=(
                    f"**{record.get('game_name') or 'That game'}** could not "
                    f"be started: {reason}"
                ),
                view=self.retired.get(int(retired.message_id or 0)) or retired,
            )
        except discord.HTTPException:
            log.warning("Could not report a failed Resume.", exc_info=True)

    @staticmethod
    async def _whisper_interaction(interaction, message: str) -> None:
        """Tell only the person who clicked, whether or not we have replied."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
                return
        except Exception:
            log.debug("Could not answer an interaction directly.", exc_info=True)
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            log.debug("Could not deliver an interaction notice.", exc_info=True)

    async def _hibernate_locked(
        self, view: RetroView, reason: typing.Optional[str] = None
    ) -> None:
        emulator, view.emulator = view.emulator, None
        if emulator is not None:
            # Always write the save state *before* the core is freed: after
            # emulator.stop() the machine state is gone for good.
            await self._write_state(view, emulator)
            await asyncio.to_thread(emulator.stop)
        # The controls stay enabled so the next press can wake the session
        # back up; nothing about the buttons changes when a game sleeps.
        await self._save_record(view)
        if reason is not None:
            await view.refresh(reason)

    async def _wake_locked(self, view: RetroView) -> None:
        """Load the core and the last save state for a hibernated session."""
        if view.live:
            return
        core_path = await self._core_path(view.core)
        if core_path is None:
            raise EmulatorError(
                f"The {view.system.name} core ({view.core}) is not installed "
                "any more, so this game cannot be resumed."
            )
        rom_path = self._rom_path(view.rom_filename)
        if rom_path is None or not rom_path.is_file():
            raise EmulatorError(
                "The cached ROM for this game is gone. Start the game again "
                "to play it."
            )

        await self._evict_locked(exclude=view)

        state_path = self._state_path(view.channel_id, view.slug)
        state: typing.Optional[bytes] = None
        if state_path.is_file():
            try:
                state = state_path.read_bytes()
            except OSError:
                log.warning("Could not read the Libretro save state %s", state_path)
        # The insurance policy. A save state is tied to the exact build of the
        # core that wrote it, so updating a core invalidates every state on
        # disk; the cartridge's battery save is not, and holds whatever the
        # player saved from inside the game.
        sram = self._read_sram(view.channel_id, view.slug)

        emulator = RetroEmulator(
            core_path,
            rom_path,
            system_dir=self._system_dir(),
            options=await self._core_options(view.core),
        )

        def _resume() -> str:
            """Bring the game back, and say how much of it came back."""
            emulator.start()
            if state:
                try:
                    emulator.load_state(state)
                    return "state"
                except EmulatorError as error:
                    # A state from another core, a truncated file, or -- the
                    # case this whole feature exists for -- a state whose size
                    # no longer matches because the core was updated. That
                    # should cost the exact moment, not the session and not
                    # the player's in-game save.
                    log.warning(
                        "Discarding an unusable Libretro save state for %s: %s",
                        view.slug,
                        error,
                    )
            # Cold boot. The core only allocates the cartridge's save memory
            # once it has loaded the game, so the battery save goes in here,
            # after start(), and the boot frames run afterwards so the game
            # gets as far as its own title screen with the save in place.
            restored = emulator.load_sram(sram) if sram else False
            emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
            return "sram" if restored else "fresh"

        try:
            outcome = await asyncio.to_thread(_resume)
        except Exception:
            await asyncio.to_thread(emulator.stop)
            raise
        if state and outcome != "state":
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
            view.notice = self._cold_boot_notice(outcome == "sram")
            log.info(
                "Cold-booted %s after an unusable save state; battery save %s.",
                view.slug,
                "restored" if outcome == "sram" else "not available",
            )
        view.emulator = emulator
        await self._learn_options(view.core, emulator)

    def _saved_progress(
        self, channel_id: int, slug: str
    ) -> typing.Tuple[typing.Optional[bytes], typing.Optional[bytes], typing.Optional[str]]:
        """
        What this channel already has saved for one game.

        Returns ``(save state, battery save, the line to say if it all works)``.
        Read on every start, not only on a wake: a channel that played this
        game before -- even weeks and several other games ago -- has its
        progress cached under the same key, and booting over the top of it
        would quietly throw the player's game away.
        """
        state: typing.Optional[bytes] = None
        path = self._state_path(channel_id, slug)
        try:
            if path.is_file():
                state = path.read_bytes() or None
        except OSError:
            log.warning("Could not read the Libretro save state %s", path, exc_info=True)
        sram = self._read_sram(channel_id, slug)
        if state:
            notice = "Picked up from where this channel left off."
        elif sram:
            notice = (
                "Started from the title screen with this channel's in-game "
                "save already in place \N{EM DASH} load it from the game's own "
                "menu to carry on."
            )
        else:
            notice = None
        return state, sram, notice

    def _settle_boot(
        self,
        view: RetroView,
        state: typing.Optional[bytes],
        notice: typing.Optional[str],
    ) -> None:
        """
        Say how a boot went, and throw away a save state that did not work.

        The counterpart of the same handling in :meth:`_wake_locked`: a save
        state is tied to the exact build of the core that wrote it, so a core
        update invalidates every one on disk. That should cost the exact
        moment, never the session and never the player's own in-game save.
        """
        outcome = getattr(view, "boot_outcome", "fresh")
        if state and outcome != "state":
            try:
                self._state_path(view.channel_id, view.slug).unlink(missing_ok=True)
            except OSError:
                pass
            view.notice = self._cold_boot_notice(outcome == "sram")
            log.info(
                "Cold-booted %s after an unusable save state; battery save %s.",
                view.slug,
                "restored" if outcome == "sram" else "not available",
            )
            return
        if outcome == "state":
            view.notice = notice
        elif outcome == "sram":
            view.notice = notice or (
                "Started from the title screen with this channel's in-game "
                "save already in place."
            )

    @staticmethod
    def _cold_boot_notice(sram_restored: bool) -> str:
        """What to tell the channel when a save state could not be used."""
        if sram_restored:
            return (
                "This game's save state could not be used (most likely the "
                "emulator core was updated), so it started from the title "
                "screen \N{EM DASH} but your in-game save survived. Load it "
                "from the game's own menu to carry on."
            )
        return (
            "This game's save state could not be used (most likely the "
            "emulator core was updated), so it started over from the "
            "beginning. This game keeps no in-game save, so there was nothing "
            "else to restore."
        )

    async def _evict_locked(
        self, exclude: typing.Optional[RetroView] = None
    ) -> typing.List[RetroView]:
        """
        Put other sessions to sleep so only MAX_LIVE_EMULATORS stay loaded.

        Each one is saved before its core is freed, and its own message is
        edited to say it went to sleep. Returns the sessions that were
        evicted so the caller can mention them.
        """
        evicted: typing.List[RetroView] = []
        live = [v for v in self.sessions.values() if v.live and v is not exclude]
        live.sort(key=lambda v: v.last_active)
        while len(live) >= MAX_LIVE_EMULATORS:
            view = live.pop(0)
            await self._hibernate_locked(
                view,
                "Another channel started playing, so this game was saved and "
                "went to sleep. Press a button to continue.",
            )
            evicted.append(view)
        return evicted

    def _eviction_notice(
        self, ctx: commands.Context, evicted: typing.List[RetroView]
    ) -> typing.Optional[str]:
        """
        Tell this channel which other game had to be paused, if any.

        Only names the other game and channel when the person who asked can
        see that channel anyway; otherwise it stays deliberately vague rather
        than leaking where the bot is being used.
        """
        if not evicted:
            return None
        parts = []
        for view in evicted:
            channel = self.bot.get_channel(view.channel_id)
            visible = False
            if channel is not None and ctx.guild is not None and view.guild_id == ctx.guild.id:
                try:
                    visible = channel.permissions_for(ctx.author).view_channel
                except Exception:
                    visible = False
            if visible:
                parts.append(f"**{view.game_name}** in {channel.mention}")
            else:
                parts.append("a game in another channel")
        return (
            f"Paused {humanize_list(parts)} first \N{EM DASH} the bot can only "
            "run one game at a time. It was saved and will pick up where it "
            "left off."
        )

    async def _write_state(
        self, view: RetroView, emulator: typing.Optional[RetroEmulator] = None
    ) -> bool:
        """
        Save the session's progress to disk. Never raises.

        Writes both halves of it: the save state (the exact moment, only ever
        loadable by the same build of the same core) and the cartridge's
        battery save (the player's own in-game save, in a format that outlives
        a core update). Both are captured here, before any caller frees the
        emulator, because afterwards there is nothing left to read.

        The return value is whether the *state* was written; a cartridge with
        no battery is the normal case and is not a failure.
        """
        emulator = emulator if emulator is not None else view.emulator
        if emulator is None or not emulator.started:
            return False
        # SRAM first: it is the copy that survives a core update, so if only
        # one of the two can be written it should be this one.
        await self._write_sram(view, emulator)
        try:
            data = await asyncio.to_thread(emulator.save_state)
        except EmulatorError as error:
            log.warning("Could not save the Libretro state for %s: %s", view.slug, error)
            return False
        path = self._state_path(view.channel_id, view.slug)
        try:
            await asyncio.to_thread(self._write_atomic, path, data)
        except OSError:
            log.warning("Could not write the Libretro state %s", path, exc_info=True)
            return False
        return True

    async def _write_sram(
        self, view: RetroView, emulator: typing.Optional[RetroEmulator] = None
    ) -> bool:
        """
        Write the cartridge's battery save next to the save state.

        Returns False, and writes nothing at all, for a cartridge that has no
        battery -- most NES and Game Boy puzzle games, every test ROM. That is
        the ordinary case, not an error, so it is not logged and never leaves
        an empty ``.srm`` behind for the resume path to trip over.
        """
        emulator = emulator if emulator is not None else view.emulator
        if emulator is None or not emulator.started:
            return False
        try:
            data = await asyncio.to_thread(emulator.save_sram)
        except Exception:
            log.warning(
                "Could not read the battery save for %s.", view.slug, exc_info=True
            )
            return False
        if not data:
            return False
        path = self._sram_path(view.channel_id, view.slug)
        try:
            await asyncio.to_thread(self._write_atomic, path, data)
        except OSError:
            log.warning("Could not write the battery save %s", path, exc_info=True)
            return False
        return True

    def _read_sram(self, channel_id: int, slug: str) -> typing.Optional[bytes]:
        """The stored battery save for a game, or None if there isn't one."""
        path = self._sram_path(channel_id, slug)
        try:
            if not path.is_file():
                return None
            return path.read_bytes() or None
        except OSError:
            log.warning("Could not read the battery save %s", path, exc_info=True)
            return None

    async def _hibernation_loop(self) -> None:
        """Put sessions to sleep once they have been idle for long enough."""
        try:
            await self.bot.wait_until_red_ready()
        except Exception:
            pass
        while True:
            try:
                await asyncio.sleep(IDLE_CHECK_SECONDS)
                await self._hibernate_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("The Libretro hibernation task hit an error.")

    async def _hibernate_idle(self) -> None:
        minutes = await self.config.session_timeout_minutes()
        cutoff = time.time() - minutes * 60
        for view in list(self.sessions.values()):
            if view.live and view.last_active < cutoff:
                await self.hibernate(
                    view,
                    f"Asleep after {minutes} minutes without input. Press a "
                    "button to pick up where you left off.",
                )

    # -- Talking to Discord safely -------------------------------------------

    async def _safe_send(self, ctx, content=None, **kwargs):
        """
        ``ctx.send`` that reports a Discord failure instead of raising it.

        Every one of these paths is reachable in production: the bot can lose
        a permission mid-command, the channel can be deleted, an attachment
        can be too large for the server's boost tier, and Discord will reject
        a component outright (that is how an invalid button emoji took out a
        whole command). None of those should surface as a traceback.
        """
        try:
            return await ctx.send(content, **kwargs)
        except discord.Forbidden:
            log.warning(
                "Missing permission to post a Retro message in channel %s.",
                getattr(getattr(ctx, "channel", None), "id", None),
                exc_info=True,
            )
        except discord.HTTPException:
            log.exception(
                "Discord rejected a Retro message in channel %s.",
                getattr(getattr(ctx, "channel", None), "id", None),
            )
            # Components and files are the usual culprits, so try once more
            # with nothing but the text. If that fails too, give up quietly.
            if kwargs:
                try:
                    return await ctx.send(content or self._http_error_message(None))
                except discord.HTTPException:
                    pass
        return None

    @staticmethod
    def _http_error_message(error: typing.Optional[Exception]) -> str:
        """Turn a Discord rejection into something worth reading."""
        if isinstance(error, discord.Forbidden):
            return (
                "Discord would not let me post that here. I need the **Attach "
                "Files** permission in this channel to post a clip."
            )
        code = getattr(error, "code", 0) or 0
        if code == 50035:
            return (
                "Discord rejected the message as invalid (`50035 Invalid Form "
                "Body`). That is a bug in this cog rather than anything you "
                "did; the details are in the bot's log."
            )
        if code == 40005 or getattr(error, "status", 0) == 413:
            return "That clip was too large for Discord to accept."
        status = getattr(error, "status", None)
        if status is None:
            return "Discord rejected the message. The details are in the bot's log."
        return (
            f"Discord rejected the message (HTTP {status}"
            + (f", code {code}" if code else "")
            + "). Try again in a moment."
        )

    def _friendly_error(self, error: BaseException) -> typing.Optional[str]:
        """A user-facing sentence for an exception, or None if we have none."""
        if isinstance(error, discord.HTTPException):
            return self._http_error_message(error)
        if isinstance(error, EmulatorError):
            return f"The emulator could not do that: {error}"
        if isinstance(error, (DownloadError, archives.ArchiveError)):
            return str(error)
        if isinstance(error, aiohttp.ClientError):
            return f"A download failed: {error}"
        if isinstance(error, asyncio.TimeoutError):
            return "That took too long and was given up on."
        if isinstance(error, OSError):
            # Out of disk, read-only data directory, too many open files.
            return (
                f"The bot could not read or write its data folder: {error}. "
                "It may be out of disk space."
            )
        return None

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        """
        Turn the failures this cog can actually hit into plain sentences.

        Anything unrecognised is handed back to Red, which logs it properly
        rather than having this cog guess at a message for it.
        """
        if isinstance(error, commands.BotMissingPermissions):
            # discord.py calls the attribute `missing`; older releases (and
            # some forks) call it `missing_permissions`.
            names = getattr(error, "missing_permissions", None) or getattr(
                error, "missing", []
            )
            missing = humanize_list(
                [f"**{str(name).replace('_', ' ').title()}**" for name in names]
            ) or "required"
            await self._safe_send(
                ctx, f"I need the {missing} permission(s) in this channel to do that."
            )
            return
        if isinstance(error, commands.MaxConcurrencyReached):
            await self._safe_send(
                ctx, "This channel is already starting a game. Give it a moment."
            )
            return
        original = getattr(error, "original", None)
        if original is not None:
            message = self._friendly_error(original)
            if message is not None:
                log.error(
                    "The %s command failed in channel %s.",
                    getattr(ctx.command, "qualified_name", "retro"),
                    getattr(ctx.channel, "id", None),
                    exc_info=original,
                )
                await self._safe_send(ctx, message)
                return
        await ctx.bot.on_command_error(ctx, error, unhandled_by_cog=True)

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        name = Path(filename).name
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "rom"
        # libretro.py 0.6.x matches the extension against the core's
        # valid_extensions case-sensitively, so ".GB" must become ".gb".
        path = Path(name)
        name = path.stem + path.suffix.lower()
        return name[-64:]

    @staticmethod
    def _buildbot_url(core: str) -> typing.Optional[typing.Tuple[str, str]]:
        """Return (url, core filename) for this platform, or None if unknown."""
        machine = platform.machine().lower()
        if sys.platform.startswith("linux"):
            arch = {
                "x86_64": "x86_64",
                "amd64": "x86_64",
                "i686": "x86",
                "aarch64": "aarch64",
                "arm64": "aarch64",
                "armv7l": "armhf",
            }.get(machine)
            if arch is None:
                return None
            name = f"{core}_libretro.so"
            return f"{BUILDBOT}/linux/{arch}/latest/{name}.zip", name
        if sys.platform == "darwin":
            arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
            name = f"{core}_libretro.dylib"
            return f"{BUILDBOT}/apple/osx/{arch}/latest/{name}.zip", name
        if sys.platform in ("win32", "cygwin"):
            arch = "x86_64" if machine in ("amd64", "x86_64") else "x86"
            name = f"{core}_libretro.dll"
            return f"{BUILDBOT}/windows/{arch}/latest/{name}.zip", name
        return None

    async def _download_core(
        self, session: aiohttp.ClientSession, core: str
    ) -> typing.Tuple[bool, int, str]:
        """
        Fetch one core from the buildbot and record where it landed.

        Returns (installed, bytes on disk, message). Never raises: a core
        that fails is reported and the rest of the set carries on.
        """
        target = self._buildbot_url(core)
        if target is None:
            return False, 0, "no build for this platform"
        url, name = target
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, 0, f"buildbot returned status {resp.status}"
                payload = await resp.read()
        except aiohttp.ClientError as error:
            return False, 0, f"download failed: {error}"
        except asyncio.TimeoutError:
            return False, 0, "download timed out"

        core_path = self._cores_dir() / name
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                member = next(
                    (entry for entry in archive.namelist() if entry.endswith(name)),
                    None,
                )
                if member is None:
                    return False, 0, "the archive did not contain the core"
                data = archive.read(member)
        except (zipfile.BadZipFile, OSError) as error:
            return False, 0, f"the archive could not be read: {error}"

        try:
            await asyncio.to_thread(self._write_atomic, core_path, data)
        except OSError as error:
            return False, 0, f"could not be saved: {error}"

        try:
            async with self.config.cores() as cores:
                cores[core] = str(core_path)
        except Exception:
            # Not fatal any more: the file is on disk in the cog's own cores
            # folder, and that folder is scanned on every lookup, so the core
            # is playable whether or not the settings remember it.
            log.warning(
                "Could not record where the %s core was installed; it will "
                "still be found by scanning %s.",
                core,
                core_path.parent,
                exc_info=True,
            )
        return True, len(data), "installed"

    async def _download_bytes(
        self, url: str, max_size: int, size_label: str, what: str = "file"
    ) -> typing.Tuple[str, bytes]:
        """
        Fetch a URL into memory, capped at ``max_size``.

        Returns (filename, data). Raises DownloadError with a message written
        for the person who gave us the URL.
        """
        if not str(url).lower().startswith(("http://", "https://")):
            raise DownloadError(f"The {what} URL must start with `http://` or `https://`.")
        too_big = f"That {what} is bigger than the {size_label} limit."
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise DownloadError(
                            f"Downloading the {what} failed with status {resp.status}."
                        )
                    if (resp.content_length or 0) > max_size:
                        raise DownloadError(too_big)
                    # read(n) only returns the next chunk, so loop until the
                    # body ends or the size cap is exceeded.
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > max_size:
                            break
                    data = b"".join(chunks)
                    # Redirects (e.g. GitHub release assets) often end at a
                    # URL whose path has no real filename, so prefer the
                    # Content-Disposition header, then the URL we were given.
                    filename = ""
                    if resp.content_disposition is not None:
                        filename = resp.content_disposition.filename or ""
                    if not filename:
                        filename = Path(urlparse(url).path).name
                    if not filename:
                        filename = Path(str(resp.url.path)).name
        except aiohttp.ClientError as error:
            raise DownloadError(f"Downloading the {what} failed: {error}") from error
        except asyncio.TimeoutError as error:
            raise DownloadError(f"Downloading the {what} timed out.") from error
        if len(data) > max_size:
            raise DownloadError(too_big)
        return filename, data

    async def _auto_download_loop(self) -> None:
        """
        Fetch whatever cores are missing, once, in the background.

        Runs from cog_load as a detached task so loading the cog never waits
        on the buildbot. Every failure is logged and then dropped: a bot that
        cannot reach the internet must still come up, and a core the buildbot
        has stopped publishing must not be retried forever.
        """
        try:
            if not await self.config.auto_download_cores():
                return
            last = float(await self.config.auto_download_attempted_at() or 0.0)
            if time.time() - last < AUTO_DOWNLOAD_COOLDOWN_SECONDS:
                log.debug(
                    "Skipping the automatic core download; the last attempt "
                    "was less than %s hours ago.",
                    AUTO_DOWNLOAD_COOLDOWN_SECONDS // 3600,
                )
                return
            try:
                await self.bot.wait_until_red_ready()
            except Exception:
                pass
            await self._auto_download_cores()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "The automatic core download failed; giving up until the cog "
                "is loaded again. Run `[p]retroset download` to retry now."
            )

    async def _auto_download_cores(self) -> None:
        """Install every core that is not already on disk. Attempts each once."""
        installed = await self._installed_cores()
        missing = [name for name in CORES if name not in installed]
        if not missing:
            log.debug("All %s libretro cores are installed already.", len(CORES))
            return
        if self._buildbot_url(missing[0]) is None:
            log.warning(
                "The libretro buildbot has no builds for %s/%s, so the %s "
                "missing core(s) cannot be downloaded automatically.",
                sys.platform,
                platform.machine(),
                len(missing),
            )
            return
        # Recorded before the work starts, so a crash mid-download still
        # counts as an attempt and the cooldown still applies.
        await self.config.auto_download_attempted_at.set(time.time())
        log.info(
            "Downloading %s missing libretro core(s) in the background: %s",
            len(missing),
            ", ".join(missing),
        )
        succeeded = 0
        failed = 0
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name in missing:
                ok, size, message = await self._download_core(session, name)
                if ok:
                    succeeded += 1
                    log.info(
                        "Installed the %s libretro core (%s KiB).", name, size // 1024
                    )
                else:
                    failed += 1
                    log.warning(
                        "Could not download the %s libretro core: %s", name, message
                    )
        log.info(
            "Automatic core download finished: %s installed, %s failed.",
            succeeded,
            failed,
        )

    async def _fetch_rom(
        self, ctx: commands.Context, url: typing.Optional[str]
    ) -> typing.Optional[typing.Tuple[str, bytes]]:
        """
        Return (filename, data) from the URL or attachment, or None on error.

        A URL wins over an attachment, because it is the one the caller named
        explicitly (a preset resolves to a URL before we get here).
        """
        if url:
            try:
                filename, data = await self._download_bytes(
                    url, MAX_ROM_SIZE, MAX_ROM_SIZE_LABEL, "ROM"
                )
            except DownloadError as error:
                await self._safe_send(ctx, str(error))
                return None
            return filename or "rom.gb", data

        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > MAX_ROM_SIZE:
                await self._safe_send(
                    ctx,
                    f"That file is bigger than the {MAX_ROM_SIZE_LABEL} limit "
                    "for ROMs.",
                )
                return None
            try:
                return attachment.filename, await attachment.read()
            except discord.HTTPException as error:
                log.warning("Could not read a Retro ROM attachment.", exc_info=True)
                await self._safe_send(
                    ctx, f"The attached file could not be downloaded: {error}"
                )
                return None

        return None

    async def _extract_rom(
        self, ctx: commands.Context, filename: str, data: bytes
    ) -> typing.Optional[typing.Tuple[str, bytes]]:
        """
        Pull the first playable ROM out of a zip, or explain why we can't.

        The member is read into memory and handed back under its own name;
        nothing is ever unpacked using the paths stored in the archive.
        """
        try:
            found = await asyncio.to_thread(
                archives.extract,
                data,
                accept=lambda name: system_for_extension(Path(name).suffix) is not None,
                max_size=MAX_ROM_SIZE,
                what="ROM",
            )
        except archives.NoSupportedMember as error:
            lines = [f"`{filename}`: {error}", "", "Supported file types:"]
            lines.extend(self._supported_lines())
            for page in pagify("\n".join(lines)):
                await self._safe_send(ctx, page)
            return None
        except archives.ArchiveError as error:
            await self._safe_send(ctx, f"`{filename}`: {error}")
            return None
        except Exception:
            log.exception("Unpacking the zip %s failed unexpectedly.", filename)
            await self._safe_send(ctx, f"`{filename}` could not be unpacked.")
            return None

        if len(found.candidates) > 1:
            await self._safe_send(
                ctx,
                f"`{filename}` holds {len(found.candidates)} playable ROMs; "
                f"starting `{found.name}` (first in alphabetical order). "
                "Upload the one you want on its own to pick another.",
            )
        else:
            log.debug("Extracted %s from %s.", found.name, filename)
        return Path(found.name).name, found.data

    async def _no_rom_help(self, ctx: commands.Context) -> None:
        presets = await self.config.games()
        lines = [
            "Attach a console ROM (a `.zip` is fine) to your message, or pass "
            f"a URL: `{ctx.clean_prefix}retro <url>`.",
            "",
            "Try \N{GREEK SMALL LETTER MU}City, a free, open source city "
            "builder for the Game Boy Color:",
            f"`{ctx.clean_prefix}retro {EXAMPLE_ROM_URL}`",
        ]
        if presets:
            names = ", ".join(f"`{name}`" for name in sorted(presets)[:15])
            lines.append("")
            lines.append(f"You can also start a saved game by name: {names}")
        else:
            lines.append("")
            lines.append(
                "The bot owner can save games by name with "
                f"`{ctx.clean_prefix}retroset game add <name> <url>`, for "
                f"example `{ctx.clean_prefix}retroset game add "
                f"{EXAMPLE_GAME} {EXAMPLE_ROM_URL}`."
            )
        installed = await self._installed_cores()
        playable = [system for system in SYSTEMS if system.core in installed]
        if playable:
            lines.append("")
            lines.append("Consoles this bot can play right now:")
            lines.extend(self._supported_lines(playable))
        lines.append("")
        lines.append(
            "Only use ROMs you have the rights to. There are hundreds of free "
            f"homebrew games for these consoles at <{HOMEBREW_URL}>."
        )
        for page in pagify("\n".join(lines)):
            await self._safe_send(ctx, page)

    async def _resume_session(self, ctx: commands.Context, view: RetroView) -> None:
        """Point the channel at its existing session instead of starting over."""
        message = await view.resolve_message()
        if message is not None:
            status = "is already running" if view.live else "is asleep"
            await view.refresh()
            await self._safe_send(
                ctx,
                f"**{view.game_name}** {status} in this channel. Use the "
                f"controls to play: {message.jump_url}",
            )
            return
        # The old message is gone (deleted, or the bot lost it), so put the
        # game back on screen with a fresh one.
        await self._repost_session(ctx, view)

    async def _repost_session(self, ctx: commands.Context, view: RetroView) -> None:
        """Wake a session up and post a new message for it."""
        async with ctx.typing():
            try:
                clip = await self.run_press(view, None)
            except EmulatorError as error:
                await self._safe_send(ctx, f"The game could not be resumed: {error}")
                return
        view.last_clip = clip
        view.touch()
        try:
            view.message = await ctx.send(
                view._content(),
                file=view._clip_file(clip),
                view=view,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
            )
        except discord.HTTPException as error:
            # The game is fine; the new message is not. Put it back to sleep
            # so a core is not left running for a message nobody can see.
            log.exception(
                "Discord rejected the reposted Retro message in channel %s.",
                view.channel_id,
            )
            try:
                await self.hibernate(view, None)
            except Exception:
                log.exception("Could not hibernate after a failed repost.")
            await self._safe_send(ctx, self._http_error_message(error))
            return
        view.message_id = view.message.id
        await self._save_record(view)

    async def _abandon_session(
        self, ctx: commands.Context, view: RetroView, emulator: RetroEmulator
    ) -> None:
        """
        Drop a session that never got off the ground, without losing anything.

        Called when starting a game fails after the core came up: the record
        is removed, whatever was emulated is written to the save state, and
        the core is freed. Without this a failed send would leave an emulator
        loaded forever, and MAX_LIVE_EMULATORS is one.
        """
        self.sessions.pop(getattr(ctx.channel, "id", view.channel_id), None)
        try:
            await self._write_state(view, emulator)
        except Exception:
            log.exception("Could not save the state of an abandoned session.")
        try:
            await asyncio.to_thread(emulator.stop)
        except Exception:
            log.exception("Could not stop the emulator of an abandoned session.")
        view.emulator = None
        view.closed = True

    # -- Commands -----------------------------------------------------------

    @commands.max_concurrency(1, commands.BucketType.channel)
    @commands.guild_only()
    # Only Attach Files: the game is a clip and a row of buttons, with no
    # embed anywhere in the play loop. (`[p]retroset settings` still uses one,
    # and asks for Embed Links itself.)
    @commands.bot_has_permissions(attach_files=True)
    @commands.command()
    async def retro(self, ctx: commands.Context, *, game: typing.Optional[str] = None) -> None:
        """
        Play a retro console game in this channel.

        Pass the name of a saved game, a URL to a ROM, or attach one to the
        message. The console is picked from the file extension, so a `.gb`
        starts a Game Boy and a `.sfc` starts a Super Nintendo. A `.zip` is
        unpacked for you. Run it with no arguments to bring back the game
        already going in this channel.

        Games keep their progress: a session goes to sleep when nobody plays,
        and the next button press picks it back up, even after the bot
        restarts. Anyone in the channel can press the buttons.

        Only use ROMs you have the rights to. Hundreds of free homebrew games
        for these consoles are at <https://retrobrews.github.io/>.

        **Examples:**
        - `[p]retro` (with a ROM attached, or to resume this channel's game)
        - `[p]retro https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc`
        - `[p]retro ucity`

        **Arguments:**
        - `[game]` - A saved game name (see `[p]retroset game list`) or a ROM URL.
        """
        installed = await self._installed_cores()
        if not installed:
            await ctx.send(
                "No emulator cores are installed. Ask the bot owner to run "
                f"`{ctx.clean_prefix}retroset download` first."
            )
            return

        game = game.strip() if game else None
        existing = self.sessions.get(ctx.channel.id)

        # Bare `[p]retro` with a session in the channel means "bring it back".
        if game is None and not ctx.message.attachments:
            if existing is not None:
                await self._resume_session(ctx, existing)
                return
            await self._no_rom_help(ctx)
            return

        url: typing.Optional[str] = None
        source = "attachment"
        if game is not None:
            presets = await self.config.games()
            preset = presets.get(self._slug(game))
            if preset:
                url, source = preset, self._slug(game)
            elif game.lower().startswith(("http://", "https://")):
                url, source = game, game
            else:
                await ctx.send(
                    f"There's no saved game called `{game}`. Pass a ROM URL, "
                    f"attach a ROM, or see `{ctx.clean_prefix}retroset game list`."
                )
                return
            # Asking for the game that is already going here resumes it
            # rather than downloading the ROM all over again.
            if existing is not None and existing.source.lower() == source.lower():
                await self._resume_session(ctx, existing)
                return

        async with ctx.typing():
            rom = await self._fetch_rom(ctx, url)
            if rom is None:
                return
            filename, data = rom

            # Homebrew is nearly always distributed zipped, so look inside
            # before deciding there is no console for this file.
            if archives.is_zip(data) or filename.lower().endswith(".zip"):
                unpacked = await self._extract_rom(ctx, Path(filename).name, data)
                if unpacked is None:
                    return
                filename, data = unpacked

        filename = self._sanitize_filename(filename)
        system = system_for_extension(Path(filename).suffix)
        if system is None:
            lines = [
                f"`{Path(filename).suffix or filename}` isn't a console this "
                "bot knows. Supported file types:",
            ]
            lines.extend(self._supported_lines())
            for page in pagify("\n".join(lines)):
                await ctx.send(page)
            return
        if system.core not in installed:
            await ctx.send(
                self._missing_core_message(ctx.clean_prefix, system, system.core)
            )
            return

        # Catch obviously-broken content before handing it to the core. The
        # most common failure is a URL that serves an HTML page (for example
        # a GitHub "blob" page) instead of the ROM file itself.
        if data.lstrip()[:1] == b"<":
            await ctx.send(
                "That looks like a web page, not a ROM. If you used a URL, "
                "make sure it is a direct download link to the file."
            )
            return
        if len(data) < MIN_ROM_SIZE:
            await ctx.send("That file is too small to be a ROM.")
            return

        game_name = Path(filename).stem
        slug = self._slug(game_name)

        # Same game, already here: resume instead of restarting it.
        if existing is not None and existing.slug == slug:
            await self._resume_session(ctx, existing)
            return

        # A different game: bank the current one's progress and switch. Its
        # ROM and save state stay cached, so it can be started again later.
        if existing is not None:
            await self._retire(
                existing,
                f"Replaced by **{game_name}**. **{existing.game_name}** was "
                "saved \N{EM DASH} press Resume to come back to it.",
            )
            self.sessions.pop(ctx.channel.id, None)

        await self._start_session(ctx, game_name, slug, filename, data, source, system)

    async def _start_session(
        self,
        ctx: commands.Context,
        game_name: str,
        slug: str,
        filename: str,
        data: bytes,
        source: str,
        system,
    ) -> None:
        """Cache the ROM, boot it, and post the controls."""
        extension = Path(filename).suffix.lower() or f".{system.extensions[0]}"
        rom_filename = f"{ctx.channel.id}-{slug}{extension}"
        try:
            rom_path = self._roms_dir() / rom_filename
            await asyncio.to_thread(self._write_atomic, rom_path, data)
        except OSError as error:
            log.warning("Could not cache a Retro ROM.", exc_info=True)
            await self._safe_send(
                ctx,
                f"The ROM could not be saved: {error}. The bot may be out of "
                "disk space.",
            )
            return
        self._prune_cached_games(ctx.channel.id, slug)

        core_path = await self._core_path(system.core)
        if core_path is None:
            await self._safe_send(
                ctx, self._missing_core_message(ctx.clean_prefix, system, system.core)
            )
            return

        timeout_minutes = await self.config.session_timeout_minutes()
        view = RetroView(
            self,
            game_name=game_name,
            slug=slug,
            rom_filename=rom_filename,
            channel_id=ctx.channel.id,
            system=system,
            guild_id=ctx.guild.id if ctx.guild else None,
            starter_id=ctx.author.id,
            source=source,
            timeout_minutes=timeout_minutes,
            clip_seconds=await self.config.clip_seconds(),
            hold_ms=await self.config.hold_ms(),
        )
        self.sessions[ctx.channel.id] = view

        # This channel may well have played this game before -- five minutes
        # ago or a month and six games ago -- in which case its progress is
        # still cached under the same key. Starting it again picks that up
        # rather than booting over the top of it. Same fallback chain as
        # waking a hibernated session: save state, then the cartridge's
        # battery save, then the beginning.
        state, sram, restored_notice = self._saved_progress(ctx.channel.id, slug)

        emulator = RetroEmulator(
            core_path,
            rom_path,
            system_dir=self._system_dir(),
            options=await self._core_options(system.core),
        )
        try:
            async with ctx.typing():
                async with self.emulator_lock:
                    # Only one core at a time; park whatever else is playing
                    # (saving it first) and say so, so the other channel's
                    # players are not left wondering what happened.
                    evicted = await self._evict_locked(exclude=view)
                    notice = self._eviction_notice(ctx, evicted)
                    if notice:
                        # A courtesy message: if Discord refuses it, the game
                        # should still start.
                        await self._safe_send(ctx, notice)
                    await view.start(
                        ctx,
                        emulator,
                        state,
                        sram,
                        on_booted=lambda booted: self._settle_boot(
                            booted, state, restored_notice
                        ),
                    )
        except EmulatorError as error:
            await self._abandon_session(ctx, view, emulator)
            await self._safe_send(ctx, f"The game could not be started: {error}")
            return
        except discord.HTTPException as error:
            # Discord refused the message itself: a bad component, a missing
            # permission, an attachment the server will not take. The core is
            # already running at this point, so bank the progress and free it
            # rather than leaving an emulator loaded with no message to drive
            # it. The reply is deliberately plain text, since whatever
            # Discord objected to was in the rich version.
            log.exception(
                "Discord rejected the Retro game message in channel %s.",
                ctx.channel.id,
            )
            await self._abandon_session(ctx, view, emulator)
            await self._safe_send(ctx, self._http_error_message(error))
            return
        except Exception:
            await self._abandon_session(ctx, view, emulator)
            raise
        # This game is live on a new message now, so any older message still
        # offering to resume it stands down.
        await self._forget_retired_slug(ctx.channel.id, slug)
        # A core that declares nothing until a ROM is loaded (FCEUmm declares
        # all 44 of its options only now) has just told us what it offers, so
        # write that down while it is in front of us.
        await self._learn_options(system.core, emulator)
        await self._save_record(view)

    @commands.guild_only()
    @commands.command()
    async def retrostop(self, ctx: commands.Context) -> None:
        """
        Put this channel's game to sleep.

        The game is saved and the emulator is freed, but the controls stay
        live: pressing any button picks the game up again where it left off.
        This is the only way to stop a game; there is no Stop button.

        Only the person who started the game, members with the Manage
        Messages permission, and the bot owner can stop it.

        **Examples:**
        - `[p]retrostop`
        """
        view = self.sessions.get(ctx.channel.id)
        if view is None:
            await ctx.send("No game is running in this channel.")
            return
        if not await view.can_stop(ctx.author):
            await ctx.send(
                "Only the person who started the game, moderators, or the "
                "bot owner can stop it."
            )
            return
        try:
            async with view.lock:
                await self.hibernate(
                    view,
                    f"Stopped by {ctx.author.display_name}. Press a button "
                    "to pick up where you left off.",
                )
        except Exception:
            # Even if the view or its message is stale, make sure the game is
            # saved and the emulator is freed.
            log.exception("Failed to stop the Libretro session cleanly.")
            emulator, view.emulator = view.emulator, None
            if emulator is not None:
                await self._write_state(view, emulator)
                await asyncio.to_thread(emulator.stop)
        await ctx.send(
            "The game has been saved and put to sleep. Press any button on "
            "it to carry on."
        )

    @commands.group()
    @commands.is_owner()
    async def retroset(self, ctx: commands.Context):
        """
        Configure the Retro cog.
        """

    @retroset.command(name="download")
    async def retroset_download(
        self, ctx: commands.Context, core: typing.Optional[str] = None
    ) -> None:
        """
        Download emulator cores from the libretro buildbot.

        With no arguments this installs every console the cog supports,
        skipping any core that is already installed, so it is safe to run
        again after a failure. Naming a single core downloads just that one
        and replaces it if it is already there.

        This also happens by itself when the cog loads; see
        `[p]retroset autodownload`. There is nothing to configure either way:
        the cog finds whatever cores are in its own cores folder, so one
        dropped in by hand is picked up too, as long as it keeps its buildbot
        filename (`snes9x_libretro.so`, `gambatte_libretro.dll`).

        The whole set is about 5 MiB. None of these cores need a BIOS file.
        For one that does, see `[p]retroset bios`.

        **Examples:**
        - `[p]retroset download`
        - `[p]retroset download snes9x`

        **Arguments:**
        - `[core]` - One core name, or nothing for all of them.
        """
        if core is not None:
            core = core.strip().lower()
            core = core_name_from_filename(core) or core
            if core not in CORES:
                known = humanize_list([f"`{name}`" for name in sorted(CORES)])
                await ctx.send(
                    f"`{core}` is not a core this cog knows about. The "
                    f"supported cores are: {known}."
                )
                return
            wanted = [core]
            already: typing.Dict[str, Path] = {}
        else:
            already = await self._installed_cores()
            wanted = [name for name in CORES if name not in already]

        if not wanted:
            await ctx.send(
                f"All {len(CORES)} cores are already installed. Run "
                f"`{ctx.clean_prefix}retroset settings` to see them, or name "
                "one to re-download it."
            )
            return

        if self._buildbot_url(wanted[0]) is None:
            await ctx.send(
                "There is no libretro buildbot build for this platform. "
                "Download the cores yourself and drop them into "
                f"`{self._cores_dir()}` under their usual names "
                f"(`{core_filename('gambatte')}` and so on); the cog picks up "
                "whatever is in there."
            )
            return

        # May be None if the channel refused it; the download still runs and
        # the report at the end is what actually matters.
        status = await self._safe_send(
            ctx, f"Downloading {len(wanted)} core(s) from the libretro buildbot..."
        )
        installed: typing.List[str] = []
        failed: typing.List[str] = []
        total_bytes = 0
        timeout = aiohttp.ClientTimeout(total=300)
        async with ctx.typing():
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for index, name in enumerate(wanted, start=1):
                    ok, size, message = await self._download_core(session, name)
                    if ok:
                        installed.append(f"`{name}` ({CORES[name]}, {size // 1024} KiB)")
                        total_bytes += size
                    else:
                        failed.append(f"`{name}`: {message}")
                    if status is None:
                        continue
                    try:
                        await status.edit(
                            content=(
                                f"Downloading cores... {index}/{len(wanted)} "
                                f"({len(installed)} installed, {len(failed)} failed)"
                            )
                        )
                    except discord.HTTPException:
                        pass

        lines = []
        if installed:
            lines.append(
                f"Installed {len(installed)} core(s), "
                f"{total_bytes // 1024} KiB in total:"
            )
            lines.extend(f"- {entry}" for entry in installed)
        if already:
            lines.append(f"Already installed: {len(already)} core(s), left alone.")
        if failed:
            lines.append(f"Failed ({len(failed)}):")
            lines.extend(f"- {entry}" for entry in failed)
        if not installed and not failed:
            lines.append("Nothing to do.")
        lines.append(
            f"Play something with `{ctx.clean_prefix}retro`, or start with "
            f"\N{GREEK SMALL LETTER MU}City: `{ctx.clean_prefix}retro "
            f"{EXAMPLE_ROM_URL}`"
        )
        if status is not None:
            try:
                await status.delete()
            except discord.HTTPException:
                pass
        for page in pagify("\n".join(lines)):
            await self._safe_send(ctx, page)

    # -- Core options command -----------------------------------------------

    async def _send_pages(self, ctx: commands.Context, text: str) -> None:
        """
        Post a long answer, paginated, with buttons if it needs more than one.

        Red's SimpleMenu keeps a 44-option listing to a single message with
        arrows on it instead of four messages in a row.
        """
        pages = list(pagify(text, delims=["\n\n", "\n"], page_length=1400))
        if not pages:
            return
        if len(pages) == 1:
            await self._safe_send(ctx, pages[0])
            return
        numbered = [
            f"{page}\n\n*Page {index} of {len(pages)}*"
            for index, page in enumerate(pages, start=1)
        ]
        try:
            await SimpleMenu(numbered, timeout=OPTION_MENU_TIMEOUT).start(ctx)
        except discord.HTTPException:
            # The menu is a nicety; the content is the point.
            log.warning("Could not post a paginated Retro listing.", exc_info=True)
            for page in pages:
                await self._safe_send(ctx, page)

    async def _coreoptions_overview(self, ctx: commands.Context) -> None:
        """`[p]retroset coreoptions` with no core named."""
        installed = await self._installed_cores()
        overrides = await self.config.core_options()
        known = await self.config.core_option_definitions()
        lines = [
            "Core options are the emulator's own settings \N{EM DASH} region, "
            "sound quality, palette, and so on. They belong to the core, not "
            "to this cog, so you have to name one:",
            f"`{ctx.clean_prefix}retroset coreoptions <core> [key] [value]`",
            "",
        ]
        if not installed:
            lines.append(
                "No cores are installed yet. Run "
                f"`{ctx.clean_prefix}retroset download` first."
            )
            await self._send_pages(ctx, "\n".join(lines))
            return
        lines.append("Installed cores:")
        for name in sorted(installed):
            count = len(known.get(name) or {})
            set_here = len(overrides.get(name) or {})
            if count:
                detail = f"{count} option(s) known"
            else:
                detail = "options not read yet"
            if set_here:
                detail += f", **{set_here} changed**"
            lines.append(f"- `{name}` \N{EM DASH} {CORES.get(name, 'unknown core')}; {detail}")
        example = "gambatte" if "gambatte" in installed else sorted(installed)[0]
        lines.extend(
            [
                "",
                f"For example: `{ctx.clean_prefix}retroset coreoptions {example}`",
                "A core whose options have not been read yet is not a core "
                "without options \N{EM DASH} some only declare them once a game "
                "is loaded. Naming it here reads them.",
            ]
        )
        await self._send_pages(ctx, "\n".join(lines))

    async def _coreoptions_list(
        self,
        ctx: commands.Context,
        core: str,
        definitions: typing.Dict[str, dict],
        source: str,
    ) -> None:
        """`[p]retroset coreoptions <core>`: every option this core has."""
        overrides = await self._core_options(core)
        lines = [
            f"**`{core}`** \N{EM DASH} {CORES.get(core, 'unknown core')}",
            f"{len(definitions)} option(s), read from {source}.",
            f"Change one with `{ctx.clean_prefix}retroset coreoptions {core} "
            "<key> <value>`, see one in full with "
            f"`{ctx.clean_prefix}retroset coreoptions {core} <key>`, or put a "
            f"core default back with `... <key> {OPTION_RESET}`.",
            "",
        ]
        for key in sorted(definitions):
            definition = definitions[key]
            override = overrides.get(key)
            effective = self._effective_value(definition, override)
            default = self._option_default(definition)
            values = self._option_values(definition)
            marker = " *(default)*" if effective == default else " **(changed)**"
            shown = [f"`{value}`" for value in values[:MAX_LISTED_VALUES]]
            if len(values) > MAX_LISTED_VALUES:
                shown.append(f"+{len(values) - MAX_LISTED_VALUES} more")
            listed = ", ".join(shown) or "any value"
            lines.append(
                f"**{self._short_key(core, key)}** = `{effective}`{marker}"
            )
            lines.append(f"\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}`{key}` \N{BULLET} {listed}")
        await self._send_pages(ctx, "\n".join(lines))

    async def _coreoptions_show(
        self,
        ctx: commands.Context,
        core: str,
        key: str,
        definition: typing.Optional[dict],
    ) -> None:
        """`[p]retroset coreoptions <core> <key>`: one option, in full."""
        override = (await self._core_options(core)).get(key)
        effective = self._effective_value(definition, override)
        default = self._option_default(definition)
        values = self._option_values(definition)
        lines = [f"**`{key}`** \N{EM DASH} `{core}`"]
        if definition:
            if definition.get("desc"):
                lines.append(f"**{definition['desc']}**")
            if definition.get("info"):
                lines.append(definition["info"])
        lines.append("")
        if override is None:
            lines.append(f"Current: `{effective}` (the core's own default)")
        elif override != effective:
            lines.append(
                f"Current: `{effective}` \N{EM DASH} this core does not accept "
                f"`{override}`, so it falls back to its default."
            )
        else:
            lines.append(f"Current: `{effective}` (set here)")
        lines.append(f"Default: `{default or 'unknown'}`")
        if values:
            lines.append("Values: " + ", ".join(f"`{value}`" for value in values))
            labels = [
                f"`{value}` = {label}"
                for value, label in (definition.get("values") or ())
                if label and label != value
            ]
            if labels:
                lines.append("Labelled: " + ", ".join(labels))
        else:
            lines.append(
                "Values: unknown \N{EM DASH} this core has not told us what it "
                "accepts, so anything set here is passed through unchecked."
            )
        lines.append("")
        lines.append(
            f"Set it with `{ctx.clean_prefix}retroset coreoptions {core} "
            f"{self._short_key(core, key)} <value>`"
            + (
                f", or clear it with `... {OPTION_RESET}`."
                if override is not None
                else "."
            )
        )
        await self._send_pages(ctx, "\n".join(lines))

    async def _coreoptions_set(
        self,
        ctx: commands.Context,
        core: str,
        key: str,
        definition: typing.Optional[dict],
        value: str,
    ) -> None:
        """`[p]retroset coreoptions <core> <key> <value>`."""
        if value.lower() == OPTION_RESET:
            if (await self._core_options(core)).get(key) is None:
                await self._safe_send(
                    ctx,
                    f"`{key}` was not changed here, so it is already the "
                    f"core's default (`{self._option_default(definition) or 'unknown'}`).",
                )
                return
            await self._set_core_option(core, key, None)
            default = self._option_default(definition)
            applied = await self._apply_live(core, key, default) if default else ""
            await self._safe_send(
                ctx,
                f"`{key}` is back to the `{core}` core's own default"
                + (f" (`{default}`)." if default else ".")
                + applied,
            )
            return

        values = self._option_values(definition)
        if values:
            # Cores are inconsistent about case ("GBC", "disabled", "Low"), so
            # match case-insensitively but store the core's own spelling --
            # libretro.py compares the stored bytes exactly.
            match = next((v for v in values if v.lower() == value.lower()), None)
            if match is None:
                listed = ", ".join(f"`{v}`" for v in values)
                await self._safe_send(
                    ctx,
                    f"`{value}` is not something the `{core}` core accepts for "
                    f"`{key}`. Valid values: {listed}. Use "
                    f"`{OPTION_RESET}` to put the default "
                    f"(`{self._option_default(definition) or 'unknown'}`) back.",
                )
                return
            value = match
            warning = ""
        else:
            # FACT of the libretro option driver: a key or value the core does
            # not recognise is ignored and the core reads its default instead,
            # so an unvalidated setting can waste the owner's time but cannot
            # break a game.
            warning = (
                f"\n\N{WARNING SIGN}\N{VARIATION SELECTOR-16} This could not be "
                f"checked: the `{core}` core has not told us what `{key}` "
                "accepts. If the key or the value is wrong the core will "
                "quietly use its default instead \N{EM DASH} nothing will break, "
                "but nothing will change either. Start a game on this core "
                "once and its options become listable."
            )

        await self._set_core_option(core, key, value)
        applied = await self._apply_live(core, key, value)
        await self._safe_send(
            ctx, f"`{key}` is now `{value}` for the `{core}` core.{applied}{warning}"
        )

    async def _apply_live(self, core: str, key: str, value: str) -> str:
        """
        Push a changed option into a game that is already running.

        Returns the sentence to append to the reply, which says whether the
        change is live or waiting for the next session. A core decides for
        itself how much of its configuration it re-reads mid-game, so this
        deliberately does not claim the picture has changed.
        """
        async with self.emulator_lock:
            emulator = self._live_emulator_for(core)
            if emulator is None:
                return " It takes effect the next time a game starts on this core."
            changed = await asyncio.to_thread(emulator.set_option, key, value)
        if not changed:
            return (
                " The game running on this core could not be told about it, so "
                "it takes effect the next time a game starts."
            )
        return (
            " The game running on this core has been told about it now, though "
            "some settings (the console region, for one) only take hold when a "
            "game next starts."
        )

    @retroset.command(name="coreoptions", aliases=["coreopts"])
    async def retroset_coreoptions(
        self,
        ctx: commands.Context,
        core: typing.Optional[str] = None,
        key: typing.Optional[str] = None,
        *,
        value: typing.Optional[str] = None,
    ) -> None:
        """
        Read and change a libretro core's own settings.

        Each core has its own options — Gambatte has 32, FCEUmm 44, Genesis
        Plus GX 62 — covering things like the console region, sound quality
        and colour palette. Changes are stored per core and applied every time
        that core loads a game.

        The key may be given with or without the core's prefix, so
        `fceumm_region` and `region` both work, as does any unambiguous ending
        of a key. Use `reset` as the value to put the core's own default back.

        Reading a core's options may need to load it, and only one core can be
        loaded at a time, so a game that is running is saved and put to sleep
        first — exactly as starting a game in another channel would.

        **Examples:**
        - `[p]retroset coreoptions`
        - `[p]retroset coreoptions gambatte`
        - `[p]retroset coreoptions gambatte gb_colorization`
        - `[p]retroset coreoptions gambatte gambatte_gb_colorization GBC`
        - `[p]retroset coreoptions fceumm region PAL`
        - `[p]retroset coreoptions fceumm region reset`

        **Arguments:**
        - `[core]` - A core name, e.g. `gambatte`. Omit it to list the cores.
        - `[key]` - An option key, with or without the core's prefix.
        - `[value]` - The value to set, or `reset` for the core's default.
        """
        if core is None:
            await self._coreoptions_overview(ctx)
            return

        core = core.strip().strip("`").lower()
        core = core_name_from_filename(core) or core
        if core not in CORES:
            known = humanize_list([f"`{name}`" for name in sorted(CORES)])
            await self._safe_send(
                ctx,
                f"`{core}` is not a core this cog knows about. The supported "
                f"cores are: {known}.",
            )
            return

        installed = await self._core_path(core) is not None
        async with ctx.typing():
            definitions, source = await self._definitions_for(core, probe=installed)

        if not definitions:
            system = system_for_core(core)
            hint = (
                f"Start a {system.name} game once "
                f"(`{ctx.clean_prefix}retro <rom>`) and its options become "
                "listable from then on."
                if system is not None
                else "Start a game on it once and its options become listable."
            )
            if not installed:
                hint = (
                    f"It is not installed; run `{ctx.clean_prefix}retroset "
                    f"download {core}` first."
                )
            message = (
                f"The `{core}` core has not told us what options it has. That "
                "does **not** mean it has none: some cores only declare their "
                f"settings once a ROM is loaded. {hint}"
            )
            if key is None or value is None:
                await self._safe_send(ctx, message)
                return
            # Setting still goes ahead, unvalidated: libretro.py checks every
            # key and value against the core's own list and falls back to the
            # default for anything it does not recognise, so the worst case is
            # that nothing happens. _coreoptions_set says as much.
            await self._coreoptions_set(ctx, core, key.strip().strip("`"), None, value)
            return

        if key is None:
            await self._coreoptions_list(ctx, core, definitions, source)
            return

        resolved, error = self._resolve_option_key(
            core, definitions, key, ctx.clean_prefix
        )
        if error is not None:
            await self._safe_send(ctx, error)
            return
        if resolved is None:
            await self._safe_send(ctx, f"`{key}` is not an option of the `{core}` core.")
            return

        if value is None:
            await self._coreoptions_show(ctx, core, resolved, definitions[resolved])
            return
        await self._coreoptions_set(
            ctx, core, resolved, definitions[resolved], value.strip().strip("`")
        )

    @retroset.command(name="timeout")
    async def retroset_timeout(self, ctx: commands.Context, minutes: int) -> None:
        """
        Set how long a game can idle before it goes to sleep.

        A sleeping game is saved and its emulator is freed, but its controls
        keep working: the next button press wakes it up where it left off.
        The value is clamped between 1 and 120 minutes and applies to
        sessions started afterwards. The default is 10 minutes.

        **Examples:**
        - `[p]retroset timeout 30`

        **Arguments:**
        - `<minutes>` - Minutes without input before the game sleeps (1-120).
        """
        minutes = max(1, min(120, minutes))
        await self.config.session_timeout_minutes.set(minutes)
        await ctx.send(
            f"Games now go to sleep after {minutes} minutes without input. "
            "Pressing a button wakes them up again."
        )

    @retroset.command(name="cliplength", aliases=["clip"])
    async def retroset_cliplength(self, ctx: commands.Context, seconds: int) -> None:
        """
        Set how many seconds of play each clip shows.

        Every button press posts an animated clip of what happened next.
        Longer clips show more of the game but take longer to record and
        upload. The value is clamped between 1 and 15 seconds and applies to
        clips recorded afterwards. The default is 4 seconds.

        **Examples:**
        - `[p]retroset cliplength 8`

        **Arguments:**
        - `<seconds>` - Seconds of play per clip (1-15).
        """
        seconds = max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, seconds))
        await self.config.clip_seconds.set(seconds)
        for view in self.sessions.values():
            view.clip_seconds = seconds
        await ctx.send(f"Clips now show {seconds} seconds of play.")

    @retroset.command(name="hold")
    async def retroset_hold(self, ctx: commands.Context, milliseconds: int) -> None:
        """
        Set how long a button is held down when someone presses it.

        Too short and a game polling its controller a few times a second
        misses the press entirely; too long and one press does the job twice
        — a Game Boy walk cycle is 16 frames, so holding a direction past
        about 270ms walks two tiles instead of one. Every button, directions
        included, is held for this long.

        The value is clamped between 50 and 2000 milliseconds. The default is
        160.

        **Examples:**
        - `[p]retroset hold 200`

        **Arguments:**
        - `<milliseconds>` - How long a button stays down (50-2000).
        """
        milliseconds = max(MIN_HOLD_MS, min(MAX_HOLD_MS, milliseconds))
        await self.config.hold_ms.set(milliseconds)
        for view in self.sessions.values():
            view.hold_ms = milliseconds
        await ctx.send(
            f"Every button, directions included, is now held for "
            f"{milliseconds}ms."
        )

    @retroset.group(name="game")
    async def retroset_game(self, ctx: commands.Context) -> None:
        """
        Manage the games players can start by name.
        """

    @retroset_game.command(name="add")
    async def retroset_game_add(self, ctx: commands.Context, name: str, url: str) -> None:
        """
        Save a game so anyone can start it with `[p]retro <name>`.

        The URL must be a direct download link to a ROM whose file extension
        matches one of the supported consoles, or to a `.zip` containing one.

        Only add ROMs you have the rights to share. There are hundreds of free
        homebrew games at <https://retrobrews.github.io/>.

        **Examples:**
        - `[p]retroset game add ucity https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc`
        - `[p]retroset game add tobu https://example.com/tobu.gb`

        **Arguments:**
        - `<name>` - The name players will type.
        - `<url>` - A direct `http://` or `https://` link to the ROM.
        """
        if not url.lower().startswith(("http://", "https://")):
            await ctx.send("The ROM URL must start with `http://` or `https://`.")
            return
        parsed = urlparse(url)
        if not parsed.netloc:
            await ctx.send("That URL is missing a hostname.")
            return
        key = self._slug(name)
        async with self.config.games() as games:
            existed = key in games
            games[key] = url
        verb = "updated" if existed else "added"
        await ctx.send(
            f"Game `{key}` {verb}. Start it with `{ctx.clean_prefix}retro {key}`."
        )

    @retroset_game.command(name="remove", aliases=["delete", "del"])
    async def retroset_game_remove(self, ctx: commands.Context, name: str) -> None:
        """
        Forget a saved game.

        **Examples:**
        - `[p]retroset game remove tobu`

        **Arguments:**
        - `<name>` - The saved game to remove.
        """
        key = self._slug(name)
        async with self.config.games() as games:
            if key not in games:
                await ctx.send(f"There's no saved game called `{key}`.")
                return
            del games[key]
        await ctx.send(f"Game `{key}` removed.")

    @retroset_game.command(name="list")
    async def retroset_game_list(self, ctx: commands.Context) -> None:
        """
        List the games players can start by name.

        **Examples:**
        - `[p]retroset game list`
        """
        games = await self.config.games()
        if not games:
            await ctx.send(
                "No games are saved yet. Add one with "
                f"`{ctx.clean_prefix}retroset game add <name> <url>`."
            )
            return
        lines = "\n".join(f"- `{name}`: <{games[name]}>" for name in sorted(games))
        for page in pagify(lines):
            await ctx.send(page)

    @retroset.group(name="bios", aliases=["firmware", "system"])
    async def retroset_bios(self, ctx: commands.Context) -> None:
        """
        Manage the BIOS/firmware files cores can use.

        Some consoles cannot boot a game without a copy of their own firmware.
        Cores look for it in the frontend's *system directory*, which this cog
        keeps inside its data folder; `[p]retroset settings` shows where.

        Most cores want a bare file at the top of that directory, but a few
        want theirs in a subfolder of it, so a `.zip` is unpacked with its own
        folders intact and every file in it is installed.

        **Nothing is downloaded or suggested for you.** This cog ships no
        firmware, will never fetch any on its own, and names none: console
        BIOS images are copyrighted, and it is up to you to supply a copy you
        are entitled to use. Every core the cog installs by default is
        BIOS-free, so this is only needed if you add a core that is not.
        """

    @retroset_bios.command(name="add")
    async def retroset_bios_add(
        self,
        ctx: commands.Context,
        filename: typing.Optional[str] = None,
        url: typing.Optional[str] = None,
    ) -> None:
        """
        Put BIOS files you supply into the system directory.

        Attach the file to your message, or pass a direct URL. A `.zip` is
        unpacked for you and **every** file in it is installed, keeping the
        folders it had inside the archive, because a firmware set is usually
        several files and some cores want theirs in a subfolder.

        `<filename>` is optional and only means anything for a single file: it
        is the exact name the core will look for, so it has to match what that
        core documents. Leave it off and the file keeps its own name.

        Only add firmware you are entitled to use. This cog does not provide
        any and cannot tell you where to find it.

        **Examples:**
        - `[p]retroset bios add` (with a file or a `.zip` attached)
        - `[p]retroset bios add https://example.com/firmware.zip`
        - `[p]retroset bios add somesystem_bios.bin` (with the file attached)
        - `[p]retroset bios add somesystem_bios.bin https://example.com/bios.bin`

        **Arguments:**
        - `[filename]` - The exact filename one core expects, if you need to rename it.
        - `[url]` - A direct link to the file, if you are not attaching it.
        """
        # `bios add <url>` has to work, and so does the older
        # `bios add <name> [url]`, so a first argument that is obviously a URL
        # is treated as one rather than as a (hopeless) filename.
        if url is None and filename and filename.lower().startswith(("http://", "https://")):
            filename, url = None, filename

        name: typing.Optional[str] = None
        if filename:
            name = self._bios_name(filename)
            if name is None:
                await ctx.send(
                    "That is not a usable filename. Give the bare name the "
                    "core looks for, with no folders in it, for example "
                    "`somesystem_bios.bin` \N{EM DASH} or leave it off "
                    "entirely and the file keeps its own name."
                )
                return

        fetched = await self._fetch_bios(ctx, name, url)
        if fetched is None:
            return
        source, data = fetched

        if archives.is_zip(data) or str(source).lower().endswith(".zip"):
            await self._install_bios_archive(ctx, source, data, name)
            return

        name = name or self._bios_name(Path(str(source)).name)
        if name is None:
            await ctx.send(
                f"`{Path(str(source)).name}` is not a usable filename. Say "
                "what the core should see it as: `"
                f"{ctx.clean_prefix}retroset bios add <filename>"
                f"{' <url>' if url else ''}`."
            )
            return
        if not data:
            await ctx.send("That file is empty.")
            return
        if len(data) > MAX_BIOS_SIZE:
            await ctx.send(
                f"That BIOS file is bigger than the {MAX_BIOS_SIZE_LABEL} limit."
            )
            return

        target = self._system_dir() / name
        try:
            await asyncio.to_thread(self._write_atomic, target, data)
        except OSError as error:
            log.warning("Could not write the BIOS file %s", target, exc_info=True)
            await ctx.send(f"The file could not be saved: {error}")
            return
        log.info("Installed the BIOS file %s (%s bytes).", name, len(data))
        await ctx.send(
            f"Stored `{name}` ({len(data):,} bytes) in the system directory. "
            f"Cores will find it from now on. See "
            f"`{ctx.clean_prefix}retroset bios list`."
        )

    async def _fetch_bios(
        self,
        ctx: commands.Context,
        name: typing.Optional[str],
        url: typing.Optional[str],
    ) -> typing.Optional[typing.Tuple[str, bytes]]:
        """(source name, bytes) from the URL or attachment, or None on error."""
        if url:
            try:
                return await self._download_bytes(
                    url,
                    MAX_BIOS_ARCHIVE_SIZE,
                    MAX_BIOS_ARCHIVE_SIZE_LABEL,
                    "BIOS file",
                )
            except DownloadError as error:
                await self._safe_send(ctx, str(error))
                return None
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > MAX_BIOS_ARCHIVE_SIZE:
                await self._safe_send(
                    ctx,
                    "That file is bigger than the "
                    f"{MAX_BIOS_ARCHIVE_SIZE_LABEL} limit.",
                )
                return None
            try:
                return attachment.filename, await attachment.read()
            except discord.HTTPException as error:
                log.warning("Could not read a Retro BIOS attachment.", exc_info=True)
                await self._safe_send(
                    ctx, f"The attached file could not be downloaded: {error}"
                )
                return None
        await self._safe_send(
            ctx,
            "Attach the BIOS file (or a `.zip` of them) to your message, or "
            f"pass a direct URL: `{ctx.clean_prefix}retroset bios add "
            f"{name or '<url>'}{' <url>' if name else ''}`.",
        )
        return None

    async def _install_bios_archive(
        self,
        ctx: commands.Context,
        source: str,
        data: bytes,
        name: typing.Optional[str],
    ) -> None:
        """
        Unpack a whole firmware archive into the system directory.

        On where things land: libretro's *system directory* is the one folder
        a core is handed, and cores disagree about what is in it. Most ask for
        a bare filename at its root (`disksys.rom`, `scph5501.bin`); a good
        few ask for a subfolder of it (`dc/dc_boot.bin`, `np2kai/FONT.ROM`,
        `Mupen64plus/*`). So the archive's own layout is preserved, relative
        to the system directory, which is the layout every firmware set is
        packaged in and the only one that can satisfy both kinds of core. Each
        path component is validated (never rewritten) first: cores look for an
        exact filename, so a name that cannot be stored truthfully is refused
        rather than mangled into one the core will never ask for.
        """
        try:
            unpacked = await asyncio.to_thread(
                archives.extract_all,
                data,
                max_total_size=MAX_BIOS_TOTAL_SIZE,
                max_file_size=MAX_BIOS_SIZE,
                max_files=MAX_BIOS_FILES,
                what="BIOS file",
            )
        except archives.NoSupportedMember as error:
            await self._safe_send(ctx, f"`{source}`: {error}")
            return
        except archives.ArchiveError as error:
            await self._safe_send(ctx, f"That zip could not be read: {error}")
            return
        except Exception:
            log.exception("Unpacking the BIOS zip %s failed unexpectedly.", source)
            await self._safe_send(ctx, f"`{source}` could not be unpacked.")
            return

        renamed = ""
        files = list(unpacked.files)
        if name and len(files) == 1:
            # One file in the zip and a name was given: the old behaviour,
            # which is how somebody puts `bios.bin` in as `scph5501.bin`.
            if files[0].path != name:
                renamed = f" `{files[0].member}` was stored as `{name}`."
            files = [files[0]._replace(path=name)]
        elif name:
            renamed = (
                f" The `{name}` you named was ignored: a zip of "
                f"{len(files)} files keeps its own names."
            )

        try:
            written, total = await asyncio.to_thread(self._write_bios_files, files)
        except OSError as error:
            log.warning("Could not unpack a BIOS archive.", exc_info=True)
            await self._safe_send(
                ctx,
                f"The files could not be saved: {error}. The bot may be out "
                "of disk space.",
            )
            return

        if not written:
            await self._safe_send(
                ctx, f"Nothing from `{source}` could be written to disk."
            )
            return
        log.info(
            "Installed %s BIOS file(s) from %s (%s bytes).", len(written), source, total
        )

        listed = ", ".join(f"`{path}`" for path in written[:MAX_LISTED_BIOS_FILES])
        if len(written) > MAX_LISTED_BIOS_FILES:
            listed += f", and {len(written) - MAX_LISTED_BIOS_FILES} more"
        lines = [
            f"Installed **{len(written)}** file(s) from `{source}`, "
            f"{total:,} bytes in total, into the system directory: {listed}."
            + renamed
        ]
        if unpacked.skipped:
            lines.append(
                f"{len(unpacked.skipped)} entr(y/ies) in the zip were skipped: "
                "an unusable name, a symlink, an empty file, or one over the "
                f"{MAX_BIOS_SIZE_LABEL} per-file limit."
            )
        lines.append(
            "Folders inside the zip were kept, since some cores look for "
            "their firmware in one. See "
            f"`{ctx.clean_prefix}retroset bios list`."
        )
        for page in pagify("\n".join(lines)):
            await self._safe_send(ctx, page)

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

    @retroset_bios.command(name="list")
    async def retroset_bios_list(self, ctx: commands.Context) -> None:
        """
        List the BIOS files in the system directory.

        **Examples:**
        - `[p]retroset bios list`
        """
        files = self._bios_files()
        directory = self._system_dir()
        if not files:
            await ctx.send(
                f"No BIOS files are installed. The system directory is "
                f"`{directory}`; add a file you are entitled to use with "
                f"`{ctx.clean_prefix}retroset bios add`. Every core this cog "
                "installs by default works without one."
            )
            return
        total = sum(size for _, size in files)
        lines = [
            f"System directory: `{directory}`",
            f"{len(files)} file(s), {total:,} bytes.",
            "",
        ]
        lines.extend(f"- `{name}` ({size:,} bytes)" for name, size in files)
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    @retroset_bios.command(name="remove", aliases=["delete", "del"])
    async def retroset_bios_remove(self, ctx: commands.Context, filename: str) -> None:
        """
        Delete a BIOS file from the system directory.

        Give the name exactly as `[p]retroset bios list` shows it, including
        the folder if it is in one.

        **Examples:**
        - `[p]retroset bios remove somesystem_bios.bin`
        - `[p]retroset bios remove somesystem/bios.bin`

        **Arguments:**
        - `<filename>` - The file to delete, as shown by `[p]retroset bios list`.
        """
        target = self._bios_path(filename)
        if target is None:
            await ctx.send(
                "That is not a usable filename. Give it exactly as "
                f"`{ctx.clean_prefix}retroset bios list` shows it."
            )
            return
        name = filename.strip().strip('"').strip("'")
        try:
            if not target.is_file():
                await ctx.send(
                    f"There is no `{name}` in the system directory. See "
                    f"`{ctx.clean_prefix}retroset bios list`."
                )
                return
            target.unlink()
            # Take the folder with it if that was the last thing in it, so
            # removing a firmware set does not leave an empty tree behind.
            parent = target.parent
            root = self._system_dir().resolve()
            while parent != root and root in parent.parents:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except OSError as error:
            log.warning("Could not delete the BIOS file %s", target, exc_info=True)
            await ctx.send(f"`{name}` could not be deleted: {error}")
            return
        await ctx.send(f"`{name}` was deleted from the system directory.")

    @retroset.command(name="autodownload")
    async def retroset_autodownload(
        self, ctx: commands.Context, enabled: typing.Optional[bool] = None
    ) -> None:
        """
        Fetch missing cores automatically when the cog loads.

        On by default. Missing cores are downloaded in the background shortly
        after the cog starts, so a fresh install can play something straight
        away. Cores already on disk are never re-downloaded, and a failed
        attempt is logged and dropped rather than retried in a loop.

        Run it with no argument to see the current setting.

        **Examples:**
        - `[p]retroset autodownload`
        - `[p]retroset autodownload false`

        **Arguments:**
        - `[enabled]` - `true` or `false`.
        """
        if enabled is None:
            current = await self.config.auto_download_cores()
            missing = len(CORES) - len(await self._installed_cores())
            await ctx.send(
                f"Automatic core downloads are **{'on' if current else 'off'}**. "
                f"{missing} core(s) are missing. Change it with "
                f"`{ctx.clean_prefix}retroset autodownload <true|false>`."
            )
            return
        await self.config.auto_download_cores.set(bool(enabled))
        if enabled:
            # Clear the cooldown so turning it back on takes effect at the
            # next load rather than up to six hours later.
            await self.config.auto_download_attempted_at.set(0.0)
            await ctx.send(
                "Missing cores will be downloaded automatically the next time "
                f"the cog loads. Run `{ctx.clean_prefix}retroset download` to "
                "do it now."
            )
        else:
            await ctx.send(
                "Cores will no longer be downloaded automatically. Install "
                f"them with `{ctx.clean_prefix}retroset download`."
            )

    @retroset.command(name="settings")
    @commands.bot_has_permissions(embed_links=True)
    async def retroset_settings(self, ctx: commands.Context) -> None:
        """
        Show the current Retro settings.

        **Examples:**
        - `[p]retroset settings`
        """
        installed = await self._installed_cores()
        cores_dir = self._cores_dir()
        embed = discord.Embed(
            title="Retro Settings",
            colour=await ctx.embed_colour(),
        )

        if not installed:
            cores_value = (
                f"None installed. Run `{ctx.clean_prefix}retroset download` "
                f"to get them, or drop them into `{cores_dir}` yourself."
            )
        else:
            entries = []
            for name in sorted(CORES):
                if name not in installed:
                    continue
                path = installed[name]
                # Say so when a core is being used from outside the folder the
                # cog manages, since that one is not something `[p]retroset
                # download` will ever replace.
                elsewhere = "" if path.parent == cores_dir else f" (from `{path.parent}`)"
                entries.append(
                    f"\N{WHITE HEAVY CHECK MARK} `{name}` - {CORES[name]}{elsewhere}"
                )
            missing = len(CORES) - len(installed)
            if missing > 0:
                entries.append(
                    f"{missing} more available from "
                    f"`{ctx.clean_prefix}retroset download`."
                )
            cores_value = "\n".join(entries)[:1024]
        embed.add_field(
            name=f"Cores ({len(installed)}/{len(CORES)} installed)",
            value=f"`{cores_dir}`\n{cores_value}"[:1024],
            inline=False,
        )

        overrides = await self.config.core_options()
        known = await self.config.core_option_definitions()
        changed = {name: values for name, values in overrides.items() if values}
        if changed:
            entries = []
            for name in sorted(changed):
                for key in sorted(changed[name]):
                    entries.append(f"- `{key}` = `{changed[name][key]}`")
            total = sum(len(values) for values in changed.values())
            options_value = (
                f"{total} option(s) changed on {len(changed)} core(s):\n"
                + "\n".join(entries)
            )
        else:
            options_value = (
                "Every core is running on its own defaults. Change one with "
                f"`{ctx.clean_prefix}retroset coreoptions <core> <key> <value>`."
            )
        if known:
            options_value += (
                f"\nOption definitions are known for {len(known)} core(s)."
            )
        embed.add_field(name="Core options", value=options_value[:1024], inline=False)

        auto = await self.config.auto_download_cores()
        embed.add_field(
            name="Automatic core downloads",
            value=(
                ("**On** \N{EM DASH} missing cores are fetched in the "
                 "background when the cog loads."
                 if auto else
                 "**Off** \N{EM DASH} install cores with "
                 f"`{ctx.clean_prefix}retroset download`.")
            ),
            inline=False,
        )

        bios = self._bios_files()
        if bios:
            listed = "\n".join(f"- `{name}` ({size:,} bytes)" for name, size in bios)
            bios_value = f"{len(bios)} file(s):\n{listed}"
        else:
            bios_value = (
                "No BIOS files installed. Every core above works without one; "
                f"add your own with `{ctx.clean_prefix}retroset bios add`."
            )
        embed.add_field(
            name="System directory (BIOS)",
            value=f"`{self._system_dir()}`\n{bios_value}"[:1024],
            inline=False,
        )

        timeout_minutes = await self.config.session_timeout_minutes()
        embed.add_field(
            name="Sleep after",
            value=(
                f"{timeout_minutes} minutes without input. Sleeping games are "
                "saved and wake up on the next button press."
            ),
            inline=False,
        )
        clip_seconds = await self.config.clip_seconds()
        embed.add_field(
            name="Clip length",
            value=(
                f"{clip_seconds} seconds of play per button press. Replay "
                f"stitches the last {REPLAY_SECONDS} seconds back together "
                "from clips kept in memory, so a restart empties it."
            ),
            inline=False,
        )
        hold_ms = await self.config.hold_ms()
        embed.add_field(
            name="Button hold",
            value=f"{hold_ms}ms per press, directions included",
            inline=False,
        )
        games = await self.config.games()
        if not games:
            games_value = "None saved"
        else:
            names = ", ".join(f"`{name}`" for name in sorted(games))
            games_value = names if len(names) <= 1000 else f"{len(games)} saved"
        embed.add_field(name="Saved games", value=games_value, inline=False)
        awake = sum(1 for view in self.sessions.values() if view.live)
        batteries = sum(
            1
            for view in self.sessions.values()
            if self._sram_path(view.channel_id, view.slug).is_file()
        )
        sessions_value = (
            f"{len(self.sessions)} total, {awake} awake "
            f"(at most {MAX_LIVE_EMULATORS} can be awake at once)"
        )
        if batteries:
            sessions_value += (
                f"\n{batteries} have an in-game battery save on disk, which "
                "survives a core update even when the save state does not."
            )
        embed.add_field(name="Sessions", value=sessions_value, inline=False)
        await self._safe_send(ctx, embed=embed)
