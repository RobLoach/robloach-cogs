"""Reading one member out of a .zip, safely.

Pure standard library. Everything a stranger can upload goes through here:
zip bombs, lying metadata, encrypted members, path traversal and archives
with nothing playable in them at all.
"""

import io
import zipfile
from pathlib import Path

import pytest

from .loader import load_standalone

A = load_standalone("retro_archives_standalone", "archives.py")
S = load_standalone("retro_systems_for_archives", "systems.py")

ROM = b"\x00" * 4096
MAX = 32 * 1024 * 1024


def is_rom(name):
    return S.system_for_extension(Path(name).suffix) is not None


def zipped(entries, **kwargs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", **kwargs) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buf.getvalue()


def extract(data, max_size=MAX, what="ROM"):
    return A.extract(data, accept=is_rom, max_size=max_size, what=what)


# -- Spotting an archive ------------------------------------------------------


def test_is_zip_spots_a_zip():
    assert A.is_zip(zipped([("a.gb", ROM)]))


def test_is_zip_rejects_a_raw_rom_and_tolerates_short_input():
    assert not A.is_zip(ROM)
    assert not A.is_zip(b"PK")
    assert not A.is_zip(b"")


# -- Choosing a member --------------------------------------------------------


def test_the_rom_is_picked_out_from_beside_a_readme():
    found = extract(zipped([("readme.txt", b"hi" * 50), ("game.gb", ROM)]))
    assert found.name == "game.gb"
    assert found.data == ROM
    assert found.candidates == ("game.gb",)
    assert set(found.members) == {"readme.txt", "game.gb"}


def test_several_roms_pick_the_first_alphabetically_and_report_them_all():
    found = extract(
        zipped([("zeta.nes", ROM + b"z"), ("alpha.gb", ROM + b"a"), ("mid.sfc", ROM + b"m")])
    )
    assert found.name == "alpha.gb"
    assert found.candidates == ("alpha.gb", "mid.sfc", "zeta.nes")
    assert found.data.endswith(b"a")


def test_the_choice_does_not_depend_on_the_order_inside_the_zip():
    first = extract(zipped([("mid.sfc", ROM), ("alpha.gb", ROM), ("zeta.nes", ROM)]))
    second = extract(zipped([("alpha.gb", ROM), ("zeta.nes", ROM), ("mid.sfc", ROM)]))
    assert first.name == second.name == "alpha.gb"


def test_a_mixed_zip_reports_every_candidate_sorted():
    found = extract(zipped([("z.gb", ROM), ("a.nes", ROM), ("r.txt", b"x" * 40)]))
    assert found.name == "a.nes"
    assert found.candidates == ("a.nes", "z.gb")


def test_a_nested_directory_is_searched():
    found = extract(zipped([("pack/v1.0/roms/deep.gbc", ROM)]))
    assert found.name == "pack/v1.0/roms/deep.gbc"
    assert Path(found.name).name == "deep.gbc"


def test_macos_junk_is_ignored_and_not_even_listed():
    found = extract(
        zipped([("__MACOSX/._game.gb", b"x" * 99), ("game.gb", ROM), (".DS_Store", b"y" * 99)])
    )
    assert found.name == "game.gb"
    assert "__MACOSX/._game.gb" not in found.members
    assert ".DS_Store" not in found.members


# -- Nothing usable inside ----------------------------------------------------


def test_a_zip_with_no_rom_is_refused_and_says_what_was_in_it():
    with pytest.raises(A.NoSupportedMember) as error:
        extract(zipped([("notes.txt", b"nothing here"), ("cover.png", b"x" * 40)]))
    message = str(error.value)
    assert "no ROM this bot can use" in message
    assert "notes.txt" in message and "cover.png" in message
    assert set(error.value.members) == {"notes.txt", "cover.png"}


def test_a_long_listing_is_truncated_rather_than_dumped():
    with pytest.raises(A.NoSupportedMember) as error:
        extract(zipped([(f"f{i:03d}.txt", b"x" * 20) for i in range(30)]))
    message = str(error.value)
    assert "and 22 more" in message
    assert len(message) < 400, len(message)


def test_an_empty_zip_is_refused():
    with pytest.raises(A.NoSupportedMember, match="no files in it"):
        extract(zipped([]))


def test_a_zip_of_nothing_but_folders_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(zipfile.ZipInfo("folder/"), b"")
    with pytest.raises(A.NoSupportedMember, match="no files in it"):
        extract(buf.getvalue())


def test_an_empty_member_is_refused():
    with pytest.raises(A.ArchiveError, match="is empty"):
        extract(zipped([("empty.gb", b"")]))


# -- Hostile archives ---------------------------------------------------------


def test_an_oversized_member_is_refused_before_anything_is_unpacked():
    bomb = zipped([("huge.gb", b"\x00" * (2 * 1024 * 1024))])
    with pytest.raises(A.ArchiveError, match="over the"):
        extract(bomb, max_size=1024 * 1024)


def test_a_compressible_member_unpacks_normally():
    data = zipped([("liar.gb", b"\x00" * (2 * 1024 * 1024))], compression=zipfile.ZIP_DEFLATED)
    found = extract(data, max_size=4 * 1024 * 1024)
    assert len(found.data) == 2 * 1024 * 1024


def test_a_member_that_lies_about_its_size_cannot_exceed_the_cap():
    # The metadata is attacker-controlled, so the read is capped too and
    # zipfile's own CRC check turns the lie into an error we translate.
    data = zipped([("liar.gb", b"\x00" * (2 * 1024 * 1024))], compression=zipfile.ZIP_DEFLATED)
    raw = bytearray(data)
    local = raw.find(b"PK\x03\x04")
    raw[local + 22 : local + 26] = (10).to_bytes(4, "little")
    central = raw.find(b"PK\x01\x02")
    raw[central + 24 : central + 28] = (10).to_bytes(4, "little")
    try:
        found = extract(bytes(raw), max_size=1024 * 1024)
    except A.ArchiveError as error:
        assert "corrupt" in str(error) or "limit" in str(error), str(error)
    else:
        assert len(found.data) <= 1024 * 1024


def test_a_corrupt_zip_is_refused_with_a_sentence():
    with pytest.raises(A.ArchiveError) as error:
        extract(b"PK\x03\x04 this is not really a zip at all")
    assert "not a readable zip" in str(error.value) or "could not" in str(error.value)


def test_a_truncated_zip_is_refused():
    with pytest.raises(A.ArchiveError):
        extract(zipped([("game.gb", ROM)])[:200])


def test_a_password_protected_member_is_spotted_from_its_flag_bits():
    data = bytearray(zipped([("secret.gb", ROM)]))
    data[data.find(b"PK\x03\x04") + 6] |= 0x01
    data[data.find(b"PK\x01\x02") + 8] |= 0x01
    with pytest.raises(A.ArchiveError, match="password-protected"):
        extract(bytes(data))


def test_a_traversing_member_is_read_into_memory_and_never_written(tmp_path):
    found = extract(zipped([("../../../etc/evil.gb", ROM)]))
    assert found.data == ROM
    assert Path(found.name).name == "evil.gb"
    # Nothing here ever calls ZipFile.extract, so nothing lands on disk.
    assert not (tmp_path / "evil.gb").exists()
    assert not Path("/tmp/evil.gb").exists()


# -- Reporting ----------------------------------------------------------------


def test_describe_members_truncates():
    assert "and 2 more" in A.describe_members(list("abcdefghij"), limit=8)


def test_describe_members_handles_nothing():
    assert A.describe_members([]) == "nothing at all"


@pytest.mark.parametrize(
    "size, expected",
    [(0, "0 bytes"), (1023, "1023 bytes"), (1024, "1.0 KiB"), (1024 * 1024, "1.0 MiB")],
)
def test_sizes_are_reported_in_units_a_person_reads(size, expected):
    assert A._human_size(size) == expected
