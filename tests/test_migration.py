"""The RetroCog -> Retro rename, and the data it must not lose.

Red names both of a cog's storage locations after its Python class: Config
keys every setting and session by ``type(self).__name__``, and
``cog_data_path()`` puts the data folder under the same name. Renaming the
class therefore points the running bot at two empty places -- its downloaded
cores, cached ROMs, save states, battery saves, BIOS files, saved games and
live sessions all still sitting under the old name.

These tests are the proof that they come across, and that doing so is safe to
interrupt, safe to repeat, and safe to fail.
"""

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import ROM_BYTES  # noqa: E402

LEGACY = "RetroCog"


def furnish(directory):
    """Fill a data directory the way a year of playing would have."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cores").mkdir(exist_ok=True)
    (directory / "cores" / "gambatte_libretro.so").write_bytes(b"\x7fELF gambatte")
    (directory / "cores" / "snes9x_libretro.so").write_bytes(b"\x7fELF snes9x")
    (directory / "roms").mkdir(exist_ok=True)
    (directory / "roms" / "8000-ucity.gbc").write_bytes(ROM_BYTES)
    (directory / "states").mkdir(exist_ok=True)
    (directory / "states" / "8000-ucity.state").write_bytes(b"STATE:1234")
    (directory / "states" / "8000-ucity.srm").write_bytes(b"\xff" * 512)
    (directory / "system").mkdir(exist_ok=True)
    (directory / "system" / "some_bios.bin").write_bytes(b"\x01" * 256)
    (directory / "system" / "dc").mkdir(exist_ok=True)
    (directory / "system" / "dc" / "dc_boot.bin").write_bytes(b"\x02" * 128)
    return directory


def furnish_legacy_config(store, core_dir):
    """Fill the old cog name's Config the way a real install would have."""
    store.globals.update(
        {
            "cores": {
                "gambatte": str(core_dir / "cores" / "gambatte_libretro.so"),
                "snes9x": str(core_dir / "cores" / "snes9x_libretro.so"),
            },
            "games": {"ucity": "https://example.com/ucity.gbc"},
            "session_timeout_minutes": 42,
            "clip_seconds": 7,
            "hold_ms": 250,
            "core_options": {"gambatte": {"gambatte_gb_colorization": "GBC"}},
            "auto_download_cores": False,
        }
    )
    store.channels[8000] = {
        "session": {
            "message_id": 4242,
            "channel_id": 8000,
            "guild_id": 777,
            "game_name": "ucity",
            "slug": "ucity",
            "rom_filename": "8000-ucity.gbc",
            "system": "gbc",
            "core": "gambatte",
            "source": "ucity",
            "starter_id": 42,
            "last_active": 1.0,
        }
    }


@pytest.fixture
def pre_rename(retro):
    """A cog as it looks the first time it loads after the rename."""
    legacy_dir = furnish(retro.legacy_data)
    furnish_legacy_config(retro.legacy_store(), legacy_dir)
    cog, bot = retro.make_cog(fresh_config=True)
    return cog, bot, legacy_dir


# -- The class itself ---------------------------------------------------------


def test_the_cog_class_is_called_retro(retro):
    assert retro.cogmod.Retro.__name__ == "Retro"
    assert retro.cogmod.LEGACY_COG_NAME == LEGACY


def test_the_load_bearing_identifiers_did_not_move(retro):
    """The two strings baked into data that already exists.

    A new value for either silently hands every existing install an empty
    configuration (the Config identifier) or orphans every live game in
    every channel (the custom_id prefix Discord routes clicks by).
    """
    assert retro.viewmod.CUSTOM_ID_PREFIX == "libretro"
    assert retro.migrationmod.CONFIG_IDENTIFIER == sum(b"robloach-cogs/pyboy") == 1925
    # ...and it is one constant, so the cog's own Config handle and the
    # legacy namespace's cannot be given different numbers.
    assert retro.cogmod.CONFIG_IDENTIFIER is retro.migrationmod.CONFIG_IDENTIFIER
    for module in (retro.cogmod, retro.migrationmod):
        source = open(module.__file__).read()
        assert source.count("identifier=CONFIG_IDENTIFIER") == 1, module.__name__


# -- Moving the data directory ------------------------------------------------


async def test_everything_in_the_old_data_folder_comes_across(retro, pre_rename):
    cog, _, legacy_dir = pre_rename
    await cog._migrate_legacy_namespace()

    new = retro.data
    assert (new / "cores" / "gambatte_libretro.so").read_bytes() == b"\x7fELF gambatte"
    assert (new / "roms" / "8000-ucity.gbc").read_bytes() == ROM_BYTES
    assert (new / "states" / "8000-ucity.state").read_bytes() == b"STATE:1234"
    assert (new / "states" / "8000-ucity.srm").read_bytes() == b"\xff" * 512
    assert (new / "system" / "some_bios.bin").is_file()
    assert (new / "system" / "dc" / "dc_boot.bin").is_file(), "subfolders too"
    assert not legacy_dir.exists(), "the emptied old folder is tidied away"


async def test_the_stored_core_paths_are_repointed_at_the_new_folder(retro, pre_rename):
    cog, _, legacy_dir = pre_rename
    await cog._migrate_legacy_namespace()

    cores = await cog.config.cores()
    for name, path in cores.items():
        assert str(legacy_dir) not in path, (name, path)
        assert path.startswith(str(retro.data))
    # And the repointed paths are real, so the cores are actually installed.
    assert set(await cog._installed_cores()) == {"gambatte", "snes9x"}
    assert await cog._core_path("gambatte") is not None


async def test_a_core_path_outside_the_old_folder_is_left_alone(retro):
    furnish(retro.legacy_data)
    retro.legacy_store().globals["cores"] = {"mgba": "/opt/retroarch/mgba_libretro.so"}
    cog, _ = retro.make_cog(fresh_config=True)
    await cog._migrate_legacy_namespace()
    assert await cog.config.cores() == {"mgba": "/opt/retroarch/mgba_libretro.so"}


async def test_a_fresh_install_migrates_nothing_and_says_nothing(retro):
    cog, _ = retro.make_cog(fresh_config=True)
    assert not retro.legacy_data.exists()
    await cog._migrate_legacy_namespace()
    assert await cog.config.legacy_namespace_migrated() is True
    assert await cog.config.cores() == {}
    assert await cog.config.games() == {}
    assert not retro.legacy_data.exists(), "and no empty old folder was conjured"


async def test_the_config_store_file_is_never_moved_as_if_it_were_data(retro):
    # Red's default (JSON) driver keeps a cog's settings in settings.json
    # *inside* its data directory, so the data folder and the Config namespace
    # are the same folder. Moving that file would drop the old namespace's
    # settings straight on top of the new one's, sight unseen.
    legacy_dir = furnish(retro.legacy_data)
    (legacy_dir / retro.cogmod.CONFIG_STORE_FILENAME).write_text('{"old": "store"}')
    retro.data.mkdir(parents=True, exist_ok=True)
    (retro.data / retro.cogmod.CONFIG_STORE_FILENAME).write_text('{"new": "store"}')
    furnish_legacy_config(retro.legacy_store(), legacy_dir)
    cog, _ = retro.make_cog(fresh_config=True)

    await cog._migrate_legacy_namespace()

    assert (retro.data / "settings.json").read_text() == '{"new": "store"}'
    assert (legacy_dir / "settings.json").read_text() == '{"old": "store"}'
    # Everything that really is data still came across...
    assert (retro.data / "roms" / "8000-ucity.gbc").is_file()
    assert (retro.data / "states" / "8000-ucity.state").is_file()
    # ...and the settings came across through Config, not through the file.
    assert await cog.config.games() == {"ucity": "https://example.com/ucity.gbc"}
    # The old folder is kept, because the settings in it were copied rather
    # than moved and are a free backup.
    assert sorted(p.name for p in legacy_dir.iterdir()) == ["settings.json"]


# -- Both directions ----------------------------------------------------------


async def test_the_new_folder_wins_when_both_have_the_same_thing(retro):
    # An interrupted first attempt, restarted: `cores` is already across and
    # must not be clobbered by the stale copy, but `roms` still has to move.
    furnish(retro.legacy_data)
    (retro.data / "cores").mkdir(parents=True, exist_ok=True)
    (retro.data / "cores" / "gambatte_libretro.so").write_bytes(b"\x7fELF newer")
    cog, _ = retro.make_cog(fresh_config=True)

    await cog._migrate_legacy_namespace()

    assert (retro.data / "cores" / "gambatte_libretro.so").read_bytes() == b"\x7fELF newer"
    assert (retro.data / "roms" / "8000-ucity.gbc").read_bytes() == ROM_BYTES
    # The contested folder is left where it was rather than merged or deleted.
    assert (retro.legacy_data / "cores" / "gambatte_libretro.so").is_file()
    assert retro.legacy_data.exists()


async def test_a_half_finished_move_is_simply_finished(retro):
    furnish(retro.legacy_data)
    # Pretend `cores` and `roms` were moved and the bot was killed.
    (retro.data).mkdir(parents=True, exist_ok=True)
    for name in ("cores", "roms"):
        (retro.legacy_data / name).rename(retro.data / name)
    cog, _ = retro.make_cog(fresh_config=True)

    await cog._migrate_legacy_namespace()

    assert (retro.data / "states" / "8000-ucity.state").is_file()
    assert (retro.data / "system" / "some_bios.bin").is_file()
    assert (retro.data / "roms" / "8000-ucity.gbc").is_file()
    assert not retro.legacy_data.exists()


async def test_it_never_runs_twice(retro, pre_rename):
    cog, _, legacy_dir = pre_rename
    await cog._migrate_legacy_namespace()
    assert await cog.config.legacy_namespace_migrated() is True

    # Somebody changes a setting, then the cog loads again.
    await cog.config.clip_seconds.set(3)
    retro.legacy_store().globals["clip_seconds"] = 7
    await cog._migrate_legacy_namespace()
    assert await cog.config.clip_seconds() == 3, "the old value did not come back"


async def test_a_second_cog_on_the_same_data_copies_nothing_over_it(retro, pre_rename):
    cog, _, _ = pre_rename
    await cog._migrate_legacy_namespace()
    await cog.config.games.set({"newgame": "https://example.com/new.gb"})

    # A reload: the marker is gone (a failed write), but the new namespace
    # already has data, so the copy must refuse anyway.
    await cog.config.legacy_namespace_migrated.set(False)
    cog2, _ = retro.make_cog()
    await cog2._migrate_legacy_namespace()
    assert await cog2.config.games() == {"newgame": "https://example.com/new.gb"}


# -- Copying the settings -----------------------------------------------------


async def test_every_setting_and_session_comes_across(retro, pre_rename):
    cog, bot, _ = pre_rename
    await cog._migrate_legacy_namespace()

    assert await cog.config.games() == {"ucity": "https://example.com/ucity.gbc"}
    assert await cog.config.session_timeout_minutes() == 42
    assert await cog.config.clip_seconds() == 7
    assert await cog.config.hold_ms() == 250
    assert await cog.config.auto_download_cores() is False
    assert await cog.config.core_options() == {
        "gambatte": {"gambatte_gb_colorization": "GBC"}
    }
    channels = await cog.config.all_channels()
    assert channels[8000]["session"]["game_name"] == "ucity"


async def test_the_live_session_survives_the_whole_load(retro, pre_rename):
    cog, bot, _ = pre_rename
    channel = retro.channel(8000, bot=bot)
    await cog._migrate_legacy_namespace()
    await cog._restore_sessions()

    view = cog.sessions.get(channel.id)
    assert view is not None
    assert view.game_name == "ucity" and view.slug == "ucity"
    assert view.clip_seconds == 7 and view.hold_ms == 250
    # The ROM and its save state moved with it, so the session can wake up.
    assert cog._rom_path(view.rom_filename).is_file()
    assert cog._state_path(8000, "ucity").is_file()
    await view._press(retro.interaction(view, message=None), "a")
    assert view.live
    assert view.emulator.loaded_from == 1234, "it woke from its own save state"


async def test_a_setting_this_version_no_longer_has_is_left_behind(retro):
    furnish(retro.legacy_data)
    retro.legacy_store().globals["some_removed_setting"] = "whatever"
    retro.legacy_store().globals["games"] = {"old": "https://example.com/o.gb"}
    cog, _ = retro.make_cog(fresh_config=True)
    await cog._migrate_legacy_namespace()
    assert await cog.config.games() == {"old": "https://example.com/o.gb"}
    assert "some_removed_setting" not in cog.config.globals


# -- Failure is never fatal ---------------------------------------------------


async def test_a_read_only_data_folder_does_not_break_the_load(retro, pre_rename, caplog):
    import shutil

    cog, _, _ = pre_rename

    def refuse(source, target):
        raise PermissionError(13, "Permission denied", str(source))

    original = shutil.move
    shutil.move = refuse
    try:
        await cog.cog_load()
    finally:
        shutil.move = original
        await cog.cog_unload()

    # Nothing moved, nothing was destroyed, and the cog is up.
    assert (retro.legacy_data / "roms" / "8000-ucity.gbc").is_file()
    assert "could not move" in caplog.text.lower()


async def test_an_unreadable_legacy_config_does_not_break_the_load(retro):
    furnish(retro.legacy_data)
    cog, _ = retro.make_cog(fresh_config=True)
    retro.configs.broken = RuntimeError("the config database is down")
    try:
        await cog._migrate_legacy_namespace()
    finally:
        retro.configs.broken = None
    # The files still came across; only the settings could not be read.
    assert (retro.data / "roms" / "8000-ucity.gbc").is_file()


async def test_a_config_that_refuses_everything_does_not_break_the_load(retro):
    furnish(retro.legacy_data)
    cog, _ = retro.make_cog(fresh_config=True)

    class AngryConfig:
        def __getattr__(self, name):
            raise RuntimeError("config is down")

    cog.config = AngryConfig()
    await cog._migrate_legacy_namespace()  # must not raise


async def test_cog_load_survives_a_migration_that_explodes(retro, caplog):
    cog, _ = retro.make_cog(fresh_config=True)

    async def boom():
        raise RuntimeError("everything is on fire")

    cog._migrate_legacy_namespace = boom
    await cog.cog_load()
    await cog.cog_unload()
    assert "failed to migrate" in caplog.text.lower()


# -- Against the real Red-DiscordBot ------------------------------------------


@pytest.mark.redbot
def test_red_really_does_offer_both_escape_hatches():
    # The whole migration rests on these two keyword arguments existing.
    import inspect

    from redbot.core import Config
    from redbot.core.data_manager import cog_data_path

    assert "cog_name" in inspect.signature(Config.get_conf).parameters
    assert "raw_name" in inspect.signature(cog_data_path).parameters


@pytest.mark.redbot
def test_reds_json_driver_keeps_its_store_inside_the_data_folder(retro, tmp_path):
    """Why _migrate_data_directory has to skip exactly one filename.

    With Red's default driver a cog's data folder and its Config namespace
    are the *same folder*, so a naive "move everything" would drop the old
    namespace's settings straight on top of the new one's. Asked of the
    driver rather than read out of the text of ``redbot.core._drivers.json``,
    which is a private module whose wording is nobody's contract.
    """
    from redbot.core._drivers import JsonDriver
    from redbot.core.data_manager import cog_data_path

    driver = JsonDriver("Retro", "1925", data_path_override=tmp_path)
    assert driver.data_path.name == retro.cogmod.CONFIG_STORE_FILENAME
    assert driver.data_path.parent == tmp_path
    # ...and with no override it really is the cog's own data directory.
    assert JsonDriver("Retro", "1925").data_path.parent == cog_data_path(
        raw_name="Retro"
    )


@pytest.mark.redbot
def test_the_cog_registers_itself_under_the_new_name(retro):
    # What [p]help and [p]cog list show, and what cog_data_path() uses.
    assert type(retro.cog).__name__ == "Retro"
    assert retro.cogmod.Retro.__module__ == "retro.Retro"


@pytest.mark.redbot
def test_there_is_no_retroset_core_command_any_more(retro):
    walked = {
        command.qualified_name
        for top in retro.cogmod.Retro.__cog_commands__
        for command in [top, *getattr(top, "walk_commands", lambda: ())()]
    }
    assert "retroset core" not in walked
    assert "retroset download" in walked
    assert "retroset bios add" in walked
