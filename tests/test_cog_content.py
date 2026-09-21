"""Getting things into the cog: cores, ROMs, zips, BIOS files and settings.

Everything here is about what a stranger can hand the bot -- a URL, an
attachment, a zip, a 4 GiB "ROM" -- and what the bot says back when it is
not usable.
"""

import asyncio
import contextlib
import io
import time
import types
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

import discord  # noqa: E402

from .fakes import NES_BYTES, ROM_BYTES, FakeAttachment, FakeEmulator, zip_of  # noqa: E402


def http_error(status=400, code=50035, message="Invalid Form Body"):
    return discord.HTTPException(
        types.SimpleNamespace(status=status, reason="Bad Request"),
        {"code": code, "message": message},
    )


# -- retroset download --------------------------------------------------------


@pytest.fixture
def buildbot(retro, monkeypatch):
    """A stand-in libretro buildbot that serves every core but snes9x."""
    served = []

    class FakeResponse:
        def __init__(self, core):
            self.core = core
            self.status = 404 if core == "snes9x" else 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr(
                    f"{self.core}_libretro.so", b"\x7fELF" + self.core.encode() * 64
                )
            return buf.getvalue()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            core = url.rsplit("/", 1)[-1].replace("_libretro.so.zip", "")
            served.append(core)
            return FakeResponse(core)

    monkeypatch.setattr(
        retro.cogmod,
        "aiohttp",
        types.SimpleNamespace(
            ClientSession=lambda **kw: FakeSession(),
            ClientTimeout=lambda **kw: None,
            ClientError=Exception,
        ),
    )

    # Core downloads go through the SSRF guard, so that is what has to be
    # stood in for here. The guard itself is tested in test_net.py; these
    # tests are about which cores get fetched and what is reported.
    @contextlib.asynccontextmanager
    async def fake_guarded_get(url, **kwargs):
        core = url.rsplit("/", 1)[-1].replace("_libretro.so.zip", "")
        served.append(core)
        yield FakeResponse(core)

    monkeypatch.setattr(retro.netmod, "guarded_get", fake_guarded_get)
    return served


async def test_an_unknown_core_name_is_refused(retro, buildbot):
    ctx = retro.context(retro.channel(8500))
    await retro.cogmod.Retro.retroset_download.callback(retro.cog, ctx, "nestopia")
    assert "not a core this cog knows" in str(ctx.sent[-1])


async def test_downloading_everything_reports_what_installed_and_what_failed(retro, buildbot):
    ctx = retro.context(retro.channel(8501))
    await retro.cog.config.cores.set({})
    await retro.cogmod.Retro.retroset_download.callback(retro.cog, ctx, None)

    cores = await retro.cog.config.cores()
    assert set(buildbot) == set(retro.sysmod.CORES)
    assert set(cores) == set(retro.sysmod.CORES) - {"snes9x"}
    report = " ".join(str(s) for s in ctx.sent[-3:])
    assert "Installed 9 core(s)" in report
    assert "KiB in total" in report
    assert "snes9x" in report and "404" in report
    assert all(Path(p).is_file() for p in cores.values())


async def test_re_running_the_download_only_retries_what_is_missing(retro, buildbot):
    ctx = retro.context(retro.channel(8502))
    await retro.cog.config.cores.set({})
    download = retro.cogmod.Retro.retroset_download.callback
    await download(retro.cog, ctx, None)

    buildbot.clear()
    await download(retro.cog, ctx, None)
    assert buildbot == ["snes9x"]
    assert "Already installed" in " ".join(str(s) for s in ctx.sent[-3:])


async def test_naming_one_core_re_downloads_it_even_when_present(retro, buildbot):
    ctx = retro.context(retro.channel(8503))
    await retro.cog.config.cores.set({})
    download = retro.cogmod.Retro.retroset_download.callback
    await download(retro.cog, ctx, None)

    buildbot.clear()
    await download(retro.cog, ctx, "gambatte")
    assert buildbot == ["gambatte"]
    buildbot.clear()
    await download(retro.cog, ctx, "gambatte_libretro.so")
    assert buildbot == ["gambatte"], "a core filename is accepted too"


async def test_a_complete_install_downloads_nothing_and_says_so(retro, buildbot):
    ctx = retro.context(retro.channel(8504))
    await retro.cog.config.cores.set({})
    download = retro.cogmod.Retro.retroset_download.callback
    await download(retro.cog, ctx, None)
    async with retro.cog.config.cores() as cores:
        cores["snes9x"] = str(retro.cog._cores_dir() / "snes9x_libretro.so")
    (retro.cog._cores_dir() / "snes9x_libretro.so").write_bytes(b"\x7fELF")

    buildbot.clear()
    await download(retro.cog, ctx, None)
    assert "already installed" in str(ctx.sent[-1])
    assert buildbot == []


# -- Upgrading from the single-core setting -----------------------------------


@pytest.mark.parametrize(
    "old_path, expected",
    [
        ("/opt/cores/gambatte_libretro.so", {"gambatte": "/opt/cores/gambatte_libretro.so"}),
        # A filename we recognise wins over the gambatte default...
        ("/opt/cores/snes9x_libretro.so", {"snes9x": "/opt/cores/snes9x_libretro.so"}),
        # ...and one we do not falls back to it, because that is what the
        # single-core setting could only ever have been.
        ("/opt/weird-thing.so", {"gambatte": "/opt/weird-thing.so"}),
    ],
)
async def test_the_old_core_path_becomes_a_core_mapping(retro, old_path, expected):
    cog, _ = retro.make_cog()
    await cog.config.core_path.set(old_path)
    await cog._migrate_core_path()
    assert await cog.config.cores() == expected
    assert await cog.config.core_path() == ""


async def test_an_existing_mapping_is_never_clobbered(retro):
    cog, _ = retro.make_cog()
    await cog.config.cores.set({"mgba": "/x/mgba_libretro.so"})
    await cog.config.core_path.set("/opt/cores/gambatte_libretro.so")
    await cog._migrate_core_path()
    assert await cog.config.cores() == {"mgba": "/x/mgba_libretro.so"}


async def test_a_fresh_install_migrates_nothing(retro):
    cog, _ = retro.make_cog()
    await cog._migrate_core_path()
    assert await cog.config.cores() == {}


# -- Core detection / settings ------------------------------------------------


async def test_there_is_no_way_to_set_a_core_path_by_hand(retro):
    # Cores are detected, not configured. `[p]retroset core <path>` is gone.
    assert not hasattr(retro.cogmod.Retro, "retroset_core")
    source = (Path(retro.cogmod.__file__)).read_text()
    assert 'name="core"' not in source


async def test_a_core_in_the_managed_directory_is_found_without_any_config(retro):
    assert await retro.cog._installed_cores() == {}
    dropped = retro.cog._cores_dir() / "gambatte_libretro.so"
    dropped.write_bytes(b"\x7fELF dropped in by hand")

    installed = await retro.cog._installed_cores()
    assert installed == {"gambatte": dropped}
    assert await retro.cog._core_path("gambatte") == dropped
    assert await retro.cog.config.cores() == {}, "nothing was written to settings"


async def test_a_file_in_the_cores_directory_that_is_not_a_core_is_ignored(retro):
    (retro.cog._cores_dir() / "nestopia_libretro.so").write_bytes(b"\x7fELF")
    (retro.cog._cores_dir() / "notes.txt").write_text("hello")
    (retro.cog._cores_dir() / "subdir").mkdir()
    assert await retro.cog._installed_cores() == {}


async def test_a_recorded_path_outside_the_managed_directory_still_works(retro):
    # The old `cores` setting is still honoured, which is what an install made
    # before core detection existed relies on.
    await retro.install_cores("snes9x")
    installed = await retro.cog._installed_cores()
    assert installed["snes9x"] == Path(retro.core_path("snes9x"))


async def test_a_recorded_path_that_has_gone_falls_back_to_the_directory(retro):
    await retro.cog.config.cores.set({"gambatte": "/nowhere/gambatte_libretro.so"})
    assert await retro.cog._core_path("gambatte") is None
    assert await retro.cog._installed_cores() == {}

    real = retro.cog._cores_dir() / "gambatte_libretro.so"
    real.write_bytes(b"\x7fELF")
    assert await retro.cog._core_path("gambatte") == real
    assert (await retro.cog._installed_cores())["gambatte"] == real


async def test_a_game_starts_from_a_core_that_was_only_ever_detected(retro):
    (retro.cog._cores_dir() / "gambatte_libretro.so").write_bytes(b"\x7fELF")
    channel = retro.channel(8602)
    ctx = retro.context(channel)
    retro.serve("detected.gbc", ROM_BYTES)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/detected.gbc"
    )
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.live


async def test_an_unreadable_cores_directory_is_survivable(retro, monkeypatch):
    def boom(self):
        raise OSError("no")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert retro.cog._scan_cores_dir() == {}
    assert await retro.cog._installed_cores() == {}


async def test_the_settings_embed_describes_the_whole_install(retro):
    ctx = retro.context(retro.channel(8601))
    await retro.install_cores("gambatte")
    await retro.cogmod.Retro.retroset_settings.callback(retro.cog, ctx)

    embed = ctx.sent[-1]["embed"]
    names = [field.name for field in embed.fields]
    values = " ".join(field.value for field in embed.fields)
    assert any(name.startswith("Cores") for name in names)
    assert not any("Game Boy core" in name for name in names), "it is not one console any more"
    assert any("hold" in name.lower() for name in names)
    assert any("Clip" in name for name in names)
    assert any("System directory" in name for name in names)
    assert any("Automatic core" in name for name in names)
    assert "awake at once" in values, "the one-at-a-time rule is explained"
    # The clip length is a float, and the default must not read "1.0 seconds".
    assert "1 second of play per button press" in values, values
    assert "0.2-15, fractions allowed" in values, values
    assert "160ms per press" in values


async def test_the_settings_embed_says_what_a_short_clip_does_to_a_press(retro):
    await retro.cog.config.clip_seconds.set(0.2)
    ctx = retro.context(retro.channel(8602))
    await retro.cogmod.Retro.retroset_settings.callback(retro.cog, ctx)

    values = " ".join(field.value for field in ctx.sent[-1]["embed"].fields)
    assert "0.2 seconds of play" in values, values
    # The hold is a ceiling, and at a fifth of a second it is not honoured.
    assert "held for about 133ms rather than the 160ms" in values, values
    assert "repeat button is greyed out" in values, values


# -- ROM size and content checks ----------------------------------------------


def test_the_rom_size_limit_and_its_label_agree(retro):
    assert retro.cogmod.MAX_ROM_SIZE == 32 * 1024 * 1024
    assert retro.cogmod.MAX_ROM_SIZE_LABEL == "32 MiB"


async def test_an_oversized_attachment_is_refused_with_the_real_limit(retro):
    channel = retro.channel(9008)
    oversized = FakeAttachment(retro.cogmod.MAX_ROM_SIZE + 1, filename="attached.gb")
    ctx = retro.context(channel, attachments=[oversized])
    assert await retro.cogmod.Retro._fetch_rom(retro.cog, ctx, None) is None
    assert "32 MiB" in ctx.sent[-1]


async def test_a_twelve_mib_snes_rom_is_accepted(retro):
    ctx = retro.context(
        retro.channel(9009), attachments=[FakeAttachment(12 * 1024 * 1024, "big.sfc")]
    )
    got = await retro.cogmod.Retro._fetch_rom(retro.cog, ctx, None)
    assert got is not None and got[0] == "big.sfc"


async def test_an_html_error_page_is_caught_before_the_emulator_sees_it(retro):
    await retro.install_cores("gambatte")
    ctx = retro.context(retro.channel(9010))
    retro.serve("page.gb", b"<!DOCTYPE html><html>nope</html>")
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game="https://example.com/page.gb")
    assert "web page" in ctx.sent[-1]
    assert "Game Boy" not in ctx.sent[-1], "the message is not console specific"


async def test_a_file_too_small_to_be_a_rom_is_caught(retro):
    await retro.install_cores("gambatte")
    ctx = retro.context(retro.channel(9011))
    retro.serve("tiny.gb", b"\0" * 64)
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game="https://example.com/tiny.gb")
    assert "too small" in ctx.sent[-1]


# -- Zipped ROMs --------------------------------------------------------------


async def test_a_zipped_rom_starts_a_game_named_after_the_rom(retro):
    await retro.install_cores()
    channel = retro.channel(9300)
    ctx = retro.context(channel)
    retro.serve("ucity.zip", zip_of([("ucity.gbc", ROM_BYTES)]))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/ucity.zip"
    )

    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.live, ctx.said()
    assert view.game_name == "ucity", "named after the ROM, not the zip"
    assert view.rom_filename.endswith(".gbc"), "the cached ROM keeps the inner extension"
    assert view.system.key == "gb", "the console came from the ROM inside"
    assert retro.cog._rom_path(view.rom_filename).read_bytes() == ROM_BYTES


async def test_a_zip_is_spotted_by_its_magic_not_its_name(retro):
    await retro.install_cores()
    channel = retro.channel(9301)
    ctx = retro.context(channel)
    retro.serve("mystery.bin", zip_of([("inside.nes", NES_BYTES)]))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/mystery.bin"
    )
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.system.key == "nes", ctx.said()


async def test_several_roms_in_a_zip_pick_the_first_and_say_which(retro):
    await retro.install_cores()
    channel = retro.channel(9302)
    ctx = retro.context(channel)
    retro.serve(
        "pack.zip",
        zip_of([("zzz.gbc", ROM_BYTES), ("aaa.nes", NES_BYTES), ("readme.txt", b"hello" * 50)]),
    )
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/pack.zip"
    )
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.game_name == "aaa"
    said = ctx.said()
    assert "aaa.nes" in said and "2 playable ROMs" in said


async def test_a_rom_in_a_nested_folder_is_found_and_flattened(retro):
    await retro.install_cores()
    channel = retro.channel(9303)
    ctx = retro.context(channel)
    retro.serve("deep.zip", zip_of([("release/v1/game.gbc", ROM_BYTES)]))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/deep.zip"
    )
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.live
    assert "/" not in view.rom_filename


async def test_a_zip_with_nothing_playable_is_refused_with_a_listing(retro):
    await retro.install_cores()
    channel = retro.channel(9304)
    ctx = retro.context(channel)
    retro.serve("docs.zip", zip_of([("readme.txt", b"x" * 99), ("art.png", b"y" * 99)]))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/docs.zip"
    )
    said = ctx.said()
    assert "no ROM this bot can use" in said
    assert "readme.txt" in said and "art.png" in said
    assert "Game Boy" in said, "and the consoles that would have worked"
    assert retro.cog.sessions.get(channel.id) is None


async def test_a_zip_bomb_is_refused_on_its_metadata(retro):
    await retro.install_cores()
    channel = retro.channel(9305)
    ctx = retro.context(channel)
    retro.serve("bomb.zip", zip_of([("huge.gb", b"\0" * (retro.cogmod.MAX_ROM_SIZE + 1))]))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/bomb.zip"
    )
    assert "limit" in ctx.said()
    assert retro.cog.sessions.get(channel.id) is None


async def test_a_corrupt_zip_is_refused_without_a_traceback(retro):
    await retro.install_cores()
    channel = retro.channel(9306)
    ctx = retro.context(channel)
    retro.serve("broken.zip", b"PK\x03\x04" + b"\x00" * 4000)
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/broken.zip"
    )
    assert "zip" in ctx.said().lower()
    assert retro.cog.sessions.get(channel.id) is None


async def test_a_password_protected_zip_is_refused(retro):
    await retro.install_cores()
    channel = retro.channel(9307)
    ctx = retro.context(channel)
    locked = bytearray(zip_of([("secret.gb", ROM_BYTES)]))
    locked[locked.find(b"PK\x03\x04") + 6] |= 0x01
    locked[locked.find(b"PK\x01\x02") + 8] |= 0x01
    retro.serve("locked.zip", bytes(locked))
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game="https://example.com/locked.zip"
    )
    assert "password-protected" in ctx.said()


async def test_an_attached_zip_works_end_to_end(retro):
    await retro.install_cores()
    payload = zip_of([("attached.gbc", ROM_BYTES)])
    channel = retro.channel(9308)
    ctx = retro.context(channel, attachments=[FakeAttachment(payload, "game.zip")])
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game=None)
    view = retro.cog.sessions.get(channel.id)
    assert view is not None and view.live, ctx.said()
    assert view.game_name == "attached"


async def test_a_plain_unzipped_rom_is_unaffected(retro):
    await retro.install_cores()
    channel = retro.channel(9309)
    ctx = retro.context(channel)
    retro.serve("raw.gbc", ROM_BYTES)
    await retro.cogmod.Retro.retro.callback(retro.cog, ctx, game="https://example.com/raw.gbc")
    assert retro.cog.sessions.get(channel.id) is not None


# -- BIOS / the system directory ----------------------------------------------


@pytest.fixture
def bios(retro):
    """The BIOS commands plus a channel to run them in."""
    channel = retro.channel(9400)
    return types.SimpleNamespace(
        add=retro.cogmod.Retro.retroset_bios_add.callback,
        listing=retro.cogmod.Retro.retroset_bios_list.callback,
        remove=retro.cogmod.Retro.retroset_bios_remove.callback,
        channel=channel,
        directory=retro.cog._system_dir(),
    )


async def test_the_system_directory_lives_in_the_cog_s_data_folder(retro, bios):
    assert bios.directory == retro.data / "system"
    assert bios.directory.is_dir()


async def test_an_empty_bios_list_says_where_to_put_them(retro, bios):
    ctx = retro.context(bios.channel)
    await bios.listing(retro.cog, ctx)
    assert str(bios.directory) in ctx.sent[-1]
    assert "without one" in ctx.sent[-1], "and that most cores need none"


async def test_adding_with_nothing_attached_explains_itself(retro, bios):
    ctx = retro.context(bios.channel)
    await bios.add(retro.cog, ctx, "thing_bios.bin", None)
    assert "Attach" in ctx.sent[-1]


@pytest.mark.parametrize(
    "filename",
    ["../../etc/passwd", "/etc/passwd", "..\\..\\evil.bin", ".hidden", "a" * 80, "weird;name.bin"],
)
async def test_a_dangerous_bios_filename_is_refused(retro, bios, filename):
    ctx = retro.context(bios.channel)
    await bios.add(retro.cog, ctx, filename, "https://example.com/x.bin")
    assert "not a usable filename" in ctx.sent[-1], (filename, ctx.sent[-1])
    assert not (retro.data / "passwd").exists()


async def test_a_url_on_its_own_needs_no_filename(retro, bios):
    async def fake_download(url, max_size, label, what="file"):
        return "console_bios.bin", b"\x77" * 64

    retro.cog._download_bytes = fake_download
    ctx = retro.context(bios.channel)
    # `[p]retroset bios add <url>`: the first argument is the URL, not a name.
    await bios.add(retro.cog, ctx, "https://example.com/console_bios.bin", None)
    assert (bios.directory / "console_bios.bin").read_bytes() == b"\x77" * 64


async def test_an_attachment_on_its_own_keeps_its_own_name(retro, bios):
    ctx = retro.context(
        bios.channel, attachments=[FakeAttachment(b"\x88" * 32, "kept_bios.bin")]
    )
    await bios.add(retro.cog, ctx, None, None)
    assert (bios.directory / "kept_bios.bin").read_bytes() == b"\x88" * 32


async def test_an_attached_bios_is_stored_byte_for_byte(retro, bios):
    payload = b"\x5a" * 2048
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(payload, "thing_bios.bin")])
    await bios.add(retro.cog, ctx, "thing_bios.bin", None)
    stored = bios.directory / "thing_bios.bin"
    assert stored.is_file(), ctx.sent[-1]
    assert stored.read_bytes() == payload
    assert "thing_bios.bin" in ctx.sent[-1] and "2,048" in ctx.sent[-1]


async def test_every_file_in_a_bios_zip_is_installed(retro, bios):
    # A firmware set: several files, one of them in a folder, plus the junk
    # a Mac puts in every archive it makes.
    zipped = zip_of(
        [
            ("readme.txt", b"hi" * 40),
            ("other_bios.bin", b"\x11" * 512),
            ("dc/dc_boot.bin", b"\x22" * 256),
            ("dc/dc_flash.bin", b"\x33" * 128),
            ("__MACOSX/._other_bios.bin", b"junk"),
            (".DS_Store", b"junk"),
        ]
    )
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(zipped, "pack.zip")])
    await bios.add(retro.cog, ctx, None, None)

    assert (bios.directory / "other_bios.bin").read_bytes() == b"\x11" * 512
    assert (bios.directory / "readme.txt").is_file()
    # Folders are kept: some cores look for their firmware in one.
    assert (bios.directory / "dc" / "dc_boot.bin").read_bytes() == b"\x22" * 256
    assert (bios.directory / "dc" / "dc_flash.bin").read_bytes() == b"\x33" * 128
    # Archive noise never lands.
    assert not (bios.directory / "__MACOSX").exists()
    assert not (bios.directory / ".DS_Store").exists()

    said = ctx.sent[-1]
    assert "**4**" in said, said
    assert f"{512 + 80 + 256 + 128:,} bytes" in said
    assert "dc/dc_boot.bin" in said


async def test_a_zip_of_many_files_reports_and_lists_a_sample(retro, bios):
    entries = [(f"f{index:03d}.bin", bytes([index % 256]) * 16) for index in range(40)]
    ctx = retro.context(
        bios.channel, attachments=[FakeAttachment(zip_of(entries), "many.zip")]
    )
    await bios.add(retro.cog, ctx, None, None)
    said = " ".join(str(part) for part in ctx.sent)
    assert "**40**" in said
    assert f"and {40 - retro.cogmod.MAX_LISTED_BIOS_FILES} more" in said
    assert len(list(bios.directory.glob("f*.bin"))) == 40


async def test_a_single_file_zip_still_takes_the_name_it_was_given(retro, bios):
    solo = zip_of([("whatever.rom", b"\x22" * 256)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(solo, "solo.zip")])
    await bios.add(retro.cog, ctx, "renamed_bios.bin", None)
    assert (bios.directory / "renamed_bios.bin").read_bytes() == b"\x22" * 256
    assert "stored as" in ctx.sent[-1]


async def test_a_name_given_for_a_multi_file_zip_is_ignored_out_loud(retro, bios):
    pack = zip_of([("a.rom", b"\x33" * 64), ("b.rom", b"\x44" * 64)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "many.zip")])
    await bios.add(retro.cog, ctx, "nope_bios.bin", None)
    assert "was ignored" in ctx.sent[-1]
    assert (bios.directory / "a.rom").is_file()
    assert (bios.directory / "b.rom").is_file()
    assert not (bios.directory / "nope_bios.bin").exists()


@pytest.mark.parametrize(
    "member",
    [
        "../escape.bin",
        "/etc/passwd",
        "..\\..\\evil.bin",
        "a/../../b.bin",
        "deep/er/and/deeper/still/too_deep.bin",
    ],
)
async def test_a_zip_member_that_would_escape_is_skipped(retro, bios, member, tmp_path):
    pack = zip_of([(member, b"\x99" * 32), ("good_bios.bin", b"\xaa" * 32)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "evil.zip")])
    await bios.add(retro.cog, ctx, None, None)

    assert (bios.directory / "good_bios.bin").read_bytes() == b"\xaa" * 32
    assert "skipped" in ctx.sent[-1]
    # Nothing landed outside the system directory, at any depth.
    inside = {p.name for p in bios.directory.rglob("*") if p.is_file()}
    assert inside == {"good_bios.bin"}, inside
    assert not (retro.data / "escape.bin").exists()
    assert not (retro.cogs_root / "evil.bin").exists()


async def test_a_symlink_in_a_bios_zip_is_never_written(retro, bios):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("link_bios.bin")
        info.create_system = 3  # Unix
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        archive.writestr(info, "/etc/passwd")
        archive.writestr("real_bios.bin", b"\xbb" * 32)
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(buf.getvalue(), "l.zip")])
    await bios.add(retro.cog, ctx, None, None)

    assert (bios.directory / "real_bios.bin").read_bytes() == b"\xbb" * 32
    assert not (bios.directory / "link_bios.bin").exists()
    assert "skipped" in ctx.sent[-1]


async def test_a_zip_with_nothing_usable_says_so(retro, bios):
    pack = zip_of([("../nope.bin", b"x" * 16), (".hidden", b"y" * 16)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "bad.zip")])
    await bios.add(retro.cog, ctx, None, None)
    assert "no BIOS file this bot can use" in ctx.sent[-1], ctx.sent[-1]


async def test_a_bios_zip_that_unpacks_too_large_is_refused(retro, bios, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "MAX_BIOS_TOTAL_SIZE", 1024)
    pack = zip_of([(f"f{i}.bin", b"\x01" * 512) for i in range(4)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "big.zip")])
    await bios.add(retro.cog, ctx, None, None)
    assert "limit" in ctx.sent[-1]
    assert not list(bios.directory.glob("f*.bin"))


async def test_too_many_files_in_a_bios_zip_is_refused(retro, bios, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "MAX_BIOS_FILES", 3)
    pack = zip_of([(f"f{i}.bin", b"\x01" * 8) for i in range(6)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "lots.zip")])
    await bios.add(retro.cog, ctx, None, None)
    assert "more than the 3" in ctx.sent[-1]
    assert not list(bios.directory.glob("f*.bin"))


async def test_a_file_in_a_zip_over_the_per_file_limit_is_skipped(retro, bios, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "MAX_BIOS_SIZE", 256)
    pack = zip_of([("huge.bin", b"\x01" * 4096), ("small_bios.bin", b"\x02" * 64)])
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(pack, "mixed.zip")])
    await bios.add(retro.cog, ctx, None, None)
    assert (bios.directory / "small_bios.bin").is_file()
    assert not (bios.directory / "huge.bin").exists()
    assert "skipped" in ctx.sent[-1]


async def test_an_oversized_or_empty_bios_is_refused(retro, bios):
    big = FakeAttachment(b"x" * (retro.cogmod.MAX_BIOS_SIZE + 1), "big.bin")
    ctx = retro.context(bios.channel, attachments=[big])
    await bios.add(retro.cog, ctx, "big_bios.bin", None)
    assert "limit" in ctx.sent[-1]
    assert not (bios.directory / "big_bios.bin").exists()

    ctx2 = retro.context(bios.channel, attachments=[FakeAttachment(b"", "empty.bin")])
    await bios.add(retro.cog, ctx2, "empty_bios.bin", None)
    assert "empty" in ctx2.sent[-1]


async def test_a_bios_downloads_from_a_url_and_a_failure_is_reported(retro, bios):
    async def fake_download(url, max_size, label, what="file"):
        if "boom" in url:
            raise retro.cogmod.DownloadError("Downloading the BIOS file failed: nope")
        return "url_bios.bin", b"\x66" * 128

    retro.cog._download_bytes = fake_download
    ctx = retro.context(bios.channel)
    await bios.add(retro.cog, ctx, "url_bios.bin", "https://example.com/bios.bin")
    assert (bios.directory / "url_bios.bin").read_bytes() == b"\x66" * 128

    await bios.add(retro.cog, ctx, "fail_bios.bin", "https://example.com/boom.bin")
    assert "failed" in ctx.sent[-1], "reported, not raised"


async def test_bios_files_can_be_listed_and_removed(retro, bios):
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(b"\x5a" * 2048, "t.bin")])
    await bios.add(retro.cog, ctx, "thing_bios.bin", None)

    ctx2 = retro.context(bios.channel)
    await bios.listing(retro.cog, ctx2)
    assert "thing_bios.bin" in ctx2.sent[-1]
    assert "2,048 bytes" in ctx2.sent[-1]

    await bios.remove(retro.cog, ctx2, "thing_bios.bin")
    assert not (bios.directory / "thing_bios.bin").exists()
    await bios.remove(retro.cog, ctx2, "thing_bios.bin")
    assert "no `thing_bios.bin`" in ctx2.sent[-1]
    await bios.remove(retro.cog, ctx2, "../../../etc/passwd")
    assert "not a usable filename" in ctx2.sent[-1]


async def test_the_emulator_is_pointed_at_the_system_directory(retro, bios):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9401, "biostest")
    assert view.emulator.system_dir == bios.directory

    await retro.cog.hibernate(view, None)
    await view._press(retro.interaction(view, message=view.message), "a")
    assert view.emulator.system_dir == bios.directory, "a resumed emulator gets it too"


async def test_the_settings_embed_lists_the_installed_bios_files(retro, bios):
    ctx = retro.context(bios.channel, attachments=[FakeAttachment(b"\x11" * 512, "o.bin")])
    await bios.add(retro.cog, ctx, "other_bios.bin", None)
    ctx2 = retro.context(bios.channel)
    await retro.cogmod.Retro.retroset_settings.callback(retro.cog, ctx2)
    values = " ".join(field.value for field in ctx2.sent[-1]["embed"].fields)
    assert str(bios.directory) in values
    assert "other_bios.bin" in values


# -- Automatic core downloads on load -----------------------------------------


@pytest.fixture
def auto(retro, monkeypatch):
    """A second cog whose core downloads are faked, and what it attempted."""
    cog, _ = retro.make_cog()
    attempted = []

    # _download_core makes its own guarded session now, so it takes just
    # the core name.
    async def fake_download(name):
        attempted.append(name)
        if name == "snes9x":
            return False, 0, "buildbot returned status 404"
        path = cog._cores_dir() / f"{name}_libretro.so"
        path.write_bytes(b"\x7fELF")
        async with cog.config.cores() as cores:
            cores[name] = str(path)
        return True, 4, "installed"

    class NullSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        retro.cogmod,
        "aiohttp",
        types.SimpleNamespace(
            ClientSession=lambda **kw: NullSession(),
            ClientTimeout=lambda **kw: None,
            ClientError=Exception,
        ),
    )
    cog._download_core = fake_download
    return cog, attempted


async def test_auto_download_is_on_by_default(retro):
    cog, _ = retro.make_cog()
    assert await cog.config.auto_download_cores() is True


async def test_every_missing_core_is_fetched_once_and_a_failure_stops_nothing(retro, auto):
    cog, attempted = auto
    await cog._auto_download_loop()
    assert set(attempted) == set(retro.sysmod.CORES)
    assert set(await cog.config.cores()) == set(retro.sysmod.CORES) - {"snes9x"}
    assert await cog.config.auto_download_attempted_at() > 0


async def test_a_reload_inside_the_cooldown_downloads_nothing(retro, auto):
    cog, attempted = auto
    await cog._auto_download_loop()
    attempted.clear()
    await cog._auto_download_loop()
    assert attempted == []


async def test_past_the_cooldown_only_the_missing_core_is_retried(retro, auto):
    cog, attempted = auto
    await cog._auto_download_loop()
    attempted.clear()
    await cog.config.auto_download_attempted_at.set(
        time.time() - retro.cogmod.AUTO_DOWNLOAD_COOLDOWN_SECONDS - 1
    )
    await cog._auto_download_loop()
    assert attempted == ["snes9x"]


async def test_a_complete_install_never_opens_a_session(retro, auto):
    cog, attempted = auto
    await cog._auto_download_loop()
    attempted.clear()
    await cog.config.auto_download_attempted_at.set(0.0)
    async with cog.config.cores() as cores:
        cores["snes9x"] = str(cog._cores_dir() / "snes9x_libretro.so")
    (cog._cores_dir() / "snes9x_libretro.so").write_bytes(b"\x7fELF")

    await cog._auto_download_loop()
    assert attempted == []
    assert await cog.config.auto_download_attempted_at() == 0.0, "not even a timestamp"


async def test_switching_it_off_downloads_nothing(retro, auto):
    cog, attempted = auto
    await cog.config.auto_download_cores.set(False)
    await cog._auto_download_loop()
    assert attempted == []


async def test_a_download_that_explodes_never_escapes_the_task(retro):
    cog, _ = retro.make_cog()

    async def boom(session, name):
        raise RuntimeError("the network is on fire")

    cog._download_core = boom
    await cog._auto_download_loop()  # must not raise


async def test_a_broken_config_never_escapes_the_task(retro):
    cog, _ = retro.make_cog()

    class AngryConfig:
        def __getattr__(self, name):
            raise RuntimeError("config is down")

    cog.config = AngryConfig()
    await cog._auto_download_loop()  # must not raise


async def test_cog_load_survives_a_doomed_download_task(retro):
    cog, _ = retro.make_cog()

    async def doomed():
        raise RuntimeError("nope")

    cog._auto_download_loop = doomed
    await cog.cog_load()
    assert cog._download_task is not None
    await asyncio.sleep(0)
    await cog.cog_unload()
    assert cog._download_task is None


async def test_the_autodownload_toggle_reports_and_clears_the_cooldown(retro):
    ctx = retro.context(retro.channel(9500))
    toggle = retro.cogmod.Retro.retroset_autodownload.callback

    await toggle(retro.cog, ctx, None)
    assert "**on**" in ctx.sent[-1]
    await toggle(retro.cog, ctx, False)
    assert await retro.cog.config.auto_download_cores() is False
    await toggle(retro.cog, ctx, None)
    assert "**off**" in ctx.sent[-1]

    await retro.cog.config.auto_download_attempted_at.set(time.time())
    await toggle(retro.cog, ctx, True)
    assert await retro.cog.config.auto_download_cores() is True
    assert await retro.cog.config.auto_download_attempted_at() == 0.0


# -- Discord rejections never kill a command ----------------------------------


async def test_a_rejected_game_message_saves_the_game_and_leaves_no_session(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9600)
    ctx = retro.context(channel)
    real_send = ctx.send

    async def rejecting_send(content=None, **kwargs):
        if "view" in kwargs:
            raise http_error()
        return await real_send(content, **kwargs)

    ctx.send = rejecting_send
    system = retro.sysmod.system_for_extension(".gbc")
    await retro.cog._start_session(
        ctx, "doomed", "doomed", "doomed.gbc", ROM_BYTES, "attachment", system
    )

    assert retro.cog.sessions.get(channel.id) is None
    assert not FakeEmulator.instances[-1].started
    assert retro.cog._state_path(channel.id, "doomed").is_file()
    said = ctx.said()
    assert "50035" in said
    assert "bug in this cog" in said


async def test_a_forbidden_send_names_the_permission_it_needs(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9601)
    ctx = retro.context(channel)
    real_send = ctx.send

    async def forbidden_send(content=None, **kwargs):
        if "view" in kwargs:
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="Forbidden"),
                {"code": 50013, "message": "Missing Permissions"},
            )
        return await real_send(content, **kwargs)

    ctx.send = forbidden_send
    system = retro.sysmod.system_for_extension(".gbc")
    await retro.cog._start_session(
        ctx, "denied", "denied", "denied.gbc", ROM_BYTES, "attachment", system
    )
    assert "Attach Files" in ctx.said()
    assert retro.cog.sessions.get(channel.id) is None


async def test_safe_send_falls_back_to_plain_text_and_then_gives_up(retro):
    ctx = retro.context(retro.channel(9602))
    real_send = ctx.send
    plain = []

    async def picky_send(content=None, **kwargs):
        if kwargs:
            raise http_error()
        plain.append(content)
        return await real_send(content)

    ctx.send = picky_send
    got = await retro.cog._safe_send(ctx, "hello", embed=object())
    assert plain == ["hello"]
    assert got is not None

    async def always_fails(content=None, **kwargs):
        raise http_error()

    ctx.send = always_fails
    assert await retro.cog._safe_send(ctx, "x") is None


async def test_an_edit_discord_refuses_mid_press_leaves_the_controls_usable(retro):
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9603, "editfail")
    interaction = retro.interaction(view, message=view.message)
    interaction.fail_edits = True

    await view._press(interaction, "a")
    assert "followup.send" in interaction.kinds()
    assert any(
        "safe" in str(data.get("content", ""))
        for kind, data in interaction.log
        if kind == "followup.send"
    )
    assert not any(getattr(c, "disabled", False) for c in retro.playable(view))
    assert retro.cog.sessions.get(channel.id) is view


@pytest.mark.parametrize(
    "error_name",
    ["http", "forbidden", "emulator", "disk", "archive", "download", "timeout", "aiohttp"],
)
async def test_a_known_failure_becomes_a_sentence(retro, error_name):
    from retro.emulator import EmulatorError

    cases = {
        "http": (http_error(), "50035"),
        "forbidden": (
            discord.Forbidden(
                types.SimpleNamespace(status=403, reason="f"),
                {"code": 50013, "message": "Missing Permissions"},
            ),
            "Attach Files",
        ),
        "emulator": (EmulatorError("the core died"), "the core died"),
        "disk": (OSError(28, "No space left on device"), "disk space"),
        "archive": (retro.cogmod.archives.ArchiveError("that zip is broken"), "that zip is broken"),
        "download": (retro.cogmod.DownloadError("the download failed"), "the download failed"),
        "timeout": (asyncio.TimeoutError(), "too long"),
        "aiohttp": (retro.cogmod.aiohttp.ClientError("boom"), "download failed"),
    }
    original, expected = cases[error_name]

    ctx = retro.context(retro.channel(9604))
    ctx.command = types.SimpleNamespace(qualified_name="retro")
    ctx.bot = retro.bot

    class FakeCommandError(Exception):
        def __init__(self, wrapped):
            self.original = wrapped

    await retro.cog.cog_command_error(ctx, FakeCommandError(original))
    assert expected in str(ctx.sent[-1])


async def test_an_unknown_error_is_handed_back_to_red(retro):
    handled = []

    async def note_unhandled(ctx_, error, unhandled_by_cog=False):
        handled.append(error)

    retro.bot.on_command_error = note_unhandled
    ctx = retro.context(retro.channel(9605))
    ctx.command = types.SimpleNamespace(qualified_name="retro")
    ctx.bot = retro.bot

    class FakeCommandError(Exception):
        def __init__(self, wrapped):
            self.original = wrapped

    await retro.cog.cog_command_error(ctx, FakeCommandError(ValueError("who knows")))
    assert len(handled) == 1


async def test_missing_bot_permissions_are_named_and_not_passed_on(retro):
    handled = []

    async def note_unhandled(ctx_, error, unhandled_by_cog=False):
        handled.append(error)

    retro.bot.on_command_error = note_unhandled
    ctx = retro.context(retro.channel(9606))
    ctx.command = types.SimpleNamespace(qualified_name="retro")
    ctx.bot = retro.bot

    error = retro.cogmod.commands.BotMissingPermissions(["attach_files", "embed_links"])
    await retro.cog.cog_command_error(ctx, error)
    assert "Attach Files" in str(ctx.sent[-1]) and "Embed Links" in str(ctx.sent[-1])
    assert handled == []


async def test_refreshing_a_deleted_message_is_a_no_op(retro):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9607, "deleted")
    view.message = None
    view.message_id = 999999999
    await view.refresh("gone")  # must not raise
