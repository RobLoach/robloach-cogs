"""
BIOS and firmware files: fetching them, and unpacking them where cores look.

Some consoles cannot boot a game without a copy of their own firmware. Cores
look for it in libretro's *system directory* -- one folder the frontend hands
over -- and this is everything the cog does with that folder's contents:
fetching a file (or a whole zip of them) from an attachment or a URL, and
writing it where the core will actually ask for it.

Two things are deliberately **not** here.

The *paths* are in retro/storage.py, with every other path this cog owns:
``_bios_name`` and ``_bios_path`` are what decide whether a name can be
stored truthfully, and they belong beside the containment rules the rest of
the data directory is written under.

The *commands* are in retro/Retro.py, and cannot move. ``[p]retroset bios``
is a subgroup of ``[p]retroset``, and discord.py binds a subcommand to its
parent group object at import time -- so a subcommand can only be written
where the group it hangs off is. retro/cores.py lives with exactly the same
constraint for `[p]retroset download` and `[p]retroset coreoptions`. What is
here is what the commands *do*, which is the part worth having in one place:
a command body is then a docstring, a permission check and a call.
"""

import asyncio
import logging
import typing
from pathlib import Path

import discord
from redbot.core import commands
from redbot.core.utils.chat_formatting import pagify

from . import archives
from .abc import MixinMeta
from .net import DownloadError

log = logging.getLogger("red.robloach.retro")

# Console firmware is small (a Game Boy boot ROM is 256 bytes, a PlayStation
# BIOS 512 KiB, the largest anyone is likely to install a couple of MiB), so
# this is only here to stop a mistyped URL filling the bot's disk.
MAX_BIOS_SIZE = 16 * 1024 * 1024
MAX_BIOS_SIZE_LABEL = "16 MiB"

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

# `[p]retroset bios add` is owner-only and downloads up to 64 MiB, so this is
# a guard against a fat-fingered loop rather than against a stranger.
BIOS_COOLDOWN_RATE = 3
BIOS_COOLDOWN_SECONDS = 60.0


class BiosMixin(MixinMeta):
    """Fetching firmware files and unpacking them into the system directory."""

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

    async def _install_bios_file(
        self,
        ctx: commands.Context,
        source: str,
        data: bytes,
        name: typing.Optional[str],
    ) -> None:
        """
        Store one firmware file in the system directory under a usable name.

        The name is validated rather than rewritten (see
        ``StorageMixin._bios_name``): a core asks for an exact filename, so a
        name that cannot be stored truthfully is refused with an explanation
        instead of being mangled into one the core will never look for.
        """
        name = name or self._bios_name(Path(str(source)).name)
        if name is None:
            await ctx.send(
                f"`{Path(str(source)).name}` is not a usable filename. Say "
                "what the core should see it as: `"
                f"{ctx.clean_prefix}retroset bios add <filename>"
                f"{' <url>' if source else ''}`."
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
        # Unpacked in two passes, and never all at once. A firmware pack is
        # allowed to be 64 MiB compressed and 64 MiB unpacked, so holding
        # every member in a list while the archive itself is still in memory
        # peaked at both together -- on the small VPS a Red bot usually lives
        # on, that is the whole machine. `archives.extract_each` hands over
        # one file at a time and drops it, so the peak is the archive plus a
        # single member.
        #
        # The cost is decompressing twice, and the reason is the disk budget:
        # `_make_room` has to be told the real total *before* anything is
        # written (it may have to prune to make space, and it may refuse), it
        # is async, and the sink below runs in a worker thread. So the first
        # pass counts and validates without keeping anything, and the second
        # writes. Both passes are bounded by the same caps, and this is an
        # owner-only command that installs firmware once.
        def _unpack(sink):
            return archives.extract_each(
                data,
                sink=sink,
                max_total_size=MAX_BIOS_TOTAL_SIZE,
                max_file_size=MAX_BIOS_SIZE,
                max_files=MAX_BIOS_FILES,
                what="BIOS file",
            )

        try:
            counted = await asyncio.to_thread(_unpack, lambda entry: None)
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

        # How many files there are is known now, so the rename decision is
        # settled before a single byte is written.
        renamed = ""
        rename_to: typing.Optional[str] = None
        if name and len(counted.paths) == 1:
            # One file in the zip and a name was given: the old behaviour,
            # which is how somebody puts `bios.bin` in as `scph5501.bin`.
            if counted.paths[0] != name:
                renamed = f" `{counted.members[0]}` was stored as `{name}`."
            rename_to = name
        elif name:
            renamed = (
                f" The `{name}` you named was ignored: a zip of "
                f"{len(counted.paths)} files keeps its own names."
            )

        room, budget_note = await self._make_room(
            counted.total_size, prefix=ctx.clean_prefix
        )
        if budget_note:
            await self._safe_send(ctx, budget_note)
        if not room:
            return

        written: typing.List[str] = []
        total = 0

        def _write(entry) -> None:
            nonlocal total
            if rename_to is not None:
                entry = entry._replace(path=rename_to)
            # One file at a time through the same writer the whole list used
            # to go through, so the containment check that is the last line
            # between an archive and the bot's filesystem is the same one.
            paths, size = self._write_bios_files([entry])
            written.extend(paths)
            total += size

        try:
            await asyncio.to_thread(_unpack, _write)
        except OSError as error:
            log.warning("Could not unpack a BIOS archive.", exc_info=True)
            await self._safe_send(
                ctx,
                f"The files could not be saved: {error}. The bot may be out "
                "of disk space.",
            )
            return
        except archives.ArchiveError as error:
            # The first pass read this archive happily, so reaching here
            # means it changed underneath us or the caps caught something the
            # count did not. Report it rather than half-claiming success.
            await self._safe_send(ctx, f"That zip could not be read: {error}")
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
        if counted.skipped:
            lines.append(
                f"{len(counted.skipped)} entr(y/ies) in the zip were skipped: "
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
