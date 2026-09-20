"""Stand-ins for Discord, Red's Config, and the emulator.

The cog tests drive the *real* RetroCog and the *real* RetroView (real
discord.py View machinery, real component payloads) against these. Only the
three things a unit test cannot have are faked:

* Discord itself -- channels, messages and interactions, which record what
  the cog said and did rather than sending it anywhere;
* Red's Config, which is an in-memory dict with the same await/async-with
  shape;
* the emulator, because a libretro core is a process-global shared object
  that takes seconds to boot. :class:`FakeEmulator` keeps the same contract
  as :class:`retro.emulator.RetroEmulator`; the real one is covered against
  real cores in ``test_emulator.py``.

Importing this module needs discord.py, and ``redbot`` (real or stubbed).
"""

import io
import sys
import types
import zipfile
from pathlib import Path

import discord

from retro.emulator import EmulatorError

# A stand-in ROM: big enough to pass the cog's MIN_ROM_SIZE, not a zip, and
# not an HTML error page. The cog never looks at a ROM's contents, and the
# fake emulator never runs it, so no real game is needed for these tests.
ROM_BYTES = bytes(range(256)) * 512          # 128 KiB, like a GBC cartridge
NES_BYTES = b"NES\x1a" + bytes(range(256)) * 96  # 24 KiB, with an iNES header


class FakeEmulator:
    """A stand-in with the same contract as RetroEmulator."""

    #: Every instance built during a test, newest last.
    instances = []
    #: Give this fake a battery save of that many bytes; 0 means "a cartridge
    #: with no battery", which is the common case.
    sram_bytes = 0
    #: What option definitions a started core reports, keyed by core name.
    definitions_by_core = {}

    def __init__(self, core_path, rom_path, system_dir=None, options=None):
        self.core_path = Path(core_path)
        self.rom_path = Path(rom_path)
        self.system_dir = Path(system_dir) if system_dir is not None else None
        self.options = dict(options or {})
        self.runtime_options = {}
        self.started = False
        self.frame = 0
        self.loaded_from = None
        self.last_presses = None
        self.last_format = None
        self.sram = None
        self.loaded_sram = None
        FakeEmulator.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.sram_bytes = 0
        cls.definitions_by_core = {}

    # -- lifecycle
    def start(self):
        if not self.rom_path.is_file():
            raise EmulatorError(f"ROM not found: {self.rom_path}")
        self.started = True
        if FakeEmulator.sram_bytes:
            # A freshly booted cartridge's battery memory, all 0xFF like an
            # erased chip, allocated only once the game is loaded.
            self.sram = bytearray(b"\xff" * FakeEmulator.sram_bytes)

    def stop(self):
        self.started = False

    close = stop

    def _require(self):
        if not self.started:
            raise EmulatorError("The emulator is not running.")

    # -- timing
    @property
    def fps(self):
        # Gambatte's real rate, so the frame arithmetic under test is the
        # arithmetic that runs in production -- and never a tidy 60.
        return 59.7275

    def frames_for_seconds(self, seconds):
        return max(1, round(self.fps * seconds))

    def frames_for_ms(self, ms):
        return max(1, round(self.fps * ms / 1000.0))

    # -- emulation
    def advance(self, frames=1):
        self._require()
        self.frame += max(0, frames)

    def press(self, button, hold_frames=12, release_frames=40):
        self._require()
        self.frame += hold_frames + release_frames

    def record(self, frames=None, *, scale=2, fps=15, presses=None, clip_format="WEBP"):
        self._require()
        self.last_presses = list(presses or ())
        self.last_format = clip_format
        self.frame += frames or 300
        return b"RIFF\0\0\0\0WEBPVP8X" + f"frame={self.frame}".encode().ljust(58, b"\0")

    def screenshot(self, scale=2):
        self._require()
        return b"PNG" + str(self.frame).encode()

    def save_state(self):
        self._require()
        return f"STATE:{self.frame}".encode().ljust(64, b"\0")

    def load_state(self, data):
        self._require()
        if not data:
            raise EmulatorError("The save state is empty.")
        text = data.rstrip(b"\0").decode("utf-8", "replace")
        if not text.startswith("STATE:"):
            raise EmulatorError("The core refused to load the save state.")
        self.frame = int(text.split(":", 1)[1])
        self.loaded_from = self.frame
        self.advance(1)

    # -- battery saves
    def save_sram(self):
        if not self.started or not self.sram:
            return None
        return bytes(self.sram)

    def load_sram(self, data):
        if not self.started or not data or self.sram is None:
            return False
        if len(data) != len(self.sram):
            return False
        self.sram[:] = data
        self.loaded_sram = bytes(data)
        return True

    @property
    def sram_size(self):
        return 0 if not self.sram else len(self.sram)

    # -- core options
    @property
    def core_name(self):
        return self.core_path.name.split("_libretro")[0]

    def option_definitions(self):
        if not self.started:
            return {}
        return dict(FakeEmulator.definitions_by_core.get(self.core_name, {}))

    def option_value(self, key):
        return self.runtime_options.get(key, self.options.get(key))

    def set_option(self, key, value):
        if not self.started:
            return False
        self.runtime_options[key] = value
        return True


# -- Red's Config -------------------------------------------------------------


class FakeValue:
    """One Config value: awaitable, settable, and usable as a context."""

    def __init__(self, store, key, default):
        self.store, self.key, self.default = store, key, default

    def __await__(self):
        async def get():
            value = self.store.get(self.key, self.default)
            return dict(value) if isinstance(value, dict) else value

        return get().__await__()

    def __call__(self):
        return self

    async def set(self, value):
        self.store[self.key] = value

    async def clear(self):
        self.store.pop(self.key, None)

    async def __aenter__(self):
        self._working = self.store.setdefault(self.key, self.default)
        return self._working

    async def __aexit__(self, *exc):
        self.store[self.key] = self._working
        return False


class FakeScope:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return FakeValue(self.store, name, None)


class FakeConfig:
    def __init__(self):
        self.globals = {}
        self.channels = {}
        self._global_defaults = {}
        self._channel_defaults = {}

    def register_global(self, **kwargs):
        self._global_defaults.update(kwargs)

    def register_channel(self, **kwargs):
        self._channel_defaults.update(kwargs)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        default = self._global_defaults.get(name)
        if isinstance(default, dict):
            default = dict(default)
        return FakeValue(self.globals, name, default)

    def channel_from_id(self, channel_id):
        return FakeScope(self.channels.setdefault(int(channel_id), {}))

    async def all_channels(self):
        out = {}
        for channel_id, data in self.channels.items():
            merged = dict(self._channel_defaults)
            merged.update(data)
            out[channel_id] = merged
        return out


# -- Discord ------------------------------------------------------------------


class FakeMessage:
    _next_id = 1000

    def __init__(self, channel, **kwargs):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.attachments = []
        self.edits = []
        self.kwargs = kwargs

    @property
    def jump_url(self):
        return f"https://discord.test/{self.id}"

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def delete(self):
        return None

    def to_reference(self, **kwargs):
        return None


class FakePermissions:
    def __init__(self, view_channel=True):
        self.view_channel = view_channel


class FakeChannel:
    def __init__(self, cid=555, name="general", visible=True):
        self.id = cid
        self.name = name
        self.messages = {}
        self.sent = []
        self.visible = visible

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, user):
        return FakePermissions(self.visible)

    async def fetch_message(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "nope")


class FakeUser:
    def __init__(self, uid=42, name="Tester"):
        self.id = uid
        self.display_name = name


class FakeBot:
    def __init__(self):
        self.added_views = []
        self.channels = {}

    async def is_owner(self, user):
        return user.id == 1

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def get_embed_colour(self, location):
        return discord.Colour.blurple()

    get_embed_color = get_embed_colour

    async def wait_until_red_ready(self):
        return None

    def add_view(self, view, message_id=None):
        if not view.is_persistent():
            raise ValueError("View is not persistent.")
        if view.is_finished():
            raise ValueError("View is already finished.")
        self.added_views.append((view, message_id))


def playable(viewmod, view):
    """Every child except the permanently-inert layout spacers."""
    return [c for c in view.children if not isinstance(c, viewmod._SpacerButton)]


def snapshot(viewmod, kwargs, view):
    """What an edit would have put on the wire, in a comparable shape."""
    files = kwargs.get("attachments") or []
    return {
        "has_attachments": "attachments" in kwargs,
        "n_attachments": len(files),
        "filenames": [getattr(f, "filename", None) for f in files],
        "all_disabled": all(getattr(c, "disabled", False) for c in playable(viewmod, view)),
        "any_disabled": any(getattr(c, "disabled", False) for c in playable(viewmod, view)),
        "labels": [getattr(c, "label", None) or getattr(c, "custom_id", None) for c in view.children],
        "content": kwargs.get("content"),
        "has_embed": "embed" in kwargs and kwargs["embed"] is not None,
        "spacers_disabled": all(
            c.disabled for c in view.children if isinstance(c, viewmod._SpacerButton)
        ),
    }


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self.done = False

    async def edit_message(self, **kwargs):
        if self.done:
            raise RuntimeError("InteractionResponded")
        self.done = True
        self.interaction.log.append(
            ("response.edit_message", self.interaction.snapshot(kwargs))
        )

    async def defer(self, **kwargs):
        if self.done:
            raise RuntimeError("InteractionResponded")
        self.done = True
        self.interaction.log.append(("response.defer", {}))

    async def send_message(self, content=None, **kwargs):
        if self.done:
            raise RuntimeError("InteractionResponded")
        self.done = True
        self.interaction.log.append(
            (
                "response.send_message",
                {"content": content, "ephemeral": kwargs.get("ephemeral")},
            )
        )


class FakeFollowup:
    def __init__(self, interaction):
        self.interaction = interaction

    async def send(self, content=None, **kwargs):
        self.interaction.log.append(
            ("followup.send", {"content": content, "ephemeral": kwargs.get("ephemeral")})
        )


class FakeInteraction:
    def __init__(self, viewmod, view, user=None, message=None):
        self.viewmod = viewmod
        self.view = view
        self.user = user or FakeUser()
        self.message = message
        self.channel_id = view.channel_id
        self.log = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        #: Set to make every edit_original_response raise a 400, the way
        #: Discord does when it refuses a component payload.
        self.fail_edits = False

    def snapshot(self, kwargs):
        return snapshot(self.viewmod, kwargs, self.view)

    async def edit_original_response(self, **kwargs):
        if self.fail_edits:
            raise discord.HTTPException(
                types.SimpleNamespace(status=400, reason="Bad Request"),
                {"code": 50035, "message": "Invalid Form Body"},
            )
        self.log.append(("edit_original_response", self.snapshot(kwargs)))
        return self.view.message or FakeMessage(None)

    def kinds(self):
        return [kind for kind, _ in self.log]


class FakeContext:
    def __init__(self, channel, author=None, attachments=(), guild_id=777):
        self.channel = channel
        self.author = author or FakeUser()
        self.guild = types.SimpleNamespace(id=guild_id)
        self.clean_prefix = "!"
        self.sent = []
        self.message = types.SimpleNamespace(
            attachments=list(attachments), to_reference=lambda **kw: None
        )

    async def send(self, content=None, **kwargs):
        message = FakeMessage(self.channel, content=content, **kwargs)
        self.sent.append(content if content is not None else kwargs)
        self.channel.messages[message.id] = message
        return message

    async def embed_colour(self):
        return discord.Colour.blurple()

    def typing(self):
        class _Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _Typing()

    def said(self):
        """Everything this context has been told to send, as one string."""
        return " ".join(str(x) for x in self.sent)


class FakeAttachment:
    def __init__(self, data, filename="attached.gb"):
        self.size = len(data) if isinstance(data, (bytes, bytearray)) else data
        self.filename = filename
        self._data = data if isinstance(data, (bytes, bytearray)) else b"x" * data

    async def read(self):
        return bytes(self._data)


class FakeMenu:
    """Stands in for Red's SimpleMenu, which needs a real Discord channel."""

    last_pages = []

    def __init__(self, pages, timeout=None, **kwargs):
        self.pages = list(pages)
        FakeMenu.last_pages = self.pages

    async def start(self, ctx, **kwargs):
        for page in self.pages:
            await ctx.send(page)


def zip_of(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buf.getvalue()


# -- The environment a cog test runs in ---------------------------------------


class RetroEnv:
    """A RetroCog wired to the fakes above, with the helpers tests need."""

    def __init__(self, tmp_path, monkeypatch):
        import retro  # noqa: F401  (imports the package, and therefore Red)

        self.cogmod = sys.modules["retro.RetroCog"]
        self.viewmod = sys.modules["retro.RetroView"]
        self.sysmod = sys.modules["retro.systems"]
        self.data = Path(tmp_path) / "cogdata"
        self.data.mkdir(parents=True, exist_ok=True)
        # Stand-in core files: the cog only ever checks that the path exists
        # and what the filename says the core is.
        self.cores_dir = Path(tmp_path) / "cores"
        self.cores_dir.mkdir(parents=True, exist_ok=True)
        for core in self.sysmod.CORES:
            (self.cores_dir / f"{core}_libretro.so").write_bytes(b"\x7fELF fake core")
        (self.cores_dir / "nestopia_libretro.so").write_bytes(b"\x7fELF not ours")

        FakeEmulator.reset()
        monkeypatch.setattr(self.cogmod, "cog_data_path", lambda cog: self.data)
        monkeypatch.setattr(
            self.cogmod, "Config", types.SimpleNamespace(get_conf=lambda *a, **k: FakeConfig())
        )
        monkeypatch.setattr(self.cogmod, "RetroEmulator", FakeEmulator)
        monkeypatch.setattr(self.cogmod, "SimpleMenu", FakeMenu)

        self.cog, self.bot = self.make_cog()

    # -- building blocks
    def make_cog(self):
        """Another cog on the same fakes, for restart and migration tests."""
        bot = FakeBot()
        return self.cogmod.RetroCog(bot), bot

    def core_path(self, core):
        return str(self.cores_dir / f"{core}_libretro.so")

    async def install_cores(self, *cores):
        """Record these cores (or all of them) as installed."""
        names = cores or tuple(self.sysmod.CORES)
        await self.cog.config.cores.set({name: self.core_path(name) for name in names})

    def channel(self, cid=8000, name="general", bot=None):
        channel = FakeChannel(cid, name=name)
        (bot or self.bot).channels[channel.id] = channel
        return channel

    def context(self, channel, **kwargs):
        return FakeContext(channel, **kwargs)

    def interaction(self, view, **kwargs):
        return FakeInteraction(self.viewmod, view, **kwargs)

    def button(self, view, custom_id):
        return next(c for c in view.children if c.custom_id == custom_id)

    def control(self, view, name):
        return self.button(view, f"{self.viewmod.CUSTOM_ID_PREFIX}:{name}")

    def playable(self, view):
        return playable(self.viewmod, view)

    def serve(self, name, data):
        """Replace the cog's downloader with one that always serves this."""

        async def _fetch(ctx, url):
            return name, data

        self.cog._fetch_rom = _fetch
        return _fetch

    async def start_game(
        self, ctx, name="ucity", data=ROM_BYTES, source="attachment", filename=None, cog=None
    ):
        """Start a session the way `[p]retro` does, and return its view."""
        cog = cog or self.cog
        filename = filename or f"{name}.gbc"
        system = self.sysmod.system_for_extension(Path(filename).suffix)
        slug = cog._slug(name)
        await cog._start_session(ctx, name, slug, filename, data, source, system)
        view = cog.sessions.get(ctx.channel.id)
        if view is not None and view.message is None and ctx.channel.messages:
            view.message = list(ctx.channel.messages.values())[-1]
            view.message_id = view.message.id
        return view

    async def posted_game(self, cid=8000, name="ucity", **kwargs):
        """A started session plus the channel and context it lives in."""
        channel = self.channel(cid)
        ctx = self.context(channel)
        view = await self.start_game(ctx, name, **kwargs)
        return view, ctx, channel
