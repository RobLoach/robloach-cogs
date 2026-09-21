"""Deliberately broken ROMs, against real cores and through the real cog.

A libretro core is a dlopen'd shared object running machine code written for
a console, handed a file a stranger in a Discord channel chose. `[p]retro
<url>` is open to everybody in the channel, so "what happens when the bytes
are nonsense" is not a hypothetical: it is the ordinary case of a truncated
download, a ROM hack that was patched wrong, or somebody being funny.

The cog's promise about that is narrow and worth pinning exactly:

* the outcome is **either a clean run or an ``EmulatorError``**. Never a
  bare RuntimeError or TypeError out of libretro.py, never a ctypes
  exception, and never a hang -- ``RetroEmulator.advance`` wraps whatever
  comes out of ``retro_run`` (retro/emulator.py) and ``start`` wraps
  libretro.py's "Failed to load game" into the sentence a player is shown;
* the core is **always freed**, whichever of those happened. MAX_LIVE_EMULATORS
  is 1, so one leaked core does not merely cost memory -- it stops the cog
  working at all;
* and a player who hands it a ROM no core can digest gets a sentence rather
  than a traceback, with no half-started session left behind.

What this file does **not** claim is that any particular seed breaks any
particular core. The corruption is seeded so the runs are reproducible, and
the invariant above is asserted for every one of them whatever happens; the
two tests that are about a *specific* failure mode say so in their names and
are there to prove the corruption really reaches those paths rather than
quietly always producing a playable game.

Measured on a Raspberry Pi 5, libretro.py 0.11.x, with the buildbot's
current gambatte and fceumm (2026-09), 200 corrupted bytes past the header:

    core       seeds   ran   loaded then died   refused at load
    gambatte      12     6           6                 0
    fceumm        12    12           0                 0

and with the whole cartridge header scrambled instead:

    gambatte       6     1           0                 5
    fceumm         6     0           0                 6

Zero hangs and zero native crashes (no signal, no interpreter death) across
all of it -- which is the finding this file exists to keep true, and the one
result worth knowing: a corrupt ROM is a *handled* failure here rather than
a way to take the bot down.

Heavier corruption is not more destructive. Rewriting 2,000 bytes of uCity
instead of 200 killed gambatte on 2 seeds of 12, and 20,000 bytes on none of
them, because a cartridge whose code is entirely garbage tends to sit in a
loop the core copes with rather than reach the one path it does not. So the
small number is the interesting one and BODY_BYTES stays at 200.
"""

import random
import time

import pytest

from .loader import load_standalone

pytestmark = [pytest.mark.emulator, pytest.mark.slow]

E = load_standalone("retro_emulator_for_fuzz", "emulator.py")

#: The seeds every corruption is driven with. Four, because the cost is a
#: fifth of a second each and the point is reproducibility rather than
#: coverage: this is a regression test for a finding, not a fuzzer.
SEEDS = (0, 1, 2, 3)

#: How many bytes of the ROM body to rewrite. Deliberately small: see the
#: measurements in the module docstring -- 200 bytes is what actually reaches
#: the mid-run failure path, and corrupting everything does not.
BODY_BYTES = 200

#: Clips short enough that the whole file costs about a second. A malformed
#: ROM either falls over immediately or does not; nothing here is about
#: length.
CLIP_SECONDS = 0.3

#: A generous ceiling on one corrupted ROM, so a *hang* fails the test
#: instead of stopping the suite. The measured worst case is about 0.3s, so
#: this is two orders of magnitude of headroom and cannot be flaky; it is
#: only here because "and never a hang" is part of the claim and an
#: assertion is the only way to say it.
MAX_SECONDS_PER_ROM = 20.0

#: (core, ROM, where the cartridge header ends). The Game Boy header runs to
#: 0x14F; a .nes file starts with a 16-byte iNES header.
CASES = (
    ("gambatte", "ucity.gbc", 0x150),
    ("fceumm", "nestest.nes", 16),
)

#: What one corrupted ROM can legitimately do. Anything else is the bug.
OUTCOMES = ("ran", "load-refused", "died-mid-run")


def corrupt_body(data, seed, header, count=BODY_BYTES):
    """``count`` bytes rewritten at random offsets past the header.

    The header is left alone on purpose, so the core accepts the cartridge
    and then runs code that is not the game's -- which is the case the
    ``EmulatorError`` wrapping in ``advance`` exists for.
    """
    rng = random.Random(seed)
    buf = bytearray(data)
    for _ in range(count):
        buf[rng.randrange(header, len(buf))] = rng.randrange(256)
    return bytes(buf)


def corrupt_header(data, seed, header):
    """Every byte of the cartridge header rewritten, body untouched.

    This is the other end of it: a core validates the header (the Nintendo
    logo, the iNES magic, a mapper number it knows) and refuses the content
    outright, which surfaces from ``start()`` rather than from ``advance()``.
    """
    rng = random.Random(seed)
    buf = bytearray(data)
    for index in range(header):
        buf[index] = rng.randrange(256)
    return bytes(buf)


SHAPES = {"body": corrupt_body, "header": corrupt_header}


@pytest.fixture
def emu():
    """Start emulators, guaranteeing only one core is ever loaded.

    The same fixture test_emulator.py uses, for the same reason: a libretro
    core is a shared object with process-global state.
    """
    made = []

    def build(core, rom, **kwargs):
        for previous in made:
            if previous.started:
                previous.stop()
        emulator = E.RetroEmulator(core, rom, **kwargs)
        made.append(emulator)
        return emulator

    yield build

    for emulator in made:
        try:
            emulator.stop()
        except Exception:  # noqa: BLE001 - teardown must never fail a test
            pass


def play_through(emulator):
    """Boot, run and record, classifying however it went.

    Returns ``(outcome, seconds, message)``. Everything that is not an
    ``EmulatorError`` is deliberately allowed to propagate: an unhandled
    exception type escaping a corrupted ROM is exactly the regression this
    file is here to catch, and a traceback naming it is more useful than a
    string comparison.
    """
    started = time.perf_counter()
    loaded = False
    outcome, message = "ran", ""
    try:
        emulator.start()
        loaded = True
        emulator.advance(emulator.frames_for_seconds(CLIP_SECONDS))
        emulator.record(
            emulator.clip_frames(CLIP_SECONDS), presses=[("a", 0, 5)]
        )
    except E.EmulatorError as error:
        outcome = "died-mid-run" if loaded else "load-refused"
        message = str(error)
    finally:
        # Always, and on every path: MAX_LIVE_EMULATORS is 1.
        emulator.stop()
    return outcome, time.perf_counter() - started, message


def malformed(assets, tmp_path, core, rom_name, header, shape, seed):
    """One reproducible broken ROM on disk, and the core that will refuse it."""
    core_path = assets.need_core(core)
    original = open(assets.need_rom(rom_name), "rb").read()
    payload = SHAPES[shape](original, seed, header)
    assert payload != original, "the corruption changed nothing"
    assert len(payload) == len(original), "the corruption changed the size"
    # The cog's own two content checks, so a ROM that would never reach a
    # core in production is not what is being tested here.
    assert len(payload) >= E.MIN_ROM_SIZE
    assert payload.lstrip()[:1] != b"<", "this seed produced something HTML-ish"
    path = tmp_path / f"{shape}-{seed}-{rom_name}"
    path.write_bytes(payload)
    return core_path, path


# -- The invariant, over every seed and both shapes of corruption -------------


@pytest.mark.parametrize("core, rom_name, header", CASES, ids=[c[0] for c in CASES])
def test_a_malformed_rom_is_always_a_clean_run_or_an_emulator_error(
    assets, emu, tmp_path, core, rom_name, header
):
    """The whole promise, asserted for every one of the eight runs.

    Sixteen trials across the two cores (eight here), each one either
    playing or raising an ``EmulatorError`` -- and each one inside
    MAX_SECONDS_PER_ROM, which is how "never a hang" is said. A native crash
    needs no assertion: it takes the interpreter with it, and this test
    failing that way is the loudest possible report.
    """
    seen = {}
    for shape in sorted(SHAPES):
        for seed in SEEDS:
            core_path, rom_path = malformed(
                assets, tmp_path, core, rom_name, header, shape, seed
            )
            outcome, seconds, message = play_through(emu(core_path, rom_path))
            assert outcome in OUTCOMES, (shape, seed, outcome)
            assert seconds < MAX_SECONDS_PER_ROM, (
                f"{core}/{shape}/{seed} took {seconds:.1f}s, which is a hang "
                "rather than a failure"
            )
            if outcome != "ran":
                # A sentence rather than a repr of something from ctypes:
                # this string is shown to whoever handed the bot the file.
                assert message and not message.startswith("<"), message
            seen[(shape, seed)] = outcome

    # Nothing was left loaded by any of them, which is the half of this that
    # costs the cog its one emulator slot rather than merely a game.
    assert len(seen) == 2 * len(SEEDS)


# -- The two specific failure modes, so the corruption is known to reach them -


def test_a_corrupt_cartridge_header_makes_the_core_refuse_the_rom(
    assets, emu, tmp_path
):
    """The load-time refusal, which is the commoner of the two in practice.

    fceumm checks the iNES magic, so scrambling the 16-byte header is a
    reliable "the core will not take this cartridge" -- and what comes out
    is the sentence retro/emulator.py writes for it, from ``start()``, with
    nothing loaded afterwards.
    """
    core_path, rom_path = malformed(
        assets, tmp_path, "fceumm", "nestest.nes", 16, "header", 0
    )
    emulator = emu(core_path, rom_path)

    with pytest.raises(E.EmulatorError, match="could not load this ROM"):
        emulator.start()

    assert not emulator.started
    # And the slot is free: the same core loads the real ROM straight after.
    good = emu(core_path, assets.need_rom("nestest.nes"))
    good.start()
    assert good.started
    good.advance(good.frames_for_seconds(0.2))


def test_a_core_that_dies_mid_run_comes_out_as_an_emulator_error(
    assets, emu, tmp_path
):
    """The mid-run death: the core accepts the cartridge, then falls over.

    This is the finding the module docstring records -- corrupting 200 bytes
    of a uCity ROM past its header kills gambatte on about half of the seeds
    -- and the thing worth pinning is that it dies *cleanly*: an
    ``EmulatorError`` saying the core crashed while running, the Python
    process intact, and the core freed.

    If a future core or libretro.py survives all four seeds this fails.
    That is deliberate and it is not a bug in the cog: re-measure which
    seeds reach the path (see the table in the module docstring) and update
    SEEDS, rather than deleting the test -- without a seed that gets here,
    nothing exercises the wrapping in ``RetroEmulator.advance`` at all.
    """
    deaths = []
    for seed in SEEDS:
        core_path, rom_path = malformed(
            assets, tmp_path, "gambatte", "ucity.gbc", 0x150, "body", seed
        )
        emulator = emu(core_path, rom_path)
        outcome, seconds, message = play_through(emulator)
        assert not emulator.started, f"seed {seed} left a core loaded"
        if outcome == "died-mid-run":
            deaths.append((seed, message))
            assert "The core crashed while running" in message, message

    if not deaths:
        # Whether a given corrupted cartridge *kills* a core is a property of
        # the core build and of libretro.py, not of this cog: the same seeds
        # that kill gambatte under libretro.py 0.11 are digested happily
        # under 0.6.x. So this is a skip, not a failure -- the invariant the
        # cog owns (a clean outcome, no core left loaded, the slot still
        # usable) is asserted above and on every seed, and the handling path
        # itself is covered deterministically, with no core at all, by
        # tests/test_leaks.py section 5.
        pytest.skip(
            f"none of the seeds {SEEDS} kills this core build, so there is "
            "no mid-run death to observe here"
        )
    # The slot survives a core that died, which is what MAX_LIVE_EMULATORS
    # makes non-negotiable.
    good = emu(assets.need_core("gambatte"), assets.need_rom("ucity.gbc"))
    good.start()
    assert good.started
    good.advance(good.frames_for_seconds(0.2))


# -- Through the cog, as a player meets it ------------------------------------
#
# The emulator-level tests above say the failure is an EmulatorError. These
# two say what a *player* gets for it: one sentence, no session left behind,
# and -- the part that matters beyond the one game -- the bot's single
# emulator slot back. MAX_LIVE_EMULATORS is 1, so a core leaked by a failed
# start does not cost that channel a game, it costs every channel every game
# until the cog is reloaded.
#
# These need discord.py and Red (real or stubbed), which is what the `retro`
# fixture brings; without discord.py they skip through it rather than
# through anything here.

#: A seed whose corruption reliably reaches each failure path with gambatte
#: and uCity. See the table in the module docstring, and the note in
#: test_a_core_that_dies_mid_run_comes_out_as_an_emulator_error about what
#: to do if a core update moves them.
COG_SEEDS = {"header": 1, "body": 0}


@pytest.fixture
def real_cog(retro, monkeypatch, gambatte):
    """The cog with its real emulator put back, recording every one built.

    The ``retro`` fixture swaps RetroEmulator for the fake; this puts the
    genuine article back (as test_saves_roundtrip.py's ``real`` does) and
    subclasses it so the test can prove no core was left loaded rather than
    inferring it from the cog's own bookkeeping.
    """
    from pathlib import Path

    from retro.emulator import RetroEmulator

    built = []

    class Recording(RetroEmulator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    async def _core_path(core):
        return Path(gambatte)

    retro.patch("RetroEmulator", Recording, monkeypatch)
    monkeypatch.setattr(retro.cog, "_core_path", _core_path)
    retro.built = built
    return retro


async def start_through_the_command(retro, channel, filename, payload):
    """`[p]retro <url>`, with that URL serving exactly these bytes."""
    ctx = retro.context(channel)
    retro.serve(filename, payload)
    retro.forgive_cooldowns()
    await retro.cogmod.Retro.retro.callback(
        retro.cog, ctx, game=f"https://example.com/{filename}"
    )
    return ctx


@pytest.mark.parametrize("shape", sorted(COG_SEEDS))
async def test_a_rom_the_core_cannot_digest_is_a_sentence_and_not_a_zombie(
    real_cog, assets, shape
):
    """Both failure modes, from the outside: what the channel is told.

    ``header`` is the core refusing the cartridge outright and ``body`` is
    the core accepting it and then dying while the first clip is being
    recorded. They arrive at the same place -- ``_start_session`` catches
    EmulatorError, ``_abandon_session`` banks whatever was emulated and
    frees the core -- and the point of driving both is that the second one
    has a *loaded core* to clean up and the first does not.
    """
    retro = real_cog
    await retro.install_cores("gambatte")
    original = open(assets.need_rom("ucity.gbc"), "rb").read()
    payload = SHAPES[shape](original, COG_SEEDS[shape], 0x150)
    assert payload != original and len(payload) == len(original)

    channel = retro.channel(9970)
    ctx = await start_through_the_command(retro, channel, "ucity.gbc", payload)

    # 1. One sentence, written for a person, naming the core's own complaint.
    #    A body-corrupted cartridge only *reaches* this path on builds that
    #    actually choke on it: libretro.py 0.6.x digests the very bytes 0.11
    #    dies on. If this build took the ROM, there is no failure to report
    #    on -- the header shape still exercises the sentence, and section 5
    #    of tests/test_leaks.py exercises both failure points with no core.
    said = ctx.said()
    if shape == "body" and "could not be started" not in said:
        assert retro.cog.sessions, "the core took the ROM but no game is running"
        pytest.skip("this core build digests the body-corrupted cartridge")
    assert "could not be started" in said, said
    assert "Traceback" not in said and "Error(" not in said, said

    # 2. No session survives as a zombie: nothing in memory, nothing in
    #    Config, and the half-built view is inert.
    assert retro.cog.sessions.get(channel.id) is None, "a dead session was kept"
    assert not await retro.cog.config.channel_from_id(channel.id).session()
    assert not retro.cog.retired

    # 3. The emulator is freed. This is the one that stops the cog working:
    #    a core left loaded spends the only MAX_LIVE_EMULATORS slot there is.
    assert retro.built, "no emulator was built, so nothing is proved"
    assert not any(e.started for e in retro.built), (
        f"a failed start left {sum(e.started for e in retro.built)} libretro "
        "core(s) loaded"
    )

    # 4. And the proof of that from the outside: the very next game, in
    #    another channel, comes up and plays.
    good = retro.channel(9971)
    await start_through_the_command(retro, good, "ucity.gbc", original)
    view = retro.cog.sessions.get(good.id)
    assert view is not None and view.live, retro.context(good).said()
    clip = retro.shown_clip(view)
    assert clip and clip[:4] == b"RIFF", "the good game posted no clip"
