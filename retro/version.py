"""
Which build of this cog is actually running, answerable from inside Discord.

This exists because "am I running the new code?" has cost real debugging
time twice: `[p]retroset cliplength 0.8` came back with *must be an integer*
on a bot whose copy of the cog predated the clip length becoming a float,
and the answer looked like a bug in the cog rather than a stale install.
`[p]retroset version` (and the top of `[p]retroset settings`) answers it.

Three separate facts, because only together are they honest:

* **the version**, from ``retro/info.json``. That file is the single source
  of truth and this module *reads* it -- there is no version literal in any
  ``.py`` here to fall out of step with it, which is the one kind of drift
  that cannot then happen. It is still a number a human has to remember to
  bump, which is exactly why it is not the only thing shown.
* **the checkout**, when the cog was installed from one: Red's Downloader
  clones a repo, so ``<repo>/.git`` is usually one directory above this
  package. What is shown is the one line of ``.git/HEAD`` -- the branch it
  is on, or the commit id when HEAD is detached. Absent (the cog copied in
  by hand, an installed tree with no ``.git``) it is simply not mentioned.
  Read from the files, never by running ``git``: there may be no git
  binary, and a subprocess on a command that answers a question is not
  worth the risk.
* **a fingerprint of the loaded source**, which is the part that cannot go
  stale. It is a hash of every ``retro/*.py`` as they were when this module
  was imported -- i.e. when the cog was loaded -- so two bots showing the
  same fingerprint really are running the same code, and a ``git pull``
  without a ``[p]reload retro`` does *not* change it. That last property is
  the whole point: the stale-install case is precisely the one where the
  files on disk and the code in memory disagree.

Everything here is captured at import and degrades to ``None`` rather than
raising: this module is imported while the cog is loading, and no amount of
missing metadata is worth a cog that will not load. It deliberately imports
nothing but the standard library.
"""

import hashlib
import json
import logging
import time
import typing
from pathlib import Path

__all__ = [
    "VERSION",
    "UNKNOWN_VERSION",
    "COMMIT",
    "BRANCH",
    "FINGERPRINT",
    "SOURCE_TIME",
    "LOADED_AT",
    "PACKAGE_DIR",
    "INFO_PATH",
    "GIT_SEARCH_DEPTH",
    "read_version",
    "git_checkout",
    "scan_sources",
    "code_fingerprint",
    "newest_source_time",
    "describe",
    "summary",
]

log = logging.getLogger("red.robloach.retro")

#: This package, i.e. the directory the loaded code was read from.
PACKAGE_DIR = Path(__file__).resolve().parent

#: Red's Downloader reads this file; so does :func:`read_version`.
INFO_PATH = PACKAGE_DIR / "info.json"

#: What is shown when info.json cannot be read or carries no version. Not an
#: exception and not a lie: it sorts below every real version and says so.
UNKNOWN_VERSION = "0.0.0+unknown"

#: How far above this package to look for a ``.git``. Two: the package
#: itself and the repository root above it, which is the layout both a
#: development checkout and a Downloader clone have. Deliberately not "keep
#: walking to /": a bot whose data directory happens to live inside somebody
#: else's repository would otherwise be told a commit that has nothing to do
#: with this cog.
GIT_SEARCH_DEPTH = 2

#: How many hex characters of the source hash to show. Twelve is plenty to
#: tell two builds apart by eye and short enough to read out over chat.
FINGERPRINT_LENGTH = 12


def read_version(path: typing.Optional[Path] = None) -> str:
    """
    The ``version`` field of info.json, or :data:`UNKNOWN_VERSION`.

    info.json is the single source of truth for the version, so this is the
    only place it is read from and there is nothing for it to disagree with.
    Never raises: a missing, unreadable or malformed file costs the version
    string and nothing else.
    """
    path = INFO_PATH if path is None else Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = data.get("version")
    except Exception:
        log.debug("Could not read the Retro version from %s.", path, exc_info=True)
        return UNKNOWN_VERSION
    if not isinstance(version, str) or not version.strip():
        return UNKNOWN_VERSION
    return version.strip()


def _git_dir(start: typing.Optional[Path] = None) -> typing.Optional[Path]:
    """
    The ``.git`` directory this package sits in, if it sits in one.

    Handles the plain directory and the ``gitdir: ...`` file a worktree or a
    submodule leaves behind. Returns None for anything else, including a
    ``.git`` that is there but unreadable.
    """
    start = PACKAGE_DIR if start is None else Path(start)
    for directory in [start, *start.parents][:GIT_SEARCH_DEPTH]:
        candidate = directory / ".git"
        try:
            if candidate.is_dir():
                return candidate
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir:"):
                    pointed = Path(text.split(":", 1)[1].strip())
                    if not pointed.is_absolute():
                        pointed = directory / pointed
                    if pointed.is_dir():
                        return pointed
        except OSError:
            log.debug("Could not look at %s.", candidate, exc_info=True)
    return None


def _looks_like_a_sha(value: str) -> bool:
    """Whether this is a commit id rather than something else in the file."""
    return len(value) in (40, 64) and all(c in "0123456789abcdef" for c in value.lower())


class Checkout(typing.NamedTuple):
    """
    What ``.git/HEAD`` says, as one of the two things it can say.

    Exactly one field is filled in: ``branch`` when HEAD is on a branch,
    ``commit`` when it is detached. Both being optional is the honest shape
    -- see :func:`git_checkout` for why the commit is not looked up as well.
    """

    commit: typing.Optional[str]
    branch: typing.Optional[str]


def git_checkout(start: typing.Optional[Path] = None) -> typing.Optional[Checkout]:
    """
    Where this package was checked out, from one line of ``.git/HEAD``.

    A branch name (``ref: refs/heads/main`` -> ``main``) or, on a detached
    HEAD, the commit id sitting in the file. Read straight out of the files
    so it works with no git binary installed and cannot hang. None is an
    ordinary answer, not a failure: plenty of installs have no ``.git``.

    This used to go further and turn a branch into its commit id, which
    meant reimplementing a chunk of git's on-disk format: the loose ref
    file, then packed-refs with its comment and peeled-tag lines -- around
    ninety lines of parsing, all of it to decorate one line of chat output.
    It was never the fact that answers the question this module exists for:
    a commit id describes what was *committed*, which a dirty tree or a pull
    without a reload already disagrees with -- the fingerprint is the one
    that cannot. And a branch name is what a human compares against the
    repository anyway. So HEAD's own words are shown as they are written,
    ref storage stays git's business, and nothing here has to keep up with
    it.
    """
    git_dir = _git_dir(start)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        log.debug("Could not read %s.", git_dir / "HEAD", exc_info=True)
        return None
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        # The last segment, so refs/heads/main reads as "main". A ref with
        # nothing after the colon is no answer at all.
        branch = ref.rpartition("/")[2] or None
        return None if branch is None else Checkout(None, branch)
    if _looks_like_a_sha(head):
        # A detached HEAD, which is what `git checkout <tag>` leaves behind.
        return Checkout(head, None)
    return None


class Sources(typing.NamedTuple):
    """Both facts about the ``.py`` files, from one walk over them."""

    fingerprint: typing.Optional[str]
    newest: typing.Optional[float]


def scan_sources(directory: typing.Optional[Path] = None) -> Sources:
    """
    Hash every ``.py`` in this package and note the newest one, in one pass.

    One glob and one visit per file, because both facts are about the same
    files and are wanted at the same moment (import time). They were two
    passes -- glob, read, hash; glob again, stat -- which walked the package
    twice on every load for no gain.

    The hash covers filenames as well as contents, so adding an empty module
    changes the answer. A file that cannot be *read* costs the fingerprint
    entirely, because a partial hash would be a confident wrong answer; a
    file that cannot be *stat*ed only drops out of the newest-mtime
    calculation, which is a decoration either way.
    """
    directory = PACKAGE_DIR if directory is None else Path(directory)
    try:
        found = sorted(directory.glob("*.py"))
    except OSError:
        log.debug("Could not list %s.", directory, exc_info=True)
        found = []
    if not found:
        return Sources(None, None)

    digest = hashlib.sha256()
    readable = True
    times = []
    for path in found:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            log.debug("Could not stat %s.", path, exc_info=True)
        try:
            body = path.read_bytes()
        except OSError:
            log.debug("Could not read %s.", path, exc_info=True)
            readable = False
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return Sources(
        digest.hexdigest()[:FINGERPRINT_LENGTH] if readable else None,
        max(times) if times else None,
    )


def code_fingerprint(
    directory: typing.Optional[Path] = None,
) -> typing.Optional[str]:
    """
    A short hash of every ``.py`` in this package, or None.

    Called once, at import, which is what makes it a fingerprint of the code
    that is *running* rather than of whatever is on disk now -- see this
    module's own docstring. Kept as its own name because that is the fact
    people ask for; :func:`scan_sources` does the work.
    """
    return scan_sources(directory).fingerprint


def newest_source_time(
    directory: typing.Optional[Path] = None,
) -> typing.Optional[float]:
    """When the most recently changed file in this package was written."""
    return scan_sources(directory).newest


#: The declared version, from info.json.
VERSION: str = read_version()

_CHECKOUT = git_checkout()
#: The commit the loaded code came from -- only on a detached HEAD, which is
#: the one case where there is no branch name to show instead.
COMMIT: typing.Optional[str] = None if _CHECKOUT is None else _CHECKOUT.commit
#: The branch HEAD is on, or None (no ``.git``, or a detached HEAD).
BRANCH: typing.Optional[str] = None if _CHECKOUT is None else _CHECKOUT.branch

_SOURCES = scan_sources()
#: A hash of the sources as they were when the cog was loaded.
FINGERPRINT: typing.Optional[str] = _SOURCES.fingerprint
#: The newest source file's mtime, as of the same moment.
SOURCE_TIME: typing.Optional[float] = _SOURCES.newest
#: And when that moment was, i.e. when this cog was loaded.
LOADED_AT: float = time.time()


def _stamp(when: typing.Optional[float]) -> str:
    if not when:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def summary() -> str:
    """One line: the version, and the checkout if there is one."""
    line = f"v{VERSION}"
    if BRANCH:
        line += f" (on {BRANCH})"
    elif COMMIT:
        line += f" ({COMMIT[:8]}, detached)"
    return line


def describe(prefix: str = "") -> str:
    """
    Everything known about the running build, as lines for a chat message.

    Written to be read out loud in a support conversation, which is what it
    is for: the version somebody can compare against the repository, the
    branch (or commit) if this is a checkout, and the fingerprint that
    settles it when the two of them are not enough.

    ``prefix`` is the bot's real command prefix. These lines are *sent*, and
    Red only rewrites ``[p]`` in a docstring, so the reload command named at
    the end has to be spelled out by the caller (``ctx.clean_prefix``).
    """
    lines = [f"**Version** `{VERSION}`"]
    if VERSION == UNKNOWN_VERSION:
        lines.append(
            "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} `info.json` carries no "
            "readable version, so this is not the cog's own number."
        )
    if BRANCH:
        lines.append(f"**Checkout** on `{BRANCH}`")
    elif COMMIT:
        lines.append(f"**Checkout** `{COMMIT[:12]}` (detached HEAD)")
    if FINGERPRINT:
        lines.append(f"**Loaded code** `{FINGERPRINT}`, newest file {_stamp(SOURCE_TIME)}")
    lines.append(f"**Loaded at** {_stamp(LOADED_AT)}")
    lines.append(
        "The version is what `info.json` declares; the fingerprint is a hash "
        "of the `.py` files as they were **when the cog was loaded**, so it "
        "answers \N{LEFT DOUBLE QUOTATION MARK}am I running the new "
        "code?\N{RIGHT DOUBLE QUOTATION MARK} even when nobody remembered to "
        f"bump a number. Pulling new code without `{prefix}reload retro` "
        "deliberately does not change it."
    )
    return "\n".join(lines)
