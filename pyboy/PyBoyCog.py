import asyncio
import io
import logging
import platform
import re
import sys
import typing
import zipfile
from pathlib import Path

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from .emulator import MIN_ROM_SIZE, EmulatorError, GameBoyEmulator
from .PyBoyView import DEFAULT_TIMEOUT_MINUTES, PyBoyView

log = logging.getLogger("red.robloach.pyboy")

MAX_ROM_SIZE = 8 * 1024 * 1024  # 8 MiB, larger than any Game Boy ROM
ROM_EXTENSIONS = (".gb", ".gbc")
BUILDBOT = "https://buildbot.libretro.com/nightly"


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
            session_timeout_minutes=DEFAULT_TIMEOUT_MINUTES
        )
        self.sessions: typing.Dict[int, PyBoyView] = {}

    async def cog_unload(self) -> None:
        for view in list(self.sessions.values()):
            try:
                await view.close("The PyBoy cog was unloaded.")
            except Exception:
                log.exception("Failed to close a PyBoy session on unload.")
        self.sessions.clear()

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
        """Return (filename, data) from the attachment or URL, or None on error."""
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > MAX_ROM_SIZE:
                await ctx.send("That file is too big to be a Game Boy ROM.")
                return None
            return attachment.filename, await attachment.read()

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
                        data = await resp.content.read(MAX_ROM_SIZE + 1)
            except aiohttp.ClientError as error:
                await ctx.send(f"Downloading the ROM failed: {error}")
                return None
            if len(data) > MAX_ROM_SIZE:
                await ctx.send("That file is too big to be a Game Boy ROM.")
                return None
            filename = Path(str(resp.url.path)).name or "rom.gb"
            return filename, data

        await ctx.send(
            "Attach a Game Boy ROM (`.gb` or `.gbc`) to your message, or pass "
            f"a URL: `{ctx.clean_prefix}pyboy <url>`. Only use ROMs you have "
            "the rights to, such as homebrew games."
        )
        return None

    # -- Commands -----------------------------------------------------------

    @commands.max_concurrency(1, commands.BucketType.channel)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    @commands.command()
    async def pyboy(self, ctx: commands.Context, url: typing.Optional[str] = None) -> None:
        """
        Play a Game Boy game in this channel.

        Attach a `.gb` or `.gbc` ROM to the message, or pass a URL to one.
        Anyone in the channel can press the buttons. Only use ROMs you have
        the rights to, such as homebrew games.

        **Examples:**
        - `[p]pyboy` (with a ROM attached)
        - `[p]pyboy https://example.com/homebrew.gb`
        """
        core_path = await self.config.core_path()
        if not core_path or not Path(core_path).is_file():
            await ctx.send(
                "No Game Boy core is configured. Ask the bot owner to run "
                f"`{ctx.clean_prefix}pyboyset download` first."
            )
            return

        if ctx.channel.id in self.sessions:
            await ctx.send(
                "A game is already running in this channel. Stop it first "
                f"with its Stop button or `{ctx.clean_prefix}pyboystop`."
            )
            return

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

        rom_dir = cog_data_path(self) / "roms"
        rom_dir.mkdir(parents=True, exist_ok=True)
        rom_path = rom_dir / f"{ctx.channel.id}-{filename}"
        rom_path.write_bytes(data)

        emulator = GameBoyEmulator(core_path, rom_path)
        timeout_minutes = await self.config.session_timeout_minutes()
        view = PyBoyView(self, emulator, Path(filename).stem, timeout_minutes=timeout_minutes)
        self.sessions[ctx.channel.id] = view
        try:
            await view.start(ctx)
        except EmulatorError as error:
            self.sessions.pop(ctx.channel.id, None)
            await asyncio.to_thread(emulator.stop)
            await ctx.send(f"The game could not be started: {error}")
        except Exception:
            self.sessions.pop(ctx.channel.id, None)
            await asyncio.to_thread(emulator.stop)
            raise
        finally:
            try:
                rom_path.unlink(missing_ok=True)
            except OSError:
                pass

    @commands.guild_only()
    @commands.command()
    async def pyboystop(self, ctx: commands.Context) -> None:
        """
        Stop the Game Boy session running in this channel.

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
                await view.close(f"Stopped by {ctx.author.display_name}.")
        except Exception:
            # Even if the view or its message is stale, make sure the
            # emulator is freed and the session entry is removed.
            log.exception("Failed to close the PyBoy session cleanly.")
            await asyncio.to_thread(view.emulator.stop)
        finally:
            self.sessions.pop(ctx.channel.id, None)
        await ctx.send("The Game Boy session has been stopped.")

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
        Set how long a Game Boy session can idle before it ends.

        The value is clamped between 1 and 120 minutes and applies to
        sessions started afterwards. The default is 10 minutes.

        **Examples:**
        - `[p]pyboyset timeout 30`

        **Arguments:**
        - `<minutes>` - Minutes without input before the session ends (1-120).
        """
        minutes = max(1, min(120, minutes))
        await self.config.session_timeout_minutes.set(minutes)
        await ctx.send(f"Game Boy sessions now end after {minutes} minutes without input.")

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
        embed.add_field(name="Session timeout", value=f"{timeout_minutes} minutes", inline=False)
        embed.add_field(name="Active sessions", value=str(len(self.sessions)), inline=False)
        await ctx.send(embed=embed)
