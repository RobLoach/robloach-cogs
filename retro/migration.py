"""
The one-off move out of the old ``RetroCog`` namespace.

Write-once code: the class was renamed from ``RetroCog`` to ``Retro``, Red
names both of a cog's storage locations after its class, and this brings a
pre-rename install's files and settings across. It will never need to change
again, which is exactly why it lives on its own.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from redbot.core import Config
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_list

from .abc import MixinMeta

log = logging.getLogger("red.robloach.retro")

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


class MigrationMixin(MixinMeta):
    """Bring a pre-rename install's data and settings across. Once."""

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
        # Imported here rather than at the top of the module: the Config
        # schema belongs to the cog, the cog imports this mixin, and a
        # module-level import would be a cycle. Doing it this way also means
        # there is still exactly one copy of the defaults, which is the whole
        # point of the comparison below -- a second copy that drifted would
        # make this quietly refuse to migrate anything.
        from .Retro import DEFAULT_CHANNEL, DEFAULT_GLOBALS

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
