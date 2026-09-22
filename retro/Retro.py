"""
The Retro cog: one game per channel, driven by buttons under a clip.

The class must stay called `Retro` and must stay in `retro/Retro.py`: Red
derives BOTH of this cog's storage locations from the class name (Config's
namespace and the data directory), so renaming it orphans every install. See
retro/migration.py, which exists because it was renamed once already.

It is assembled from mixins, which is how Red's own multi-file cogs are
built. Each one is a separate concern with its own module:

* retro/storage.py -- the data directory, every path in it, the atomic
  writes, the pruning and the disk budget;
* retro/cores.py -- installing libretro cores and reading and writing their
  own options;
* retro/saves.py -- the whole `[p]retrosaves` group;
* retro/migration.py -- the one-off move out of the old `RetroCog`
  namespace, which will never need to change again.

What is left here is the cog itself: its Config schema, the session
lifecycle (start, hibernate, wake, retire, resume, forget), talking to
Discord without letting an HTTP failure surface as a traceback, the rate
limits, and the `[p]retro`, `[p]retrostop`, `[p]retroreset` and `[p]retroset`
commands.

Constants that moved into those modules are re-exported at the bottom of
this one, so `retro.Retro.<NAME>` keeps meaning what it always did.
"""

import asyncio
import io
import logging
import re
import time
import typing
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_list, pagify
from redbot.core.utils.views import SimpleMenu

from . import archives, net, version
from .abc import CompositeMetaClass
from .cores import (
    AUTO_DOWNLOAD_COOLDOWN_SECONDS,
    BUILDBOT,
    CORE_DOWNLOAD_TIMEOUT_SECONDS,
    MAX_LISTED_VALUES,
    OPTION_RESET,
    CoresMixin,
)
from .emulator import (
    CLIP_SECONDS,
    DEFAULT_FPS,
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    MIN_ROM_SIZE,
    EmulatorError,
    RetroEmulator,
    clamp_clip_seconds,
    describe_seconds,
    format_seconds,
)
from .migration import CONFIG_STORE_FILENAME, LEGACY_COG_NAME, MigrationMixin
from .RetroView import (
    DEFAULT_HOLD_MS,
    DEFAULT_TIMEOUT_MINUTES,
    MAX_HOLD_MS,
    MIN_HOLD_MS,
    MIN_REPEAT_TAPS,
    REPEAT_TAPS,
    SAVE_STATE_EVERY_PRESSES,
    Progress,
    RetiredView,
    RetroView,
    press_plan,
    presser_name,
    restore_into,
)
from .saves import (
    CONFIRM_TIMEOUT,
    DEFAULT_UPLOAD_LIMIT,
    EXPORT_CHOICES,
    MAX_IMPORT_SRAM_LABEL,
    MAX_IMPORT_SRAM_SIZE,
    MAX_IMPORT_STATE_LABEL,
    MAX_IMPORT_STATE_SIZE,
    SAVE_COOLDOWN_RATE,
    SAVE_COOLDOWN_SECONDS,
    SRAM_EXTENSIONS,
    STATE_EXTENSIONS,
    SaveInfo,
    SavesMixin,
)
from .storage import (
    BACKUP_SUFFIX,
    DEFAULT_DISK_BUDGET_MB,
    MAX_CACHED_GAMES_PER_CHANNEL,
    MAX_DISK_BUDGET_MB,
    StorageMixin,
)
from .systems import (
    CORES,
    SYSTEMS,
    core_filename,
    core_name_from_filename,
    system_for_extension,
)

#: Everything this module publishes, which is the cog class plus the names
#: that used to be defined here and are now re-exported from the modules the
#: cog was split into. Tests and `retro/__init__.py` reach for several of
#: them through `retro.Retro`, and an import site is a promise like any
#: other, so the list is explicit rather than incidental.
__all__ = [
    "Retro",
    "DownloadError",
    "SaveInfo",
    # Storage
    "BACKUP_SUFFIX",
    "DEFAULT_DISK_BUDGET_MB",
    "MAX_CACHED_GAMES_PER_CHANNEL",
    "MAX_DISK_BUDGET_MB",
    # Cores and their options
    "AUTO_DOWNLOAD_COOLDOWN_SECONDS",
    "BUILDBOT",
    "CORE_DOWNLOAD_TIMEOUT_SECONDS",
    "MAX_LISTED_VALUES",
    "OPTION_RESET",
    # Saves
    "CONFIRM_TIMEOUT",
    "DEFAULT_UPLOAD_LIMIT",
    "EXPORT_CHOICES",
    "MAX_IMPORT_SRAM_LABEL",
    "MAX_IMPORT_SRAM_SIZE",
    "MAX_IMPORT_STATE_LABEL",
    "MAX_IMPORT_STATE_SIZE",
    "SAVE_COOLDOWN_RATE",
    "SAVE_COOLDOWN_SECONDS",
    "SRAM_EXTENSIONS",
    "STATE_EXTENSIONS",
    # The RetroCog -> Retro rename
    "CONFIG_STORE_FILENAME",
    "LEGACY_COG_NAME",
]

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

# How long one user-supplied download may take in total. A 32 MiB ROM on a
# slow link is a couple of minutes; past that it is a tarpit rather than a
# download, and the bot has other things to do.
DOWNLOAD_TIMEOUT_SECONDS = 120

# Where to point people who want games they are allowed to play.
HOMEBREW_URL = "https://retrobrews.github.io/"

# The worked example in the help text: a complete, open source SimCity-like
# game for the Game Boy Color, MIT licensed, and a direct release download.
EXAMPLE_GAME = "ucity"
EXAMPLE_ROM_URL = (
    "https://github.com/AntonioND/ucity/releases/download/v1.3/ucity.gbc"
)

# A libretro core is a shared object with process-global state, so two live
# cores quietly corrupt each other's emulation (it segfaults the bot often
# enough to be obvious). Waking a hibernated session costs about 25ms against
# a multi-second clip, so keeping a single core loaded and evicting the least
# recently used one is effectively free and removes the hazard entirely.
MAX_LIVE_EMULATORS = 1

# How often the background task looks for sessions to put to sleep.
IDLE_CHECK_SECONDS = 60

# -- Rate limits ---------------------------------------------------------------
#
# `[p]retro <url>` makes the bot download up to 32 MiB and write a ROM plus
# save files to disk, and it is open to everybody in the channel. Without a
# limit, one person holding down Enter is an outbound-bandwidth and disk
# amplifier pointed at whatever they like.
#
# The numbers below are chosen to be invisible in normal play and to bite hard
# on abuse. Normal play is: start a game once, then press buttons for an hour
# -- and a button press is not a command, so it goes through none of this and
# is never rate limited. Starting *three different games in a minute* is
# already unusual, and the channel bucket allows six across everybody, so a
# busy channel of several people each starting something is fine.
#
# The cost of getting this wrong in the other direction is worse than the
# attack: a cooldown that blocks normal play makes the cog annoying, while the
# disk budget (retro/storage.py) is what actually bounds the damage.
START_COOLDOWN_RATE = 3
START_COOLDOWN_SECONDS = 60.0
CHANNEL_START_COOLDOWN_RATE = 6
CHANNEL_START_COOLDOWN_SECONDS = 60.0

# `[p]retrosaves import`/`export` carry the other half of this; the
# numbers live with the commands, in retro/saves.py.

# `[p]retroset bios add` is owner-only and downloads up to 64 MiB, so this is
# a guard against a fat-fingered loop rather than against a stranger.
BIOS_COOLDOWN_RATE = 3
BIOS_COOLDOWN_SECONDS = 60.0

# How long the paginated core option listing stays clickable.
OPTION_MENU_TIMEOUT = 180.0

# How many retired messages one channel keeps a working Resume button on.
# Matches MAX_CACHED_GAMES_PER_CHANNEL because a Resume button is only worth
# keeping while the ROM it names is still cached: in practice the *cache* is
# the tighter of the two bounds, since pruning a ROM now drops the records
# that pointed at it (see _forget_pruned_roms), so a busy channel settles at
# one record per cached game that is not the one playing.
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
#: a literal inside register_global() because the migration (retro/migration.py)
#: has to be able to tell an untouched configuration from a real one.
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
    # How much disk the whole data directory may use, in MiB. 0 is no limit.
    "disk_budget_mb": DEFAULT_DISK_BUDGET_MB,
    # The SSRF guard's escape hatch, off by default: with it on, a URL that
    # resolves to a private or loopback address is fetched instead of refused.
    # See `[p]retroset allowprivateurls`, which spells out what that means.
    "allow_private_urls": False,
}

#: The per-channel settings and their defaults.
#:
#: Both of these are *pointers* -- "this message, in this channel, was playing
#: this game" -- and neither holds any of the player's progress: that is the
#: ``.state`` and ``.srm`` on disk, keyed by channel and game rather than by
#: message. So both are deleted automatically once they can no longer do their
#: job, and the saves are kept when they are. See the "Forgetting a channel"
#: section below for the four things that drop one and why that is safe.
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


class Retro(
    MigrationMixin,
    StorageMixin,
    CoresMixin,
    SavesMixin,
    commands.Cog,
    metaclass=CompositeMetaClass,
):
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
        # The per-channel half of the start rate limit. `[p]retro` carries a
        # per-user cooldown as a decorator, which discord.py can only give a
        # command one of; this is the second bucket, checked by hand at the
        # point a start is about to cost a download. See _channel_start_delay.
        self.start_buckets = commands.CooldownMapping.from_cooldown(
            CHANNEL_START_COOLDOWN_RATE,
            CHANNEL_START_COOLDOWN_SECONDS,
            commands.BucketType.channel,
        )
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
        # The one moment when no write of this cog's is in flight, so the one
        # moment a leftover `.tmp` can be judged abandoned. An orphan is
        # invisible to every pruner and counted by the disk budget, so left
        # alone it is disk nobody can ever reclaim; see _sweep_partial_writes.
        try:
            await asyncio.to_thread(self._sweep_partial_writes)
        except Exception:
            log.exception("Failed to sweep the Retro data directory.")
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
        # `except Exception` was not enough here: CancelledError is not an
        # Exception, so a single hibernate cancelled by the shutdown that
        # asked for the unload took the whole loop with it and abandoned
        # every session after it -- unsaved, and with a core still loaded.
        # Once one cancellation has been seen no further await can be
        # trusted to come back, so the rest are saved and freed on this
        # thread and the cancellation is re-raised at the end, where it
        # costs nothing.
        cancelled: typing.Optional[BaseException] = None
        for view in list(self.sessions.values()):
            try:
                if cancelled is None:
                    await self.hibernate(
                        view,
                        "The console was put away. Press a button to pick up "
                        "where you left off.",
                    )
                else:
                    self._hibernate_now(view)
            except asyncio.CancelledError as error:
                cancelled = error
                log.warning(
                    "Putting a Libretro session away was cancelled during "
                    "unload; the rest are being saved without waiting."
                )
                # Whatever this one still held has already been freed by
                # _hibernate_locked's finally, but the write may not have
                # happened; there is nothing left to save if it has.
                self._hibernate_now(view)
            except Exception:
                log.exception("Failed to hibernate a Libretro session on unload.")
            # The buttons stay enabled on the message so the game can be
            # resumed after a reload, but this now-orphaned view object must
            # not answer them; cog_load builds fresh ones and re-registers
            # them for the same message.
            view.closed = True
            self._release_view(view)
        self.sessions.clear()
        for retired in self.retired.values():
            retired.alive = False
            self._release_view(retired)
        self.retired.clear()
        if cancelled is not None:
            # Re-raised rather than swallowed: the task really was cancelled
            # and its caller is entitled to know. Everything above has
            # already been done.
            raise cancelled

    # -- Red's end-user data API --------------------------------------------
    #
    # Red asks every cog that stores anything about a member to implement
    # these two, and this cog stores exactly one thing: the id of whoever
    # started a game. It is in two places -- the channel's live session record
    # and the records behind the Resume buttons of the games it has moved on
    # from -- and it is used for exactly one decision: whether somebody may
    # stop or destroy a game they did not start (see RetroView.can_stop and
    # _may_manage_saves).
    #
    # Both of those records are also deleted on their own once they cannot do
    # their job -- a deleted channel, a guild the bot has left, a cached ROM
    # that has been pruned -- which takes the stored id with them. See the
    # "Forgetting a channel" section below; this API is the way to have it
    # removed sooner.
    #
    # Nothing else here is end user data. A save state, a battery save and a
    # cached ROM belong to the *channel*: several people play one game, the
    # files are keyed by channel and game, and no part of them records who
    # pressed which button. So a deletion request scrubs the id and leaves the
    # game alone. The alternative -- deleting the save -- would let one member
    # of a channel destroy everybody else's progress by asking politely, which
    # is not what a privacy request is for.

    async def red_delete_data_for_user(
        self,
        *,
        requester: str,
        user_id: int,
    ) -> None:
        """
        Forget that this member started anything. Keeps the games.

        The stored id is replaced with nothing, in Config and in the live
        session objects, so the session carries on with an anonymous starter:
        anyone can still play it, and stopping or wiping it falls back to
        moderators and the bot owner.

        Every requester gets the same treatment, including ``"user"`` (where
        Red allows a cog to keep data it genuinely needs): a starter id is a
        convenience, not something this cog cannot run without. An unknown
        requester string -- which Red's own documentation warns may appear --
        is treated the same way and logged, because scrubbing is the safe
        default for a request this code does not recognise.
        """
        user_id = int(user_id)
        if requester not in ("discord_deleted_user", "owner", "user", "user_strict"):
            log.warning(
                "Retro was asked to delete user data for an unrecognised "
                "requester %r; treating it as a full deletion.",
                requester,
            )
        scrubbed = 0
        try:
            channels = await self.config.all_channels()
        except Exception:
            log.exception("Could not read the Retro channels to scrub a user id.")
            channels = {}
        for channel_id in list(channels):
            scope = self.config.channel_from_id(int(channel_id))
            try:
                async with scope.session() as session:
                    if session and int(session.get("starter_id") or 0) == user_id:
                        session["starter_id"] = None
                        scrubbed += 1
            except Exception:
                log.exception(
                    "Could not scrub a user id from channel %s's Retro session.",
                    channel_id,
                )
            try:
                async with scope.retired() as retired:
                    for key, record in list((retired or {}).items()):
                        if record and int(record.get("starter_id") or 0) == user_id:
                            record["starter_id"] = None
                            retired[key] = record
                            scrubbed += 1
            except Exception:
                log.exception(
                    "Could not scrub a user id from channel %s's retired Retro "
                    "records.",
                    channel_id,
                )
        # And in memory, so a session that is already loaded does not write
        # the id straight back on its next save.
        for view in self.sessions.values():
            if view.starter_id == user_id:
                view.starter_id = None
                scrubbed += 1
        for retired_view in self.retired.values():
            record = getattr(retired_view, "record", None)
            if record and int(record.get("starter_id") or 0) == user_id:
                record["starter_id"] = None
                scrubbed += 1
        log.info(
            "Retro scrubbed %s reference(s) to user %s at the request of %r.",
            scrubbed,
            user_id,
            requester,
        )

    async def red_get_data_for_user(
        self, *, user_id: int
    ) -> typing.MutableMapping[str, io.BytesIO]:
        """
        Everything this cog knows about one member, as a readable file.

        Which is a list of the games they started that something still
        remembers, and nothing else -- there is no per-user anything here. An
        empty mapping when their id appears nowhere, which is what Red expects
        from a cog with no data for somebody.
        """
        user_id = int(user_id)
        lines: typing.List[str] = []
        try:
            channels = await self.config.all_channels()
        except Exception:
            log.exception("Could not read the Retro channels for a data request.")
            channels = {}
        for channel_id, data in (channels or {}).items():
            session = (data or {}).get("session") or {}
            if session and int(session.get("starter_id") or 0) == user_id:
                lines.append(
                    f"- You started \"{session.get('game_name') or 'a game'}\" "
                    f"in channel {channel_id}. It is the game that channel is "
                    "currently playing."
                )
            for record in ((data or {}).get("retired") or {}).values():
                if record and int(record.get("starter_id") or 0) == user_id:
                    lines.append(
                        f"- You started \"{record.get('game_name') or 'a game'}\" "
                        f"in channel {channel_id}. That channel has moved on to "
                        "another game, and this one keeps a Resume button."
                    )
        if not lines:
            return {}
        text = "\n".join(
            [
                "Retro (the retro game emulator cog) stores your Discord user "
                "id as the person who started a game, so that you -- as well "
                "as the channel's moderators and the bot owner -- can stop it "
                "or manage its saves. That is the only thing it stores about "
                "you.",
                "",
                "The game itself belongs to the channel: its emulator save "
                "state, the game's in-cartridge battery save and a cached copy "
                "of the ROM are shared by everybody who plays there, and none "
                "of them record who pressed which button.",
                "",
                *lines,
                "",
                "The clip on a game's message is an attachment on that "
                "message and is not kept anywhere else. The save states the "
                "Undo button steps back through are held in memory only and "
                "are lost whenever the bot restarts.",
                "",
                "The record that lets a game be resumed from its message is "
                "deleted by itself once it can no longer do that -- when the "
                "channel is deleted, when the bot leaves the server, or when "
                "the cached ROM it names is cleaned up. That removes your id "
                "along with it. The game's saves are kept, because they "
                "belong to the channel rather than to anybody.",
            ]
        )
        return {"retro.txt": io.BytesIO(text.encode("utf-8"))}

    # -- Session storage ----------------------------------------------------

    async def _restore_sessions(self) -> None:
        """Rebuild hibernated sessions from Config and re-arm their buttons."""
        timeout_minutes = await self.config.session_timeout_minutes()
        clip_seconds = await self.config.clip_seconds()
        hold_ms = await self.config.hold_ms()
        forget: typing.List[int] = []
        for channel_id, data in (await self.config.all_channels()).items():
            if self._channel_is_gone(channel_id):
                # A channel this bot can no longer see. Building a view for it
                # would register a persistent view against a message nobody
                # can reach, on every single load, for ever. Only the pointer
                # is dropped; see _forget_channel.
                forget.append(int(channel_id))
                continue
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
            # Newest first and capped, so the in-memory dictionary is bounded
            # by MAX_RETIRED_PER_CHANNEL whatever is on disk. _remember_retired
            # trims as it writes, but a store written by another version of
            # the cog (or left behind by a Config write that failed halfway)
            # must not be able to fill memory on load.
            stored = (data.get("retired") or {}).items()
            newest = sorted(
                stored,
                key=lambda pair: float((pair[1] or {}).get("last_active") or 0.0),
                reverse=True,
            )
            for key, retired_record in newest[:MAX_RETIRED_PER_CHANNEL]:
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
        for channel_id in forget:
            await self._forget_channel(
                channel_id, "the bot can no longer see that channel"
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
            # Before the new one is registered, or remove_view would take
            # the new view's entry out of discord.py's table with the old
            # one's; see _release_view.
            self._release_view(existing)
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
        Config entry per message forever.

        This cap is the *ceiling*, not the usual bound. The cached ROM is
        tighter: pruning one drops the records that pointed at it
        (_forget_pruned_roms), so a channel normally holds a record per
        cached game rather than MAX_RETIRED_PER_CHANNEL of them. The cap is
        still here because the two limits are separate -- a ROM can be
        deleted by hand, and a store written by another version of the cog
        must not be able to grow without one.
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
                        self._release_view(stale)
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
            self._release_view(view)
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
                self._release_view(view)

    # -- Forgetting a channel -----------------------------------------------
    #
    # A session record and the records behind a channel's Resume buttons are
    # *pointers*: "this message, in this channel, was playing this game".
    # Dropping one costs the ability to resume from that message and nothing
    # else, because the two things that hold the player's progress -- the
    # ``.state`` and the ``.srm``, with one previous generation each -- are
    # keyed by channel *and* game rather than by message. Starting the game
    # again by name re-fetches the ROM and restores from them (see
    # _saved_progress, which every start goes through), so the game comes
    # back exactly where it was; only the button is gone.
    #
    # That is what makes the four things below safe to do automatically:
    #
    #   * a cached ROM was pruned, by the disk budget or the per-channel game
    #     cap, so the record points at a file that is no longer there
    #     (_forget_pruned_roms);
    #   * the channel was deleted, or a thread was (the listeners below);
    #   * the bot was removed from the guild (ditto);
    #   * the bot can no longer see the channel at all, which is the same
    #     thing noticed a restart later (_forget_unreachable_sessions).
    #
    # Without them a record was never deleted: there was no `session.clear()`
    # anywhere in the cog and no listeners, so every channel that had *ever*
    # played had a RetroView rebuilt for it on every load, for the life of
    # the install. Bounded per channel, unbounded in channels.
    #
    # **The saves on disk are deliberately kept**, even for a channel or a
    # guild that is gone. They are small next to a ROM, the disk budget
    # already prunes ROMs (and never saves -- see _prune_roms_for_budget),
    # and the cases are not distinguishable from the outside anyway: an
    # archived thread, a channel the bot has temporarily lost sight of and a
    # channel that was really deleted all look identical here. Deleting
    # somebody's progress on that evidence is not a trade worth making, and
    # the opposite mistake costs a few hundred kilobytes that `[p]retroset
    # diskbudget` reports and `[p]retrosaves delete` can clear.

    def _channel_is_gone(self, channel_id: int) -> bool:
        """
        Whether the bot is in a position to say this channel does not exist.

        Deliberately conservative, because being wrong here deletes a
        record: it answers False whenever the answer is *unknown*. In
        particular Red loads its cogs before the bot connects, so during a
        startup load the channel cache is empty and every channel would
        otherwise look deleted. ``wait_until_red_ready`` is what makes the
        cache trustworthy, so nothing is judged until it has returned --
        see the sweep in _hibernation_loop, which is where a record left
        alone at load time is reconsidered.
        """
        try:
            if not self.bot.is_ready():
                return False
            return self.bot.get_channel(int(channel_id)) is None
        except Exception:
            # A bot object that cannot answer must never cost a record.
            log.debug("Could not check whether a channel still exists.", exc_info=True)
            return False

    def _drop_session_view(self, channel_id: int) -> None:
        """
        Take a channel's live view out of service, freeing its core.

        The core is the part that cannot be skipped. ``self.sessions`` is the
        only place _evict_locked looks, so the moment the view is popped out
        of it a still-loaded core is unreachable: nothing can ever hibernate
        it, nothing can ever stop it, and with MAX_LIVE_EMULATORS at 1 the
        slot accounting believes it is free. The next game to start loads a
        second core into a process that already has one, which is the state
        the whole single-emulator machinery exists to prevent.

        The save state is written first, exactly as every other path that
        frees a core does: forgetting the *pointer* to a channel's game is
        never meant to cost the channel's progress, which is the entire
        argument for doing it automatically (see the section above).

        Both are done on this thread rather than awaited. This is called from
        ``_forget_channel`` and ``_forget_session``, whose own callers are
        Discord listeners and a startup sweep, and staying synchronous is
        worth more than the few milliseconds it costs: there is no moment at
        which the view is out of ``self.sessions`` with its core still
        attached, so no other task can see the half-dropped state. See
        _free_emulator and _write_state_now, which exist for the same reason.
        """
        view = self.sessions.pop(int(channel_id), None)
        if view is None:
            return
        view.closed = True
        emulator, view.emulator = getattr(view, "emulator", None), None
        if emulator is not None:
            self._write_state_now(view, emulator)
            self._free_emulator(emulator)
        self._release_view(view)

    def _drop_retired_views(self, channel_id: int) -> None:
        """Take every Resume button of one channel out of service."""
        for message_id, view in list(self.retired.items()):
            if int(getattr(view, "channel_id", 0) or 0) != int(channel_id):
                continue
            self.retired.pop(message_id, None)
            view.alive = False
            self._release_view(view)

    async def _forget_channel(self, channel_id: int, why: str) -> None:
        """
        Forget everything a channel could be resumed from. Keeps its saves.

        A channel that has nothing stored is left completely alone, which is
        the overwhelmingly common case for the listeners: most channels a bot
        can see have never played anything, and a deletion should not cost a
        Config write and a log line each.

        Never raises: every caller is a Discord event handler or the cog
        load, and neither may be broken by a Config write that failed.
        """
        channel_id = int(channel_id)
        held = channel_id in self.sessions or any(
            int(getattr(view, "channel_id", 0) or 0) == channel_id
            for view in self.retired.values()
        )
        scope = None
        stored = True
        try:
            scope = self.config.channel_from_id(channel_id)
            stored = bool(await scope.session()) or bool(await scope.retired())
        except Exception:
            # Unknown, so act: leaving a record behind is the failure mode
            # this whole section exists to stop. The in-memory half below
            # happens either way, because it cannot fail.
            log.exception("Could not read channel %s's Retro records.", channel_id)
        if not stored and not held:
            return
        self._drop_session_view(channel_id)
        self._drop_retired_views(channel_id)
        if scope is None:
            return
        try:
            # The whole per-channel scope, so neither the session nor the
            # retired records are left behind. Red removes the row outright,
            # which is what stops all_channels() growing with channels the
            # bot has not been able to see for months.
            await scope.clear()
        except Exception:
            log.exception(
                "Could not forget the Retro records for channel %s.", channel_id
            )
            return
        log.info(
            "Forgot channel %s's Retro session and Resume buttons (%s). "
            "Its save states and battery saves were kept.",
            channel_id,
            why,
        )

    async def _forget_guild(self, guild_id: int, why: str) -> None:
        """Forget every channel of one guild. Keeps every save."""
        guild_id = int(guild_id)
        channels: typing.Set[int] = set()
        try:
            stored = await self.config.all_channels()
        except Exception:
            log.exception("Could not read the Retro channels to forget a guild.")
            stored = {}
        for channel_id, data in (stored or {}).items():
            records = [(data or {}).get("session")]
            records.extend(((data or {}).get("retired") or {}).values())
            for record in records:
                if record and int((record or {}).get("guild_id") or 0) == guild_id:
                    channels.add(int(channel_id))
                    break
        # A record written before guild_id was stored has none, so the live
        # views are consulted as well rather than only the stored records.
        for view in list(self.sessions.values()):
            if int(getattr(view, "guild_id", 0) or 0) == guild_id:
                channels.add(int(view.channel_id))
        for view in list(self.retired.values()):
            record = getattr(view, "record", None) or {}
            if int(record.get("guild_id") or 0) == guild_id:
                channels.add(int(getattr(view, "channel_id", 0) or 0))
        for channel_id in sorted(channels):
            if channel_id:
                await self._forget_channel(channel_id, why)

    async def _forget_pruned_roms(self, filenames: typing.Iterable[str]) -> None:
        """
        Drop the records that pointed at a cached ROM that has just gone.

        Both pruners end up here: the per-channel game cap
        (``_prune_cached_games``) and the disk budget
        (``_prune_roms_for_budget``). A Resume button whose ROM has been
        cleaned up can only apologise when it is clicked, and a session
        record for one is a view rebuilt on every load for a game that
        cannot come back from it -- so the pointer goes with the file.

        The saves stay exactly where they are. For a budget prune that is
        the whole point: `[p]retro <name>` re-downloads the ROM and picks
        the progress straight back up. Never raises.
        """
        gone = {str(name) for name in filenames if name}
        if not gone:
            return
        try:
            stored = await self.config.all_channels()
        except Exception:
            log.exception("Could not read the Retro channels after pruning a ROM.")
            return
        for channel_id, data in (stored or {}).items():
            channel_id = int(channel_id)
            session = (data or {}).get("session") or {}
            if session and str(session.get("rom_filename") or "") in gone:
                await self._forget_session(
                    channel_id, "its cached ROM was pruned"
                )
            stale = [
                key
                for key, record in ((data or {}).get("retired") or {}).items()
                if str((record or {}).get("rom_filename") or "") in gone
            ]
            for key in stale:
                try:
                    await self._forget_retired(channel_id, int(key))
                except (TypeError, ValueError):
                    log.debug("Ignoring an unreadable retired key %r.", key)

    async def _forget_session(self, channel_id: int, why: str) -> None:
        """Drop one channel's session pointer. Keeps its saves. Never raises."""
        channel_id = int(channel_id)
        self._drop_session_view(channel_id)
        try:
            await self.config.channel_from_id(channel_id).session.clear()
        except Exception:
            log.exception(
                "Could not forget channel %s's Retro session record.", channel_id
            )
            return
        log.info(
            "Forgot channel %s's Retro session (%s); its saves were kept.",
            channel_id,
            why,
        )

    async def _forget_unreachable_sessions(self) -> None:
        """
        Drop the records of channels the bot cannot see, once it is ready.

        The counterpart to the check in _restore_sessions, which runs before
        the bot has connected on a cold start and therefore has to keep
        everything. Run once from the hibernation loop, after
        ``wait_until_red_ready``. Never raises.
        """
        try:
            stored = await self.config.all_channels()
        except Exception:
            log.exception("Could not read the Retro channels to sweep them.")
            return
        for channel_id in list(stored or {}):
            if self._channel_is_gone(channel_id):
                await self._forget_channel(
                    channel_id, "the bot can no longer see that channel"
                )

    # -- Discord's own events ------------------------------------------------
    #
    # Each one is wrapped in its own try/except and logs rather than raises:
    # discord.py dispatches a listener as a task it does not await, so an
    # exception in here would surface as an unhandled task error and (on
    # Red) a traceback in the owner's console for something nobody can act
    # on. Forgetting a record is also never urgent -- the sweep above picks
    # up whatever a failed listener missed on the next load.

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel) -> None:
        """A text channel was deleted: nothing here can be resumed from it."""
        try:
            await self._forget_channel(
                getattr(channel, "id", 0), "the channel was deleted"
            )
        except Exception:
            log.exception("Failed to forget a deleted channel's Retro records.")

    @commands.Cog.listener()
    async def on_thread_delete(self, thread) -> None:
        """
        The same for a thread, which is a channel a game can be played in.

        Discord only sends this for a thread that is actually deleted. A
        thread that is merely *archived* stays in Config until the sweep
        notices the bot can no longer see it, which costs the Resume button
        and nothing else.
        """
        try:
            await self._forget_channel(
                getattr(thread, "id", 0), "the thread was deleted"
            )
        except Exception:
            log.exception("Failed to forget a deleted thread's Retro records.")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild) -> None:
        """The bot left (or was thrown out of) a server."""
        try:
            await self._forget_guild(
                getattr(guild, "id", 0), "the bot is no longer in that server"
            )
        except Exception:
            log.exception("Failed to forget a departed guild's Retro records.")

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

    @staticmethod
    def _release_view(view) -> None:
        """
        Give a view the cog has finished with back to discord.py.

        This is the counterpart to _register_view, and it has to exist
        because a Discord bot runs for months. discord.py keeps every
        persistent view in a store keyed by message id -- filled by
        ``Client.add_view`` and by *every* send or edit that carries a view,
        and emptied by nothing except ``ViewStore.remove_view`` -- and the
        only public way to reach that is ``View.stop()``. There is no
        ``bot.remove_view``.

        So without this, a channel that plays twenty games leaves twenty
        RetroViews reachable for the life of the process, each one holding a
        stack of save states. Marking them ``closed`` (which is still done,
        and still what answers a click that arrives in the gap) stops them
        acting; it does not let go of them.

        The undo history is dropped here as well as handed over, because it
        is the expensive part: a stray reference to the view from somewhere
        unexpected should cost a few kilobytes of object rather than a stack
        of save states. (It used to be worth far more than that: a session
        kept up to MAX_REPLAY_BYTES -- eight megabytes -- of footage for the
        Replay button, and then one clip after that button went; a session
        holds no footage at all now, so there is nothing of that left to
        release.) Emptying the history costs nothing either way: this is only
        called for a view the cog has finished with, whose game is either
        retired or gone, so there is nothing anybody could still want to
        undo.

        Call this *before* registering a replacement view on the same
        message: remove_view unconditionally drops that message id from
        discord.py's synced-view table (discord/ui/view.py:986), so the other
        order would take the new view's entry with it.

        Never raises: it is on every discard path, including cog_unload.
        """
        forget = getattr(view, "forget_history", None)
        if forget is not None:
            try:
                forget()
            except Exception:  # pragma: no cover - a deque cannot fail here
                log.debug("Could not empty an undo history.", exc_info=True)
        try:
            view.stop()
        except Exception:
            log.debug("Could not release a Retro view.", exc_info=True)

    @staticmethod
    def _free_emulator(emulator: typing.Optional[RetroEmulator]) -> None:
        """
        Free a core synchronously, for paths that must not await.

        The ordinary paths free a core in a worker thread, because unloading
        one takes long enough to be worth keeping off the event loop. That is
        not available while a task is being cancelled: awaiting anything
        there can raise CancelledError again and abandon the core half-freed,
        and a leaked libretro core is not merely memory -- only one may be
        loaded at a time (MAX_LIVE_EMULATORS), so the cog stops working
        altogether. A few milliseconds on the loop is the cheaper mistake.
        """
        if emulator is None:
            return
        try:
            emulator.stop()
        except Exception:
            log.exception("Could not free a libretro core.")

    def _hibernate_now(self, view: RetroView) -> None:
        """
        Put one session to sleep without awaiting anything. Never raises.

        The two halves of :meth:`_hibernate_locked` that cannot be skipped --
        write the progress, then free the core -- done on the calling thread,
        for a teardown that has already been cancelled once and therefore
        cannot rely on an ``await`` coming back. The lock is deliberately not
        taken, for the same reason: acquiring it is an await.

        Safe on a session that is already asleep, which is the common case
        by the time this is reached.
        """
        emulator, view.emulator = getattr(view, "emulator", None), None
        if emulator is None:
            return
        self._write_state_now(view, emulator)
        self._free_emulator(emulator)

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

    async def run_undo(self, view: RetroView) -> bytes:
        """
        Step a session back one press, waking it up first if it was asleep.

        The save state is written straight through rather than waiting for
        the autosave cadence, which matters here in a way it does not for an
        ordinary press: the state on disk may well be *newer* than the one
        Undo has just restored (it is written every
        SAVE_STATE_EVERY_PRESSES presses and whenever a game sleeps), so
        without this a restart or a hibernate immediately after an undo
        would quietly bring the undone press back. An undo is a deliberate
        correction and a couple of hundred kilobytes of write; it is worth
        being durable.

        ``press_count`` is deliberately left alone. It only paces the
        autosave, this path has just saved, and it is a count of presses the
        channel made rather than a position in the game.

        Raises EmulatorError if the session cannot be woken, if there is
        nothing to undo, or if the core will not take the state back. Called
        by the view from the Undo button.
        """
        async with self.emulator_lock:
            await self._wake_locked(view)
            clip = await asyncio.to_thread(view.run_undo)
            view.touch()
            await self._write_state(view)
            await self._save_record(view)
            return clip

    async def run_reset(self, view: RetroView) -> bytes:
        """
        Reboot a session's game, waking it up first if it was asleep.

        Waking first is not a formality: a reset has to be a reset of *this*
        game as the channel left it, and the only way to reach the core's
        power switch is to have a core. A sleeping session is therefore
        restored from its save state and then immediately rebooted, which
        looks odd written down and is exactly right -- what the player asked
        for is "put this game back to its title screen", and the ROM is the
        only thing that needs to be in place for that.

        Nothing is written to disk, unlike ``run_undo``, which writes the
        state it restored straight through. That asymmetry is the whole of the
        save-state decision behind `[p]retroreset`: an undo is a correction
        that should survive a restart, while a reset must not quietly
        overwrite a good save state with the title screen. The record is
        saved (it is only the session's bookkeeping) but the ``.state`` file
        is left holding the moment before the reset until the game saves
        again of its own accord -- the next autosave, or its next sleep. See
        ``RetroView.run_reset`` and the command's own help.

        Raises EmulatorError if the session cannot be woken or the core will
        not reset. Called by `[p]retroreset` and nothing else: there is no
        Reset button.
        """
        async with self.emulator_lock:
            await self._wake_locked(view)
            clip = await asyncio.to_thread(view.run_reset)
            view.touch()
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
        if message is None:
            # No message to put a button on, so there is nothing to resume
            # from; leave the view inert and say nothing more about it.
            # refresh() has nothing to edit either, so releasing the view
            # here cannot be undone by a later edit re-registering it.
            view.retire()
            await view.refresh(reason)
            self._release_view(view)
            return
        # Hand these controls back to discord.py *before* the Resume button
        # takes the message over; see _release_view for why the order
        # matters. This message now belongs to `retired`.
        self._release_view(view)
        retired = self._arm_retired(record)
        if retired is None:
            # The record carries no message id, so there is nothing to put a
            # Resume button on. refresh() edits the message it does have,
            # which re-registers this view with discord.py, so release it
            # again afterwards rather than before.
            view.retire()
            await view.refresh(reason)
            self._release_view(view)
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
            # Rare now rather than routine: pruning a cached ROM drops the
            # records that pointed at it, so a Resume button whose ROM has
            # gone normally goes with it (_forget_pruned_roms). This is the
            # gap -- a ROM deleted by hand, or a Config write that failed on
            # the way. Say so; the save state is very probably still there,
            # so starting the game again by name will pick it back up (see
            # _start_session).
            # There is no ctx.clean_prefix on a button click, and Red only
            # substitutes `[p]` in a docstring, so the prefix is asked for.
            prefix = await self._prefix_for(getattr(interaction, "guild", None))
            await self._whisper_interaction(
                interaction,
                f"The cached ROM for **{game_name}** has been cleaned up, so "
                "this button cannot start it. Start it again with "
                f"`{prefix}retro <name or url>` \N{EM DASH} its save is still "
                "here, and it will pick up where it left off.",
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

        progress, notice = self._saved_progress(channel_id, view.slug)
        emulator = RetroEmulator(
            core_path,
            rom_path,
            system_dir=self._system_dir(),
            options=await self._core_options(view.core),
        )
        # Booted *before* anything is taken away from the channel, so a core
        # that will not come up costs nothing: whatever was playing is merely
        # hibernated (which its own buttons undo) rather than retired.
        previous: typing.Optional[RetroView] = None
        try:
            async with self.emulator_lock:
                # One core at a time, here as everywhere else.
                await self._evict_locked(exclude=view)
                clip = await asyncio.to_thread(view._boot, emulator, progress)
                # The new session becomes the channel's *inside the lock*,
                # with its core already attached, and before anything that
                # can await. This used to happen after the lock had been
                # released and after _retire (which re-takes the lock and
                # edits a message), which left a window where a live core
                # belonged to no session in self.sessions: a press in another
                # channel arriving in it takes the lock, finds nothing live
                # to evict, and loads a second core into a process that is
                # only ever allowed one (MAX_LIVE_EMULATORS).
                #
                # Whatever this replaces is remembered rather than retired
                # here, because retiring it awaits; _evict_locked has already
                # saved it and freed its core, so the only thing left to do
                # to it is cosmetic and can wait for the lock to be free.
                previous = self.sessions.get(channel_id)
                if previous is view:
                    previous = None
                view.emulator = emulator
                self.sessions[channel_id] = view
        except asyncio.CancelledError:
            # A reload or shutdown landing on the boot. Nothing is awaited
            # from here (see _free_emulator) and there is nothing to report
            # to: the interaction is going away with the task. The Resume
            # button is left as it is, which is also how the message looks.
            if self.sessions.get(channel_id) is view:
                del self.sessions[channel_id]
            view.emulator = None
            self._free_emulator(emulator)
            view.closed = True
            self._release_view(view)
            raise
        except Exception as error:
            log.warning(
                "Could not resume %s in channel %s: %s", view.slug, channel_id, error
            )
            if self.sessions.get(channel_id) is view:
                del self.sessions[channel_id]
            view.emulator = None
            try:
                await asyncio.to_thread(emulator.stop)
            except Exception:
                log.exception("Could not stop a failed resume's emulator.")
            view.closed = True
            self._release_view(view)
            # Put the Resume button back so the click was not destructive.
            self._arm_retired(record)
            await self._restore_retired_message(interaction, retired, record, error)
            return

        # It is up and the channel has already changed hands. The game it
        # replaces is retired exactly as starting a different game by name
        # would retire it, and gets a Resume button of its own.
        if previous is not None:
            await self._retire(
                previous,
                f"Replaced by **{game_name}**. **{previous.game_name}** was "
                "saved \N{EM DASH} press Resume to come back to it.",
            )

        # Same reason as RetroView.start(): with a core in hand the repeat
        # button's label can be written from its real frame rate, and this is
        # the last chance before the message goes back out.
        view._update_repeat_label()
        view.touch()
        self._settle_boot(view, progress, notice)
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

    async def _prefix_for(self, guild) -> str:
        """
        The bot's own command prefix, for a message with no ``ctx`` to ask.

        Red rewrites ``[p]`` in a command's *help text* and nowhere else, so
        a sent string that says `[p]retro` says exactly that to the player --
        and `[p]` is not a prefix anybody can type. A command replies through
        ``ctx.clean_prefix``; a button click has no context, so this asks the
        bot, which is the same question ``clean_prefix`` answers.

        A mention prefix is skipped when there is anything else, because
        Red's ``--mentionable`` puts ``<@id>`` first in the list and a raw
        mention in the middle of a sentence reads as a bug. If the bot cannot
        answer at all the commands are named without a prefix, which is
        wrong-but-readable rather than actively misleading.
        """
        try:
            prefixes = [
                str(prefix)
                for prefix in (await self.bot.get_valid_prefixes(guild) or [])
                if prefix
            ]
        except Exception:
            log.debug("Could not read the bot's command prefixes.", exc_info=True)
            return ""
        for prefix in prefixes:
            if not prefix.startswith("<@"):
                return prefix
        return prefixes[0] if prefixes else ""

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
            # This local is now the only reference to the core, so every way
            # out of here has to free it -- including the one `try` used to
            # miss. A CancelledError from the write (a bot shutdown, or
            # `[p]unload retro` landing mid-save) is not an Exception, and
            # without the finally below it left a loaded core with nothing
            # pointing at it: the one MAX_LIVE_EMULATORS slot, spent for the
            # life of the process. Same shape as _wake_locked's.
            stopped = False
            try:
                # Always write the save state *before* the core is freed:
                # after emulator.stop() the machine state is gone for good.
                await self._write_state(view, emulator)
                await asyncio.to_thread(emulator.stop)
                stopped = True
            finally:
                if not stopped:
                    # Only reached while unwinding, which on the path that
                    # matters means the task is being cancelled and the next
                    # await would raise straight back out. Freed on this
                    # thread instead; see _free_emulator.
                    self._free_emulator(emulator)
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

        # Exactly what a fresh start reads, by the same method: the save state
        # first, then the cartridge's battery save as the insurance policy (a
        # state is tied to the exact build of the core that wrote it, so
        # updating a core invalidates every state on disk; SRAM is not and
        # holds whatever the player saved from inside the game). The notice is
        # dropped here on purpose -- waking a session up is not an event worth
        # narrating, so only a *failed* restore says anything.
        progress, _ = self._saved_progress(view.channel_id, view.slug)

        emulator = RetroEmulator(
            core_path,
            rom_path,
            system_dir=self._system_dir(),
            options=await self._core_options(view.core),
        )

        # Three ways out, and the core has to be accounted for in all of
        # them: handed to the view, freed in a thread after an ordinary
        # failure, or -- the case `except Exception` missed -- freed on the
        # spot when the task is cancelled. CancelledError is not an
        # Exception, and a core left loaded spends the one
        # MAX_LIVE_EMULATORS slot for the life of the process.
        settled = False
        try:
            # The same restore chain a fresh start runs, from the same
            # function, so waking and starting cannot drift apart.
            view.boot_outcome = await asyncio.to_thread(
                restore_into, emulator, progress, view.slug
            )
            self._settle_boot(view, progress, None)
            view.emulator = emulator
            settled = True
        except Exception:
            # A core that will not take the state, or will not boot. Freed in
            # a thread, as everywhere else on a path that can still await.
            await asyncio.to_thread(emulator.stop)
            settled = True
            raise
        finally:
            if not settled:
                # Only a BaseException reaches here: cancellation, or the
                # interpreter shutting down. Nothing may be awaited.
                self._free_emulator(emulator)
        await self._learn_options(view.core, emulator)

    def _saved_progress(
        self, channel_id: int, slug: str
    ) -> typing.Tuple[Progress, typing.Optional[str]]:
        """
        What this channel already has saved for one game.

        Returns ``(the progress, the line to say if it all works)``. Read on
        every start, not only on a wake: a channel that played this game
        before -- even weeks and several other games ago -- has its progress
        cached under the same key, and booting over the top of it would
        quietly throw the player's game away.

        All four files are read, the previous generation of each included, so
        the boot has everything :func:`RetroView.restore_into` might need. The
        backups cost a couple of hundred kilobytes of read that is usually
        wasted, which is a great deal cheaper than being unable to offer them
        at the moment the newest file turns out to be bad.
        """
        state_path, state_backup, sram_path, sram_backup = self._save_paths(
            channel_id, slug
        )
        progress = Progress(
            state=self._read_file(state_path),
            state_backup=self._read_file(state_backup),
            sram=self._read_file(sram_path),
            sram_backup=self._read_file(sram_backup),
        )
        if progress.state:
            notice = "Picked up from where this channel left off."
        elif progress.sram or progress.sram_backup:
            notice = (
                "Started from the title screen with this channel's in-game "
                "save already in place \N{EM DASH} load it from the game's own "
                "menu to carry on."
            )
        else:
            notice = None
        return progress, notice

    def _settle_boot(
        self,
        view: RetroView,
        progress: typing.Optional[Progress],
        notice: typing.Optional[str],
    ) -> None:
        """
        Say how a boot went, and throw away a save state that did not work.

        Runs after every :func:`restore_into`, whichever path called it -- a
        fresh start, a Resume click, or waking a hibernated session -- so the
        consequences of a restore are decided in one place too.

        ``notice`` is what to say when the restore worked, which is the one
        thing the paths differ on: starting a game says where it picked up
        from, and waking one passes ``None``, because a session coming back
        from its own save state is not an event worth narrating. A restore
        that *failed* always says so, since a save state is tied to the exact
        build of the core that wrote it and a core update invalidates every
        one on disk -- that should cost the exact moment, never the session
        and never the player's own in-game save.

        An unusable state is deleted so it is not retried on every press for
        the rest of the game's life. Which files that means depends on how far
        down the chain the boot had to go: a boot that came back from the
        *previous* generation deletes only the newer, broken one, and leaves
        the generation that worked exactly where it is -- it is the newest
        good copy the channel has, and the next boot finds it in the same way.
        """
        progress = progress if progress is not None else Progress()
        outcome = getattr(view, "boot_outcome", "fresh")
        state_path, state_backup, _, _ = self._save_paths(view.channel_id, view.slug)
        if outcome == "backup-state":
            self._discard(state_path)
            view.notice = (
                "This game's latest save state could not be used (most likely "
                "the emulator core was updated), so it came back from the one "
                "before it \N{EM DASH} a few presses of play earlier."
            )
            log.info("Restored %s from its previous save state.", view.slug)
            return
        if progress.has_state and outcome not in ("state", "backup-state"):
            # Neither generation loaded, so both are dead weight.
            self._discard(state_path)
            self._discard(state_backup)
            view.notice = self._cold_boot_notice(outcome in ("sram", "backup-sram"))
            log.info(
                "Cold-booted %s after an unusable save state; battery save %s.",
                view.slug,
                "restored" if outcome in ("sram", "backup-sram") else "not available",
            )
            return
        if outcome in ("state", "sram", "backup-sram"):
            # "fresh" is deliberately not here: nothing was restored, so there
            # is nothing to announce, and the caller's notice (which would say
            # a battery save is in place) would be a lie.
            view.notice = notice

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

        Each write rotates the file it replaces to ``<name>.bak`` first, so
        the channel always has the generation before this one to fall back to.
        See BACKUP_SUFFIX and ``[p]retrosaves rollback``.

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
            await asyncio.to_thread(self._write_atomic, path, data, True)
        except OSError:
            log.warning("Could not write the Libretro state %s", path, exc_info=True)
            return False
        return True

    def _write_state_now(
        self, view: RetroView, emulator: typing.Optional[RetroEmulator] = None
    ) -> bool:
        """
        The same two writes as :meth:`_write_state`, without awaiting.

        For cancellation handlers only. A task that is being cancelled cannot
        rely on ``await`` -- the next one may raise CancelledError straight
        back out and leave the core loaded and the progress unwritten -- so
        this does both writes on the calling thread. It is a few milliseconds
        of blocking on a path that is already tearing down.

        Never raises, for the same reason _write_state does not.
        """
        emulator = emulator if emulator is not None else view.emulator
        if emulator is None or not emulator.started:
            return False
        # SRAM first, as in _write_state: it is the copy that survives a core
        # update, so if only one of the two gets written it should be this.
        try:
            sram = emulator.save_sram()
            if sram:
                self._write_atomic(self._sram_path(view.channel_id, view.slug), sram, True)
        except Exception:
            log.warning(
                "Could not write the battery save for %s while shutting down.",
                view.slug,
                exc_info=True,
            )
        try:
            data = emulator.save_state()
            self._write_atomic(self._state_path(view.channel_id, view.slug), data, True)
        except Exception:
            log.warning(
                "Could not write the save state for %s while shutting down.",
                view.slug,
                exc_info=True,
            )
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
            await asyncio.to_thread(self._write_atomic, path, data, True)
        except OSError:
            log.warning("Could not write the battery save %s", path, exc_info=True)
            return False
        return True

    async def _hibernation_loop(self) -> None:
        """Put sessions to sleep once they have been idle for long enough."""
        try:
            await self.bot.wait_until_red_ready()
        except Exception:
            pass
        # Only now is the channel cache worth reading: Red loads its cogs
        # before the bot connects, so _restore_sessions had to keep every
        # record it could not judge. This is where one belonging to a channel
        # that has since gone is dropped. Once, not on every pass -- a
        # channel that disappears while the bot is up has a listener for it.
        try:
            await self._forget_unreachable_sessions()
        except Exception:
            log.exception("Could not sweep the Retro session records.")
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
        if isinstance(error, commands.CommandOnCooldown):
            # Every expensive command in this cog carries a cooldown, and
            # every one of them is expensive because it downloads or uploads
            # something. So the answer says what the limit is for, and points
            # at the thing that is never limited.
            seconds = max(1, int(getattr(error, "retry_after", 0) or 0) + 1)
            await self._safe_send(
                ctx,
                f"That has been run a few times in the last minute, so it is "
                f"rate limited. Try again in {seconds}s \N{EM DASH} the limit "
                "is on fetching and uploading files, not on playing: the "
                "buttons under a game are never rate limited.",
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

    async def _allow_private_urls(self) -> bool:
        """Whether the owner has turned the private-address guard off."""
        try:
            return bool(await self.config.allow_private_urls())
        except Exception:
            # A Config that will not answer must not turn the guard off.
            log.exception("Could not read the Retro private-URL setting.")
            return False

    async def _download_bytes(
        self, url: str, max_size: int, size_label: str, what: str = "file"
    ) -> typing.Tuple[str, bytes]:
        """
        Fetch a URL into memory, capped at ``max_size``.

        Every byte the bot fetches on somebody else's word comes through here,
        so this is where the SSRF guard lives: :func:`net.guarded_get` refuses
        a scheme that is not http(s), resolves the hostname and refuses any
        address that is not a public one, connects to the address it just
        checked, and re-checks every redirect hop. See retro/net.py.

        Returns (filename, data). Raises DownloadError with a message written
        for the person who gave us the URL -- and a *single* message, shared
        between "that address is not allowed", "nothing answered" and "it
        timed out", so the reply cannot be used to map the bot's network.
        """
        too_big = f"That {what} is bigger than the {size_label} limit."
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        allow_private = await self._allow_private_urls()
        try:
            async with net.guarded_get(
                url, timeout=timeout, allow_private=allow_private
            ) as resp:
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
        except net.BlockedURL as error:
            # The reason goes to the log and nowhere else. The reply below is
            # the same one a refused connection and a timeout get.
            log.warning("Refusing to fetch a %s URL: %s", what, error)
            raise DownloadError(net.REFUSAL) from error
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as error:
            # Deliberately indistinguishable from a blocked address: "connection
            # refused" and "timed out" are exactly the two answers a port
            # scanner is looking for. The detail is logged instead.
            log.info("A %s download did not connect: %r", what, error)
            raise DownloadError(net.REFUSAL) from error
        except aiohttp.ClientError as error:
            # Something went wrong *after* a connection to an allowed address
            # was made (a malformed response, a broken chunked body), which
            # says nothing about the bot's network, so it can be reported.
            raise DownloadError(f"Downloading the {what} failed: {error}") from error
        if len(data) > max_size:
            raise DownloadError(too_big)
        return filename, data

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
        self._release_view(view)

    # -- Rate limiting ------------------------------------------------------
    #
    # Two buckets on the one command, because the two things being protected
    # are different: the per-user cooldown (a decorator) stops one person
    # looping a download, and the per-channel one stops a roomful of people
    # doing it between them. Neither is charged for anything cheap -- see
    # _forgive_cooldown, which is called on every path through `[p]retro` that
    # does not fetch a ROM.

    @staticmethod
    def _forgive_cooldown(ctx: commands.Context) -> None:
        """
        Hand a command's cooldown back: this invocation cost nothing.

        Bare `[p]retro` to bring the channel's game back, asking for the game
        that is already running, and a name that is not a saved game all take
        this path. Charging for them is what turns a rate limit that only bites
        on abuse into one that makes the cog annoying.
        """
        command = getattr(ctx, "command", None)
        reset = getattr(command, "reset_cooldown", None)
        if reset is None:
            return
        try:
            reset(ctx)
        except Exception:
            log.debug("Could not reset a Retro cooldown.", exc_info=True)

    def _channel_start_delay(self, ctx: commands.Context) -> float:
        """
        Seconds this channel must wait before starting another game, or 0.

        Checked (and charged) only where a start is about to cost a download,
        so the channel bucket is not spent on a resume either.
        """
        try:
            bucket = self.start_buckets.get_bucket(ctx)
            if bucket is None:  # pragma: no cover - only a custom BucketType
                return 0.0
            return float(bucket.update_rate_limit() or 0.0)
        except Exception:
            # A rate limit that cannot be calculated must not stop the game.
            log.debug("Could not check the Retro channel cooldown.", exc_info=True)
            return 0.0

    # -- Commands -----------------------------------------------------------

    @commands.max_concurrency(1, commands.BucketType.channel)
    # Three fetches a minute each. Starting a game is a once-an-hour action in
    # normal play and every press afterwards is a button, not a command, so
    # this is only ever reached by somebody hammering it -- and the cheap
    # paths hand it straight back (see _forgive_cooldown).
    @commands.cooldown(
        START_COOLDOWN_RATE, START_COOLDOWN_SECONDS, commands.BucketType.user
    )
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
            self._forgive_cooldown(ctx)
            await ctx.send(
                "No emulator cores are installed. Ask the bot owner to run "
                f"`{ctx.clean_prefix}retroset download` first."
            )
            return

        game = game.strip() if game else None
        existing = self.sessions.get(ctx.channel.id)

        # Bare `[p]retro` with a session in the channel means "bring it back".
        if game is None and not ctx.message.attachments:
            # Nothing is fetched down either of these paths, so neither is
            # charged against the cooldown.
            self._forgive_cooldown(ctx)
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
                self._forgive_cooldown(ctx)
                await ctx.send(
                    f"There's no saved game called `{game}`. Pass a ROM URL, "
                    f"attach a ROM, or see `{ctx.clean_prefix}retroset game list`."
                )
                return
            # Asking for the game that is already going here resumes it
            # rather than downloading the ROM all over again.
            if existing is not None and existing.source.lower() == source.lower():
                self._forgive_cooldown(ctx)
                await self._resume_session(ctx, existing)
                return

        # Past this point a ROM really is going to be fetched and written, so
        # this is where the channel's share of the rate limit and the bot's
        # disk budget are spent.
        delay = self._channel_start_delay(ctx)
        if delay:
            await self._safe_send(
                ctx,
                "This channel has started a lot of games in the last minute, "
                f"so this one was not fetched. Try again in {delay:.0f}s "
                "\N{EM DASH} the game already on screen still works, and "
                "pressing its buttons is never rate limited.",
            )
            return
        room, note = await self._make_room(0, prefix=ctx.clean_prefix)
        if note:
            # Either the refusal, or the report of what was pruned to avoid
            # one. Both are worth saying out loud.
            await self._safe_send(ctx, note)
        if not room:
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
        # The real size is only known now, so this is the check that counts:
        # the one in `[p]retro` refuses a download when the disk is already
        # full, and this one refuses to write what came back. Pruning here
        # excludes this game's own ROM, which may already be on disk from the
        # last time the channel played it.
        room, note = await self._make_room(
            len(data), keep_rom=rom_filename, prefix=ctx.clean_prefix
        )
        if note:
            await self._safe_send(ctx, note)
        if not room:
            return
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
        # The oldest of this channel's cached games fall off the end here, and
        # the records that pointed at them go with them.
        await self._forget_pruned_roms(
            self._prune_cached_games(ctx.channel.id, slug)
        )

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
        progress, restored_notice = self._saved_progress(ctx.channel.id, slug)

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
                        progress,
                        on_booted=lambda booted: self._settle_boot(
                            booted, progress, restored_notice
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
        except asyncio.CancelledError:
            # `[p]unload retro`, a bot shutdown or a cancelled command task,
            # arriving while the core is booting or the message is being
            # sent. Exception does not cover this, and the core is already
            # loaded: without freeing it here, MAX_LIVE_EMULATORS is spent
            # for the life of the process. Nothing is awaited on the way out
            # -- a cancelled task cannot rely on that -- so the state is
            # written and the core freed synchronously.
            self.sessions.pop(getattr(ctx.channel, "id", view.channel_id), None)
            self._write_state_now(view, emulator)
            self._free_emulator(emulator)
            view.emulator = None
            view.closed = True
            self._release_view(view)
            raise
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
        # Sanitised, exactly as the press line's name is: this sentence goes
        # on the game's message, where a nickname full of markdown would
        # otherwise reformat it. See RetroView.presser_name.
        who = presser_name(ctx.author)
        try:
            async with view.lock:
                await self.hibernate(
                    view,
                    f"Stopped{' by ' + who if who else ''}. Press a button "
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

    @commands.guild_only()
    @commands.command()
    async def retroreset(self, ctx: commands.Context) -> None:
        """
        Reboot this channel's game, as if you had flipped its power switch.

        The game starts again from its title screen, here and now, and the
        clip on its message shows it booting. **This is not the same thing as
        `[p]retrosaves reset`**, which touches no running game at all: that
        one deletes a save state *file* on disk so the game starts from the
        last in-game save the next time somebody plays it. This one reboots
        the game that is playing right now.

        Nothing on disk is deleted or overwritten. The save state still holds
        the moment before the reset until the game saves again of its own
        accord -- a few presses of the rebooted game, or the next time it
        goes to sleep -- so a reset that was a mistake is recoverable: press
        **Undo**, which steps straight back to the moment before it. The
        cartridge's battery save (the one the game itself writes when you
        save from its own menu) is not touched at all, exactly as resetting a
        real console never wiped one.

        Only the person who started the game, members with the Manage
        Messages permission, and the bot owner can reset it, for the same
        reason only they can stop it: it throws away everybody's
        progress-in-flight.

        **Examples:**
        - `[p]retroreset`
        """
        view = self.sessions.get(ctx.channel.id)
        if view is None:
            await self._safe_send(
                ctx,
                "No game is running in this channel. Start one with "
                f"`{ctx.clean_prefix}retro <name or url>`.",
            )
            return
        if not await view.can_stop(ctx.author):
            await self._safe_send(
                ctx,
                "Only the person who started the game, moderators, or the "
                "bot owner can reset it.",
            )
            return
        # The view's own lock, exactly as `[p]retrostop` takes it, so a press
        # that is already being emulated finishes before the machine is
        # rebooted underneath it.
        async with ctx.typing():
            try:
                async with view.lock:
                    clip = await self.run_reset(view)
            except EmulatorError as error:
                await self._safe_send(
                    ctx, f"**{view.game_name}** could not be reset: {error}"
                )
                return
            except Exception:
                log.exception(
                    "Unexpected failure resetting the Retro session in channel %s.",
                    ctx.channel.id,
                )
                await self._safe_send(
                    ctx, "The emulator hit an unexpected error; the game is untouched."
                )
                return
        log.info(
            "Reset %s in channel %s at %s's request.",
            view.slug,
            ctx.channel.id,
            getattr(ctx.author, "id", "?"),
        )
        # The reset clip goes on the game's own message, with the line that
        # says who did what -- one edit, like a press, and named the same way
        # a press is even though this is a command rather than a button.
        await view.show_clip(clip, view.reset_note(ctx.author))
        # Sanitised for the same reason the line on the message is, and for
        # one more: this is a plain channel message rather than an edit, so
        # escaping the name is the only thing between a nickname of
        # "@everyone" and a notification. See RetroView.presser_name.
        who = presser_name(ctx.author)
        await self._safe_send(
            ctx,
            f"**{view.game_name}** has been reset{' by ' + who if who else ''} "
            "\N{EM DASH} it is back at its title screen. Its in-game battery "
            "save is untouched, and nothing on disk has been overwritten: "
            "press **Undo** on the game to step straight back to the moment "
            "before the reset, or carry on playing and the reset becomes the "
            "save a few presses from now. To throw away a save state *file* "
            f"instead, use `{ctx.clean_prefix}retrosaves reset`.",
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

        The whole set is about 4.5 MiB to download and about 31 MiB once
        unpacked, which is what counts against `[p]retroset diskbudget`. None
        of these cores need a BIOS file. For one that does, see
        `[p]retroset bios`.

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
        async with ctx.typing():
            for index, name in enumerate(wanted, start=1):
                ok, size, message = await self._download_core(name)
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
        # Declared here because discord.py registers a subcommand by
        # calling a decorator on its parent Group object, which lives in this
        # module; everything it does is in CoresMixin. See cores.py.
        await self._coreoptions(ctx, core, key, value)

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

    @staticmethod
    def _describe_press_fit(clip_seconds: float, hold_ms: int) -> str:
        """
        What a clip this short does to the input scheduled inside it.

        Said by both `[p]retroset cliplength` and `[p]retroset hold`, because
        the two settings constrain each other: a clip has to have room to
        show the button come back up, so a short one is a ceiling on the hold
        and on how many taps the repeat button can fit. Saying nothing would
        leave an owner who set a 400ms hold and a 0.2 second clip wondering
        why presses feel shorter than they asked for.

        Worked out at DEFAULT_FPS rather than a live core's rate, because
        this is about a setting rather than about one session, and every
        console here is within half a percent of it. See
        :func:`RetroView.press_plan`, which is what actually decides.
        """
        plan = press_plan(DEFAULT_FPS, clip_seconds, hold_ms, REPEAT_TAPS)
        held_ms = round(1000 * plan[0][1] / DEFAULT_FPS)
        taps = len(plan)
        notes = []
        if held_ms < hold_ms - 5:
            notes.append(
                f"A press is held for about {held_ms}ms rather than the "
                f"{hold_ms}ms configured, so the clip can still show the "
                "button coming back up."
            )
        if taps < MIN_REPEAT_TAPS:
            notes.append(
                "The repeat button is not shown at this length: only one tap "
                "fits, which is what the confirm button already does. It "
                "comes back on the next press at a longer clip."
            )
        elif taps < REPEAT_TAPS:
            notes.append(
                f"The repeat button taps {taps} times rather than {REPEAT_TAPS}."
            )
        return " ".join(notes)

    @retroset.command(name="cliplength", aliases=["clip"])
    async def retroset_cliplength(self, ctx: commands.Context, seconds: float) -> None:
        """
        Set how many seconds of play each clip shows.

        Every button press posts an animated clip of what happened next.
        Longer clips show more of the game; shorter ones make a turn — press,
        watch, press again — quicker, which is what most of these games want.
        Fractions are allowed, so `0.8` is a real answer.

        The value is clamped between 0.2 and 15 seconds and applies to clips
        recorded afterwards. The default is 1 second.

        A very short clip is also a ceiling on the input inside it: a button
        is never held past the point where the clip can still show it coming
        back up, and the repeat button taps as many times as fit. Below about
        0.48 seconds only one tap fits -- which is what the console's own
        confirm button already does -- so the repeat button is not shown at
        all, and it reappears on the next press at a longer clip. Wait and
        Undo stay where they are either way.

        **Examples:**
        - `[p]retroset cliplength 0.8`
        - `[p]retroset cliplength 4`

        **Arguments:**
        - `<seconds>` - Seconds of play per clip (0.2-15, fractions allowed).
        """
        seconds = clamp_clip_seconds(seconds)
        await self.config.clip_seconds.set(seconds)
        for view in self.sessions.values():
            view.clip_seconds = seconds
        fit = self._describe_press_fit(seconds, await self.config.hold_ms())
        await ctx.send(
            f"Clips now show {describe_seconds(seconds)} of play.{' ' + fit if fit else ''}"
        )

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
        160. It is a ceiling rather than a promise: a hold longer than the
        clip can show being released is cut down to fit (see
        `[p]retroset cliplength`).

        **Examples:**
        - `[p]retroset hold 200`

        **Arguments:**
        - `<milliseconds>` - How long a button stays down (50-2000).
        """
        milliseconds = max(MIN_HOLD_MS, min(MAX_HOLD_MS, milliseconds))
        await self.config.hold_ms.set(milliseconds)
        for view in self.sessions.values():
            view.hold_ms = milliseconds
        fit = self._describe_press_fit(
            clamp_clip_seconds(await self.config.clip_seconds()), milliseconds
        )
        await ctx.send(
            f"Every button, directions included, is now held for "
            f"{milliseconds}ms.{' ' + fit if fit else ''}"
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
        # A stored URL is fetched later, on a player's `[p]retro <name>`, and
        # that fetch is guarded (see _download_bytes). Checking it here as well
        # means a URL that can never work is refused while the person adding it
        # is still looking at it, rather than confusing a player next week.
        # This is the owner, so the reason is spelled out rather than hidden
        # behind the one generic refusal players get.
        refusal = await net.refuse_reason(
            url, allow_private=await self._allow_private_urls()
        )
        if refusal is not None:
            await ctx.send(
                f"That URL will not be fetched: {refusal}. The bot refuses to "
                "make requests to its own network (see **ROM URLs** in the "
                "cog's README). If this really is a ROM library on your LAN, "
                f"`{ctx.clean_prefix}retroset allowprivateurls true` turns the "
                "guard off \N{EM DASH} read what that command says first."
            )
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

    # Owner-only already, so this is a guard against a script gone wrong
    # rather than against a stranger: one `bios add` can pull 64 MiB.
    @commands.cooldown(
        BIOS_COOLDOWN_RATE, BIOS_COOLDOWN_SECONDS, commands.BucketType.user
    )
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
        room, note = await self._make_room(len(data), prefix=ctx.clean_prefix)
        if note:
            await self._safe_send(ctx, note)
        if not room:
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

        room, budget_note = await self._make_room(
            sum(len(f.data) for f in files), prefix=ctx.clean_prefix
        )
        if budget_note:
            await self._safe_send(ctx, budget_note)
        if not room:
            return

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

    @retroset.command(name="diskbudget", aliases=["disk", "budget"])
    async def retroset_diskbudget(
        self, ctx: commands.Context, megabytes: typing.Optional[int] = None
    ) -> None:
        """
        Set how much disk the cog may use in total, in MiB.

        Everything the cog stores counts: the emulator cores, every channel's
        cached ROMs, every save state and battery save with the one previous
        generation each, and any BIOS files you have installed. When a new
        download would go over the budget, cached ROMs nobody is playing are
        deleted oldest first to make room, and if that is not enough the
        download is refused with an explanation.

        **No save is ever deleted to make room**, not even to let somebody
        else start a game. If the budget is full of saves, free some with
        `[p]retrosaves delete <game>` or raise the number here.

        The default is 1024 MiB. `0` means no limit at all, which is a real
        answer for a machine with a big disk and a small number of channels.

        Run it with no argument to see the budget and what is using it.

        **Examples:**
        - `[p]retroset diskbudget`
        - `[p]retroset diskbudget 4096`
        - `[p]retroset diskbudget 0`

        **Arguments:**
        - `[megabytes]` - The ceiling in MiB, or `0` for no limit.
        """
        if megabytes is None:
            await self._safe_send(ctx, await self._usage_report(ctx))
            return
        megabytes = max(0, min(MAX_DISK_BUDGET_MB, int(megabytes)))
        await self.config.disk_budget_mb.set(megabytes)
        if not megabytes:
            await ctx.send(
                "The disk budget is off: the cog will keep downloading games "
                "until the disk itself runs out. Cached ROMs are still pruned "
                f"to the {MAX_CACHED_GAMES_PER_CHANNEL} most recent games per "
                "channel."
            )
            return
        await self._safe_send(
            ctx,
            f"The disk budget is now {megabytes:,} MiB.\n"
            + await self._usage_report(ctx),
        )

    async def _usage_report(self, ctx: commands.Context) -> str:
        """What the data directory holds, against what it is allowed."""
        usage = await asyncio.to_thread(self._data_usage)
        budget = await self._disk_budget()
        total = usage.get("total", 0)
        headline = (
            f"Game storage is using **{self._humanize_bytes(total)}**"
            + (
                f" of the {self._humanize_bytes(budget)} allowed"
                f" ({100 * total / budget:.0f}%)."
                if budget
                else " and has no budget set."
            )
        )
        named = {
            "cores": "emulator cores",
            "roms": "cached ROMs",
            "states": "saves (and their previous generation)",
            "system": "BIOS files",
            "other": "everything else",
        }
        parts = [
            f"{label}: {self._humanize_bytes(usage[key])}"
            for key, label in named.items()
            if usage.get(key)
        ]
        lines = [headline]
        if parts:
            lines.append(humanize_list(parts) + ".")
        if budget and total > budget:
            lines.append(
                "It is over budget, so the next download will prune cached "
                "ROMs (never saves) and may be refused."
            )
        lines.append(f"`{cog_data_path(self)}`")
        return "\n".join(lines)

    @retroset.command(name="allowprivateurls", aliases=["allowprivate"])
    async def retroset_allowprivateurls(
        self, ctx: commands.Context, enabled: typing.Optional[bool] = None
    ) -> None:
        """
        Let ROM URLs point inside your own network. Off, and best left off.

        **What this protects.** `[p]retro <url>` is open to everybody in the
        channel, and the bot fetches that URL from wherever the bot is
        running. Normally every address the URL resolves to must be a public
        one: loopback, private, link-local, unique-local, multicast and
        reserved addresses are all refused, at every redirect hop, so nobody
        can use the bot to reach `http://localhost:8080`, the Docker bridge,
        your router, or a cloud metadata service at 169.254.169.254 — which
        on most hosting providers hands out credentials to anything that asks.

        **Turning this on removes that protection for everybody**, not just
        for you: any member who can run `[p]retro` can then aim the bot at any
        address it can reach, and use the difference between the replies to
        map your network. Only do it on a bot you run at home, for a ROM
        library on your own LAN, in a server whose members you trust
        completely — and prefer `[p]retroset game add` with the guard left on
        if the library is reachable from the internet at all.

        Run it with no argument to see the current setting.

        **Examples:**
        - `[p]retroset allowprivateurls`
        - `[p]retroset allowprivateurls true`
        - `[p]retroset allowprivateurls false`

        **Arguments:**
        - `[enabled]` - `true` or `false`.
        """
        if enabled is None:
            current = await self._allow_private_urls()
            await ctx.send(
                "Private and loopback ROM URLs are "
                + (
                    "**allowed**. Anybody who can run "
                    f"`{ctx.clean_prefix}retro` can make the bot fetch from "
                    "inside your network; turn it back off with "
                    f"`{ctx.clean_prefix}retroset allowprivateurls false`."
                    if current
                    else "**refused**, which is the default and the safe "
                    "setting. A URL that resolves to a private or loopback "
                    "address is not fetched."
                )
            )
            return
        await self.config.allow_private_urls.set(bool(enabled))
        if enabled:
            log.warning(
                "The Retro cog's private-address URL guard has been turned "
                "OFF by the bot owner: any member who can run the retro "
                "command can now make this bot issue requests inside its own "
                "network."
            )
            await ctx.send(
                "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} ROM URLs may now "
                "point inside your network, including at `localhost`. Every "
                "member who can run "
                f"`{ctx.clean_prefix}retro` can use that, so only leave it on "
                "if you trust everybody in every server this bot is in. Turn "
                f"it off with `{ctx.clean_prefix}retroset allowprivateurls "
                "false`."
            )
            return
        await ctx.send(
            "ROM URLs must point at public addresses again. A URL that "
            "resolves to a loopback, private, link-local or reserved address "
            "is refused, and so is a public URL that redirects to one."
        )

    @retroset.command(name="version")
    async def retroset_version(self, ctx: commands.Context) -> None:
        """
        Say which build of this cog is actually loaded.

        Three things: the version `info.json` declares, the commit it was
        installed from when there is a `.git` to read, and a hash of the
        `.py` files as they were when the cog was loaded. The last one is
        the one that cannot go stale -- pulling new code without
        `[p]reload retro` deliberately does not change it, which is exactly
        the situation worth being able to prove.

        Plain text rather than an embed, so it needs no extra permission and
        can be pasted into a bug report as it stands.

        **Examples:**
        - `[p]retroset version`
        """
        await self._safe_send(ctx, version.describe(ctx.clean_prefix))

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

        # First, and deliberately: half the confusing answers this command
        # has ever given were because the bot was running an older build
        # than the person reading it. `[p]retroset version` says more.
        build = [version.summary()]
        if version.FINGERPRINT:
            build.append(f"loaded code `{version.FINGERPRINT}`")
        embed.add_field(
            name="Build",
            value=(
                " \N{EM DASH} ".join(build)
                + f"\n`{ctx.clean_prefix}retroset version` for the details."
            )[:1024],
            inline=False,
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
        clip_seconds = clamp_clip_seconds(await self.config.clip_seconds())
        embed.add_field(
            name="Clip length",
            value=(
                f"{describe_seconds(clip_seconds)} of play per button press "
                f"({format_seconds(MIN_CLIP_SECONDS)}-"
                f"{format_seconds(MAX_CLIP_SECONDS)}, fractions allowed)."
            ),
            inline=False,
        )
        hold_ms = await self.config.hold_ms()
        fit = self._describe_press_fit(clip_seconds, hold_ms)
        embed.add_field(
            name="Button hold",
            value=f"{hold_ms}ms per press, directions included{'. ' + fit if fit else ''}",
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
        embed.add_field(
            name="Storage",
            value=(await self._usage_report(ctx))[:1024],
            inline=False,
        )
        embed.add_field(
            name="ROM URLs",
            value=(
                (
                    "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} **Private and "
                    "loopback addresses are allowed.** Anybody who can run "
                    f"`{ctx.clean_prefix}retro` can make the bot fetch from "
                    "inside this network."
                    if await self._allow_private_urls()
                    else "Only public addresses are fetched; a URL that "
                    "resolves to a loopback, private, link-local or reserved "
                    "address is refused, redirects included."
                )
                + f"\n`{ctx.clean_prefix}retroset allowprivateurls`"
            )[:1024],
            inline=False,
        )
        await self._safe_send(ctx, embed=embed)
