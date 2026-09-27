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

from pathlib import Path

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


async def test_a_fresh_install_does_not_write_the_core_paths_back(retro, monkeypatch):
    """The path rewrite is gated on there having been an old namespace at all.

    ``_rewrite_core_paths`` opens ``config.cores()`` as a context manager, and
    Red writes a group back when that context exits whether or not anything in
    it changed -- on the default (JSON) driver that is a serialise of the whole
    settings file. It used to run on every load of every install, gated on
    nothing, while ``_migrate_config`` beside it was gated on ``had_legacy``:
    on an install that never ran under the old name every stored path raised
    ValueError against a legacy directory that never existed, so the loop did
    precisely nothing and the write happened anyway, inside ``cog_load``.
    """
    from . import fakes

    cog, _ = retro.make_cog(fresh_config=True)
    assert not retro.legacy_data.exists()
    # Something to rewrite, so the test cannot pass merely because `cores` was
    # empty: this path is outside the old folder, which is the ordinary case.
    await cog.config.cores.set({"mgba": "/opt/retroarch/mgba_libretro.so"})

    groups = []
    original = fakes.FakeValue.__aexit__

    async def counted(self, *exc):
        groups.append(self.key)
        return await original(self, *exc)

    monkeypatch.setattr(fakes.FakeValue, "__aexit__", counted)

    await cog._migrate_legacy_namespace()

    assert "cores" not in groups, groups
    assert await cog.config.cores() == {"mgba": "/opt/retroarch/mgba_libretro.so"}
    # ...and the install that *did* run under the old name still gets the
    # rewrite, which is the half that would be silent data loss to drop; see
    # test_the_stored_core_paths_are_repointed_at_the_new_folder above.


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
    from retro.emulator import clamp_clip_seconds

    cog, bot, _ = pre_rename
    channel = retro.channel(8000, bot=bot)
    await cog._migrate_legacy_namespace()
    await cog._restore_sessions()

    view = cog.sessions.get(channel.id)
    assert view is not None
    assert view.game_name == "ucity" and view.slug == "ucity"
    # The old install's seven-second clip, through the clamp a restored
    # session puts every stored length through -- the copy hands the value
    # over unchanged (the setting itself is asserted above), and what the
    # session runs at is whatever today's ceiling allows. Written this way
    # so moving that ceiling is not a failure in the *migration* tests.
    assert view.clip_seconds == clamp_clip_seconds(7)
    assert view.hold_ms == 250
    # The ROM and its save state moved with it, so the session can wake up.
    assert cog._rom_path(view.rom_filename).is_file()
    assert cog._state_path(8000, "ucity").is_file()
    await view._press(retro.interaction(view, message=None), "a")
    assert view.live
    assert view.emulator.loaded_from == 1234, "it woke from its own save state"


async def test_the_settings_are_copied_one_scope_at_a_time(retro, monkeypatch):
    """One Config write per scope, not one per setting.

    Red's default (JSON) driver serialises the *whole* settings file on
    every set(), so copying key by key cost one whole-file write per global
    and per channel key. On a bot with hundreds of channels that is hundreds
    of serialise-the-world writes, inside cog_load, before the cog answers
    anything.
    """
    from . import fakes

    legacy_dir = furnish(retro.legacy_data)
    store = retro.legacy_store()
    furnish_legacy_config(store, legacy_dir)
    session = store.channels[8000]["session"]
    for channel_id in range(8001, 8021):
        store.channels[channel_id] = {
            "session": dict(session, channel_id=channel_id),
            "retired": {},
        }
    cog, _ = retro.make_cog(fresh_config=True)

    writes = []
    for cls in (fakes.FakeValue, fakes.FakeScope, fakes.FakeConfig):
        original = cls.set

        async def counted(self, value, _original=original, _kind=cls.__name__):
            writes.append(_kind)
            await _original(self, value)

        monkeypatch.setattr(cls, "set", counted)

    copied = await cog._migrate_config()

    # 7 globals, one channel with a session and twenty with a session and a
    # retired table: 48 settings, which the old copy paid 48 writes for.
    assert copied == 7 + 1 + 20 * 2
    # The globals in one write, then one per channel. Nothing else.
    assert len(writes) == 1 + 21, writes
    assert "FakeValue" not in writes, "a single-key write is a whole-file write"
    # ...and everything really did come across.
    assert await cog.config.games() == {"ucity": "https://example.com/ucity.gbc"}
    channels = await cog.config.all_channels()
    assert len(channels) == 21
    assert channels[8020]["session"]["game_name"] == "ucity"


async def test_a_scope_config_will_not_take_whole_is_copied_key_by_key(retro, caplog):
    """A refused batch costs the batch, not the settings in it.

    The copy is one write per scope now, so the failure it has to survive is
    a scope that will not go in one piece -- a driver that objects to one
    value in it, say. It falls back to the old key-at-a-time write, so
    everything Config will accept still lands.
    """

    class WholeScopeRefused:
        """A Config handle that only accepts one key at a time."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        async def set(self, value):
            raise RuntimeError("this driver will not take a whole group")

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    legacy_dir = furnish(retro.legacy_data)
    furnish_legacy_config(retro.legacy_store(), legacy_dir)
    cog, _ = retro.make_cog(fresh_config=True)
    real = cog.config
    cog.config = WholeScopeRefused(real)

    copied = await cog._migrate_config()

    assert copied == 7 + 1, "every global one at a time, and the one channel"
    assert await real.games() == {"ucity": "https://example.com/ucity.gbc"}
    assert await real.clip_seconds() == 7
    assert "one at a time" in caplog.text
    # The channels are reached through channel_from_id(), which is delegated
    # to the real handle, so they were unaffected by the refusal above.
    assert (await real.all_channels())[8000]["session"]["game_name"] == "ucity"


async def test_one_impossible_channel_does_not_cost_the_others(retro, caplog):
    """Per-channel granularity: the loop keeps going.

    One channel Config cannot write, and one key that is not a channel id at
    all (which no version of this cog wrote, but the old namespace is
    somebody else's data and may hold anything).
    """

    class Unwritable:
        def __init__(self, reason):
            self.reason = reason

        async def set(self, value):
            raise RuntimeError(self.reason)

        def __getattr__(self, name):
            raise RuntimeError(self.reason)

    legacy_dir = furnish(retro.legacy_data)
    store = retro.legacy_store()
    furnish_legacy_config(store, legacy_dir)
    session = store.channels[8000]["session"]
    store.channels[8001] = {"session": dict(session, channel_id=8001)}
    store.channels["not-a-channel-id"] = {"session": dict(session)}
    cog, _ = retro.make_cog(fresh_config=True)

    real = cog.config.channel_from_id

    def refuse_8000(channel_id):
        if int(channel_id) == 8000:
            return Unwritable("this channel cannot be written")
        return real(channel_id)

    cog.config.channel_from_id = refuse_8000

    copied = await cog._migrate_config()

    assert copied == 7 + 1, "the globals and the one channel that could be written"
    channels = await cog.config.all_channels()
    assert sorted(channels) == [8001], channels
    assert "not-a-channel-id" in caplog.text
    assert "this channel cannot be written" in caplog.text


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
    # And the migration is not over: see the partial-move tests below.
    assert await cog.config.legacy_namespace_migrated() is False


# -- A partial move is not a finished one -------------------------------------
#
# The marker makes this write-once code, so recording a migration that only
# half happened strands the other half under a namespace nothing reads any
# more -- permanently, with one log warning as the only trace. Moving is
# deliberately per-entry and forgiving (one core file held open by another
# process must not stop the saves coming across), which is exactly what makes
# the gate on the marker load-bearing. See migration.MoveResult.


async def test_one_entry_that_will_not_move_does_not_end_the_migration(retro, caplog):
    """The regression: `states` is locked, so the migration stays unfinished.

    Everything movable still moves -- that is the point of going entry by
    entry -- but the run does not get to call itself done while somebody's save
    states are still sitting in the old folder.
    """
    import shutil

    furnish(retro.legacy_data)
    cog, _ = retro.make_cog(fresh_config=True)
    original = shutil.move

    def refuse_states(source, target):
        if Path(source).name == "states":
            raise PermissionError(13, "Permission denied", str(source))
        return original(source, target)

    shutil.move = refuse_states
    try:
        await cog._migrate_legacy_namespace()
    finally:
        shutil.move = original

    # The movable entries are across...
    assert (retro.data / "roms" / "8000-ucity.gbc").read_bytes() == ROM_BYTES
    assert (retro.data / "cores" / "gambatte_libretro.so").is_file()
    # ...the locked one is untouched where it was, not half-copied...
    assert (retro.legacy_data / "states" / "8000-ucity.state").read_bytes() == b"STATE:1234"
    assert (retro.legacy_data / "states" / "8000-ucity.srm").read_bytes() == b"\xff" * 512
    # ...and the migration has NOT been recorded, so it runs again.
    assert await cog.config.legacy_namespace_migrated() is False
    assert "left" in caplog.text.lower()


async def test_the_next_load_picks_up_what_was_left_behind(retro):
    """And because it runs again, the save states arrive in the end.

    The whole reason a partial move may be left unmarked: every step of this is
    safe to repeat, so the retry costs a directory listing and rescues the
    entry whose lock has since gone away.
    """
    import shutil

    furnish(retro.legacy_data)
    cog, _ = retro.make_cog(fresh_config=True)
    original = shutil.move

    def refuse_states(source, target):
        if Path(source).name == "states":
            raise PermissionError(13, "Permission denied", str(source))
        return original(source, target)

    shutil.move = refuse_states
    try:
        await cog._migrate_legacy_namespace()
    finally:
        shutil.move = original

    # Second load, nothing locked any more.
    await cog._migrate_legacy_namespace()

    assert (retro.data / "states" / "8000-ucity.state").read_bytes() == b"STATE:1234"
    assert (retro.data / "states" / "8000-ucity.srm").read_bytes() == b"\xff" * 512
    assert not retro.legacy_data.exists()
    assert await cog.config.legacy_namespace_migrated() is True


async def test_an_entry_the_new_folder_already_had_is_finished_not_retried(retro):
    """A deliberate leave-behind must not hold the marker open forever.

    `cores` existing at both ends is a question that has been *answered* --
    the new copy wins -- so it is not an unfinished move. Counting it as one
    would mean the migration never records itself and logs the same warning on
    every load the bot ever does.
    """
    furnish(retro.legacy_data)
    (retro.data / "cores").mkdir(parents=True, exist_ok=True)
    (retro.data / "cores" / "gambatte_libretro.so").write_bytes(b"\x7fELF newer")
    cog, _ = retro.make_cog(fresh_config=True)

    await cog._migrate_legacy_namespace()

    assert (retro.legacy_data / "cores").is_dir(), "the old copy is kept on purpose"
    assert await cog.config.legacy_namespace_migrated() is True


async def test_an_old_folder_that_cannot_be_listed_is_not_finished(retro, caplog):
    """If its contents cannot be read, there is no basis for calling it done."""
    furnish(retro.legacy_data)
    cog, _ = retro.make_cog(fresh_config=True)
    legacy = retro.legacy_data.resolve()
    original = Path.iterdir

    def refuse(self):
        if self.resolve() == legacy:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self)

    Path.iterdir = refuse
    try:
        await cog._migrate_legacy_namespace()
    finally:
        Path.iterdir = original

    assert (retro.legacy_data / "roms" / "8000-ucity.gbc").is_file()
    assert await cog.config.legacy_namespace_migrated() is False
    assert "could not read the old" in caplog.text.lower()


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
def test_red_really_does_write_a_whole_scope_in_one_call():
    """What the batched copy rests on; see MigrationMixin._copy_settings.

    A channel scope is a ``Group``, and a Group is a ``Value``, so
    ``group.set({...})`` writes the lot. The globals are the same group Red
    hands out for attribute access on the Config itself, which is what the
    key-at-a-time copy always used -- so ``config.set({...})`` is that
    group's set(). If Config ever grows a ``set`` of its own, this stops
    being true and the globals half of the copy has to find the global group
    another way.
    """
    import inspect

    from redbot.core.config import Config, Group, Value

    assert issubclass(Group, Value)
    assert inspect.iscoroutinefunction(Group.set)
    assert "set" not in vars(Config), sorted(vars(Config))
    assert callable(getattr(Config, "__getattr__", None))


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
