"""
Emulator cores: finding them, fetching them, and their own settings.

Two halves that only meet at the core name. The first is installation --
cores are *found* rather than configured, by scanning the cog's own cores
folder, and downloaded from the libretro buildbot when they are missing. The
second is core options: every libretro core declares its own settings, and
this reads them, caches them, and applies the owner's overrides.
"""

import asyncio
import io
import logging
import platform
import sys
import time
import typing
import zipfile
from pathlib import Path

import aiohttp
from redbot.core import commands
from redbot.core.utils.chat_formatting import humanize_list

from . import net
from .abc import MixinMeta
from .emulator import EmulatorError, RetroEmulator, probe_core_options
from .systems import (
    CORES,
    SYSTEMS,
    core_filename,
    core_name_from_filename,
    system_for_core,
)

log = logging.getLogger("red.robloach.retro")

BUILDBOT = "https://buildbot.libretro.com/nightly"

# The core downloads fetch every core in CORES from one host in a row.
CORE_DOWNLOAD_TIMEOUT_SECONDS = 300

# How long to wait before the automatic core download is allowed to try
# again. Without it, a cog that is reloaded in a loop (or a core the buildbot
# has stopped publishing) would hit the buildbot on every single load.
AUTO_DOWNLOAD_COOLDOWN_SECONDS = 6 * 60 * 60

# The value that clears a core option override and puts the core's own default
# back. It has to be a word no core uses as a real value, or setting it would
# be ambiguous. "none" is out because snes9x and genesis_plus_gx both offer
# it, "off" because mGBA does and "disabled" because most of them do;
# "default" happens to be unused by the cores installed today, but it is
# exactly the word a core added tomorrow would reach for. No option in any of
# the cores this cog installs offers "reset", and an emulator test keeps
# checking that.
OPTION_RESET = "reset"

# How many of an option's allowed values one line of `[p]retroset coreoptions
# <core>` shows before it gives up and points at the single-option view. Some
# cores offer sixty-odd palettes on a single setting.
MAX_LISTED_VALUES = 8

# How long the cores directory must have been quiet before a scan of it is
# safe to cache. Filesystems round mtimes -- FAT to two whole seconds -- so a
# file dropped in just after a scan can leave the directory's mtime looking
# unchanged, and a listing cached in that window could hide the file for as
# long as the directory stays untouched. A listing taken while the mtime is
# still within this window of "now" is therefore used once and never cached,
# so the very next lookup rescans. See _scan_cores_dir.
CORES_MTIME_SETTLE_SECONDS = 2


class CoresMixin(MixinMeta):
    """Installing cores, and reading and writing their options."""

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

    # Cores are found, not configured. The managed cores directory is checked
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

    # The last full listing of the cores directory, keyed by the directory
    # mtime it was taken under. A class attribute rather than an __init__
    # assignment because the mixin has no __init__ of its own; the first
    # write creates the instance attribute, so two cogs in one process (the
    # tests build several) never share a cache.
    _cores_scan_cache: typing.Optional[
        typing.Tuple[int, typing.Dict[str, Path]]
    ] = None

    def _invalidate_cores_scan(self) -> None:
        """Forget the cached cores listing; the next lookup rescans."""
        self._cores_scan_cache = None

    # A directory's mtime moves whenever an entry is added, removed or
    # renamed, so a single stat() of the directory itself says whether the
    # last listing is still the truth. That keeps the promise above --
    # detected, not registered: a hand-dropped core still shows up on the
    # very next lookup -- while game starts and coreoptions commands stop
    # paying for a full listing, with a stat per entry, on the event loop
    # every single time. On a networked data directory that walk is what
    # used to stall every session.
    #
    # The one thing an mtime cannot promise is a change inside its own
    # rounding tick (see CORES_MTIME_SETTLE_SECONDS), so a fresh listing is
    # only cached once the directory has been quiet for longer than the
    # tick. And the one writer inside this cog, _download_core, does not
    # lean on any of this: it drops the cache by hand after every write. A
    # directory whose mtime sits in the future (a networked filesystem with
    # a skewed clock) never looks quiet, which costs the cache but never
    # the correctness -- every lookup just rescans, as it always did.

    def _scan_cores_dir(self) -> typing.Dict[str, Path]:
        """Every core this cog knows sitting in the managed cores directory."""
        try:
            directory = self._cores_dir()
            # stat() before iterdir(): a file that lands in between is then
            # listed under the pre-change mtime, and the next lookup notices
            # the newer mtime and rescans. The other order would cache a
            # listing the file is missing from under the post-change mtime,
            # and that would stick.
            mtime_ns = directory.stat().st_mtime_ns
        except OSError:
            log.warning("Could not read the Retro cores directory.", exc_info=True)
            self._cores_scan_cache = None
            return {}
        cached = self._cores_scan_cache
        if cached is not None and cached[0] == mtime_ns:
            return dict(cached[1])
        found: typing.Dict[str, Path] = {}
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            log.warning("Could not read the Retro cores directory.", exc_info=True)
            self._cores_scan_cache = None
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
        if time.time_ns() - mtime_ns > CORES_MTIME_SETTLE_SECONDS * 1_000_000_000:
            self._cores_scan_cache = (mtime_ns, dict(found))
        else:
            self._cores_scan_cache = None
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
        # for another platform's suffix. The filename probe is a live stat on
        # purpose, not a lookup in the cached scan: the scan only ever returns
        # names in CORES, and this method is also called with names that are
        # not (a core an old save record remembers, say), whose file only this
        # probe can find. One stat is the same price as the scan cache's own
        # mtime check, so there is nothing to save by folding it in.
        candidate = self._cores_dir() / core_filename(core)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            pass
        return self._scan_cores_dir().get(core)

    def _cores_dir(self) -> Path:
        return self._data_dir("cores")

    def _cores_downloading(self) -> bool:
        """
        Whether the automatic core download is still running.

        Cores fetch themselves in the background the first time the cog
        loads (see :meth:`_auto_download_loop`), and that is silent: nothing
        is posted when it starts, nothing when it finishes. So for the first
        minute or so of a fresh install there are genuinely no cores
        installed *and* asking the owner to install some is the wrong advice
        -- the install message has just promised they arrive on their own.

        Read off the task the cog created rather than a flag, so it cannot
        drift out of step with reality: a task that has finished, failed,
        been cancelled by `[p]unload`, or was never started because
        `[p]retroset autodownload` is off all answer False.
        """
        task = getattr(self, "_download_task", None)
        return task is not None and not task.done()

    def _no_cores_message(self, prefix: str) -> str:
        """
        What to say when a game cannot start because nothing is installed.

        Two genuinely different situations, and telling them apart is the
        whole point: "ask the bot owner" is a lie while the download the
        owner was promised is in flight. See :meth:`_cores_downloading`.
        """
        if self._cores_downloading():
            return (
                "The emulator cores are still downloading \N{EM DASH} they "
                "fetch themselves in the background the first time this cog "
                "loads. Try again in a moment."
            )
        return (
            "No emulator cores are installed yet. They normally download "
            "themselves; ask the bot owner to run "
            f"`{prefix}retroset download` to fetch them now."
        )

    def _missing_core_message(self, prefix: str, system, core: str) -> str:
        """
        The same, for a console whose own emulator is the one missing.

        "Core" is what libretro calls one of these and it means nothing to a
        player, so the sentence says *emulator for the Game Boy* and puts the
        core's name in backticks beside it, where it reads as the thing the
        owner has to type rather than as jargon.
        """
        if self._cores_downloading():
            return (
                f"This bot has no {system.name} emulator installed yet, and "
                "the emulators are still downloading in the background. Try "
                "again in a moment."
            )
        return (
            f"This bot has no {system.name} emulator installed (it needs "
            f"`{core}`, the libretro emulator for the {system.name}). Ask the "
            f"bot owner to run `{prefix}retroset download {core}`."
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

    async def _download_core(self, core: str) -> typing.Tuple[bool, int, str]:
        """
        Fetch one core from the buildbot and record where it landed.

        Returns (installed, bytes on disk, message). Never raises: a core
        that fails is reported and the rest of the set carries on.

        The buildbot is a fixed, known-good, public host, but it goes through
        the same guard as a stranger's URL rather than around it: a general
        bypass is the kind of thing that later grows a second caller. There is
        nothing to bypass anyway -- buildbot.libretro.com resolves to a public
        address, which is exactly what the guard allows.
        """
        target = self._buildbot_url(core)
        if target is None:
            return False, 0, "no build for this platform"
        url, name = target
        timeout = aiohttp.ClientTimeout(total=CORE_DOWNLOAD_TIMEOUT_SECONDS)
        try:
            async with net.guarded_get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return False, 0, f"buildbot returned status {resp.status}"
                payload = await resp.read()
        except net.BlockedURL as error:
            log.warning("Refusing to fetch the %s core: %s", core, error)
            return False, 0, "the buildbot URL was refused by the URL guard"
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
        # The write bumped the cores directory's mtime, which the scan cache
        # watches -- but mtimes are rounded on some filesystems, and the one
        # write this cog makes itself does not get to gamble on granularity:
        # drop the cache outright and let the next lookup rescan.
        self._invalidate_cores_scan()

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
                "is loaded again. Run the `retroset download` command to "
                "retry now."
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
        for name in missing:
            ok, size, message = await self._download_core(name)
            if ok:
                succeeded += 1
                log.info("Installed the %s libretro core (%s KiB).", name, size // 1024)
            else:
                failed += 1
                log.warning("Could not download the %s libretro core: %s", name, message)
        log.info(
            "Automatic core download finished: %s installed, %s failed.",
            succeeded,
            failed,
        )

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
        definitions: typing.Dict[str, dict] = {}
        async with self.emulator_lock:
            # A libretro core has process-global state, so nothing else may be
            # loaded while this one is. _evict_locked() with no exclusion
            # hibernates every live session, saving each one first.
            await self._evict_locked()
            try:
                definitions = await self.run_in_emulator_thread(
                    probe_core_options, core_path, seeded
                )
            except EmulatorError as error:
                log.warning("Could not probe the %s core for options: %s", core, error)
            except Exception:
                log.exception("Probing the %s core for options failed.", core)
        # Whatever this probe put to sleep is told so now, with the lock
        # given back rather than while it is held; see Retro._flush_refreshes.
        await self._flush_refreshes()
        return definitions

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
        prefix: str,
    ) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
        """
        Turn what somebody typed into a real option key.

        ``prefix`` has no default on purpose: the error this returns is sent
        to a channel and names a command, and Red only rewrites ``[p]`` in a
        docstring. A default would be a `[p]` waiting for the next caller to
        forget.

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

    # -- Core options command -----------------------------------------------

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
            changed = await self.run_in_emulator_thread(
                emulator.set_option, key, value
            )
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

    async def _coreoptions(
        self,
        ctx: commands.Context,
        core: typing.Optional[str],
        key: typing.Optional[str],
        value: typing.Optional[str],
    ) -> None:
        """
        Everything `[p]retroset coreoptions` does, once its arguments are in.

        The command itself is declared in retro/Retro.py, next to the
        ``retroset`` group it hangs off -- discord.py registers a subcommand
        by calling a decorator on its parent Group object, so the declaration
        cannot move away from the group. The work is all here.
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
