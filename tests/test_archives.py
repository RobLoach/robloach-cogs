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


# -- Unpacking a whole archive ------------------------------------------------
#
# What `[p]retroset bios add` does with a firmware set: several files, often
# with a folder per console, all of which have to land under the system
# directory and none of which may land anywhere else.

ALL_LIMITS = dict(max_total_size=MAX, max_file_size=MAX, max_files=100)


def test_extract_all_returns_every_file_in_sorted_order():
    data = zipped([("b.bin", b"bb"), ("a.bin", b"aa"), ("dc/boot.bin", b"cc")])
    found = A.extract_all(data, **ALL_LIMITS)
    assert [f.path for f in found.files] == ["a.bin", "b.bin", "dc/boot.bin"]
    assert [f.data for f in found.files] == [b"aa", b"bb", b"cc"]
    assert found.total_size == 6
    assert found.skipped == ()


def test_extract_all_keeps_folders_but_normalises_separators():
    data = zipped([("np2kai\\FONT.ROM", b"x" * 8)])
    found = A.extract_all(data, **ALL_LIMITS)
    assert [f.path for f in found.files] == ["np2kai/FONT.ROM"]
    assert found.files[0].member == "np2kai\\FONT.ROM"


def test_extract_all_drops_archive_junk():
    data = zipped(
        [
            ("real.bin", b"x" * 8),
            ("__MACOSX/._real.bin", b"junk"),
            (".DS_Store", b"junk"),
            ("folder/.hidden", b"junk"),
        ]
    )
    found = A.extract_all(data, **ALL_LIMITS)
    assert [f.path for f in found.files] == ["real.bin"]
    # Junk is dropped rather than reported: nobody meant to install it.
    assert found.skipped == ()


@pytest.mark.parametrize(
    "member",
    [
        "../escape.bin",
        "../../etc/passwd",
        "/etc/passwd",
        "C:/windows/evil.bin",
        "a/../../b.bin",
        "a/./b.bin",
        "one/two/three/four/five.bin",
        "sp ace/../out.bin",
        "weird;name.bin",
        "name\nwith\nnewlines.bin",
        "x" * 80,
    ],
)
def test_extract_all_refuses_an_unsafe_member(member):
    data = zipped([(member, b"x" * 8), ("good.bin", b"y" * 8)])
    found = A.extract_all(data, **ALL_LIMITS)
    assert [f.path for f in found.files] == ["good.bin"]
    assert found.skipped == (member,)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a.bin", "a.bin"),
        ("dir/a.bin", "dir/a.bin"),
        ("dir\\a.bin", "dir/a.bin"),
        ("./a.bin", None),
        ("../a.bin", None),
        ("/a.bin", None),
        ("", None),
        ("a\x00b.bin", None),
        (".hidden", None),
        ("a/b/c/d/e.bin", None),
    ],
)
def test_safe_member_path(name, expected):
    assert A.safe_member_path(name) == expected


def test_safe_member_path_never_escapes_a_directory():
    root = Path("/tmp/system").resolve()
    for name in ("a.bin", "dc/boot.bin", "a b/c+d-e.bin"):
        resolved = (root / A.safe_member_path(name)).resolve()
        assert root in resolved.parents


@pytest.mark.parametrize("mode", [0o120777, 0o020666, 0o060660, 0o010666, 0o140777])
def test_extract_all_refuses_anything_that_is_not_a_regular_file(mode):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("link.bin")
        info.create_system = 3  # Unix, so external_attr really is a mode
        info.external_attr = mode << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr("real.bin", b"x" * 8)
    found = A.extract_all(buf.getvalue(), **ALL_LIMITS)
    assert [f.path for f in found.files] == ["real.bin"]
    assert found.skipped == ("link.bin",)


def test_a_non_unix_archive_mode_is_not_read_as_one():
    # create_system 0 is MS-DOS: the high half of external_attr is not a
    # mode, so it must not be mistaken for one and refused.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("dos.bin")
        info.create_system = 0
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b"x" * 8)
    found = A.extract_all(buf.getvalue(), **ALL_LIMITS)
    assert [f.path for f in found.files] == ["dos.bin"]


def test_extract_all_refuses_too_many_files():
    data = zipped([(f"f{i}.bin", b"x") for i in range(20)])
    with pytest.raises(A.ArchiveError) as caught:
        A.extract_all(data, max_total_size=MAX, max_file_size=MAX, max_files=5)
    assert "more than the 5" in str(caught.value)


def test_extract_all_refuses_a_bomb_before_decompressing_it():
    data = zipped([("bomb.bin", b"\x00" * (1024 * 1024))])
    with pytest.raises(A.ArchiveError) as caught:
        A.extract_all(data, max_total_size=1024, max_file_size=MAX, max_files=10)
    assert "unpacks to" in str(caught.value)


def test_extract_all_enforces_the_total_against_the_real_bytes(monkeypatch):
    # ZipInfo.file_size is attacker-controlled metadata, so the metadata check
    # is only a first pass; the running total of bytes actually produced is
    # what really enforces the cap.
    data = zipped([(f"f{i}.bin", b"\x00" * 4096) for i in range(4)])
    real_infolist = zipfile.ZipFile.infolist

    def lying_infolist(self):
        infos = real_infolist(self)
        for info in infos:
            info.file_size = 1  # "one byte each, honest"
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying_infolist)
    # Refused either by the running total or (as CPython's zipfile gets there
    # first) by the CRC that the lie also invalidates. Either way nothing that
    # lied about its size is handed back.
    with pytest.raises(A.ArchiveError):
        A.extract_all(data, max_total_size=4096, max_file_size=MAX, max_files=10)


def test_extract_all_skips_a_single_member_over_the_per_file_cap():
    data = zipped([("big.bin", b"x" * 4096), ("small.bin", b"y" * 8)])
    found = A.extract_all(data, max_total_size=MAX, max_file_size=1024, max_files=10)
    assert [f.path for f in found.files] == ["small.bin"]
    assert found.skipped == ("big.bin",)


def test_extract_all_skips_an_empty_member():
    data = zipped([("empty.bin", b""), ("real.bin", b"x" * 8)])
    found = A.extract_all(data, **ALL_LIMITS)
    assert [f.path for f in found.files] == ["real.bin"]
    assert found.skipped == ("empty.bin",)


def test_extract_all_refuses_an_encrypted_member():
    data = bytearray(zipped([("secret.bin", b"x" * 8)]))
    data[data.find(b"PK\x03\x04") + 6] |= 0x01
    data[data.find(b"PK\x01\x02") + 8] |= 0x01
    with pytest.raises(A.ArchiveError, match="password-protected"):
        A.extract_all(bytes(data), **ALL_LIMITS)


def test_extract_all_raises_when_nothing_is_usable():
    data = zipped([("../nope.bin", b"x" * 8)])
    with pytest.raises(A.NoSupportedMember) as caught:
        A.extract_all(data, what="BIOS file", **ALL_LIMITS)
    assert "no BIOS file this bot can use" in str(caught.value)


def test_extract_all_raises_on_an_empty_archive():
    with pytest.raises(A.NoSupportedMember):
        A.extract_all(zipped([]), **ALL_LIMITS)


def test_extract_all_raises_on_something_that_is_not_a_zip():
    with pytest.raises(A.ArchiveError):
        A.extract_all(b"this is not a zip at all", **ALL_LIMITS)
