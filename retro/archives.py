"""
Reading a single file out of a ``.zip`` archive, safely.

Homebrew ROMs and console firmware are almost always distributed zipped, so
the cog accepts a ``.zip`` wherever it accepts a raw file and pulls the one
member it needs out of it.

Nothing here ever calls :meth:`zipfile.ZipFile.extract`: the member is read
into memory and the caller writes it under a filename it chose itself, so an
archive containing ``../../.ssh/authorized_keys`` cannot escape anywhere. The
uncompressed size is checked against the caller's cap *before* the member is
read, and the read itself is capped too, because ``ZipInfo.file_size`` is
attacker-controlled metadata that a zip bomb is free to lie about.

This module imports nothing but the standard library, so it can be exercised
without discord.py or Red-DiscordBot installed.
"""

import io
import typing
import zipfile

__all__ = [
    "ArchiveError",
    "NoSupportedMember",
    "Extracted",
    "ZIP_MAGIC",
    "is_zip",
    "describe_members",
    "extract",
]

# The local file header every non-empty zip starts with. An empty archive
# starts with the end-of-central-directory record instead, which is of no use
# to us anyway.
ZIP_MAGIC = b"PK\x03\x04"

# How many names an error message lists before it gives up and counts.
MAX_LISTED_MEMBERS = 8

# Archive noise that is never the file anyone wanted.
JUNK_PREFIXES = ("__MACOSX/", "__macosx/")


class ArchiveError(Exception):
    """A zip could not be read, or did not contain what was asked for.

    The message is written for the person who uploaded the file, so callers
    can show ``str(error)`` directly.
    """


class NoSupportedMember(ArchiveError):
    """The zip was readable but held nothing the caller can use."""

    def __init__(self, message: str, members: typing.Sequence[str] = ()) -> None:
        super().__init__(message)
        self.members: typing.Tuple[str, ...] = tuple(members)


class Extracted(typing.NamedTuple):
    """One member, read out of an archive."""

    # The member's path *inside* the archive, for reporting only. Never use
    # it to build a path on disk.
    name: str
    data: bytes
    # Every member that would have been acceptable, sorted. More than one
    # means the caller should say which it picked.
    candidates: typing.Tuple[str, ...]
    # Every file member, sorted, so an error can list what was in there.
    members: typing.Tuple[str, ...]


def is_zip(data: bytes) -> bool:
    """Whether these bytes start with a zip local file header."""
    return bytes(data[:4]) == ZIP_MAGIC


def _is_junk(name: str) -> bool:
    if name.startswith(JUNK_PREFIXES):
        return True
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Dotfiles in an archive are resource forks and editor droppings.
    return not base or base.startswith(".")


def describe_members(
    members: typing.Sequence[str], limit: int = MAX_LISTED_MEMBERS
) -> str:
    """A short, truncated rendering of an archive's contents."""
    if not members:
        return "nothing at all"
    shown = [f"`{name}`" for name in members[:limit]]
    if len(members) > limit:
        shown.append(f"and {len(members) - limit} more")
    return ", ".join(shown)


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile as error:
        raise ArchiveError(
            f"it is not a readable zip archive ({error})"
        ) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise ArchiveError(f"it could not be opened ({error})") from error


def _human_size(size: int) -> str:
    """A short, honest size: bytes stay bytes, big things get a unit."""
    for unit, cutoff in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if size >= cutoff:
            return f"{size / cutoff:.1f} {unit}"
    return f"{size} bytes"


def extract(
    data: bytes,
    *,
    accept: typing.Callable[[str], bool],
    max_size: int,
    what: str = "file",
) -> Extracted:
    """
    Pull the first acceptable member out of ``data``.

    ``accept`` is given each member's path inside the archive and returns
    whether it is the kind of file the caller wants. Members are considered in
    sorted order, so the same archive always yields the same file.

    ``max_size`` caps the *uncompressed* size, checked against the archive's
    own metadata first and then against the bytes actually produced.

    :raises ArchiveError: if the archive cannot be read, is encrypted, or the
        chosen member is too big.
    :raises NoSupportedMember: if nothing inside matched ``accept``.
    """
    with _open(data) as archive:
        try:
            infos = archive.infolist()
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
            raise ArchiveError(f"its index could not be read ({error})") from error

        files = sorted(
            (info for info in infos if not info.is_dir() and not _is_junk(info.filename)),
            key=lambda info: info.filename,
        )
        names = tuple(info.filename for info in files)
        if not files:
            raise NoSupportedMember(
                "the zip has no files in it (only folders, or nothing at all)."
            )

        candidates = tuple(info.filename for info in files if accept(info.filename))
        if not candidates:
            raise NoSupportedMember(
                f"the zip contains no {what} this bot can use. It contains: "
                f"{describe_members(names)}.",
                names,
            )

        chosen = next(info for info in files if info.filename == candidates[0])

        # Metadata first: this is what stops a 4 GiB bomb being decompressed
        # at all. It is only a hint, so the read below is capped as well.
        if chosen.file_size > max_size:
            raise ArchiveError(
                f"`{chosen.filename}` inside the zip unpacks to "
                f"{_human_size(chosen.file_size)}, over the "
                f"{_human_size(max_size)} limit."
            )
        # Bit 0 of the general purpose flags means the member is encrypted.
        # Reading it would raise a bare RuntimeError from deep inside
        # zipfile, so say something useful instead.
        if chosen.flag_bits & 0x1:
            raise ArchiveError(
                f"`{chosen.filename}` is password-protected. Unzip it "
                "yourself and upload the file inside."
            )

        try:
            with archive.open(chosen) as member:
                payload = member.read(max_size + 1)
        except RuntimeError as error:
            # zipfile raises this for an encrypted member and for a
            # compression method it was not built with (e.g. bzip2/lzma on a
            # stripped Python).
            raise ArchiveError(
                f"`{chosen.filename}` could not be unpacked ({error})."
            ) from error
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
            raise ArchiveError(
                f"`{chosen.filename}` is corrupt and could not be unpacked "
                f"({error})."
            ) from error

    if len(payload) > max_size:
        raise ArchiveError(
            f"`{chosen.filename}` inside the zip is bigger than the "
            f"{_human_size(max_size)} limit, whatever the archive "
            "claims."
        )
    if not payload:
        raise ArchiveError(f"`{chosen.filename}` inside the zip is empty.")
    return Extracted(chosen.filename, payload, candidates, names)
