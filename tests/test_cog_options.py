"""Core options: discovery, caching, key resolution, validation, application.

The definition shapes here are copied from what the real cores report
(gambatte 32 options, fceumm 0 until a ROM is loaded and then 44); the real
cores are read in test_emulator.py, which is where those numbers come from.
"""

from pathlib import Path

import pytest

pytest.importorskip("discord", reason="the cog tests need discord.py")

from .fakes import NES_BYTES, FakeEmulator  # noqa: E402

GAMBATTE_DEFS = {
    "gambatte_gb_colorization": {
        "desc": "GB Colorization",
        "info": "Enables colorization of Game Boy games.",
        "default": "disabled",
        "values": [
            ["disabled", "disabled"], ["auto", "Auto"], ["GBC", "GBC"],
            ["SGB", "SGB"], ["internal", "Internal"], ["custom", "Custom"],
        ],
    },
    "gambatte_gb_internal_palette": {
        "desc": "Internal Palette",
        "info": "Selects the color palette.",
        "default": "GB - DMG",
        "values": [["GB - DMG", "GB - DMG"], ["GB - Pocket", "GB - Pocket"]],
    },
    "gambatte_gbc_color_correction": {
        "desc": "Color Correction",
        "info": "",
        "default": "GBC only",
        "values": [["GBC only", "GBC only"], ["always", "Always"], ["disabled", "Disabled"]],
    },
}

FCEUMM_DEFS = {
    "fceumm_region": {
        "desc": "Region",
        "info": "Force core to use NTSC, PAL or Dendy region timings.",
        "default": "Auto",
        "values": [["Auto", "Auto"], ["NTSC", "NTSC"], ["PAL", "PAL"], ["Dendy", "Dendy"]],
    },
    "fceumm_game_genie": {
        "desc": "Game Genie Add-On",
        "info": "Enables the Game Genie add-on.",
        "default": "disabled",
        "values": [["disabled", "disabled"], ["enabled", "enabled"]],
    },
    "fceumm_sndquality": {
        "desc": "Sound Quality",
        "info": "Enable higher quality sound. Increases performance requirements.",
        "default": "Low",
        "values": [["Low", "Low"], ["High", "High"], ["Very High", "Very High"]],
    },
}


@pytest.fixture
async def options(retro):
    """The coreoptions command, with gambatte and fceumm installed."""
    await retro.install_cores("gambatte", "fceumm")
    await retro.cog.config.core_options.set({})
    await retro.cog.config.core_option_definitions.set({})
    return retro.cogmod.RetroCog.retroset_coreoptions.callback


# -- The overview -------------------------------------------------------------


async def test_the_overview_names_the_installed_cores_without_judging_them(retro, options):
    ctx = retro.context(retro.channel(9700))
    await options(retro.cog, ctx)
    said = ctx.said()
    assert "`gambatte`" in said and "`fceumm`" in said
    assert "not read yet" in said
    assert "does **not** mean" in said or "not a core without options" in said


async def test_an_unknown_core_is_reported(retro, options):
    ctx = retro.context(retro.channel(9701))
    await options(retro.cog, ctx, core="nosuchcore")
    assert "is not a core this cog knows about" in ctx.said()


# -- Reading the options off a core -------------------------------------------


async def test_the_probe_puts_a_running_game_to_sleep_first(retro, options, monkeypatch):
    # Only one core may be loaded at a time, so reading gambatte's options
    # while a NES game is running means the NES game has to come down first.
    channel = retro.channel(9702)
    playing = await retro.start_game(
        retro.context(channel), "probegame", data=NES_BYTES, filename="probegame.nes"
    )
    assert playing.live and playing.core == "fceumm"

    probed = []

    def fake_probe(core_path, opts=None):
        probed.append((Path(core_path).name, dict(opts or {})))
        assert not any(v.live for v in retro.cog.sessions.values()), (
            "the probe ran with another core still loaded"
        )
        return dict(GAMBATTE_DEFS) if "gambatte" in Path(core_path).name else {}

    monkeypatch.setattr(retro.cogmod, "probe_core_options", fake_probe)

    ctx = retro.context(channel)
    await options(retro.cog, ctx, core="gambatte")
    assert probed and probed[0][0].startswith("gambatte")
    assert not playing.live
    assert retro.cog._state_path(channel.id, playing.slug).is_file()
    assert len(await retro.cog._cached_definitions("gambatte")) >= len(GAMBATTE_DEFS)


async def test_the_listing_shows_keys_values_defaults_and_provenance(retro, options, monkeypatch):
    monkeypatch.setattr(
        retro.cogmod, "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS)
    )
    ctx = retro.context(retro.channel(9703))
    await options(retro.cog, ctx, core="gambatte")
    said = ctx.said()
    assert "**gb_colorization**" in said, "the short key"
    assert "`gambatte_gb_colorization`" in said, "and the full one"
    assert "= `disabled`" in said
    assert "*(default)*" in said
    assert "`GBC`" in said and "`SGB`" in said
    assert "the core itself" in said


async def test_a_running_game_answers_for_its_own_core_with_no_probe(retro, options, monkeypatch):
    probed = []
    monkeypatch.setattr(
        retro.cogmod,
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or {},
    )
    FakeEmulator.definitions_by_core = {"gambatte": GAMBATTE_DEFS}
    channel = retro.channel(9704)
    await retro.start_game(retro.context(channel), "livegame")

    ctx = retro.context(channel)
    await options(retro.cog, ctx, core="gambatte")
    assert not probed
    assert "the game running right now" in ctx.said()
    assert len(await retro.cog._cached_definitions("gambatte")) >= len(GAMBATTE_DEFS)


async def test_a_cached_listing_needs_no_probe_at_all(retro, options, monkeypatch):
    probed = []
    monkeypatch.setattr(
        retro.cogmod,
        "probe_core_options",
        lambda path, opts=None: (probed.append(path), dict(GAMBATTE_DEFS))[1],
    )
    await options(retro.cog, retro.context(retro.channel(9705)), core="gambatte")
    assert probed, "the first listing reads the core"

    probed.clear()
    await options(retro.cog, retro.context(retro.channel(9706)), core="gambatte")
    assert not probed


async def test_one_option_is_shown_in_full(retro, options, monkeypatch):
    monkeypatch.setattr(
        retro.cogmod, "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS)
    )
    ctx = retro.context(retro.channel(9707))
    await options(retro.cog, ctx, core="gambatte", key="gb_colorization")
    said = ctx.said()
    assert "`gambatte_gb_colorization`" in said
    assert "GB Colorization" in said
    assert "Enables colorization" in said
    assert "Current: `disabled`" in said
    assert "Default: `disabled`" in said
    assert all(f"`{value}`" in said for value in ("auto", "GBC", "SGB", "internal", "custom"))


# -- Key resolution -----------------------------------------------------------


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("gambatte_gb_colorization", "gambatte_gb_colorization"),   # exact
        ("gb_colorization", "gambatte_gb_colorization"),            # <core>_<key>
        ("GB_COLORIZATION", "gambatte_gb_colorization"),            # case
        ("`gb_colorization`", "gambatte_gb_colorization"),          # backticks
        ("color_correction", "gambatte_gbc_color_correction"),      # unique suffix
    ],
)
def test_an_option_key_can_be_typed_several_ways(retro, typed, expected):
    key, error = retro.cog._resolve_option_key("gambatte", GAMBATTE_DEFS, typed, "!")
    assert key == expected and error is None, (typed, key, error)


def test_an_ambiguous_suffix_is_refused_rather_than_guessed(retro):
    # Two options end in `_colorization` and neither is
    # `gambatte_colorization`, so there is nothing to prefer between them.
    definitions = dict(GAMBATTE_DEFS)
    definitions["gambatte_gbc_colorization"] = {"default": "off", "values": [["off", "Off"]]}
    key, error = retro.cog._resolve_option_key("gambatte", definitions, "colorization", "!")
    assert key is None
    assert error and "matches 2" in error
    assert "gb_colorization" in error and "gbc_colorization" in error


def test_an_exact_core_key_beats_a_fuzzy_suffix_match(retro):
    definitions = dict(GAMBATTE_DEFS)
    definitions["gambatte_mix_frames"] = {"default": "disabled", "values": [["disabled", "x"]]}
    definitions["gambatte_gb_mix_frames"] = {"default": "disabled", "values": [["disabled", "x"]]}
    key, error = retro.cog._resolve_option_key("gambatte", definitions, "mix_frames", "!")
    assert key == "gambatte_mix_frames" and error is None


def test_an_unknown_key_is_reported(retro):
    key, error = retro.cog._resolve_option_key("gambatte", GAMBATTE_DEFS, "nonsense", "!")
    assert key is None and error and "no option called" in error


def test_a_core_prefix_is_never_assumed_when_nothing_is_known(retro):
    # mednafen_ngp names its own option `ngp_language`, so guessing a prefix
    # from the core name would be wrong.
    key, error = retro.cog._resolve_option_key("mednafen_ngp", {}, "language", "!")
    assert key is None and error is None


def test_effective_values_fall_back_exactly_as_libretro_does(retro):
    colorization = GAMBATTE_DEFS["gambatte_gb_colorization"]
    cog = retro.cogmod.RetroCog
    assert cog._option_default(colorization) == "disabled"
    assert cog._effective_value(colorization, None) == "disabled"
    assert cog._effective_value(colorization, "GBC") == "GBC"
    # A value the core does not offer is not what the core will read.
    assert cog._effective_value(colorization, "rainbow") == "disabled"
    # A definition with no declared default uses its first value.
    assert cog._option_default({"default": "", "values": [["a", "A"]]}) == "a"


def test_short_keys_only_lose_a_prefix_that_is_really_there(retro):
    cog = retro.cogmod.RetroCog
    assert cog._short_key("gambatte", "gambatte_gb_colorization") == "gb_colorization"
    assert cog._short_key("fceumm", "ngp_language") == "ngp_language"


def test_the_reset_sentinel_is_not_something_a_core_would_offer(retro):
    # `[p]retroset coreoptions <core> <key> reset` puts a core's default
    # back, so the sentinel must not also be a real value. "default" and
    # "none" are both out already (mednafen_wswan offers the first, snes9x
    # and genesis_plus_gx the second); test_buildbot.py checks every shipped
    # core against this.
    assert retro.cogmod.OPTION_RESET == "reset"
    assert retro.cogmod.OPTION_RESET not in ("default", "none", "off", "disabled")


# -- Setting a value ----------------------------------------------------------


@pytest.fixture
async def gambatte_known(retro, options, monkeypatch):
    """The command, with gambatte's options already cached."""
    monkeypatch.setattr(
        retro.cogmod, "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS)
    )
    await options(retro.cog, retro.context(retro.channel(9710)), core="gambatte")
    return options


async def test_an_override_is_stored_under_the_full_key_in_the_core_s_spelling(
    retro, gambatte_known
):
    ctx = retro.context(retro.channel(9711))
    await gambatte_known(retro.cog, ctx, core="gambatte", key="gb_colorization", value="gbc")
    stored = await retro.cog._core_options("gambatte")
    assert stored.get("gambatte_gb_colorization") == "GBC"
    said = ctx.said()
    assert "GBC" in said and "gbc`" not in said
    assert "next time a game starts" in said


async def test_an_invalid_value_is_refused_with_the_valid_ones(retro, gambatte_known):
    ctx = retro.context(retro.channel(9712))
    await gambatte_known(retro.cog, ctx, core="gambatte", key="gb_colorization", value="GBC")
    ctx2 = retro.context(retro.channel(9713))
    await gambatte_known(retro.cog, ctx2, core="gambatte", key="gb_colorization", value="rainbow")
    said = ctx2.said()
    assert "is not something" in said
    assert all(f"`{value}`" in said for value in ("disabled", "auto", "GBC"))
    assert (await retro.cog._core_options("gambatte")).get("gambatte_gb_colorization") == "GBC"


async def test_an_override_reaches_the_next_emulator_and_the_running_one(retro, gambatte_known):
    ctx = retro.context(retro.channel(9714))
    await gambatte_known(retro.cog, ctx, core="gambatte", key="gb_colorization", value="GBC")

    view, _, _ = await retro.posted_game(9715, "optiongame")
    assert view.emulator.options.get("gambatte_gb_colorization") == "GBC"
    assert view.emulator.option_value("gambatte_gb_colorization") == "GBC"

    ctx2 = retro.context(retro.channel(9716))
    await gambatte_known(retro.cog, ctx2, core="gambatte", key="gb_colorization", value="SGB")
    assert view.emulator.option_value("gambatte_gb_colorization") == "SGB"
    assert "told about it now" in ctx2.said()


async def test_reset_puts_the_core_s_default_back_and_is_idempotent(retro, gambatte_known):
    ctx = retro.context(retro.channel(9717))
    await gambatte_known(retro.cog, ctx, core="gambatte", key="gb_colorization", value="GBC")

    ctx2 = retro.context(retro.channel(9718))
    await gambatte_known(retro.cog, ctx2, core="gambatte", key="gb_colorization", value="reset")
    assert "gambatte_gb_colorization" not in await retro.cog._core_options("gambatte")
    assert "`disabled`" in ctx2.said(), "it names the default it went back to"

    ctx3 = retro.context(retro.channel(9719))
    await gambatte_known(retro.cog, ctx3, core="gambatte", key="gb_colorization", value="reset")
    assert "already the core's default" in ctx3.said()


# -- A core that says nothing until a game is running (FCEUmm) ----------------


async def test_a_silent_core_is_not_called_optionless(retro, options, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "probe_core_options", lambda path, opts=None: {})
    ctx = retro.context(retro.channel(9720))
    await options(retro.cog, ctx, core="fceumm")
    said = ctx.said()
    assert "does **not** mean it has none" in said
    assert "Start a Nintendo Entertainment System game once" in said


async def test_setting_an_unverifiable_option_is_allowed_but_flagged(retro, options, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "probe_core_options", lambda path, opts=None: {})
    ctx = retro.context(retro.channel(9721))
    await options(retro.cog, ctx, core="fceumm", key="fceumm_region", value="PAL")
    said = ctx.said()
    assert (await retro.cog._core_options("fceumm")).get("fceumm_region") == "PAL"
    assert "could not be checked" in said
    assert "quietly use its default" in said


async def test_starting_a_game_is_what_teaches_us_a_core_like_fceumm(retro, options):
    FakeEmulator.definitions_by_core = {"fceumm": FCEUMM_DEFS}
    channel = retro.channel(9722)
    await retro.start_game(
        retro.context(channel), "nesgame", data=NES_BYTES, filename="nesgame.nes"
    )
    cached = await retro.cog._cached_definitions("fceumm")
    assert set(cached) >= set(FCEUMM_DEFS)

    ctx = retro.context(channel)
    await options(retro.cog, ctx, core="fceumm", key="sndquality")
    said = ctx.said()
    assert "`fceumm_sndquality`" in said and "Sound Quality" in said
    assert "Current: `Low`" in said, "read from the running game"


async def test_a_stale_override_is_shown_as_the_default_the_core_will_use(retro, options):
    FakeEmulator.definitions_by_core = {"fceumm": FCEUMM_DEFS}
    channel = retro.channel(9723)
    await retro.start_game(
        retro.context(channel), "nesgame", data=NES_BYTES, filename="nesgame.nes"
    )
    await retro.cog._set_core_option("fceumm", "fceumm_sndquality", "Impossible")

    ctx = retro.context(channel)
    await options(retro.cog, ctx, core="fceumm", key="sndquality")
    said = ctx.said()
    assert "Current: `Low`" in said
    assert "does not accept" in said


async def test_the_settings_embed_summarises_the_overrides(retro, options, monkeypatch):
    monkeypatch.setattr(retro.cogmod, "probe_core_options", lambda path, opts=None: {})
    ctx = retro.context(retro.channel(9724))
    await options(retro.cog, ctx, core="fceumm", key="fceumm_region", value="PAL")

    ctx2 = retro.context(retro.channel(9725))
    await retro.cogmod.RetroCog.retroset_settings.callback(retro.cog, ctx2)
    fields = {field.name: field.value for field in ctx2.sent[-1]["embed"].fields}
    assert "Core options" in fields, list(fields)
    assert "fceumm_region" in fields["Core options"]
