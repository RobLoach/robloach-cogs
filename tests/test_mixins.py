"""The cog is several modules and one class, and the fakes reach all of them.

`Retro` was one 6,000 line module and is now a cog class assembled from
mixins (retro/storage.py, retro/cores.py, retro/saves.py,
retro/migration.py; see retro/abc.py for the scaffolding). Two things about
that are load-bearing enough to be asserted rather than assumed:

* **the class must not move or change name.** Red derives both the Config
  namespace and the data directory from ``type(self).__name__``, so a cog
  called anything but `Retro`, or living anywhere but ``retro/Retro.py``,
  orphans every existing install's settings, cores, ROMs and save states.
  The commands and the Config keys are pinned here for the same reason: they
  are what is already on other people's disks and in their muscle memory.

* **a fake has to be installed on every module that looks the name up.** A
  module resolves its globals in its own namespace, so a stand-in patched
  onto ``retro.Retro`` alone stops intercepting the moment the code using it
  moves into a mixin -- and the fixture keeps passing while testing nothing.
  That has happened twice in this repository, so the tests below prove the
  fakes are reached rather than merely installed.
"""

import inspect
import sys

import pytest

from .loader import REPO_ROOT

pytest.importorskip("discord", reason="these tests build the real cog")

from . import fakes  # noqa: E402
from .fakes import FakeConfig, FakeEmulator, FakeMenu  # noqa: E402

# -- 1. One class, called Retro, in retro/Retro.py ----------------------------


def test_the_cog_class_is_still_one_class_called_retro(retro):
    assert type(retro.cog).__name__ == "Retro", "Red keys every install off this"
    assert type(retro.cog).__module__ == "retro.Retro"
    assert sys.modules["retro"].Retro is type(retro.cog)


def test_the_cog_is_assembled_from_the_mixins(retro):
    names = [base.__name__ for base in type(retro.cog).__mro__]
    for mixin in ("MigrationMixin", "StorageMixin", "CoresMixin", "SavesMixin"):
        assert mixin in names, names
    # And the mixins really are where the work lives now, rather than being
    # empty shells with everything still in Retro.py.
    assert type(retro.cog)._data_dir.__qualname__.startswith("StorageMixin")
    assert type(retro.cog)._core_path.__qualname__.startswith("CoresMixin")
    assert type(retro.cog)._saved_games.__qualname__.startswith("SavesMixin")
    assert type(retro.cog)._migrate_config.__qualname__.startswith("MigrationMixin")


def test_every_mixin_module_exists_and_is_imported():
    for name in fakes.COG_MODULES:
        assert name in sys.modules, name
        assert (REPO_ROOT / "retro" / f"{name.split('.')[-1]}.py").is_file(), name


# -- 2. The surface other people's installs already depend on -----------------

#: Every command the cog publishes, subcommands included. Pinned: a command
#: that vanished in a refactor is a command somebody's muscle memory just
#: lost, and a group whose subcommand failed to attach fails silently.
COMMANDS = {
    "retro",
    # Reboots the running game. Deliberately not a button: see the note
    # above _STYLES in retro/RetroView.py.
    "retroreset",
    "retrosaves",
    "retrosaves delete",
    "retrosaves export",
    "retrosaves import",
    "retrosaves info",
    "retrosaves list",
    "retrosaves reset",
    "retrosaves rollback",
    "retroset",
    "retroset allowprivateurls",
    "retroset autodownload",
    "retroset bios",
    "retroset bios add",
    "retroset bios list",
    "retroset bios remove",
    "retroset cliplength",
    "retroset coreoptions",
    "retroset diskbudget",
    "retroset download",
    "retroset game",
    "retroset game add",
    "retroset game list",
    "retroset game remove",
    "retroset hold",
    "retroset settings",
    "retroset timeout",
    "retroset version",
    "retrostop",
}

#: The registered Config keys. Every one of these is already written into
#: somebody's settings, so a key that moved or vanished is their data gone.
GLOBAL_KEYS = {
    "allow_private_urls",
    "auto_download_attempted_at",
    "auto_download_cores",
    "clip_seconds",
    "core_option_definitions",
    "core_options",
    "core_path",
    "cores",
    "disk_budget_mb",
    "games",
    "hold_ms",
    "legacy_namespace_migrated",
    "session_timeout_minutes",
}
CHANNEL_KEYS = {"retired", "session"}

#: The Discord events the cog listens for. Pinned because a listener that
#: silently stops being registered is invisible: nothing fails, the records
#: it was there to delete simply accumulate for ever again. Each one drops
#: the *pointers* a channel could be resumed from and keeps every save; see
#: the note above ``Retro._channel_is_gone``.
LISTENERS = {
    "on_guild_channel_delete",
    "on_guild_remove",
    "on_thread_delete",
}


@pytest.mark.redbot
def test_the_command_surface_is_exactly_what_it_was(retro):
    published = {command.qualified_name for command in type(retro.cog).__cog_commands__}
    assert published == COMMANDS


@pytest.mark.redbot
def test_every_subcommand_is_attached_to_its_group(retro):
    # A group and its subcommands can only be declared together, because
    # discord.py registers a subcommand by calling a decorator on the parent
    # Group object. If that ever stopped holding, `[p]retrosaves list` would
    # simply not exist while every other test still passed.
    by_name = {c.qualified_name: c for c in type(retro.cog).__cog_commands__}
    for name in COMMANDS:
        head, _, tail = name.rpartition(" ")
        if head:
            assert by_name[name].parent is by_name[head], name


def test_every_listener_exists_and_is_a_coroutine(retro):
    for name in LISTENERS:
        method = getattr(type(retro.cog), name, None)
        assert method is not None, name
        assert inspect.iscoroutinefunction(method), name


@pytest.mark.redbot
def test_the_listeners_are_exactly_what_they_were(retro):
    """And that Red really registers them, which the stub cannot show.

    ``tests/stubs``' ``Cog.listener`` is a passthrough -- the tests call a
    listener directly, as they call a command's callback -- so this is the
    only place the decorator's actual effect is checked.
    """
    registered = {name for name, _ in retro.cog.get_listeners()}
    assert registered == LISTENERS
    for name, method in retro.cog.get_listeners():
        assert method.__self__ is retro.cog, name


def test_the_config_schema_is_exactly_what_it_was(retro):
    assert set(retro.cog.config._global_defaults) == GLOBAL_KEYS
    assert set(retro.cog.config._channel_defaults) == CHANNEL_KEYS
    assert set(retro.cogmod.DEFAULT_GLOBALS) == GLOBAL_KEYS
    assert set(retro.cogmod.DEFAULT_CHANNEL) == CHANNEL_KEYS


def test_the_constants_that_moved_are_still_importable_from_retro_retro(retro):
    # Tests, the harness and the cog's own modules read several of these
    # through `retro.Retro`; they are re-exported rather than copied, so
    # there is still exactly one of each.
    assert retro.cogmod.MAX_CACHED_GAMES_PER_CHANNEL is (
        retro.storagemod.MAX_CACHED_GAMES_PER_CHANNEL
    )
    assert retro.cogmod.BACKUP_SUFFIX is retro.storagemod.BACKUP_SUFFIX
    assert retro.cogmod.OPTION_RESET is retro.coresmod.OPTION_RESET
    assert retro.cogmod.SRAM_EXTENSIONS is retro.savesmod.SRAM_EXTENSIONS
    assert retro.cogmod.LEGACY_COG_NAME is retro.migrationmod.LEGACY_COG_NAME
    assert retro.cogmod.SaveInfo is retro.savesmod.SaveInfo
    # Derived here, from the storage module's number. Same value, one source.
    assert (
        retro.cogmod.MAX_RETIRED_PER_CHANNEL
        == retro.storagemod.MAX_CACHED_GAMES_PER_CHANNEL
    )


# -- 3. The fakes are reached, not merely installed ---------------------------

#: name -> the modules that must have been patched with the fake, because
#: each of them contains code that looks the name up. A *smaller* answer than
#: this means a fixture has gone blind; a larger one is fine (a module that
#: only mentions the name in a type annotation is harmless to patch).
MUST_REACH = {
    "cog_data_path": {"retro.Retro", "retro.storage", "retro.migration"},
    "Config": {"retro.Retro", "retro.migration"},
    "RetroEmulator": {"retro.Retro", "retro.saves"},
    "SimpleMenu": {"retro.Retro"},
    "ConfirmView": {"retro.saves"},
}


def test_each_fake_reaches_the_modules_whose_code_uses_it(retro):
    for name, expected in MUST_REACH.items():
        assert expected <= set(retro.patched[name]), (name, retro.patched[name])


def test_no_module_binding_a_faked_name_escapes_the_fake(retro):
    """The whole point: not one namespace is left holding the real thing."""
    for name, fake in retro.fakes.items():
        binding = tuple(
            module
            for module in fakes.COG_MODULES
            if name in vars(sys.modules[module])
        )
        assert binding, f"nothing binds {name!r} any more"
        assert binding == retro.patched[name], name
        for module in binding:
            assert getattr(sys.modules[module], name) is fake, (name, module)


async def test_the_fake_data_path_is_the_one_every_module_actually_calls(retro):
    """Not identity but effect: each module's call lands under tmp_path."""
    retro.data_path_calls.clear()

    # retro/storage.py
    assert retro.cogs_root in retro.cog._data_dir("roms").parents
    # retro/migration.py
    assert retro.cogs_root in retro.cog._legacy_data_dir().parents
    # retro/Retro.py
    report = await retro.cog._usage_report(retro.context(retro.channel(9990)))
    assert str(retro.cogs_root) in report

    assert len(retro.data_path_calls) >= 3, retro.data_path_calls


def test_the_fake_config_is_the_one_both_namespaces_come_from(retro):
    assert isinstance(retro.cog.config, FakeConfig)          # retro/Retro.py
    assert isinstance(retro.cog._legacy_config(), FakeConfig)  # retro/migration.py
    assert retro.cog._legacy_config().cog_name == retro.cogmod.LEGACY_COG_NAME


async def test_the_fake_emulator_is_the_one_the_saves_module_boots(retro):
    """`[p]retrosaves import` validates against a core, and it is the fake."""
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9991)
    assert view is not None and view.live, "the game did not start"
    entry = next(e for e in await retro.cog._saved_games(channel.id) if e.current)
    before = len(FakeEmulator.instances)

    # _check_import lives in retro/saves.py and builds an emulator of its
    # own; the real one would try to dlopen a 14-byte fake core.
    assert await retro.cog._check_import(entry, None, b"\x00" * 8) is not None
    assert len(FakeEmulator.instances) > before, "saves.py booted the real thing"


async def test_the_fake_menu_is_the_one_the_paginator_uses(retro):
    FakeMenu.last_pages = []
    ctx = retro.context(retro.channel(9992))
    await retro.cog._send_pages(ctx, "\n\n".join(f"page body {i}" * 60 for i in range(6)))
    assert FakeMenu.last_pages, "SimpleMenu was not intercepted"
