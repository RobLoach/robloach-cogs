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
    return retro.cogmod.Retro.retroset_coreoptions.callback


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


async def test_the_probe_reads_the_core_when_nobody_is_playing(retro, options, monkeypatch):
    # The uninterrupted case, which is the only one that loads a core: no
    # session anywhere, so the ROM-less probe is free to take the one slot.
    probed = []

    def fake_probe(core_path, opts=None):
        probed.append((Path(core_path).name, dict(opts or {})))
        return dict(GAMBATTE_DEFS) if "gambatte" in Path(core_path).name else {}

    retro.patch("probe_core_options", fake_probe, monkeypatch)

    ctx = retro.context(retro.channel(9740))
    await options(retro.cog, ctx, core="gambatte")
    assert probed and probed[0][0].startswith("gambatte")
    assert "the core itself" in ctx.said()
    assert len(await retro.cog._cached_definitions("gambatte")) >= len(GAMBATTE_DEFS)


async def test_listing_a_core_never_puts_a_running_game_to_sleep(retro, options, monkeypatch):
    """Reading a list must not cost somebody else their game.

    This used to call ``_evict_locked()`` to take the one core slot, which
    saved and hibernated every live session in every channel -- so an owner
    typing `[p]retroset coreoptions gambatte` silently bought a stranger
    mid-game a wake-up delay on their next press, and nothing said so.
    """
    channel = retro.channel(9741)
    playing = await retro.start_game(
        retro.context(channel), "probegame", data=NES_BYTES, filename="probegame.nes"
    )
    assert playing.live and playing.core == "fceumm"

    probed = []
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or dict(GAMBATTE_DEFS),
        monkeypatch,
    )

    ctx = retro.context(retro.channel(9742))
    await options(retro.cog, ctx, core="gambatte")

    assert not probed, "the core was loaded anyway"
    assert playing.live, "somebody else's game was put to sleep to read a list"
    said = ctx.said()
    # ...and the owner is told why the answer is empty, plus both ways out.
    assert "only one emulator core can be loaded at a time" in said
    assert "put it to sleep" in said
    assert "start a Game Boy game" in said, "the way that teaches us the options"
    assert "once nothing is playing" in said, "and the way that reads them directly"


async def test_a_sleeping_session_is_not_in_the_probe_s_way(retro, options, monkeypatch):
    # A hibernated session still exists and still answers its buttons, but it
    # holds no core -- so it is no reason to refuse to load one.
    channel = retro.channel(9743)
    view = await retro.start_game(
        retro.context(channel), "napping", data=NES_BYTES, filename="napping.nes"
    )
    await retro.cog.hibernate(view, None)
    assert not view.live and channel.id in retro.cog.sessions

    probed = []
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or dict(GAMBATTE_DEFS),
        monkeypatch,
    )
    ctx = retro.context(retro.channel(9744))
    await options(retro.cog, ctx, core="gambatte")
    assert probed, "a sleeping session blocked the probe"
    assert "the core itself" in ctx.said()


async def test_what_is_already_known_still_answers_while_a_game_is_running(
    retro, options, monkeypatch
):
    # Skipping the probe costs nothing when the cog has been taught already:
    # the cache answers, and the listing is the same listing.
    await retro.cog._remember_definitions("gambatte", GAMBATTE_DEFS)
    probed = []
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or {},
        monkeypatch,
    )
    playing = await retro.start_game(
        retro.context(retro.channel(9745)), "nesgame", data=NES_BYTES, filename="nesgame.nes"
    )
    assert playing.live

    ctx = retro.context(retro.channel(9746))
    await options(retro.cog, ctx, core="gambatte")
    said = ctx.said()
    assert not probed and playing.live
    assert "**gb_colorization**" in said
    assert "the last time this core ran" in said


async def test_a_game_that_starts_mid_command_is_refused_not_evicted(retro, monkeypatch):
    """The binding check is the one under the emulator lock.

    `_coreoptions` reads "is anybody playing?" before it takes the lock, so a
    game that starts in the gap would slip past it. The probe itself checks
    again while holding the lock, which is what makes "this never evicts
    anybody" true rather than merely likely.
    """
    await retro.install_cores("gambatte", "fceumm")
    probed = []
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or dict(GAMBATTE_DEFS),
        monkeypatch,
    )
    playing = await retro.start_game(
        retro.context(retro.channel(9747)), "nesgame", data=NES_BYTES, filename="nesgame.nes"
    )
    assert playing.live

    # Called directly, as the racing caller would have called it.
    assert await retro.cog._probe_definitions("gambatte") == {}
    assert not probed and playing.live


async def test_the_listing_shows_keys_values_defaults_and_provenance(retro, options, monkeypatch):
    retro.patch(
        "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS), monkeypatch
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
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or {},
        monkeypatch,
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
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: (probed.append(path), dict(GAMBATTE_DEFS))[1],
        monkeypatch,
    )
    await options(retro.cog, retro.context(retro.channel(9705)), core="gambatte")
    assert probed, "the first listing reads the core"

    probed.clear()
    await options(retro.cog, retro.context(retro.channel(9706)), core="gambatte")
    assert not probed


async def test_one_option_is_shown_in_full(retro, options, monkeypatch):
    retro.patch(
        "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS), monkeypatch
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
    # from the core name would be wrong. Nothing known means no key -- and,
    # since this used to answer (None, None), a reason to go with it.
    key, error = retro.cog._resolve_option_key("mednafen_ngp", {}, "language", "!")
    assert key is None
    assert error and "Nothing is known" in error
    assert "mednafen_ngp_language" not in error, "it did not guess a key"
    assert "!retroset coreoptions" in error, "the prefix it was handed"


@pytest.mark.parametrize(
    "definitions, typed",
    [
        (GAMBATTE_DEFS, "gambatte_gb_colorization"),  # resolves
        (GAMBATTE_DEFS, "nonsense"),                  # no such option
        (GAMBATTE_DEFS, "   "),                       # nothing typed
        ({}, "gb_colorization"),                      # nothing to match against
        (
            {**GAMBATTE_DEFS, "gambatte_gbc_colorization": {"values": [["off", "Off"]]}},
            "colorization",                           # ambiguous
        ),
    ],
)
def test_resolving_a_key_returns_a_key_or_a_reason_and_never_neither(
    retro, definitions, typed
):
    """The contract every caller leans on.

    `_coreoptions` sends the error and then uses the key, with no third
    branch for "neither" -- there used to be one and it could not run. That
    is only safe while this is true of every path out of the function.
    """
    key, error = retro.cog._resolve_option_key("gambatte", definitions, typed, "!")
    assert (key is None) != (error is None), (typed, key, error)


def test_effective_values_fall_back_exactly_as_libretro_does(retro):
    colorization = GAMBATTE_DEFS["gambatte_gb_colorization"]
    cog = retro.cogmod.Retro
    assert cog._option_default(colorization) == "disabled"
    assert cog._effective_value(colorization, None) == "disabled"
    assert cog._effective_value(colorization, "GBC") == "GBC"
    # A value the core does not offer is not what the core will read.
    assert cog._effective_value(colorization, "rainbow") == "disabled"
    # A definition with no declared default uses its first value.
    assert cog._option_default({"default": "", "values": [["a", "A"]]}) == "a"


def test_short_keys_only_lose_a_prefix_that_is_really_there(retro):
    cog = retro.cogmod.Retro
    assert cog._short_key("gambatte", "gambatte_gb_colorization") == "gb_colorization"
    assert cog._short_key("fceumm", "ngp_language") == "ngp_language"


def test_the_reset_sentinel_is_not_something_a_core_would_offer(retro):
    # `[p]retroset coreoptions <core> <key> reset` puts a core's default
    # back, so the sentinel must not also be a real value. "none" is out
    # because snes9x and genesis_plus_gx both offer it, "off" because mGBA
    # does and "disabled" because most of them do; "default" is unused by the
    # cores installed today but is exactly the word a new core would reach
    # for. tests/test_emulator.py checks every shipped core against this.
    assert retro.cogmod.OPTION_RESET == "reset"
    assert retro.cogmod.OPTION_RESET not in ("default", "none", "off", "disabled")


# -- Setting a value ----------------------------------------------------------


@pytest.fixture
async def gambatte_known(retro, options, monkeypatch):
    """The command, with gambatte's options already cached."""
    retro.patch(
        "probe_core_options", lambda path, opts=None: dict(GAMBATTE_DEFS), monkeypatch
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
    retro.patch("probe_core_options", lambda path, opts=None: {}, monkeypatch)
    ctx = retro.context(retro.channel(9720))
    await options(retro.cog, ctx, core="fceumm")
    said = ctx.said()
    assert "does **not** mean it has none" in said
    assert "Start a Nintendo Entertainment System game once" in said


async def test_setting_an_unverifiable_option_is_allowed_but_flagged(retro, options, monkeypatch):
    retro.patch("probe_core_options", lambda path, opts=None: {}, monkeypatch)
    ctx = retro.context(retro.channel(9721))
    await options(retro.cog, ctx, core="fceumm", key="fceumm_region", value="PAL")
    said = ctx.said()
    assert (await retro.cog._core_options("fceumm")).get("fceumm_region") == "PAL"
    assert "could not be checked" in said
    assert "quietly use its default" in said


async def test_setting_an_option_still_works_while_a_game_is_running(retro, options, monkeypatch):
    # Skipping the probe only withholds the *listing*. Storing an override
    # loads nothing, so it goes through unvalidated exactly as it does on an
    # idle bot -- and still without touching the game that is playing.
    probed = []
    retro.patch(
        "probe_core_options",
        lambda path, opts=None: probed.append(path) or {},
        monkeypatch,
    )
    playing = await retro.start_game(
        retro.context(retro.channel(9748)), "nesgame", data=NES_BYTES, filename="nesgame.nes"
    )
    assert playing.live

    ctx = retro.context(retro.channel(9749))
    await options(retro.cog, ctx, core="gambatte", key="gambatte_gb_colorization", value="GBC")
    assert not probed and playing.live
    assert (await retro.cog._core_options("gambatte")).get("gambatte_gb_colorization") == "GBC"
    assert "could not be checked" in ctx.said()


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


# -- "am I running the new code?" ---------------------------------------------


async def test_retroset_version_says_what_is_actually_loaded(retro):
    ctx = retro.context(retro.channel(9730))
    await retro.cogmod.Retro.retroset_version.callback(retro.cog, ctx)
    said = ctx.said()

    version = retro.cogmod.version
    assert version.VERSION in said
    assert version.VERSION != version.UNKNOWN_VERSION, "info.json has a version"
    assert version.FINGERPRINT in said, "the part that cannot go stale"
    assert "when the cog was loaded" in said
    # Plain text, so the command needs no Embed Links permission and can be
    # pasted straight into a bug report.
    assert not any(isinstance(entry, dict) and "embed" in entry for entry in ctx.sent)


async def test_retroset_version_names_the_checkout_in_a_git_checkout(retro):
    """The branch HEAD is on, or a detached HEAD's commit id.

    Only one of the two is ever known: version.py reads HEAD and nothing
    else, so a branch is reported by name rather than resolved to a commit.
    """
    version = retro.cogmod.version
    if version.BRANCH is None and version.COMMIT is None:
        pytest.skip("this copy of the cog is not in a git checkout")
    ctx = retro.context(retro.channel(9731))
    await retro.cogmod.Retro.retroset_version.callback(retro.cog, ctx)
    said = ctx.said()
    if version.BRANCH:
        assert version.BRANCH in said
    else:
        assert version.COMMIT[:12] in said


async def test_the_settings_embed_leads_with_the_build(retro):
    # First field, deliberately: half the confusing answers this command has
    # given were because the bot was running an older build than the reader.
    ctx = retro.context(retro.channel(9732))
    await retro.cogmod.Retro.retroset_settings.callback(retro.cog, ctx)
    fields = ctx.sent[-1]["embed"].fields
    assert fields[0].name == "Build", [f.name for f in fields]
    value = fields[0].value
    assert retro.cogmod.version.VERSION in value
    assert retro.cogmod.version.FINGERPRINT in value
    assert "retroset version" in value


async def test_the_settings_embed_summarises_the_overrides(retro, options, monkeypatch):
    retro.patch("probe_core_options", lambda path, opts=None: {}, monkeypatch)
    ctx = retro.context(retro.channel(9724))
    await options(retro.cog, ctx, core="fceumm", key="fceumm_region", value="PAL")

    ctx2 = retro.context(retro.channel(9725))
    await retro.cogmod.Retro.retroset_settings.callback(retro.cog, ctx2)
    fields = {field.name: field.value for field in ctx2.sent[-1]["embed"].fields}
    assert "Core options" in fields, list(fields)
    assert "fceumm_region" in fields["Core options"]
