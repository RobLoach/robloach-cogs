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
from redbot.core.utils.chat_formatting import pagify

from .emulator import MIN_ROM_SIZE, EmulatorError, GameBoyEmulator
from .PyBoyView import (
    BOOT_FRAMES,
    DEFAULT_TIMEOUT_MINUTES,
    SAVE_STATE_EVERY_PRESSES,
    PyBoyView,
)

log = logging.getLogger("red.robloach.pyboy")

MAX_ROM_SIZE = 8 * 1024 * 1024  # 8 MiB, larger than any Game Boy ROM
ROM_EXTENSIONS = (".gb", ".gbc")
BUILDBOT = "https://buildbot.libretro.com/nightly"

# A libretro core is a shared object with process-global state, so two live
# Gambatte instances quietly corrupt each other's emulation. Waking a
# hibernated session costs about 25ms against a ~1.8s clip, so keeping a
# single core loaded and evicting the least recently used one is effectively
# free and removes the hazard entirely.
MAX_LIVE_EMULATORS = 1

# How often the background task looks for sessions to put to sleep.
IDLE_CHECK_SECONDS = 60

# Cached ROMs (and their save states) are keyed by game, so a channel can
# switch between games and keep each one's progress. This caps how many of
# them a single channel keeps on disk.
MAX_CACHED_GAMES_PER_CHANNEL = 5


class PyBoyCog(commands.Cog):
    """
    Play Game Boy games together in Discord, emulated with libretro.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config: Config = Config.get_conf(
            self,
            identifier=114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121,
            force_registration=True
        )
        self.config.register_global(
            core_path="",
            session_timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
            games={}
        )
        # A session outlives its emulator, so the record of one lives here
        # and is reloaded when the cog (or the whole bot) starts again.
        self.config.register_channel(
            session=None
        )
        self.sessions: typing.Dict[int, PyBoyView] = {}
        # Serializes every core operation across all channels.
        self.emulator_lock: asyncio.Lock = asyncio.Lock()
        self._idle_task: typing.Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        try:
            await self._restore_sessions()
        except Exception:
            log.exception("Failed to restore PyBoy sessions.")
        self._idle_task = asyncio.create_task(self._hibernation_loop())

    async def cog_unload(self) -> None:
        task, self._idle_task = self._idle_task, None
        if task is not None:
            task.cancel()
        for view in list(self.sessions.values()):
            try:
                await self.hibernate(
                    view,
                    "The Game Boy was put away. Press a button to pick up "
                    "where you left off.",
                )
            except Exception:
                log.exception("Failed to hibernate a PyBoy session on unload.")
            # The buttons stay enabled on the message so the game can be
            # resumed after a reload, but this now-orphaned view object must
            # not answer them; cog_load builds fresh ones.
            view.closed = True
        self.sessions.clear()

    # -- Session storage ----------------------------------------------------

    async def _restore_sessions(self) -> None:
        """Rebuild hibernated sessions from Config and re-arm their buttons."""
        timeout_minutes = await self.config.session_timeout_minutes()
        for channel_id, data in (await self.config.all_channels()).items():
            record = data.get("session")
            if not record:
                continue
            record.setdefault("channel_id", channel_id)
            try:
                view = PyBoyView.from_record(self, record, timeout_minutes)
            except Exception:
                log.exception(
                    "Ignoring an unreadable PyBoy session record for channel %s",
                    channel_id,
                )
                continue
            self.sessions[channel_id] = view
            self._register_view(view)
        if self.sessions:
            log.info("Restored %s hibernated PyBoy session(s).", len(self.sessions))

    def _register_view(self, view: PyBoyView) -> None:
        """
        Teach the bot to route this message's button clicks to this view.

        Persistent views need a fixed custom_id on every child and no
        timeout, both of which PyBoyView guarantees.
        """
        if view.message_id is None:
            return
        try:
            self.bot.add_view(view, message_id=view.message_id)
        except Exception:
            log.exception(
                "Could not register the PyBoy controls for message %s",
                view.message_id,
            )

    async def _save_record(self, view: PyBoyView) -> None:
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

    async def run_press(self, view: PyBoyView, button: typing.Optional[str]) -> bytes:
        """
        Emulate one press for a session, waking it up first if it was asleep.

        Raises EmulatorError if the session cannot be woken or the core
        fails. Called by the view from its button callbacks.
        """
        async with self.emulator_lock:
            await self._wake_locked(view)
            gif = await asyncio.to_thread(view.run_press, button)
            view.touch()
            view.press_count += 1
            if view.press_count % SAVE_STATE_EVERY_PRESSES == 0:
                await self._write_state(view)
                await self._save_record(view)
            return gif

    async def hibernate(self, view: PyBoyView, reason: typing.Optional[str] = None) -> None:
        """Save the game, free the emulator, and keep the controls usable."""
        async with self.emulator_lock:
            await self._hibernate_locked(view, reason)

    async def _retire(self, view: PyBoyView, reason: str) -> None:
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
        self, view: PyBoyView, reason: typing.Optional[str] = None
    ) -> None:
        emulator, view.emulator = view.emulator, None
        if emulator is not None:
            await self._write_state(view, emulator)
            await asyncio.to_thread(emulator.stop)
        # The Stop button goes away with the emulator; everything else stays
        # enabled so the next press can wake the session back up.
        view._sync_children()
        await self._save_record(view)
        if reason is not None:
            await view.refresh(reason)

    async def _wake_locked(self, view: PyBoyView) -> None:
        """Load the core and the last save state for a hibernated session."""
        if view.live:
            return
        core_path = await self.config.core_path()
        if not core_path or not Path(core_path).is_file():
            raise EmulatorError("No Game Boy core is configured.")
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
                log.warning("Could not read the PyBoy save state %s", state_path)

        emulator = GameBoyEmulator(core_path, rom_path)

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
                        "Discarding an unusable PyBoy save state for %s: %s",
                        view.slug,
                        error,
                    )
            emulator.advance(BOOT_FRAMES)
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

    async def _evict_locked(self, exclude: typing.Optional[PyBoyView] = None) -> None:
        """Put other sessions to sleep so only MAX_LIVE_EMULATORS stay loaded."""
        live = [v for v in self.sessions.values() if v.live and v is not exclude]
        live.sort(key=lambda v: v.last_active)
        while len(live) >= MAX_LIVE_EMULATORS:
            await self._hibernate_locked(
                live.pop(0),
                "Another channel started playing, so this game went to "
                "sleep. Press a button to continue.",
            )

    async def _write_state(
        self, view: PyBoyView, emulator: typing.Optional[GameBoyEmulator] = None
    ) -> bool:
        """Save the session's progress to disk. Never raises."""
        emulator = emulator if emulator is not None else view.emulator
        if emulator is None or not emulator.started:
            return False
        try:
            data = await asyncio.to_thread(emulator.save_state)
        except EmulatorError as error:
            log.warning("Could not save the PyBoy state for %s: %s", view.slug, error)
            return False
        path = self._state_path(view.channel_id, view.slug)
        try:
            await asyncio.to_thread(self._write_atomic, path, data)
        except OSError:
            log.warning("Could not write the PyBoy state %s", path, exc_info=True)
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
                log.exception("The PyBoy hibernation task hit an error.")

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
    def _buildbot_core() -> typing.Optional[typing.Tuple[str, str]]:
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
            core = "gambatte_libretro.so"
            return f"{BUILDBOT}/linux/{arch}/latest/{core}.zip", core
        if sys.platform == "darwin":
            arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
            core = "gambatte_libretro.dylib"
            return f"{BUILDBOT}/apple/osx/{arch}/latest/{core}.zip", core
        if sys.platform in ("win32", "cygwin"):
            arch = "x86_64" if machine in ("amd64", "x86_64") else "x86"
            core = "gambatte_libretro.dll"
            return f"{BUILDBOT}/windows/{arch}/latest/{core}.zip", core
        return None

    async def _fetch_rom(
        self, ctx: commands.Context, url: typing.Optional[str]
    ) -> typing.Optional[typing.Tuple[str, bytes]]:
        """
        Return (filename, data) from the URL or attachment, or None on error.

        A URL wins over an attachment, because it is the one the caller named
        explicitly (a preset resolves to a URL before we get here).
        """
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
                            await ctx.send("That file is too big to be a Game Boy ROM.")
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
                await ctx.send("That file is too big to be a Game Boy ROM.")
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
                await ctx.send("That file is too big to be a Game Boy ROM.")
                return None
            return attachment.filename, await attachment.read()

        return None

    async def _no_rom_help(self, ctx: commands.Context) -> None:
        presets = await self.config.games()
        lines = [
            "Attach a Game Boy ROM (`.gb` or `.gbc`) to your message, or pass "
            f"a URL: `{ctx.clean_prefix}pyboy <url>`.",
        ]
        if presets:
            names = ", ".join(f"`{name}`" for name in sorted(presets)[:15])
            lines.append(f"You can also start a saved game by name: {names}")
        else:
            lines.append(
                "The bot owner can save games by name with "
                f"`{ctx.clean_prefix}pyboyset game add <name> <url>`."
            )
        lines.append(
            "Only use ROMs you have the rights to, such as homebrew games."
        )
        await ctx.send("\n".join(lines))

    async def _resume_session(self, ctx: commands.Context, view: PyBoyView) -> None:
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

    async def _repost_session(self, ctx: commands.Context, view: PyBoyView) -> None:
        """Wake a session up and post a new message for it."""
        async with ctx.typing():
            try:
                gif = await self.run_press(view, None)
            except EmulatorError as error:
                await ctx.send(f"The game could not be resumed: {error}")
                return
        view.last_gif = gif
        view.touch()
        view._sync_children()
        view.message = await ctx.send(
            embed=await view._make_embed(),
            file=discord.File(io.BytesIO(gif), filename=view.screen_filename),
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
    async def pyboy(self, ctx: commands.Context, *, game: typing.Optional[str] = None) -> None:
        """
        Play a Game Boy game in this channel.

        Pass the name of a saved game, a URL to a `.gb`/`.gbc` ROM, or attach
        one to the message. Run it with no arguments to bring back the game
        already going in this channel. Games keep their progress: a session
        goes to sleep when nobody plays, and the next button press picks it
        back up, even after the bot restarts.

        Anyone in the channel can press the buttons. Only use ROMs you have
        the rights to, such as homebrew games.

        **Examples:**
        - `[p]pyboy` (with a ROM attached, or to resume this channel's game)
        - `[p]pyboy tobu`
        - `[p]pyboy https://example.com/homebrew.gb`

        **Arguments:**
        - `[game]` - A saved game name (see `[p]pyboyset game list`) or a ROM URL.
        """
        core_path = await self.config.core_path()
        if not core_path or not Path(core_path).is_file():
            await ctx.send(
                "No Game Boy core is configured. Ask the bot owner to run "
                f"`{ctx.clean_prefix}pyboyset download` first."
            )
            return

        game = game.strip() if game else None
        existing = self.sessions.get(ctx.channel.id)

        # Bare `[p]pyboy` with a session in the channel means "bring it back".
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
                    f"attach a ROM, or see `{ctx.clean_prefix}pyboyset game list`."
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
        if not filename.lower().endswith(ROM_EXTENSIONS):
            await ctx.send("That doesn't look like a Game Boy ROM (`.gb` or `.gbc`).")
            return

        # Catch obviously-broken content before handing it to the core. The
        # most common failure is a URL that serves an HTML page (for example
        # a GitHub "blob" page) instead of the ROM file itself.
        if data.lstrip()[:1] == b"<":
            await ctx.send(
                "That looks like a web page, not a Game Boy ROM. If you used "
                "a URL, make sure it is a direct download link to the file."
            )
            return
        if len(data) < MIN_ROM_SIZE:
            await ctx.send("That file is too small to be a Game Boy ROM.")
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

        await self._start_session(ctx, game_name, slug, filename, data, source)

    async def _start_session(
        self,
        ctx: commands.Context,
        game_name: str,
        slug: str,
        filename: str,
        data: bytes,
        source: str,
    ) -> None:
        """Cache the ROM, boot it, and post the controls."""
        extension = Path(filename).suffix.lower() or ".gb"
        rom_filename = f"{ctx.channel.id}-{slug}{extension}"
        rom_path = self._roms_dir() / rom_filename
        try:
            await asyncio.to_thread(self._write_atomic, rom_path, data)
        except OSError as error:
            await ctx.send(f"The ROM could not be saved: {error}")
            return
        self._prune_cached_games(ctx.channel.id, slug)

        timeout_minutes = await self.config.session_timeout_minutes()
        view = PyBoyView(
            self,
            game_name=game_name,
            slug=slug,
            rom_filename=rom_filename,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            starter_id=ctx.author.id,
            source=source,
            timeout_minutes=timeout_minutes,
        )
        self.sessions[ctx.channel.id] = view

        core_path = await self.config.core_path()
        emulator = GameBoyEmulator(core_path, rom_path)
        try:
            async with ctx.typing():
                async with self.emulator_lock:
                    # Only one core at a time; park whatever else is playing.
                    await self._evict_locked(exclude=view)
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
    async def pyboystop(self, ctx: commands.Context) -> None:
        """
        Put this channel's Game Boy session to sleep.

        The game is saved and the emulator is freed, but the controls stay
        live: pressing any button picks the game up again where it left off.

        Only the person who started the game, members with the Manage
        Messages permission, and the bot owner can stop it.

        **Examples:**
        - `[p]pyboystop`
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
            # Even if the view or its message is stale, make sure the
            # emulator is freed.
            log.exception("Failed to stop the PyBoy session cleanly.")
            if view.emulator is not None:
                await asyncio.to_thread(view.emulator.stop)
                view.emulator = None
        await ctx.send(
            "The game has been saved and put to sleep. Press any button on "
            "it to carry on."
        )

    @commands.group()
    @commands.is_owner()
    async def pyboyset(self, ctx: commands.Context):
        """
        Configure PyBoy cog settings.
        """

    @pyboyset.command(name="core")
    async def pyboyset_core(self, ctx: commands.Context, *, path: str) -> None:
        """
        Set the path to a Game Boy libretro core (e.g. gambatte_libretro.so).
        """
        core_path = Path(path.strip().strip('"'))
        if not core_path.is_file():
            await ctx.send(f"No file found at `{core_path}`.")
            return
        await self.config.core_path.set(str(core_path))
        await ctx.send(f"Game Boy core set to: `{core_path}`")

    @pyboyset.command(name="download")
    async def pyboyset_download(self, ctx: commands.Context) -> None:
        """
        Download the Gambatte Game Boy core from the libretro buildbot.
        """
        target = self._buildbot_core()
        if target is None:
            await ctx.send(
                "There is no prebuilt core for this platform. Download a "
                "Gambatte core manually and set it with "
                f"`{ctx.clean_prefix}pyboyset core <path>`."
            )
            return
        url, core_name = target

        async with ctx.typing():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            await ctx.send(
                                f"The libretro buildbot returned status {resp.status} for <{url}>."
                            )
                            return
                        payload = await resp.read()
            except aiohttp.ClientError as error:
                await ctx.send(f"Downloading the core failed: {error}")
                return

            core_dir = cog_data_path(self) / "cores"
            core_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    member = next(
                        (name for name in archive.namelist() if name.endswith(core_name)),
                        None,
                    )
                    if member is None:
                        await ctx.send("The downloaded archive did not contain the core.")
                        return
                    core_path = core_dir / core_name
                    core_path.write_bytes(archive.read(member))
            except zipfile.BadZipFile:
                await ctx.send("The libretro buildbot did not return a valid zip file.")
                return

        await self.config.core_path.set(str(core_path))
        await ctx.send(f"Downloaded the Gambatte core to: `{core_path}`")

    @pyboyset.command(name="timeout")
    async def pyboyset_timeout(self, ctx: commands.Context, minutes: int) -> None:
        """
        Set how long a game can idle before it goes to sleep.

        A sleeping game is saved and its emulator is freed, but its controls
        keep working: the next button press wakes it up where it left off.
        The value is clamped between 1 and 120 minutes and applies to
        sessions started afterwards. The default is 10 minutes.

        **Examples:**
        - `[p]pyboyset timeout 30`

        **Arguments:**
        - `<minutes>` - Minutes without input before the game sleeps (1-120).
        """
        minutes = max(1, min(120, minutes))
        await self.config.session_timeout_minutes.set(minutes)
        await ctx.send(
            f"Game Boy games now go to sleep after {minutes} minutes without "
            "input. Pressing a button wakes them up again."
        )

    @pyboyset.group(name="game")
    async def pyboyset_game(self, ctx: commands.Context) -> None:
        """
        Manage the games players can start by name.
        """

    @pyboyset_game.command(name="add")
    async def pyboyset_game_add(self, ctx: commands.Context, name: str, url: str) -> None:
        """
        Save a game so anyone can start it with `[p]pyboy <name>`.

        The URL must be a direct download link to a `.gb` or `.gbc` file.
        Only add ROMs you have the rights to share, such as homebrew games.

        **Examples:**
        - `[p]pyboyset game add tobu https://example.com/tobu.gb`

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
            f"Game `{key}` {verb}. Start it with `{ctx.clean_prefix}pyboy {key}`."
        )

    @pyboyset_game.command(name="remove", aliases=["delete", "del"])
    async def pyboyset_game_remove(self, ctx: commands.Context, name: str) -> None:
        """
        Forget a saved game.

        **Examples:**
        - `[p]pyboyset game remove tobu`

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

    @pyboyset_game.command(name="list")
    async def pyboyset_game_list(self, ctx: commands.Context) -> None:
        """
        List the games players can start by name.

        **Examples:**
        - `[p]pyboyset game list`
        """
        games = await self.config.games()
        if not games:
            await ctx.send(
                "No games are saved yet. Add one with "
                f"`{ctx.clean_prefix}pyboyset game add <name> <url>`."
            )
            return
        lines = "\n".join(f"- `{name}`: <{games[name]}>" for name in sorted(games))
        for page in pagify(lines):
            await ctx.send(page)

    @pyboyset.command(name="settings")
    @commands.bot_has_permissions(embed_links=True)
    async def pyboyset_settings(self, ctx: commands.Context) -> None:
        """
        Show the current PyBoy settings.
        """
        core_path = await self.config.core_path()
        core_status = "Not configured"
        if core_path:
            exists = Path(core_path).is_file()
            core_status = f"`{core_path}` ({'found' if exists else 'missing'})"
        embed = discord.Embed(
            title="PyBoy Settings",
            colour=await ctx.embed_colour(),
        )
        embed.add_field(name="Game Boy core", value=core_status, inline=False)
        timeout_minutes = await self.config.session_timeout_minutes()
        embed.add_field(
            name="Sleep after",
            value=(
                f"{timeout_minutes} minutes without input. Sleeping games are "
                "saved and wake up on the next button press."
            ),
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
            value=f"{len(self.sessions)} total, {awake} awake",
            inline=False,
        )
        await ctx.send(embed=embed)
