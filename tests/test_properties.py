"""Property-based cover for the handful of genuinely input-driven functions.

Everything here takes a string a stranger chose -- a game name, a filename
inside a zip, a BIOS name, a core option somebody typed -- and turns it into
something the bot then puts on a filesystem or hands to a core. Example
tests cover the cases we thought of; Hypothesis covers the ones we did not.
"""

import re
import string
import zipfile

import pytest

from .loader import load_standalone

pytest.importorskip("hypothesis", reason="the property tests need Hypothesis")
pytest.importorskip("discord", reason="RetroCog's helpers need discord.py")

from hypothesis import assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from retro.RetroCog import RetroCog  # noqa: E402

A = load_standalone("retro_archives_for_properties", "archives.py")

# Deliberately nasty: separators, dots, control characters, non-ASCII and
# the empty string are all things a filename inside a zip can really be.
NASTY = st.text(
    alphabet=st.sampled_from(
        list(string.printable) + ["/", "\\", "\x00", "é", "🎮", "​"]
    ),
    max_size=120,
)


# -- Slugs: a game name becomes part of a filename ----------------------------


@given(NASTY)
def test_a_slug_is_always_usable_as_part_of_a_filename(name):
    slug = RetroCog._slug(name)
    assert slug, "a slug is never empty"
    assert len(slug) <= 48
    assert re.fullmatch(r"[a-z0-9_-]+", slug), slug
    # Nothing that could climb out of the states directory survives.
    assert "/" not in slug and "\\" not in slug and ".." not in slug


@given(NASTY)
def test_a_slug_is_lower_case_and_stable(name):
    assert RetroCog._slug(name) == RetroCog._slug(name.upper()).lower()


# -- Sanitised filenames: a ROM is cached under one ---------------------------


@given(NASTY)
def test_a_sanitized_filename_cannot_escape_the_rom_cache(name):
    safe = RetroCog._sanitize_filename(name)
    assert safe
    assert len(safe) <= 64
    assert "/" not in safe and "\\" not in safe
    assert safe not in (".", "..")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", safe), safe


@given(NASTY)
def test_a_sanitized_extension_is_lower_case(name):
    # libretro.py 0.6.x matches the extension against the core's
    # valid_extensions case-sensitively, so ".GB" must become ".gb".
    safe = RetroCog._sanitize_filename(name)
    _, dot, suffix = safe.rpartition(".")
    if dot and suffix:
        assert suffix == suffix.lower(), safe


@given(st.sampled_from(["game.GB", "GAME.GBC", "x.SfC", "rom.NES"]))
def test_a_known_rom_name_keeps_its_stem_and_lowers_its_extension(name):
    safe = RetroCog._sanitize_filename(name)
    assert safe == name.rsplit(".", 1)[0] + "." + name.rsplit(".", 1)[1].lower()


# -- BIOS names: validated, never rewritten -----------------------------------


@given(NASTY)
def test_a_bios_name_is_either_refused_or_completely_safe(name):
    safe = RetroCog._bios_name(name)
    if safe is None:
        return
    assert "/" not in safe and "\\" not in safe and "\x00" not in safe
    assert not safe.startswith(".")
    assert 1 <= len(safe) <= 64
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ +-]*", safe), safe


@given(st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=40))
def test_an_ordinary_bios_name_is_accepted_verbatim(name):
    # Cores want an exact filename, so a name that is already safe must come
    # back unchanged rather than rewritten.
    assert RetroCog._bios_name(name) == name


# -- Archive member selection -------------------------------------------------

MEMBER_NAMES = st.lists(
    st.tuples(
        st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=8),
        st.sampled_from(["gb", "gbc", "nes", "sfc", "txt", "png", "bin"]),
    ).map(lambda pair: f"{pair[0]}.{pair[1]}"),
    min_size=1,
    max_size=8,
    unique=True,
)


@settings(max_examples=50)
@given(MEMBER_NAMES)
def test_the_chosen_member_is_always_the_first_acceptable_one(names):
    import io

    playable = {"gb", "gbc", "nes", "sfc"}

    def accept(name):
        return name.rsplit(".", 1)[-1] in playable

    assume(any(accept(name) for name in names))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for index, name in enumerate(names):
            archive.writestr(name, bytes([index % 256]) * 2048)

    found = A.extract(buf.getvalue(), accept=accept, max_size=1 << 24, what="ROM")
    assert found.name == min(name for name in names if accept(name))
    assert found.candidates == tuple(sorted(name for name in names if accept(name)))
    assert found.members == tuple(sorted(names))
    # The bytes really are the chosen member's.
    assert found.data == bytes([names.index(found.name) % 256]) * 2048


# -- Option keys --------------------------------------------------------------

OPTION_KEYS = st.lists(
    st.text(alphabet=string.ascii_lowercase + "_", min_size=3, max_size=20).filter(
        lambda key: not key.startswith("_") and not key.endswith("_")
    ),
    min_size=1,
    max_size=6,
    unique=True,
)


@given(OPTION_KEYS, st.data())
def test_the_exact_key_always_resolves_to_itself(keys, data):
    definitions = {
        f"gambatte_{key}": {"default": "a", "values": [["a", "A"]]} for key in keys
    }
    wanted = "gambatte_" + data.draw(st.sampled_from(keys))
    resolved, error = RetroCog._resolve_option_key("gambatte", definitions, wanted, "!")
    assert resolved == wanted and error is None


@given(OPTION_KEYS, st.data())
def test_a_key_is_never_silently_resolved_to_the_wrong_option(keys, data):
    definitions = {
        f"gambatte_{key}": {"default": "a", "values": [["a", "A"]]} for key in keys
    }
    typed = data.draw(st.sampled_from(keys))
    resolved, error = RetroCog._resolve_option_key("gambatte", definitions, typed, "!")
    # Either it refuses, or what it picked really is an option this core has
    # and really does end with what was typed.
    assert resolved is None or (resolved in definitions and resolved.endswith(typed))
    assert resolved is not None or error is None or isinstance(error, str)


@given(st.text(alphabet=string.printable, max_size=30))
def test_resolving_never_raises_whatever_is_typed(typed):
    definitions = {"gambatte_gb_colorization": {"default": "a", "values": [["a", "A"]]}}
    resolved, error = RetroCog._resolve_option_key("gambatte", definitions, typed, "!")
    assert resolved is None or resolved in definitions


# -- Sizes --------------------------------------------------------------------


@given(st.integers(min_value=0, max_value=1 << 40))
def test_a_size_is_always_reported_with_a_unit(size):
    text = A._human_size(size)
    assert text.endswith(("bytes", "KiB", "MiB")), text
    value = float(text.split()[0])
    assert value >= 0
    if size < 1024:
        assert text == f"{size} bytes"
