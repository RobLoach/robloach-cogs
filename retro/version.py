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
* **the commit**, when the cog was installed from a git checkout: Red's
  Downloader clones a repo, so ``<repo>/.git`` is usually one directory
  above this package. Absent (the cog copied in by hand, an installed tree
  with no ``.git``) it is simply not mentioned. Read from the files, never
  by running ``git``: there may be no git binary, and a subprocess on a
  command that answers a question is not worth the risk.
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


def _resolve_ref(git_dir: Path, ref: str) -> typing.Optional[str]:
    """One ref to a commit id, through the loose file or packed-refs."""
    loose = git_dir / ref
    try:
        if loose.is_file():
            value = loose.read_text(encoding="utf-8").strip()
            if _looks_like_a_sha(value):
                return value
    except OSError:
        log.debug("Could not read the git ref %s.", loose, exc_info=True)
    packed = git_dir / "packed-refs"
    try:
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if not line or line[0] in "#^":
                    continue
                sha, _, name = line.partition(" ")
                if name.strip() == ref and _looks_like_a_sha(sha):
                    return sha
    except OSError:
        log.debug("Could not read %s.", packed, exc_info=True)
    return None


class Checkout(typing.NamedTuple):
    """The commit the loaded code was read from, and the branch it was on."""

    commit: str
    branch: typing.Optional[str]


def git_checkout(start: typing.Optional[Path] = None) -> typing.Optional[Checkout]:
    """
    The commit this package was installed from, or None.

    Read straight out of ``.git`` (HEAD, then a loose ref or packed-refs) so
    it works with no git binary installed and cannot hang. None is an
    ordinary answer, not a failure: plenty of installs have no ``.git``.
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
        commit = _resolve_ref(git_dir, ref)
        if commit is None:
            return None
        branch = ref.rpartition("/")[2] or None
        return Checkout(commit, branch)
    if _looks_like_a_sha(head):
        # A detached HEAD, which is what `git checkout <tag>` leaves behind.
        return Checkout(head, None)
    return None


def _sources(directory: typing.Optional[Path] = None) -> typing.List[Path]:
    directory = PACKAGE_DIR if directory is None else Path(directory)
    try:
        return sorted(directory.glob("*.py"))
    except OSError:
        log.debug("Could not list %s.", directory, exc_info=True)
        return []


def code_fingerprint(
    directory: typing.Optional[Path] = None,
) -> typing.Optional[str]:
    """
    A short hash of every ``.py`` in this package, or None.

    Filenames go into the hash as well as their contents, so adding an empty
    module changes the answer. Called once, at import, which is what makes
    it a fingerprint of the code that is *running* rather than of whatever
    is on disk now -- see this module's own docstring.
    """
    digest = hashlib.sha256()
    found = _sources(directory)
    if not found:
        return None
    for path in found:
        try:
            body = path.read_bytes()
        except OSError:
            log.debug("Could not read %s.", path, exc_info=True)
            return None
        digest.update(path.name.encode("utf-8"))
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()[:FINGERPRINT_LENGTH]


def newest_source_time(
    directory: typing.Optional[Path] = None,
) -> typing.Optional[float]:
    """When the most recently changed file in this package was written."""
    times = []
    for path in _sources(directory):
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            log.debug("Could not stat %s.", path, exc_info=True)
    return max(times) if times else None


#: The declared version, from info.json.
VERSION: str = read_version()

_CHECKOUT = git_checkout()
#: The commit the loaded code came from, or None if there is no ``.git``.
COMMIT: typing.Optional[str] = None if _CHECKOUT is None else _CHECKOUT.commit
#: The branch that commit was on, or None (no ``.git``, or a detached HEAD).
BRANCH: typing.Optional[str] = None if _CHECKOUT is None else _CHECKOUT.branch

#: A hash of the sources as they were when the cog was loaded.
FINGERPRINT: typing.Optional[str] = code_fingerprint()
#: The newest source file's mtime, as of the same moment.
SOURCE_TIME: typing.Optional[float] = newest_source_time()
#: And when that moment was, i.e. when this cog was loaded.
LOADED_AT: float = time.time()


def _stamp(when: typing.Optional[float]) -> str:
    if not when:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def summary() -> str:
    """One line: the version, and the commit if there is one."""
    line = f"v{VERSION}"
    if COMMIT:
        line += f" ({COMMIT[:8]}"
        line += f" on {BRANCH})" if BRANCH else ", detached)"
    return line


def describe(prefix: str = "") -> str:
    """
    Everything known about the running build, as lines for a chat message.

    Written to be read out loud in a support conversation, which is what it
    is for: the version somebody can compare against the repository, the
    commit if there is one, and the fingerprint that settles it when the two
    of them are not enough.

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
    if COMMIT:
        where = f" on `{BRANCH}`" if BRANCH else " (detached HEAD)"
        lines.append(f"**Commit** `{COMMIT[:12]}`{where}")
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
