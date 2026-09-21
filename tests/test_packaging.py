"""The cog as Red's Downloader sees it: metadata, imports and syntax.

The cog has to run on nothing but what Red ships plus what info.json's
"requirements" installs. Nothing here needs Red, discord.py or a core.
"""

import ast
import json
import py_compile
import sys

import pytest

from .loader import REPO_ROOT

INFO_FILES = sorted(REPO_ROOT.glob("*/info.json")) + [REPO_ROOT / "info.json"]

#: What retro/info.json declares, and the module each one provides.
DECLARED = {"libretro.py": "libretro", "pillow": "PIL"}

#: Provided by Red-DiscordBot itself, so the cog may import them undeclared.
FROM_RED = {"redbot", "discord", "aiohttp"}


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
    allowed = set(sys.stdlib_module_names) | FROM_RED | set(DECLARED.values())
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
    # systems.py and archives.py are the two modules the cog's own tests and
    # CI load on their own; they must never grow a third-party import.
    for name in ("systems.py", "archives.py"):
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


def test_the_cog_module_is_named_after_its_class():
    assert (REPO_ROOT / "retro" / "Retro.py").is_file()
    assert not (REPO_ROOT / "retro" / "RetroCog.py").exists()
    assert "class Retro(commands.Cog)" in COG_SOURCE
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


def test_the_readme_describes_the_fifteen_second_replay():
    assert "Replay** shows the last **15 seconds" in COG_README
    assert "memory only" in COG_README


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


def test_the_readme_describes_the_single_restore_chain():
    assert "One restore chain, two doors" in COG_README


@pytest.mark.parametrize(
    "phrase", ["Replay", "Resume", "detected automatically", "[p]retrosaves"]
)
def test_info_json_describes_the_new_behaviour(phrase):
    assert phrase in COG_INFO["description"], phrase


def test_the_end_user_data_statement_mentions_what_is_now_stored():
    statement = COG_INFO["end_user_data_statement"]
    assert "Resume" in statement
    assert "memory only" in statement
    # Saves can now leave the bot as an attachment and arrive as one.
    assert "export" in statement and "delete" in statement


def test_the_load_bearing_constants_are_still_in_the_source():
    # Both are baked into data that already exists; see their comments.
    assert 'CUSTOM_ID_PREFIX = "libretro"' in (
        REPO_ROOT / "retro" / "RetroView.py"
    ).read_text()
    assert "114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121" in COG_SOURCE
    assert 'LEGACY_COG_NAME = "RetroCog"' in COG_SOURCE
