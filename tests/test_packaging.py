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


def string_literals_outside_docstrings(path):
    """Every ``str`` constant in one file that is not a docstring.

    A docstring is the one place `[p]` belongs: Red rewrites it there, for
    command help. Nowhere else -- so an f-string's literal parts count too,
    which they do here because ast.walk reaches the Constant nodes inside a
    JoinedStr.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and ast.get_docstring(node, clean=False) is not None:
            docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def test_no_sent_string_carries_a_literal_command_prefix():
    """`[p]` is a docstring convention, not a substitution Red does anywhere.

    Red rewrites `[p]` in a command's *help text* -- that is the whole of it.
    A string the cog builds and sends reaches the channel exactly as
    written, so `[p]retro <name>` is an instruction to type a prefix nobody
    has. Three sent strings used to do this (the disk budget's refusal, the
    Resume button's "the ROM has been cleaned up" reply and the save
    importer's), and a fourth, `[p]retroset version`'s footer, was found
    while fixing them.

    This is a source-level test rather than a behavioural one on purpose:
    the mistake is cheap to make, invisible until somebody reads the
    message, and reachable from paths (a button click, a background write)
    that no test necessarily walks. Every caller now passes a real prefix --
    `ctx.clean_prefix`, or `Retro._prefix_for` where there is no context --
    so the literal has no remaining honest use outside a docstring.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "retro").glob("*.py")):
        for lineno, value in string_literals_outside_docstrings(path):
            if "[p]" in value:
                offenders.append(f"{path.name}:{lineno}: {value[:70]!r}")
    assert not offenders, (
        "these strings are not docstrings, so Red will not substitute their "
        f"`[p]`: {offenders}"
    )


def test_the_readme_lists_the_cog():
    assert "[Retro](retro)" in (REPO_ROOT / "README.md").read_text()


def test_the_test_suite_does_not_live_in_the_cog_directory():
    # Red copies retro/ into the bot's data directory; the tests must not
    # ride along with it.
    assert not list((REPO_ROOT / "retro").glob("test_*.py"))
    assert (REPO_ROOT / "tests" / "conftest.py").is_file()


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


def published_commands():
    """Every command the cog publishes, by qualified name.

    Read off the parsed source of every module the cog class is assembled
    from, so it works without the real Red installed and cannot fall behind
    a command declared in a mixin. A subcommand's decorator names its group
    (``@retroset.command(name="version")``), which is what gives the
    qualified name.
    """
    found = {}
    for module in ("Retro.py", "saves.py", "cores.py", "storage.py", "migration.py"):
        tree = ast.parse((REPO_ROOT / "retro" / module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = ast.unparse(decorator.func)
                if not target.endswith((".command", ".group")):
                    continue
                name = next(
                    (
                        keyword.value.value
                        for keyword in decorator.keywords
                        if keyword.arg == "name"
                    ),
                    node.name,
                )
                found[node.name] = (target.rsplit(".", 1)[0], name)
                break
    def qualified(function):
        owner, name = found[function]
        return f"{qualified(owner)} {name}" if owner in found else name

    return {qualified(function) for function in found}


def test_the_readme_documents_every_command_the_cog_publishes():
    """A command nobody wrote down is a command nobody finds.

    `[p]retroset diskbudget` and `[p]retroset allowprivateurls` were both
    missing from the list for a while, and the second of those is the one
    that turns a security guard off. Derived from the source rather than
    listed here, so a new command has to be documented on the day it is
    added.
    """
    commands = published_commands()
    assert len(commands) > 25, commands
    missing = sorted(name for name in commands if f"[p]{name}" not in COG_README)
    assert not missing, f"undocumented commands: {missing}"
    # ...and the top-level ones are in the summary list, not only in prose.
    listed = {
        line.split("`")[1].removeprefix("[p]").split()[0]
        for line in COG_README.splitlines()
        if line.startswith("- `[p]retro")
    }
    assert {name for name in commands if " " not in name} <= listed


def test_every_readme_section_the_cog_sends_people_to_exists():
    """A reply that names a heading must name one that is there.

    `[p]retroset game add` told the owner to read "**ROM URLs** in the
    README" when the README had no such section anywhere in it.
    """
    headings = {
        line.lstrip("#").strip()
        for line in COG_README.splitlines()
        if line.startswith("#")
    }
    pointed_at = set()
    for module in sorted((REPO_ROOT / "retro").glob("*.py")):
        for _lineno, value in string_literals_outside_docstrings(module):
            for match in re.finditer(r"\*\*([^*]+)\*\* in the ", value):
                pointed_at.add(match.group(1))
    assert pointed_at, "nothing points at the README any more"
    assert pointed_at <= headings, sorted(pointed_at - headings)


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


def test_both_source_facts_come_from_one_walk_over_the_files(tmp_path):
    """One glob, one visit per file: the two answers are about the same files.

    They were two passes -- glob, read, hash; glob again, stat -- run back
    to back at import. Folding them together must not move either answer, so
    both are checked against the named functions that still exist for
    callers who only want one of them.
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "notes.txt").write_text("not a source file\n")
    scan = V.scan_sources(tmp_path)
    assert scan.fingerprint == V.code_fingerprint(tmp_path)
    assert scan.newest == V.newest_source_time(tmp_path)
    assert scan.newest == max(
        (tmp_path / name).stat().st_mtime for name in ("a.py", "b.py")
    )
    assert V.scan_sources(tmp_path / "empty") == (None, None)
    # ...and the module's own two constants came from one scan of its own
    # package, captured at import. Asserted on the source rather than by
    # re-scanning retro/, which would only be re-reading the same files.
    source = (REPO_ROOT / "retro" / "version.py").read_text()
    assert "_SOURCES = scan_sources()" in source
    assert "FINGERPRINT: typing.Optional[str] = _SOURCES.fingerprint" in source
    assert "SOURCE_TIME: typing.Optional[float] = _SOURCES.newest" in source


def test_the_checkout_is_read_from_the_files_and_never_from_a_subprocess():
    # There may be no git binary, and a command that answers a question must
    # not be able to hang on one.
    importers = imported_names().get("subprocess", set())
    assert "version.py" not in importers, importers
    source = (REPO_ROOT / "retro" / "version.py").read_text()
    assert "GIT_SEARCH_DEPTH = 2" in source, "a deeper walk finds other repos"


def test_only_head_is_read_and_git_s_ref_storage_is_left_to_git():
    """One line of HEAD, and no reimplementation of anything else.

    Resolving a branch to its commit id meant parsing loose refs *and*
    packed-refs -- around ninety lines of git's on-disk format, for one
    decoration on one line of chat output. The fingerprint is what actually
    answers "am I running the new code?", so the branch name is shown as
    HEAD writes it and nothing here has to keep up with how git stores refs.
    """
    # The prose still explains what was dropped and why, so the file name is
    # looked for as a string the code could open rather than as a word.
    literals = {value for _lineno, value in string_literals_outside_docstrings(
        REPO_ROOT / "retro" / "version.py"
    )}
    assert "packed-refs" not in literals, literals
    assert not hasattr(V, "_resolve_ref")


def test_the_checkout_of_this_copy_is_the_branch_head_is_on():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("this copy of the cog is not in a git checkout")
    checkout = V.git_checkout()
    assert checkout is not None
    # Exactly one of the two, always: a branch name, or a detached HEAD's
    # commit id. Which one this checkout is depends on the machine, so both
    # shapes are asserted properly against a made-up .git below.
    assert bool(checkout.branch) != bool(checkout.commit), checkout
    if checkout.commit:
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


def test_a_branch_is_named_and_a_detached_head_is_a_commit(tmp_path):
    package = tmp_path / "repo" / "retro"
    package.mkdir(parents=True)
    git = tmp_path / "repo" / ".git"
    git.mkdir()
    assert V.git_checkout(package) is None, "no HEAD at all"
    (git / "HEAD").write_text("this is not a commit id\n")
    assert V.git_checkout(package) is None
    # A branch is reported by name, with no ref file anywhere: what HEAD
    # says is the whole answer, which is the point of reading only HEAD.
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert V.git_checkout(package) == (None, "main")
    assert not (git / "refs").exists(), "and nothing else was needed"
    (git / "HEAD").write_text("ref: refs/heads/feature/long-name\n")
    assert V.git_checkout(package) == (None, "long-name")
    # ...and a detached HEAD, which is what `git checkout <tag>` leaves, has
    # no branch to name, so the commit id in the file is shown instead.
    (git / "HEAD").write_text("c" * 40 + "\n")
    assert V.git_checkout(package) == ("c" * 40, None)


def test_a_worktree_or_submodule_gitdir_file_is_followed(tmp_path):
    """``.git`` is a file, not a directory, in a worktree or a submodule.

    Kept (it is a handful of lines) because it is exactly the layout a
    developer checking "am I running the new code?" tends to be in, and
    without it those installs silently report no checkout at all.
    """
    package = tmp_path / "wt" / "retro"
    package.mkdir(parents=True)
    real = tmp_path / "main" / ".git" / "worktrees" / "wt"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/side\n")
    (tmp_path / "wt" / ".git").write_text(f"gitdir: {real}\n")
    assert V._git_dir(package) == real
    assert V.git_checkout(package) == (None, "side")


def test_the_load_bearing_constants_are_still_in_the_source():
    # All three are baked into data that already exists; see their comments.
    # They live with the thing keyed on them and are re-exported from
    # Retro.py, so `retro.Retro.LEGACY_COG_NAME` still works.
    assert 'CUSTOM_ID_PREFIX = "libretro"' in (
        REPO_ROOT / "retro" / "RetroView.py"
    ).read_text()
    assert (
        "CONFIG_IDENTIFIER = 114+111+98+108+111+97+99+104+45+99+111+103+115+47+112+121+98+111+121"
        in MIGRATION_SOURCE
    )
    assert 'LEGACY_COG_NAME = "RetroCog"' in MIGRATION_SOURCE
    for name in ("LEGACY_COG_NAME", "CONFIG_IDENTIFIER"):
        assert name in COG_SOURCE, name


# -- One word per concept, everywhere a player can see it ---------------------
#
# Four names were in use for two concepts -- "save state", "battery save",
# "in-game save" and "SRAM" -- sometimes in adjacent sentences. A player
# meeting two of them for the same file has no way to know they are the same
# file. So: **save state** for the exact moment, **in-game save** for what the
# player saved from inside the game, and nothing else.
#
# "Battery save" and "SRAM" survive in comments, internal docstrings and
# identifiers (`_sram_path`, `.srm`), where they are accurate and
# developer-facing. What is checked here is the text a player really reads:
# every **command docstring**, because Red turns those into `[p]help`.

#: The one place both words are *defined*, and therefore the one place a
#: synonym belongs: the `[p]retrosaves` group's own help.
VOCABULARY_EXEMPTION = "other emulators call it a battery"


def command_docstrings(module_name):
    """``{function name: docstring}`` for every command in one module.

    A command is a function whose decorators include a ``.command(...)`` or
    ``.group(...)`` call -- which is exactly what Red publishes and exactly
    what it turns into help text. Read off the parsed source rather than off
    the cog class, so it works without the real Red installed.
    """
    source = (REPO_ROOT / "retro" / module_name).read_text()
    found = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = ast.unparse(decorator.func)
            if target.endswith((".command", ".group")):
                doc = ast.get_docstring(node)
                if doc:
                    found[node.name] = doc
                break
    return found


@pytest.mark.parametrize("module", ["saves.py", "Retro.py"])
def test_no_command_help_text_says_battery_save_or_sram(module):
    docs = command_docstrings(module)
    assert docs, module
    offenders = {}
    for name, doc in docs.items():
        if VOCABULARY_EXEMPTION in doc:
            continue
        lowered = doc.lower()
        if "battery save" in lowered or "sram" in lowered:
            offenders[name] = doc
    assert not offenders, sorted(offenders)


def test_every_command_that_names_a_save_uses_one_of_the_two_words():
    """And they really do talk about saves, so this is not vacuous."""
    docs = command_docstrings("saves.py")
    talkers = [
        name
        for name, doc in docs.items()
        if "save state" in doc or "in-game save" in doc
    ]
    assert len(talkers) >= 5, talkers
