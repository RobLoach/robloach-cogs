"""Reading one member out of a .zip, safely.

Pure standard library. Everything a stranger can upload goes through here:
zip bombs, lying metadata, encrypted members, path traversal and archives
with nothing playable in them at all.
"""

import io
import tracemalloc
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


def extract(data, max_size=MAX, what="ROM", **kwargs):
    return A.extract(data, accept=is_rom, max_size=max_size, what=what, **kwargs)


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


def test_junk_is_spotted_through_backslashes_nesting_and_dot_folders():
    # All three sort before game.gb, so any one of them slipping past the
    # junk filter would become the ROM the user's game boots from.
    found = extract(
        zipped(
            [
                ("__MACOSX\\alpha.gb", b"x" * 99),  # Windows-repacked
                ("a/__MacOSX/beta.gb", b"y" * 99),  # nested, mixed case
                ("a/.hidden/gamma.gb", b"z" * 99),  # dot *folder*
                ("game.gb", ROM),
            ]
        )
    )
    assert found.name == "game.gb"
    assert found.members == ("game.gb",)


# -- Asking for a member by name ----------------------------------------------
#
# A zip of a dozen ROMs used to mean "upload the one you want on its own".
# `prefer` is the caller saying which one, matched on the basename alone,
# case-insensitively, with or without the extension -- what a person types
# after reading the bot's own listing back.


def test_prefer_picks_a_member_that_is_not_the_alphabetical_first():
    found = extract(zipped([("alpha.gb", ROM + b"a"), ("zeta.nes", ROM + b"z")]), prefer="zeta.nes")
    assert found.name == "zeta.nes"
    assert found.data.endswith(b"z")
    assert found.preferred
    # The report of what else was in there is unchanged by the preference.
    assert found.candidates == ("alpha.gb", "zeta.nes")


@pytest.mark.parametrize("prefer", ["zeta.nes", "ZETA.NES", "Zeta.Nes", "zeta", "ZETA"])
def test_prefer_ignores_case_and_the_extension(prefer):
    found = extract(zipped([("alpha.gb", ROM + b"a"), ("zeta.nes", ROM + b"z")]), prefer=prefer)
    assert found.name == "zeta.nes"


def test_prefer_matches_a_member_inside_a_folder_by_its_basename():
    data = zipped([("aaa.gb", ROM + b"a"), ("pack/v1.0/roms/Deep.GBC", ROM + b"d")])
    found = extract(data, prefer="deep.gbc")
    assert found.name == "pack/v1.0/roms/Deep.GBC"
    assert found.data.endswith(b"d")


def test_prefer_ignores_any_folder_the_user_typed_in_front_of_the_name():
    # Pasting a path back out of the bot's own listing has to work, and only
    # the last component of it is ever compared.
    data = zipped([("aaa.gb", ROM + b"a"), ("roms/game.gb", ROM + b"g")])
    assert extract(data, prefer="roms/game.gb").name == "roms/game.gb"
    assert extract(data, prefer="somewhere\\else\\game.gb").name == "roms/game.gb"


def test_an_exact_name_beats_one_that_only_matches_without_its_extension():
    # `game.gb.bak` sorts first and its stem is `game.gb`, so a single-pass
    # match would hand back the backup instead of the ROM that was named.
    data = zipped([("backup/game.gb.bak", ROM + b"b"), ("game.gb", ROM + b"g")])
    found = extract(data, prefer="game.gb")
    assert found.name == "game.gb"
    assert found.data.endswith(b"g")


def test_a_name_that_matches_nothing_falls_back_to_the_first_alphabetically():
    # Better a ROM and a note than an error: the caller still gets every
    # candidate to tell the user what it did instead.
    found = extract(zipped([("alpha.gb", ROM), ("zeta.nes", ROM)]), prefer="missing.gb")
    assert found.name == "alpha.gb"
    assert not found.preferred
    assert found.candidates == ("alpha.gb", "zeta.nes")


@pytest.mark.parametrize("prefer", [None, "", "   "])
def test_no_preference_at_all_is_the_old_behaviour(prefer):
    found = extract(zipped([("alpha.gb", ROM), ("zeta.nes", ROM)]), prefer=prefer)
    assert found.name == "alpha.gb"
    assert not found.preferred


def test_prefer_cannot_reach_past_accept_to_a_file_the_bot_cannot_use():
    # Naming the readme does not make the readme a ROM; the preference only
    # ever picks between members `accept` already said yes to.
    found = extract(zipped([("readme.txt", b"hi" * 50), ("game.gb", ROM)]), prefer="readme.txt")
    assert found.name == "game.gb"
    assert not found.preferred


def test_prefer_cannot_reach_into_the_junk_either():
    found = extract(
        zipped([("__MACOSX/._game.gb", b"x" * 99), ("game.gb", ROM)]),
        prefer="._game.gb",
    )
    assert found.name == "game.gb"
    assert not found.preferred


def test_two_members_share_a_basename_and_the_sorted_first_wins():
    data = zipped([("b/game.gb", ROM + b"b"), ("a/game.gb", ROM + b"a")])
    found = extract(data, prefer="game.gb")
    assert found.name == "a/game.gb"
    assert found.preferred


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
#
# These used to run against `extract_all`, the collecting entry point the module
# shipped beside `extract_each`. It was deleted for having no caller but this
# file, so the tests that care about the *rules* of the walk -- which members are
# taken, which are skipped and why -- collect the stream into a list themselves
# and read the answers off that. The tests that care about the streaming itself
# are in the next section.

ALL_LIMITS = dict(max_total_size=MAX, max_file_size=MAX, max_files=100)


def collected(data, **limits):
    """Run `extract_each` into a list, for tests that want the whole archive.

    A sink of `list.append`: the shape a caller writes when it does not mind
    holding everything at once, and exactly what the module's own `extract_all`
    was before it was deleted. Cheap in a test, where the whole archive is a few
    dozen bytes; the cog writes each file to disk as it arrives instead, and the
    next section measures the difference.
    """
    files = []
    report = A.extract_each(data, sink=files.append, **(limits or ALL_LIMITS))
    return files, report


def test_extract_each_keeps_folders_but_normalises_separators():
    data = zipped([("np2kai\\FONT.ROM", b"x" * 8)])
    files, _ = collected(data)
    assert [f.path for f in files] == ["np2kai/FONT.ROM"]
    assert files[0].member == "np2kai\\FONT.ROM"


def test_extract_each_drops_archive_junk():
    data = zipped(
        [
            ("real.bin", b"x" * 8),
            ("__MACOSX/._real.bin", b"junk"),
            ("__MACOSX\\real.bin", b"junk"),
            ("sub/__MACOSX/real.bin", b"junk"),
            (".DS_Store", b"junk"),
            ("folder/.hidden", b"junk"),
            ("folder/.hidden/deep.bin", b"junk"),
        ]
    )
    files, report = collected(data)
    assert [f.path for f in files] == ["real.bin"]
    # Junk is dropped rather than reported: nobody meant to install it. The
    # backslashed and nested __MACOSX members matter here -- an unsafe *name*
    # would be skipped and counted, junk must not be.
    assert report.skipped == ()


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
        # DOS device aliases: NT resolves these before touching the directory,
        # on the stem alone, so the extension is no disguise.
        "CON",
        "aux.rom",
        "COM1.bin",
        "dc/nul.bin",
        # A trailing dot or space is stripped by Windows on write, storing the
        # file under a name that was never validated.
        "trailing.",
        "trailing ",
        "dotted./inside.bin",
    ],
)
def test_extract_each_refuses_an_unsafe_member(member):
    data = zipped([(member, b"x" * 8), ("good.bin", b"y" * 8)])
    files, report = collected(data)
    assert [f.path for f in files] == ["good.bin"]
    assert report.skipped == (member,)


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
        # DOS device names, however cased or dressed up with an extension.
        ("CON", None),
        ("prn", None),
        ("aux.rom", None),
        ("NUL.bin.rom", None),
        ("COM1.bin", None),
        ("com0.bin", None),
        ("lpt9", None),
        ("con .rom", None),  # NT ignores the trailing space in the stem too
        ("dc/AUX.bin", None),
        # ...but only the exact stem is a device: these are honest names.
        ("console.bin", "console.bin"),
        ("communist.rom", "communist.rom"),
        ("lpt10.bin", "lpt10.bin"),
        ("aux2.bin", "aux2.bin"),
        # A trailing dot or space would be stripped by Windows on write.
        ("foo.", None),
        ("foo ", None),
        ("dir./a.bin", None),
        # Single- and two-character components still pass.
        ("a", "a"),
        ("ab", "ab"),
        ("x" * 64, "x" * 64),
        ("x" * 65, None),
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
def test_extract_each_refuses_anything_that_is_not_a_regular_file(mode):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("link.bin")
        info.create_system = 3  # Unix, so external_attr really is a mode
        info.external_attr = mode << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr("real.bin", b"x" * 8)
    files, report = collected(buf.getvalue())
    assert [f.path for f in files] == ["real.bin"]
    assert report.skipped == ("link.bin",)


def test_a_non_unix_archive_mode_is_not_read_as_one():
    # create_system 0 is MS-DOS: the high half of external_attr is not a
    # mode, so it must not be mistaken for one and refused.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("dos.bin")
        info.create_system = 0
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b"x" * 8)
    files, _ = collected(buf.getvalue())
    assert [f.path for f in files] == ["dos.bin"]


def test_extract_each_refuses_too_many_files():
    data = zipped([(f"f{i}.bin", b"x") for i in range(20)])
    with pytest.raises(A.ArchiveError) as caught:
        collected(data, max_total_size=MAX, max_file_size=MAX, max_files=5)
    assert "more than the 5" in str(caught.value)


def test_extract_each_refuses_a_bomb_before_decompressing_it():
    data = zipped([("bomb.bin", b"\x00" * (1024 * 1024))])
    with pytest.raises(A.ArchiveError) as caught:
        collected(data, max_total_size=1024, max_file_size=MAX, max_files=10)
    assert "unpacks to" in str(caught.value)


def test_extract_each_enforces_the_total_against_the_real_bytes(monkeypatch):
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
        collected(data, max_total_size=4096, max_file_size=MAX, max_files=10)


def test_extract_each_skips_a_case_insensitive_duplicate():
    # Both names are individually safe, but on the case-insensitive disks of
    # Windows/macOS hosts the second write would clobber the first. Sorted
    # order means the uppercase one is always the survivor.
    data = zipped([("BIOS.bin", b"first"), ("bios.bin", b"second")])
    files, report = collected(data)
    assert [f.path for f in files] == ["BIOS.bin"]
    assert files[0].data == b"first"
    assert len(report.skipped) == 1
    assert report.skipped[0].startswith("bios.bin ")
    assert "case" in report.skipped[0]


def test_extract_each_spots_a_case_collision_in_a_folder_name():
    data = zipped(
        [("DC/boot.bin", b"upper"), ("dc/boot.bin", b"lower"), ("dc/extra.bin", b"ok")]
    )
    files, report = collected(data)
    assert [f.path for f in files] == ["DC/boot.bin", "dc/extra.bin"]
    assert len(report.skipped) == 1
    assert report.skipped[0].startswith("dc/boot.bin ")


def test_a_case_variant_of_a_member_that_was_not_taken_is_still_taken():
    # Only *accepted* members claim a name: the empty BIOS.bin is skipped for
    # being empty, so bios.bin collides with nothing and is kept.
    data = zipped([("BIOS.bin", b""), ("bios.bin", b"real")])
    files, report = collected(data)
    assert [f.path for f in files] == ["bios.bin"]
    assert report.skipped == ("BIOS.bin",)


def test_extract_each_skips_an_empty_member():
    data = zipped([("empty.bin", b""), ("real.bin", b"x" * 8)])
    files, report = collected(data)
    assert [f.path for f in files] == ["real.bin"]
    assert report.skipped == ("empty.bin",)


def test_extract_each_refuses_an_encrypted_member():
    data = bytearray(zipped([("secret.bin", b"x" * 8)]))
    data[data.find(b"PK\x03\x04") + 6] |= 0x01
    data[data.find(b"PK\x01\x02") + 8] |= 0x01
    with pytest.raises(A.ArchiveError, match="password-protected"):
        collected(bytes(data))


def test_extract_each_raises_when_nothing_is_usable():
    data = zipped([("../nope.bin", b"x" * 8)])
    with pytest.raises(A.NoSupportedMember) as caught:
        collected(data, what="BIOS file", **ALL_LIMITS)
    assert "no BIOS file this bot can use" in str(caught.value)


def test_extract_each_raises_on_an_empty_archive():
    with pytest.raises(A.NoSupportedMember):
        collected(zipped([]))


def test_extract_each_raises_on_something_that_is_not_a_zip():
    with pytest.raises(A.ArchiveError):
        collected(b"this is not a zip at all")


def test_the_password_sentence_counts_the_files_it_is_asking_for():
    # One sentence, one plural knob: `extract` is after a single file and
    # `extract_each` after a set, and both ask the uploader to unzip it.
    data = bytearray(zipped([("secret.gb", ROM)]))
    data[data.find(b"PK\x03\x04") + 6] |= 0x01
    data[data.find(b"PK\x01\x02") + 8] |= 0x01
    with pytest.raises(A.ArchiveError) as one:
        extract(bytes(data))
    with pytest.raises(A.ArchiveError) as many:
        collected(bytes(data))
    assert str(one.value).endswith("Unzip it yourself and upload the file inside.")
    assert str(many.value).endswith("Unzip it yourself and upload the files inside.")


# -- Unpacking without holding the whole archive ------------------------------
#
# The section above reads the walk's answers off a list; these are about the
# handing-over itself -- one file at a time, in order, dropped as soon as the
# sink has had it, which is what keeps a hundred-megabyte firmware pack from
# ever sitting in memory in one piece.


def test_extract_each_hands_over_every_file_in_sorted_order():
    data = zipped([("b.bin", b"bb"), ("a.bin", b"aa"), ("dc/boot.bin", b"cc")])
    files, report = collected(data)
    assert [f.path for f in files] == ["a.bin", "b.bin", "dc/boot.bin"]
    assert [f.data for f in files] == [b"aa", b"bb", b"cc"]
    assert report.paths == ("a.bin", "b.bin", "dc/boot.bin")
    assert report.members == ("a.bin", "b.bin", "dc/boot.bin")
    assert report.skipped == ()
    assert report.total_size == 6


def test_the_report_keeps_no_payloads_of_its_own():
    # The point of the exercise: what comes back is names and numbers, so a
    # caller that wrote each file out is not still holding all of them.
    _, report = collected(zipped([("a.bin", b"x" * 8), ("b.bin", b"y" * 8)]))
    assert not hasattr(report, "files")
    assert all(isinstance(path, str) for path in report.paths)


def test_extract_each_sees_one_file_at_a_time():
    # Each call arrives with the bytes of exactly one member -- nothing is
    # batched up and handed over at the end.
    seen = []

    def sink(extracted):
        seen.append((len(seen), extracted.path, len(extracted.data)))

    A.extract_each(
        zipped([("a.bin", b"x" * 8), ("b.bin", b"y" * 16), ("c.bin", b"z" * 32)]),
        sink=sink,
        **ALL_LIMITS,
    )
    assert seen == [(0, "a.bin", 8), (1, "b.bin", 16), (2, "c.bin", 32)]


def test_one_awkward_archive_exercises_every_skip_rule_at_once(tmp_path):
    # This used to unpack the same archive both ways and assert the two agreed,
    # back when `extract_all` was a second entry point that could drift from
    # this one. There is only one walk now, so there is nothing to compare --
    # but the archive itself is worth keeping: every skip rule in the module
    # firing in a single pass, which is how a real firmware pack goes wrong.
    # Written through a sink that writes to disk rather than `collected`, so the
    # rules are checked against the report the cog actually reads.
    data = zipped(
        [
            ("BIOS.bin", b"first"),
            ("bios.bin", b"second"),  # case collision
            ("../escape.bin", b"x" * 8),  # unsafe name
            ("empty.bin", b""),  # empty
            ("__MACOSX/._junk.bin", b"junk"),  # dropped, never reported
            ("dc/boot.bin", b"z" * 8),
        ]
    )
    written = []

    def sink(extracted):
        path = tmp_path / extracted.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(extracted.data)
        written.append(extracted.path)

    report = A.extract_each(data, sink=sink, **ALL_LIMITS)
    assert written == ["BIOS.bin", "dc/boot.bin"]
    assert report.paths == ("BIOS.bin", "dc/boot.bin")
    assert (tmp_path / "BIOS.bin").read_bytes() == b"first"
    assert (tmp_path / "dc/boot.bin").read_bytes() == b"z" * 8
    # Skips are reported in the order they were met, which is sorted member
    # order: the unsafe name and the empty member counted plainly, the case
    # collision saying what it collided with, and the junk not mentioned at all.
    assert len(report.skipped) == 3
    assert report.skipped[0] == "../escape.bin"
    assert report.skipped[1].startswith("bios.bin (differs from `BIOS.bin`")
    assert report.skipped[2] == "empty.bin"
    assert "__MACOSX/._junk.bin" not in report.members
    assert report.total_size == len(b"first") + 8


def test_extract_each_peaks_far_below_holding_every_member(tmp_path):
    # The whole point of item 15, measured: eight megabytes of firmware, one
    # megabyte at a time. The sink here writes and forgets, as the cog's does.
    #
    # The baseline used to be `extract_all`, the collecting entry point that
    # lived beside this one. With that gone the test builds the baseline itself,
    # which is all `extract_all` ever was -- a sink of `list.append`. Measuring
    # against a list rather than against a sibling function is if anything the
    # better test: what is being priced is the caller's choice to hold on, not
    # one library function against another.
    data = zipped([(f"f{i}.bin", bytes(1024 * 1024)) for i in range(8)])
    limits = dict(max_total_size=MAX, max_file_size=2 * 1024 * 1024, max_files=10)

    tracemalloc.start()
    A.extract_each(data, sink=lambda f: (tmp_path / f.path).write_bytes(f.data), **limits)
    streamed = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    tracemalloc.start()
    held, report = collected(data, **limits)
    assert report.total_size == 8 * 1024 * 1024
    assert len(held) == 8
    at_once = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert streamed < at_once / 2, (streamed, at_once)


def test_a_sink_that_fails_stops_the_unpack_then_and_there():
    # A full disk should not be answered by decompressing the rest of the
    # archive anyway. Whatever the sink managed before the failure stands.
    seen = []

    def sink(extracted):
        if len(seen) == 1:
            raise OSError("no space left on device")
        seen.append(extracted.path)

    with pytest.raises(OSError, match="no space left"):
        A.extract_each(
            zipped([("a.bin", b"x" * 8), ("b.bin", b"y" * 8), ("c.bin", b"z" * 8)]),
            sink=sink,
            **ALL_LIMITS,
        )
    assert seen == ["a.bin"]


def test_extract_each_keeps_every_cap():
    big = zipped([("big.bin", b"x" * 4096), ("small.bin", b"y" * 8)])
    files, report = collected(big, max_total_size=MAX, max_file_size=1024, max_files=10)
    assert [f.path for f in files] == ["small.bin"]
    assert report.skipped == ("big.bin",)

    with pytest.raises(A.ArchiveError, match="more than the 1"):
        collected(big, max_total_size=MAX, max_file_size=MAX, max_files=1)
    with pytest.raises(A.ArchiveError, match="unpacks to"):
        collected(big, max_total_size=64, max_file_size=MAX, max_files=10)


def test_extract_each_refuses_an_archive_with_nothing_usable_without_calling_the_sink():
    def sink(extracted):
        raise AssertionError(f"nothing should have been handed over: {extracted.path}")

    with pytest.raises(A.NoSupportedMember, match="no BIOS file this bot can use"):
        A.extract_each(
            zipped([("../nope.bin", b"x" * 8)]), sink=sink, what="BIOS file", **ALL_LIMITS
        )
    with pytest.raises(A.NoSupportedMember, match="no files in it"):
        A.extract_each(zipped([]), sink=sink, **ALL_LIMITS)
    with pytest.raises(A.ArchiveError):
        A.extract_each(b"this is not a zip at all", sink=sink, **ALL_LIMITS)
