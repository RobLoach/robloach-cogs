import asyncio
import io
import logging
import platform
import re
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

from .emulator import (
    CLIP_SECONDS,
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    MIN_ROM_SIZE,
    EmulatorError,
    RetroEmulator,
)
from .RetroView import (
    BOOT_SECONDS,
    DEFAULT_HOLD_MS,
    DEFAULT_TIMEOUT_MINUTES,
    MAX_HOLD_MS,
    MIN_HOLD_MS,
    SAVE_STATE_EVERY_PRESSES,
    RetroView,
)
from .systems import (
    CORES,
    SYSTEMS,
    core_name_from_filename,
    extensions_for_core,
    system_for_extension,
)

log = logging.getLogger("red.robloach.retro")

# A ceiling rather than a console limit: the largest cartridge any of these
# cores runs is a 12 MiB Super Nintendo game, and Discord will not let a bot
# attach more than 25 MiB anyway. URL downloads are not bound by Discord, so
# this is what stops a mistyped link pulling down a disc image.
MAX_ROM_SIZE = 32 * 1024 * 1024
MAX_ROM_SIZE_LABEL = "32 MiB"

BUILDBOT = "https://buildbot.libretro.com/nightly"

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


class RetroCog(commands.Cog):
    """
    Play retro console games together in Discord, emulated with libretro.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config: Config = Config.get_conf(
            self,
            identifier=114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121,
            force_registration=True
        )
        self.config.register_global(
            # Kept only so an install from before multi-console support can be
            # migrated into `cores` on load; nothing reads it afterwards.
            core_path="",
            # core name -> path on disk, e.g. {"gambatte": "/.../gambatte_libretro.so"}
            cores={},
            session_timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
            clip_seconds=CLIP_SECONDS,
            hold_ms=DEFAULT_HOLD_MS,
            games={}
        )
        # A session outlives its emulator, so the record of one lives here
        # and is reloaded when the cog (or the whole bot) starts again.
        self.config.register_channel(
            session=None
        )
        self.sessions: typing.Dict[int, RetroView] = {}
        # Serializes every core operation across all channels.
        self.emulator_lock: asyncio.Lock = asyncio.Lock()
        self._idle_task: typing.Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        try:
            await self._migrate_core_path()
        except Exception:
            log.exception("Failed to migrate the old Libretro core setting.")
        try:
            await self._restore_sessions()
        except Exception:
            log.exception("Failed to restore Libretro sessions.")
        self._idle_task = asyncio.create_task(self._hibernation_loop())

    async def cog_unload(self) -> None:
        task, self._idle_task = self._idle_task, None
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

    async def _installed_cores(self) -> typing.Dict[str, Path]:
        """Every configured core whose file is still on disk."""
        found: typing.Dict[str, Path] = {}
        for name, raw in (await self.config.cores()).items():
            path = Path(raw)
            if path.is_file():
                found[name] = path
        return found

    async def _core_path(self, core: str) -> typing.Optional[Path]:
        raw = (await self.config.cores()).get(core)
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None

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

    # -- Session storage ----------------------------------------------------

    async def _restore_sessions(self) -> None:
        """Rebuild hibernated sessions from Config and re-arm their buttons."""
        timeout_minutes = await self.config.session_timeout_minutes()
        clip_seconds = await self.config.clip_seconds()
        hold_ms = await self.config.hold_ms()
        for channel_id, data in (await self.config.all_channels()).items():
            record = data.get("session")
            if not record:
                continue
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
                continue
            self.sessions[channel_id] = view
            self._register_view(view)
        if self.sessions:
            log.info("Restored %s hibernated Libretro session(s).", len(self.sessions))

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
        await self.config.channel_from_id(view.channel_id).session.set(view.to_record())

    # -- Files --------------------------------------------------------------

    def _data_dir(self, name: str) -> Path:
        path = cog_data_path(self) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _roms_dir(self) -> Path:
        return self._data_dir("roms")

    def _rom_path(self, rom_filename: str) -> typing.Optional[Path]:
        if not rom_filename:
            return None
        return self._roms_dir() / rom_filename

    def _state_path(self, channel_id: int, slug: str) -> Path:
        return self._data_dir("states") / f"{channel_id}-{slug}.state"

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
        Save a session and take its message out of service for good.

        Unlike hibernating, the controls are greyed out: this message is no
        longer the channel's game, and letting it wake its emulator back up
        would run two cores at once.
        """
        async with self.emulator_lock:
            await self._hibernate_locked(view, None)
        view.retire()
        await view.refresh(reason)

    async def _hibernate_locked(
        self, view: RetroView, reason: typing.Optional[str] = None
    ) -> None:
        emulator, view.emulator = view.emulator, None
        if emulator is not None:
            # Always write the save state *before* the core is freed: after
            # emulator.stop() the machine state is gone for good.
            await self._write_state(view, emulator)
            await asyncio.to_thread(emulator.stop)
        # The Stop button goes away with the emulator; everything else stays
        # enabled so the next press can wake the session back up.
        view._sync_children()
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

        emulator = RetroEmulator(core_path, rom_path)

        def _resume() -> bool:
            emulator.start()
            if state:
                try:
                    emulator.load_state(state)
                    return True
                except EmulatorError as error:
                    # A state from another core or a truncated file should
                    # cost the player their progress, not the whole session.
                    log.warning(
                        "Discarding an unusable Libretro save state for %s: %s",
                        view.slug,
                        error,
                    )
            emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
            return False

        try:
            loaded = await asyncio.to_thread(_resume)
        except Exception:
            await asyncio.to_thread(emulator.stop)
            raise
        if state and not loaded:
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
        view.emulator = emulator
        view._sync_children()

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
        """Save the session's progress to disk. Never raises."""
        emulator = emulator if emulator is not None else view.emulator
        if emulator is None or not emulator.started:
            return False
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

        async with self.config.cores() as cores:
            cores[core] = str(core_path)
        return True, len(data), "installed"

    async def _fetch_rom(
        self, ctx: commands.Context, url: typing.Optional[str]
    ) -> typing.Optional[typing.Tuple[str, bytes]]:
        """
        Return (filename, data) from the URL or attachment, or None on error.

        A URL wins over an attachment, because it is the one the caller named
        explicitly (a preset resolves to a URL before we get here).
        """
        too_big = f"That file is bigger than the {MAX_ROM_SIZE_LABEL} limit for ROMs."
        if url:
            if not url.lower().startswith(("http://", "https://")):
                await ctx.send("The ROM URL must start with `http://` or `https://`.")
                return None
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            await ctx.send(f"Downloading the ROM failed with status {resp.status}.")
                            return None
                        if (resp.content_length or 0) > MAX_ROM_SIZE:
                            await ctx.send(too_big)
                            return None
                        # read(n) only returns the next chunk, so loop until
                        # the body ends or the size cap is exceeded.
                        chunks = []
                        total = 0
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > MAX_ROM_SIZE:
                                break
                        data = b"".join(chunks)
            except aiohttp.ClientError as error:
                await ctx.send(f"Downloading the ROM failed: {error}")
                return None
            if len(data) > MAX_ROM_SIZE:
                await ctx.send(too_big)
                return None
            # Redirects (e.g. GitHub release assets) often end at a URL whose
            # path has no real filename, so prefer the Content-Disposition
            # header, then the URL the user actually gave us.
            filename = ""
            if resp.content_disposition is not None:
                filename = resp.content_disposition.filename or ""
            if not filename:
                filename = Path(urlparse(url).path).name
            if not filename:
                filename = Path(str(resp.url.path)).name or "rom.gb"
            return filename, data

        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > MAX_ROM_SIZE:
                await ctx.send(too_big)
                return None
            return attachment.filename, await attachment.read()

        return None

    async def _no_rom_help(self, ctx: commands.Context) -> None:
        presets = await self.config.games()
        lines = [
            "Attach a console ROM to your message, or pass a URL: "
            f"`{ctx.clean_prefix}retro <url>`.",
        ]
        if presets:
            names = ", ".join(f"`{name}`" for name in sorted(presets)[:15])
            lines.append(f"You can also start a saved game by name: {names}")
        else:
            lines.append(
                "The bot owner can save games by name with "
                f"`{ctx.clean_prefix}retroset game add <name> <url>`."
            )
        installed = await self._installed_cores()
        playable = [system for system in SYSTEMS if system.core in installed]
        if playable:
            lines.append("")
            lines.append("Consoles this bot can play right now:")
            lines.extend(self._supported_lines(playable))
        lines.append("")
        lines.append(
            "Only use ROMs you have the rights to, such as homebrew games."
        )
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    async def _resume_session(self, ctx: commands.Context, view: RetroView) -> None:
        """Point the channel at its existing session instead of starting over."""
        message = await view.resolve_message()
        if message is not None:
            status = "is already running" if view.live else "is asleep"
            await view.refresh()
            await ctx.send(
                f"**{view.game_name}** {status} in this channel. Use the "
                f"controls to play: {message.jump_url}"
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
                await ctx.send(f"The game could not be resumed: {error}")
                return
        view.last_clip = clip
        view.touch()
        view._sync_children()
        view.message = await ctx.send(
            embed=await view._make_embed(),
            file=view._clip_file(clip),
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        view.message_id = view.message.id
        await self._save_record(view)

    # -- Commands -----------------------------------------------------------

    @commands.max_concurrency(1, commands.BucketType.channel)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    @commands.command()
    async def retro(self, ctx: commands.Context, *, game: typing.Optional[str] = None) -> None:
        """
        Play a retro console game in this channel.

        Pass the name of a saved game, a URL to a ROM, or attach one to the
        message. The console is picked from the file extension, so a `.gb`
        starts a Game Boy and a `.sfc` starts a Super Nintendo. Run it with no
        arguments to bring back the game already going in this channel.

        Games keep their progress: a session goes to sleep when nobody plays,
        and the next button press picks it back up, even after the bot
        restarts. Anyone in the channel can press the buttons. Only use ROMs
        you have the rights to, such as homebrew games.

        **Examples:**
        - `[p]retro` (with a ROM attached, or to resume this channel's game)
        - `[p]retro tobu`
        - `[p]retro https://example.com/homebrew.gb`

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
                f"Replaced by **{game_name}**. This game was saved; start it "
                "again by name to carry on.",
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
        rom_path = self._roms_dir() / rom_filename
        try:
            await asyncio.to_thread(self._write_atomic, rom_path, data)
        except OSError as error:
            await ctx.send(f"The ROM could not be saved: {error}")
            return
        self._prune_cached_games(ctx.channel.id, slug)

        core_path = await self._core_path(system.core)
        if core_path is None:
            await ctx.send(
                self._missing_core_message(ctx.clean_prefix, system, system.core)
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

        emulator = RetroEmulator(core_path, rom_path)
        try:
            async with ctx.typing():
                async with self.emulator_lock:
                    # Only one core at a time; park whatever else is playing
                    # (saving it first) and say so, so the other channel's
                    # players are not left wondering what happened.
                    evicted = await self._evict_locked(exclude=view)
                    notice = self._eviction_notice(ctx, evicted)
                    if notice:
                        await ctx.send(notice)
                    await view.start(ctx, emulator)
        except EmulatorError as error:
            self.sessions.pop(ctx.channel.id, None)
            await asyncio.to_thread(emulator.stop)
            await ctx.send(f"The game could not be started: {error}")
            return
        except Exception:
            self.sessions.pop(ctx.channel.id, None)
            await asyncio.to_thread(emulator.stop)
            raise
        await self._save_record(view)

    @commands.guild_only()
    @commands.command()
    async def retrostop(self, ctx: commands.Context) -> None:
        """
        Put this channel's game to sleep.

        The game is saved and the emulator is freed, but the controls stay
        live: pressing any button picks the game up again where it left off.

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

    @retroset.command(name="core")
    async def retroset_core(self, ctx: commands.Context, *, path: str) -> None:
        """
        Point the cog at a libretro core that is already on this machine.

        The console is worked out from the filename, so the file has to keep
        its buildbot name (`snes9x_libretro.so`, `gambatte_libretro.dll`, and
        so on). Most people should use `[p]retroset download` instead.

        **Examples:**
        - `[p]retroset core /opt/retroarch/cores/snes9x_libretro.so`

        **Arguments:**
        - `<path>` - The full path to the core file.
        """
        core_path = Path(path.strip().strip('"'))
        if not core_path.is_file():
            await ctx.send(f"No file found at `{core_path}`.")
            return
        core = core_name_from_filename(core_path.name)
        if core is None:
            known = humanize_list([f"`{name}`" for name in sorted(CORES)])
            await ctx.send(
                f"`{core_path.name}` is not a core this cog knows how to use. "
                f"The supported cores are: {known}."
            )
            return
        async with self.config.cores() as cores:
            cores[core] = str(core_path)
        await ctx.send(
            f"The `{core}` core ({CORES[core]}) is now set to `{core_path}`. "
            f"It plays {humanize_list([f'`{e}`' for e in extensions_for_core(core)])}."
        )

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

        The whole set is about 5 MiB. None of these cores need a BIOS file.

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
                "Download the cores manually and point the cog at each one "
                f"with `{ctx.clean_prefix}retroset core <path>`."
            )
            return

        status = await ctx.send(
            f"Downloading {len(wanted)} core(s) from the libretro buildbot..."
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
        lines.append(f"Play something with `{ctx.clean_prefix}retro`.")
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

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
        clips recorded afterwards. The default is 5 seconds.

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
        misses the press entirely; too long and one tap walks through two
        menu entries. Directions are held twice as long as this, because
        movement needs sustained input to actually go anywhere.

        The value is clamped between 50 and 2000 milliseconds. The default is
        200.

        **Examples:**
        - `[p]retroset hold 300`

        **Arguments:**
        - `<milliseconds>` - How long a button stays down (50-2000).
        """
        milliseconds = max(MIN_HOLD_MS, min(MAX_HOLD_MS, milliseconds))
        await self.config.hold_ms.set(milliseconds)
        for view in self.sessions.values():
            view.hold_ms = milliseconds
        await ctx.send(
            f"Buttons are now held for {milliseconds}ms, and directions for "
            f"{milliseconds * 2}ms."
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
        matches one of the supported consoles. Only add ROMs you have the
        rights to share, such as homebrew games.

        **Examples:**
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

    @retroset.command(name="settings")
    @commands.bot_has_permissions(embed_links=True)
    async def retroset_settings(self, ctx: commands.Context) -> None:
        """
        Show the current Retro settings.

        **Examples:**
        - `[p]retroset settings`
        """
        installed = await self._installed_cores()
        configured = await self.config.cores()
        embed = discord.Embed(
            title="Retro Settings",
            colour=await ctx.embed_colour(),
        )

        if not configured:
            cores_value = (
                "None installed. Run "
                f"`{ctx.clean_prefix}retroset download` to get them."
            )
        else:
            entries = []
            for name in sorted(CORES):
                if name in installed:
                    entries.append(f"\N{WHITE HEAVY CHECK MARK} `{name}` - {CORES[name]}")
                elif name in configured:
                    entries.append(f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} `{name}` - file missing")
            missing = len(CORES) - len(installed)
            if missing > 0:
                entries.append(
                    f"{missing} more available from "
                    f"`{ctx.clean_prefix}retroset download`."
                )
            cores_value = "\n".join(entries)[:1024]
        embed.add_field(
            name=f"Cores ({len(installed)}/{len(CORES)} installed)",
            value=cores_value,
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
            value=f"{clip_seconds} seconds of play per button press",
            inline=False,
        )
        hold_ms = await self.config.hold_ms()
        embed.add_field(
            name="Button hold",
            value=f"{hold_ms}ms per press, {hold_ms * 2}ms for directions",
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
        embed.add_field(
            name="Sessions",
            value=(
                f"{len(self.sessions)} total, {awake} awake "
                f"(at most {MAX_LIVE_EMULATORS} can be awake at once)"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
