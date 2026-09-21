"""The cog as Red's Downloader sees it: metadata, imports and syntax.

The cog has to run on nothing but what Red ships plus what info.json's
"requirements" installs. Nothing here needs Red, discord.py or a core.
"""

import ast
import json
import py_compile
import re
import sys

import pytest

from .loader import REPO_ROOT, load_standalone

#: retro/version.py imports nothing but the standard library, so it loads on
#: its own like systems.py does -- which is also what lets the version be
#: checked on a machine with neither Red nor discord.py.
V = load_standalone("retro_version_standalone", "version.py")

INFO_FILES = sorted(REPO_ROOT.glob("*/info.json")) + [REPO_ROOT / "info.json"]

#: What retro/info.json declares, and the module each one provides.
DECLARED = {"libretro.py": "libretro", "pillow": "PIL"}

#: Provided by Red-DiscordBot itself, so the cog may import them undeclared.
FROM_RED = {"redbot", "discord", "aiohttp"}

#: The cog's own modules. Normally they are imported relatively (which the
#: scanner below already skips), but `python retro/emulator.py` runs with no
#: package at all and `retro/` as sys.path[0], so that one path imports its
#: sibling by bare name. See the fallback import at the top of emulator.py.
SIBLING_MODULES = {path.stem for path in (REPO_ROOT / "retro").glob("*.py")}


@pytest.mark.parametrize("path", INFO_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_info_json_is_valid_json(path):
    assert json.loads(path.read_text())


def test_the_cog_declares_exactly_the_requirements_it_needs():
    requirements = json.loads((REPO_ROOT / "retro" / "info.json").read_text())["requirements"]
    # Red does NOT ship Pillow, so it has to stay declared. A development
    # tool (pytest, hypothesis, ruff) must never appear here.
    assert sorted(requirements) == sorted(DECLARED), requirements


def imported_names():
    """Top-level module -> the cog files that import it."""
    used = {}
    for path in sorted((REPO_ROOT / "retro").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative, i.e. this cog
                names = [node.module or ""]
            else:
                continue
            for name in names:
                used.setdefault(name.split(".")[0], set()).add(path.name)
    return used


def test_nothing_is_imported_that_info_json_does_not_declare():
    allowed = (
        set(sys.stdlib_module_names)
        | FROM_RED
        | set(DECLARED.values())
        | SIBLING_MODULES
    )
    undeclared = {k: sorted(v) for k, v in imported_names().items() if k not in allowed}
    assert not undeclared, f"undeclared imports: {undeclared}"


@pytest.mark.parametrize("tool", ["pytest", "hypothesis", "ruff", "_pytest"])
def test_no_development_tool_leaks_into_the_cog(tool):
    assert tool not in imported_names()


@pytest.mark.parametrize(
    "path",
    sorted((REPO_ROOT / "retro").glob("*.py")),
    ids=lambda p: p.name,
)
def test_every_cog_source_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / f"{path.stem}.pyc"), doraise=True)


def test_the_emulator_and_tables_import_with_nothing_installed():
    # systems.py, archives.py and version.py are the modules the cog's own
    # tests and CI load on their own; they must never grow a third-party
    # import. version.py is in here because `[p]retroset version` has to be
    # able to answer on any install, however broken.
    for name in ("systems.py", "archives.py", "version.py"):
        source = (REPO_ROOT / "retro" / name).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                assert imported.split(".")[0] in sys.stdlib_module_names, (name, imported)


def test_the_readme_lists_the_cog():
    assert "[Retro](retro)" in (REPO_ROOT / "README.md").read_text()


def test_the_dev_requirements_are_not_the_cog_requirements():
    dev = (REPO_ROOT / "requirements-dev.txt").read_text()
    assert "pytest" in dev
    info = json.loads((REPO_ROOT / "retro" / "info.json").read_text())
    assert not any("pytest" in requirement for requirement in info["requirements"])


def test_the_test_suite_does_not_live_in_the_cog_directory():
    # Red copies retro/ into the bot's data directory; the tests must not
    # ride along with it.
    assert not list((REPO_ROOT / "retro").glob("test_*.py"))
    assert (REPO_ROOT / "tests" / "conftest.py").is_file()


def test_pytest_markers_are_registered():
    config = (REPO_ROOT / "pyproject.toml").read_text()
    for marker in ("emulator:", "slow:", "network:", "redbot:"):
        assert marker in config, marker


# -- The documentation says what the cog does ---------------------------------
#
# Cheap checks, but each one corresponds to a change that had a README section
# describing the *old* behaviour until somebody remembered to update it.

COG_README = (REPO_ROOT / "retro" / "README.md").read_text()
COG_INFO = json.loads((REPO_ROOT / "retro" / "info.json").read_text())
COG_SOURCE = (REPO_ROOT / "retro" / "Retro.py").read_text()
MIGRATION_SOURCE = (REPO_ROOT / "retro" / "migration.py").read_text()


def test_the_cog_module_is_named_after_its_class():
    assert (REPO_ROOT / "retro" / "Retro.py").is_file()
    assert not (REPO_ROOT / "retro" / "RetroCog.py").exists()
    # The class is assembled from mixins (see retro/abc.py), so the bases are
    # several -- but it is still exactly one class, still called Retro, and
    # still a commands.Cog, which is what Red keys the Config namespace and
    # the data directory off. Asserted on the parsed source rather than on a
    # substring, so reordering the mixins cannot break it.
    declaration = next(
        node
        for node in ast.parse(COG_SOURCE).body
        if isinstance(node, ast.ClassDef) and node.name == "Retro"
    )
    bases = [ast.unparse(base) for base in declaration.bases]
    assert "commands.Cog" in bases, bases
    init = (REPO_ROOT / "retro" / "__init__.py").read_text()
    assert "from .Retro import Retro" in init
    assert "RetroCog" not in init


def test_the_readme_documents_the_rename_and_its_migration():
    assert "Upgrading from an earlier version" in COG_README
    assert "RetroCog" in COG_README
    assert "cog list" in COG_README


def test_no_documentation_still_offers_retroset_core():
    # The command is gone; the README may explain that it is gone, but must
    # not present it as something to run.
    for line in COG_README.splitlines():
        if line.startswith("- `[p]retroset core <path>"):
            raise AssertionError(line)
    assert "[p]retroset core <path>` command for pointing" in COG_README


def test_the_readme_says_the_replay_button_is_gone_and_what_it_saved():
    # The docs may explain that it was removed -- that is where the 8 MiB a
    # session no longer holds is written down -- but must not present it as
    # something to press. The clip that replaced the buffer has since gone
    # too (nothing read it), so what the README now states is that a session
    # holds no footage at all.
    assert "Replay** shows the last" not in COG_README
    assert "session keeps no footage at all" in COG_README
    assert "8 MiB" in COG_README
    for line in COG_README.splitlines():
        assert "🔁" not in line, line
        assert "Replay 12s" not in line, line


def test_the_readme_describes_the_press_line():
    assert "Who pressed which button is written on the message" in COG_README
    # All five actions, in one voice, with the presser named.
    for line in (
        "Rob pressed A.",
        "Rob pressed ⬅️.",
        "Rob pressed A ×3.",
        "Rob waited.",
        "Rob undid the last press.",
        "Rob reset the game.",
    ):
        assert line in COG_README, line
    # The impersonal fallback, for a presser who cannot be named.
    assert "`Pressed A.`" in COG_README
    # The one that would be wrong if the line were built from RetroPad names.
    assert "`Rob pressed C.`" in COG_README


def test_the_readme_promises_a_press_can_never_notify_anybody():
    """The two guards, both written down, because either alone would do.

    A ping on every button press is the thing that would make the cog
    unusable in a channel anybody is in, so the README has to say plainly
    that it cannot happen and how.
    """
    assert "It can never notify anybody" in COG_README
    assert "no mention syntax is emitted at all" in COG_README
    assert "allowed_mentions" in COG_README
    assert "32 characters" in COG_README, "the length cap"
    assert "zero-width" in COG_README


def test_the_readme_says_when_the_repeat_button_is_there():
    """It exists; it is hidden at short clip lengths; that is the news.

    The greyed-out version was read as the feature having been removed, so
    the README now has a section saying it is there, when it appears, and
    that nothing else moves when it does not.
    """
    assert "### The ×3 button, and when it is there" in COG_README
    assert "hidden, not greyed out" in COG_README
    assert "Wait and Undo never move" in COG_README
    # The table's endpoints: the floor with no button, the default with three.
    assert "| 0.2s (the floor) | 1 | not shown |" in COG_README
    assert "| 1s (the default) | 3 | `A ×3` |" in COG_README


def test_the_readme_states_the_clip_timing_invariant():
    """And the decision behind it; see test_emulator.py for the numbers."""
    assert "**A clip plays for exactly as long as it emulated**" in COG_README
    assert "nothing dropped off either end" in COG_README
    # The case that settled how the opening of a clip is fixed, and which
    # would have been destroyed by fixing it the other way.
    assert "17ms flash" in COG_README


def test_the_readme_describes_the_clip_preroll_and_its_bound():
    """The clip-timing section, which twice described the old behaviour.

    It used to say that the dead lead-in stays and that trimming it was
    rejected -- true right up until the pre-roll replaced it. The invariant
    above is the one thing about a clip that has to keep being true, so both
    it and the mechanism that now keeps it have to keep being written down.
    """
    assert "**A clip starts where the last one ended.**" in COG_README
    assert "**The pre-roll is bounded at a quarter of a second**" in COG_README
    # The two cases the section is an argument about: the game that sits
    # still and reacts late, and the one that never reacts at all.
    assert "replay a bit from the previous clip" in COG_README
    assert "has no pre-roll at all" in COG_README, "Wait, Undo, a boot, a reset"
    # ...and it must not still claim the lead-in is left alone.
    assert "So the lead-in stays" not in COG_README
    assert "test_the_dead_lead_in_is_photographed_rather_than_trimmed" not in COG_README


def test_the_readme_describes_resetting_a_game():
    assert "### Resetting a game" in COG_README
    assert "[p]retroreset" in COG_README
    # The distinction that has to be unmistakable, and the three decisions.
    assert "completely different" in COG_README
    assert "Nothing on disk is written" in COG_README
    assert "battery save is not touched" in COG_README
    assert "A reset is an undo point" in COG_README
    assert "command and not a button" in COG_README


def test_the_readme_describes_the_undo_button():
    assert "### Undo" in COG_README
    assert "steps the game back one press" in COG_README
    # The two decisions somebody reading it has to know about: what a restart
    # does to the history, and that the buffer rewinds with the game.
    assert "The history is in memory only" in COG_README
    assert "undo's own clip replaces the undone press's" in COG_README
    assert "↩️ Undo" in COG_README, "and it is drawn in the layout"


def test_the_readme_describes_the_resume_button():
    assert "Resume" in COG_README
    assert "after a bot restart" in COG_README


def test_the_readme_describes_unpacking_a_whole_bios_zip():
    assert "installs everything in it" in COG_README
    assert "dc/dc_boot.bin" in COG_README


def test_the_readme_documents_the_save_commands():
    assert "## Managing saves" in COG_README
    for command in (
        "[p]retrosaves export",
        "[p]retrosaves import",
        "[p]retrosaves reset",
        "[p]retrosaves delete",
    ):
        assert command in COG_README, command


def test_the_readme_distinguishes_resetting_from_deleting():
    # The one thing somebody about to run these has to understand.
    assert "restart from my last in-game save" in COG_README
    assert "start this game completely fresh" in COG_README


def test_the_readme_explains_the_live_session_rule():
    assert "saved and put to sleep first" in COG_README


def test_the_readme_says_who_may_destroy_a_save():
    assert "Manage Messages" in COG_README
    assert "open to the channel" in COG_README


def test_the_readme_explains_what_the_cog_forgets_and_what_it_keeps():
    """The one place a reader could lose progress by misunderstanding.

    A session or Resume record is a pointer and is deleted automatically; a
    save state and a battery save are the player's progress and are not. The
    docs have to draw that line, name all four things that drop a record,
    and say which way the deleted-channel decision went.
    """
    assert "### What the cog forgets, and what it never does" in COG_README
    for phrase in (
        "dropping a record loses the button and nothing else",
        "the cached ROM it named was pruned",
        "the channel or thread was deleted",
        "the bot left the server",
        "can no longer see the channel",
        "saves for a deleted channel are deliberately kept",
        # The hazard worth writing down: Red loads cogs before logging in.
        "before logging in",
    ):
        assert phrase in COG_README, phrase


def test_the_documented_core_download_size_is_not_the_unpacked_one():
    """Two figures, and neither the docs nor the help may quote only one.

    The buildbot's zips for the seven cores are about 4.5 MiB and the shared
    objects they unpack to are about 31 MiB (genesis_plus_gx alone is 12).
    Only the second one counts against `[p]retroset diskbudget`, and every
    place that quoted 4.5 MiB for the disk was out by a factor of seven.
    """
    for where, text in (
        ("retro/README.md", COG_README),
        ("retro/Retro.py", COG_SOURCE),
        ("retro/storage.py", (REPO_ROOT / "retro" / "storage.py").read_text()),
    ):
        assert "4.5 MiB" in text, where
        assert "31 MiB" in text, where


def test_the_readme_documents_every_command_the_cog_publishes():
    """A command nobody wrote down is a command nobody finds.

    `[p]retroset diskbudget` and `[p]retroset allowprivateurls` were both
    missing from the list for a while, and the second of those is the one
    that turns a security guard off.
    """
    listed = {
        line.split("`")[1].split(" ")[0]
        for line in COG_README.splitlines()
        if line.startswith("- `[p]retro")
    }
    for command in ("[p]retrosaves", "[p]retroset", "[p]retro"):
        assert command in listed or any(c.startswith(command) for c in listed)
    for command in (
        "[p]retroset allowprivateurls",
        "[p]retroset diskbudget",
        "[p]retrosaves rollback",
    ):
        assert f"- `{command}" in COG_README, command


def test_the_readme_documents_the_url_guard_the_cog_points_at():
    """`[p]retroset game add` refers the owner to it by name.

    The reply used to say "see the SSRF note in the README" when the README
    had no such note anywhere in it.
    """
    assert "## ROM URLs" in COG_README
    assert "**ROM URLs** in the " in COG_SOURCE, "the reply points nowhere"
    for phrase in ("169.254.169.254", "redirect hop is checked", "the same sentence"):
        assert phrase in COG_README, phrase


def test_the_readme_says_what_a_corrupt_rom_does():
    assert "## Corrupt and unplayable ROMs" in COG_README
    assert "the emulator is freed and no half-started session is left behind" in COG_README
    assert "test_malformed_roms.py" in COG_README


def test_the_readme_describes_the_single_restore_chain():
    assert "One restore chain, two doors" in COG_README


@pytest.mark.parametrize(
    "phrase",
    [
        "Resume",
        "Undo",
        "says who pressed which button",
        "never a ping",
        "[p]retroreset",
        "detected automatically",
        "[p]retrosaves",
    ],
)
def test_info_json_describes_the_new_behaviour(phrase):
    assert phrase in COG_INFO["description"], phrase


def test_info_json_no_longer_advertises_the_replay_button():
    assert "Replay" not in COG_INFO["description"]
    assert "Replay" not in COG_INFO["end_user_data_statement"]


def test_the_end_user_data_statement_mentions_what_is_now_stored():
    statement = COG_INFO["end_user_data_statement"]
    assert "Resume" in statement
    assert "memory only" in statement
    # The undo history is a stack of save states in memory, which is a thing
    # the cog holds about a channel's game and therefore a thing to declare.
    assert "Undo" in statement
    # Saves can now leave the bot as an attachment and arrive as one.
    assert "export" in statement and "delete" in statement
    # A press now puts somebody's display name into the message text, which
    # is worth declaring even though nothing keeps a copy of it.
    assert "display name of whoever last pressed a button" in statement
    assert "is not stored anywhere either" in statement


# -- The version, and why it cannot go stale ----------------------------------
#
# This exists because a bot twice ran an older build than master and the
# symptom looked like a bug in the cog: `[p]retroset cliplength 0.8` came
# back "must be an integer" on a copy that predated the clip length becoming
# a float. `[p]retroset version` answers it now, out of three facts -- see
# retro/version.py.


def test_info_json_declares_a_version():
    assert "version" in COG_INFO, sorted(COG_INFO)
    assert re.fullmatch(r"\d+\.\d+\.\d+", COG_INFO["version"]), COG_INFO["version"]


def test_the_module_and_info_json_cannot_disagree_about_the_version():
    # Not two values held in step by this test: info.json is the only place
    # the number is written and version.py *reads* it, so the assertion is
    # that the reading works rather than that somebody remembered to copy it.
    assert V.VERSION == COG_INFO["version"]
    assert V.VERSION != V.UNKNOWN_VERSION


def test_no_python_file_carries_a_version_literal_of_its_own():
    """The one kind of drift that has to be impossible rather than tested."""
    pattern = re.compile(r"""^\s*(__version__|VERSION)\s*=\s*["']""")
    offenders = []
    for path in sorted((REPO_ROOT / "retro").glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.match(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, offenders


def test_the_version_degrades_instead_of_raising(tmp_path):
    missing = tmp_path / "nothing-here.json"
    assert V.read_version(missing) == V.UNKNOWN_VERSION
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all")
    assert V.read_version(broken) == V.UNKNOWN_VERSION
    for payload in ('{"name": "Retro"}', '{"version": ""}', '{"version": 3}'):
        broken.write_text(payload)
        assert V.read_version(broken) == V.UNKNOWN_VERSION, payload
    good = tmp_path / "good.json"
    good.write_text('{"version": " 9.9.9 "}')
    assert V.read_version(good) == "9.9.9"


def test_the_fingerprint_is_of_the_loaded_sources():
    # Captured at import, which is what makes it an answer about the running
    # code rather than about whatever is on disk now; see version.py.
    assert V.FINGERPRINT and len(V.FINGERPRINT) == V.FINGERPRINT_LENGTH
    assert V.FINGERPRINT == V.code_fingerprint()
    assert all(c in "0123456789abcdef" for c in V.FINGERPRINT)


def test_the_fingerprint_changes_with_the_code_and_with_a_new_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    first = V.code_fingerprint(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    second = V.code_fingerprint(tmp_path)
    (tmp_path / "b.py").write_text("")
    third = V.code_fingerprint(tmp_path)
    assert len({first, second, third}) == 3, (first, second, third)
    # A directory with no sources at all has no fingerprint, rather than the
    # hash of nothing (which would look like a real answer).
    assert V.code_fingerprint(tmp_path / "empty") is None


def test_the_commit_is_read_from_the_files_and_never_from_a_subprocess():
    # There may be no git binary, and a command that answers a question must
    # not be able to hang on one.
    importers = imported_names().get("subprocess", set())
    assert "version.py" not in importers, importers
    source = (REPO_ROOT / "retro" / "version.py").read_text()
    assert "GIT_SEARCH_DEPTH = 2" in source, "a deeper walk finds other repos"


def test_the_commit_of_this_checkout_is_found_and_is_a_real_sha():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("this copy of the cog is not in a git checkout")
    checkout = V.git_checkout()
    assert checkout is not None
    assert len(checkout.commit) in (40, 64), checkout.commit
    assert all(c in "0123456789abcdef" for c in checkout.commit)


def test_a_missing_git_directory_is_an_ordinary_answer(tmp_path):
    # The case every Downloader install is in: Red copies the package into
    # the bot's cog folder, which is not a checkout. It must degrade
    # silently, never raise, and never claim a commit.
    package = tmp_path / "somewhere" / "retro"
    package.mkdir(parents=True)
    assert V.git_checkout(package) is None
    assert V._git_dir(package) is None


def test_a_broken_git_directory_is_also_an_ordinary_answer(tmp_path):
    package = tmp_path / "repo" / "retro"
    package.mkdir(parents=True)
    git = tmp_path / "repo" / ".git"
    git.mkdir()
    assert V.git_checkout(package) is None, "no HEAD at all"
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert V.git_checkout(package) is None, "a ref that resolves to nothing"
    (git / "HEAD").write_text("this is not a commit id\n")
    assert V.git_checkout(package) is None
    # ...and the two shapes that do work: a packed ref, and a detached HEAD.
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{'a' * 40} refs/heads/main\n"
        f"^{'b' * 40}\n"
    )
    assert V.git_checkout(package) == ("a" * 40, "main")
    (git / "HEAD").write_text("c" * 40 + "\n")
    assert V.git_checkout(package) == ("c" * 40, None)


def test_the_readme_documents_the_version_command():
    assert "[p]retroset version" in COG_README
    assert "am I running the new code" in COG_README


def test_the_load_bearing_constants_are_still_in_the_source():
    # Both are baked into data that already exists; see their comments.
    assert 'CUSTOM_ID_PREFIX = "libretro"' in (
        REPO_ROOT / "retro" / "RetroView.py"
    ).read_text()
    assert "114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121" in COG_SOURCE
    # The old cog name lives with the migration that is keyed on it, and is
    # re-exported from Retro.py so `retro.Retro.LEGACY_COG_NAME` still works.
    assert 'LEGACY_COG_NAME = "RetroCog"' in MIGRATION_SOURCE
    assert "LEGACY_COG_NAME" in COG_SOURCE
