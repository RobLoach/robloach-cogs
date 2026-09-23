"""
Reading files out of a ``.zip`` archive, safely.

Homebrew ROMs and console firmware are almost always distributed zipped, so
the cog accepts a ``.zip`` wherever it accepts a raw file. There are two ways
in: :func:`extract` pulls out the single member the caller wants (a ROM), and
:func:`extract_each` streams a whole archive one member at a time (a BIOS set,
which is usually several files and sometimes a folder or two of them).
:func:`extract_all` is the same walk with every member collected into one
tuple, which is convenient but holds the lot in memory at once.

Nothing here ever calls :meth:`zipfile.ZipFile.extract`: members are read into
memory and the caller writes them under paths *this module* validated, so an
archive containing ``../../.ssh/authorized_keys`` cannot escape anywhere. The
uncompressed size is checked against the caller's cap *before* a member is
read, and the read itself is capped too, because ``ZipInfo.file_size`` is
attacker-controlled metadata that a zip bomb is free to lie about.

This module imports nothing but the standard library, so it can be exercised
without discord.py or Red-DiscordBot installed.
"""

import io
import re
import stat
import typing
import zipfile

__all__ = [
    "ArchiveError",
    "NoSupportedMember",
    "Extracted",
    "ExtractedFile",
    "ExtractedArchive",
    "ArchiveReport",
    "ZIP_MAGIC",
    "MAX_MEMBER_DEPTH",
    "is_zip",
    "describe_members",
    "safe_member_path",
    "extract",
    "extract_each",
    "extract_all",
]

# The local file header every non-empty zip starts with. An empty archive
# starts with the end-of-central-directory record instead, which is of no use
# to us anyway.
ZIP_MAGIC = b"PK\x03\x04"

# How many names an error message lists before it gives up and counts.
MAX_LISTED_MEMBERS = 8

# Archive noise that is never the file anyone wanted. Compared per path
# *component* (case-insensitively, after separators are normalised), because
# macOS's resource-fork folder shows up nested (``sub/__MACOSX/._rom.gb``)
# and Windows repackers write it with backslashes (``__MACOSX\``).
JUNK_COMPONENTS = frozenset({"__macosx"})

# How many folders deep a member may sit before it is refused. Real firmware
# sets nest one or two deep at most (``dc/dc_boot.bin``, ``np2kai/FONT.ROM``);
# anything past this is either a mistake or someone being clever.
MAX_MEMBER_DEPTH = 4

# What one path component may be called. This *validates* rather than
# rewrites, because a core asks its frontend for an exact filename
# (``disksys.rom``, ``scph5501.bin``) and silently storing a file under a
# mangled name would produce a BIOS the core can never find -- worse than
# refusing it and saying so. The leading character must be alphanumeric, which
# is also what keeps dotfiles and ``..`` out. The *final* character may not be
# a dot or a space either: Windows (a supported host) strips those when
# writing, so ``foo.`` would silently land on disk as ``foo`` -- exactly the
# mangling this regex exists to refuse.
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._ +-]{0,62}[A-Za-z0-9_+-])?")

# Names that NT resolves as device aliases *before* it ever looks at a
# directory -- and it does so on the stem alone, so ``aux.rom`` is the AUX
# device wearing a costume. On a Windows-hosted bot, writing one would block
# on (or write into) a device rather than produce a file any core could ever
# read back, so these fail validation like any other unsafe name. COM0/LPT0
# are included for the modern parser's sake; the superscript variants
# (``COM¹``…) cannot get past SAFE_COMPONENT's ASCII-only character set.
DOS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(10)}
    | {f"lpt{digit}" for digit in range(10)}
)


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


class ExtractedFile(typing.NamedTuple):
    """One file unpacked by :func:`extract_all`."""

    #: The relative path to write it to, with forward slashes. Every component
    #: has been validated by :func:`safe_member_path`, so joining this onto a
    #: directory cannot leave that directory.
    path: str
    #: The member's own name inside the archive, for reporting only.
    member: str
    data: bytes


class ArchiveReport(typing.NamedTuple):
    """What :func:`extract_each` made of an archive, once the bytes are gone.

    Everything :class:`ExtractedArchive` carries except the payloads, which a
    streaming caller has already written somewhere and must not be holding.
    """

    #: The validated relative path of every file handed to the sink, in the
    #: order it was handed over (sorted by member name).
    paths: typing.Tuple[str, ...]
    #: Members that were refused (unsafe name, symlink, device node, too big,
    #: a name that collides with an earlier member once case is ignored), so
    #: the caller can say how many were left behind. A collision entry
    #: carries its reason in parentheses after the member's name.
    skipped: typing.Tuple[str, ...]
    #: Every file member, sorted, whether it was taken or not.
    members: typing.Tuple[str, ...]
    #: Total uncompressed bytes of everything the sink was given.
    total_size: int


class ExtractedArchive(typing.NamedTuple):
    """Everything usable :func:`extract_all` found in one archive."""

    files: typing.Tuple[ExtractedFile, ...]
    #: As :attr:`ArchiveReport.skipped`.
    skipped: typing.Tuple[str, ...]
    #: Every file member, sorted, whether it was taken or not.
    members: typing.Tuple[str, ...]
    #: Total uncompressed bytes of ``files``.
    total_size: int


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
    # Whether ``name`` is the member the caller asked for by name rather than
    # the alphabetical first. False when no ``prefer`` was given *and* when
    # one was given but matched nothing, so a caller that asked for a
    # particular file can tell the user it was not in there. Defaulted so
    # anything that builds an Extracted positionally still does.
    preferred: bool = False


def is_zip(data: bytes) -> bool:
    """Whether these bytes start with a zip local file header."""
    return bytes(data[:4]) == ZIP_MAGIC


def _is_junk(name: str) -> bool:
    # Normalise separators before looking, exactly as safe_member_path does:
    # a junk test on the raw name would wave through ``__MACOSX\._rom.gb``
    # from a Windows-repacked archive, and a prefix test would wave through
    # the nested ``sub/__MACOSX/._rom.gb`` -- either of which could then sort
    # first and become the member a user's game is started from.
    parts = str(name).replace("\\", "/").split("/")
    if any(part.casefold() in JUNK_COMPONENTS for part in parts):
        return True
    # A dot-component anywhere is resource forks and editor droppings
    # (``.DS_Store``, ``._rom.gb``, ``sub/.git/whatever``) -- the directory
    # being hidden makes its contents droppings too. ``.`` and ``..`` are
    # exempt: those are traversal syntax, not droppings, and belong to
    # safe_member_path, which refuses them *loudly* (skipped and counted)
    # rather than pretending they were never there.
    if any(part.startswith(".") and part not in (".", "..") for part in parts):
        return True
    return not parts[-1]


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


def safe_member_path(name: str) -> typing.Optional[str]:
    """
    Turn a member's name into a relative path that is safe to join, or None.

    Every component is validated against :data:`SAFE_COMPONENT`, which refuses
    ``..``, ``.``, empty components, dotfiles, NUL bytes, anything outside a
    small, boring character set, and a trailing dot or space (which Windows
    would strip on write, storing the file under a name nobody validated).
    Components whose stem is a DOS device name (``CON``, ``aux.rom``) are
    refused too -- see :data:`DOS_DEVICE_NAMES`. Both slash flavours are
    treated as separators, so a Windows-built archive full of
    ``bios\\dc\\dc_boot.bin`` is handled the same as a Unix one, and an
    absolute path or a drive letter is refused outright rather than quietly
    relativised.
    """
    raw = str(name).replace("\\", "/")
    if not raw or "\x00" in raw:
        return None
    # Absolute, or a Windows drive/UNC path. ZIP_FILENAME is supposed to be
    # relative, so anything else is malformed or malicious either way.
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [part for part in raw.split("/") if part]
    if not parts or len(parts) > MAX_MEMBER_DEPTH:
        return None
    for part in parts:
        if not SAFE_COMPONENT.fullmatch(part):
            return None
        # NT's device-name check uses the stem alone and ignores trailing
        # spaces, so match both quirks (``NUL.bin``, ``con .rom``).
        if part.split(".", 1)[0].rstrip(" ").casefold() in DOS_DEVICE_NAMES:
            return None
    return "/".join(parts)


def _member_mode(info: zipfile.ZipInfo) -> int:
    """
    The Unix mode a Unix-built archive recorded for a member, or 0.

    ``create_system`` 3 is Unix; for anything else the high half of
    ``external_attr`` is not a mode and must not be read as one.
    """
    if info.create_system != 3:
        return 0
    return (info.external_attr >> 16) & 0xFFFF


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    """
    Whether a member is an ordinary file rather than a link or a device.

    zip stores the creating system's mode bits, so a symlink, a fifo, a socket
    or a device node all survive a round trip through an archive and would be
    recreated by a naive unpacker. This cog only ever writes bytes it read into
    memory, so such a member could not become a real symlink here anyway --
    but writing a symlink's *target path* out as a BIOS file is still nonsense,
    so they are refused and counted rather than stored.
    """
    kind = stat.S_IFMT(_member_mode(info))
    if not kind:
        # Permission bits but no file *type*, which is what
        # ``ZipFile.writestr`` itself writes (0o600 << 16), or no mode at all
        # from a Windows-built archive. Either way it is an ordinary file.
        return True
    return kind == stat.S_IFREG


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile as error:
        raise ArchiveError(
            f"it is not a readable zip archive ({error})"
        ) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise ArchiveError(f"it could not be opened ({error})") from error


def _list_files(
    archive: zipfile.ZipFile,
) -> typing.Tuple[typing.List[zipfile.ZipInfo], typing.Tuple[str, ...]]:
    """
    Every member worth considering, sorted, and their names.

    Both ways into this module start here, so the index-read error, the junk
    filter and the empty-archive sentence are written once rather than twice
    and cannot drift apart. Directories and archive noise are dropped before
    anything else looks at the list -- ``names`` is what an error message
    shows the uploader, and nobody wants ``__MACOSX/._rom.gb`` quoted back at
    them as if it were part of their upload.

    Sorting is load-bearing rather than tidiness: it is what makes the member
    :func:`extract` picks, and the order :func:`extract_each` walks in (and so
    which of two case-colliding names survives), the same every time the same
    archive is uploaded.

    :raises ArchiveError: if the central directory cannot be read.
    :raises NoSupportedMember: if there are no file members at all.
    """
    try:
        infos = archive.infolist()
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"its index could not be read ({error})") from error

    files = sorted(
        (info for info in infos if not info.is_dir() and not _is_junk(info.filename)),
        key=lambda info: info.filename,
    )
    if not files:
        raise NoSupportedMember(
            "the zip has no files in it (only folders, or nothing at all)."
        )
    return files, tuple(info.filename for info in files)


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    cap: int,
    *,
    plural: bool = False,
) -> bytes:
    """
    Decompress one member, never more than ``cap`` + 1 bytes of it.

    The caller works out ``cap`` (a per-file limit, or whatever is left of a
    total); this reads one byte past it so a member that lied about its size
    is caught by a length check afterwards rather than silently truncated into
    a BIOS file that is *almost* right -- which a core would load and then
    misbehave over, far worse than a refusal.

    ``plural`` only picks the number in the password sentence: one caller is
    after a single file and the other after a set, and telling somebody to
    upload "the file inside" a firmware pack would be poor advice.

    :raises ArchiveError: if the member is encrypted, compressed with a method
        this Python cannot read, or corrupt.
    """
    # Bit 0 of the general purpose flags means the member is encrypted.
    # Reading it would raise a bare RuntimeError from deep inside zipfile, so
    # say something useful instead.
    if info.flag_bits & 0x1:
        raise ArchiveError(
            f"`{info.filename}` is password-protected. Unzip it yourself and "
            f"upload the file{'s' if plural else ''} inside."
        )
    try:
        with archive.open(info) as member:
            return member.read(cap + 1)
    except RuntimeError as error:
        # zipfile raises this for an encrypted member and for a compression
        # method it was not built with (e.g. bzip2/lzma on a stripped Python).
        raise ArchiveError(
            f"`{info.filename}` could not be unpacked ({error})."
        ) from error
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
        raise ArchiveError(
            f"`{info.filename}` is corrupt and could not be unpacked ({error})."
        ) from error


def _human_size(size: int) -> str:
    """A short, honest size: bytes stay bytes, big things get a unit."""
    for unit, cutoff in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if size >= cutoff:
            return f"{size / cutoff:.1f} {unit}"
    return f"{size} bytes"


def _basename(name: str) -> str:
    """The last component of a member's name, both separators considered.

    Windows-built archives use backslashes, so the same normalisation the junk
    filter does applies here: ``roms\\game.gb`` is a file called ``game.gb``
    to everyone except a naive ``rsplit("/")``.
    """
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def _preferred(candidates: typing.Sequence[str], prefer: str) -> typing.Optional[str]:
    """
    The candidate the caller named, or None if no candidate is it.

    Matching is on the *basename* only and ignores case: a person typing a
    filename out of a listing into a Discord message is not going to reproduce
    ``Pack/v1.0/Roms/Game.GB`` exactly, and on the case-insensitive disks half
    the hosts here run, case is not information anyway. Only the basename of
    ``prefer`` is looked at too, so pasting a path back works.

    An exact basename wins over an extension-less one, and among equals the
    first in sorted order wins, so the answer never depends on zip order. The
    two passes matter for a genuine case: ``prefer="game.gb"`` against an
    archive holding ``backup/game.gb.bak`` and ``game.gb`` must not hand back
    the backup merely because it sorts first.

    Nothing here overrides ``accept``: only members the caller already called
    acceptable are ever offered, so naming the readme in a zip of ROMs still
    gets a ROM.
    """
    wanted = _basename(prefer).strip().casefold()
    if not wanted:
        return None
    for candidate in candidates:
        if _basename(candidate).casefold() == wanted:
            return candidate
    for candidate in candidates:
        if _basename(candidate).casefold().rsplit(".", 1)[0] == wanted:
            return candidate
    return None


def extract(
    data: bytes,
    *,
    accept: typing.Callable[[str], bool],
    max_size: int,
    what: str = "file",
    prefer: typing.Optional[str] = None,
) -> Extracted:
    """
    Pull the first acceptable member out of ``data``.

    ``accept`` is given each member's path inside the archive and returns
    whether it is the kind of file the caller wants. Members are considered in
    sorted order, so the same archive always yields the same file.

    ``prefer`` names the member to take when the caller knows which one they
    want: it is matched against each acceptable member's basename, ignoring
    case, with or without the extension (``sonic``, ``Sonic.md`` and
    ``roms/sonic.md`` all find ``Roms/Sonic.md``). It is a preference and not
    a filter -- a name that matches nothing falls back to the alphabetical
    first, with ``candidates`` still listing everything, because the caller
    that asked for a file which is not in the zip is better served by a ROM
    plus a note than by an error. :attr:`Extracted.preferred` says which of
    the two happened.

    ``max_size`` caps the *uncompressed* size, checked against the archive's
    own metadata first and then against the bytes actually produced.

    :raises ArchiveError: if the archive cannot be read, is encrypted, or the
        chosen member is too big.
    :raises NoSupportedMember: if nothing inside matched ``accept``.
    """
    with _open(data) as archive:
        files, names = _list_files(archive)

        candidates = tuple(info.filename for info in files if accept(info.filename))
        if not candidates:
            raise NoSupportedMember(
                f"the zip contains no {what} this bot can use. It contains: "
                f"{describe_members(names)}.",
                names,
            )

        wanted = _preferred(candidates, prefer) if prefer else None
        chosen_name = wanted if wanted is not None else candidates[0]
        chosen = next(info for info in files if info.filename == chosen_name)

        # Metadata first: this is what stops a 4 GiB bomb being decompressed
        # at all. It is only a hint, so the read below is capped as well.
        if chosen.file_size > max_size:
            raise ArchiveError(
                f"`{chosen.filename}` inside the zip unpacks to "
                f"{_human_size(chosen.file_size)}, over the "
                f"{_human_size(max_size)} limit."
            )
        payload = _read_member(archive, chosen, max_size)

    if len(payload) > max_size:
        raise ArchiveError(
            f"`{chosen.filename}` inside the zip is bigger than the "
            f"{_human_size(max_size)} limit, whatever the archive "
            "claims."
        )
    if not payload:
        raise ArchiveError(f"`{chosen.filename}` inside the zip is empty.")
    return Extracted(chosen.filename, payload, candidates, names, wanted is not None)


def extract_each(
    data: bytes,
    *,
    sink: typing.Callable[[ExtractedFile], typing.Any],
    max_total_size: int,
    max_file_size: int,
    max_files: int,
    what: str = "file",
) -> ArchiveReport:
    """
    Unpack every usable member of ``data``, one at a time, into ``sink``.

    The same walk as :func:`extract_all` -- same caps, same skip rules, same
    order -- except that each file is handed to ``sink`` the moment it is
    decompressed and then dropped, so only one member's bytes are ever live.
    That is the difference between a peak of the archive plus every file in it
    (a maximal firmware pack is over a hundred megabytes) and the archive plus
    the largest single file, which matters on the small VPSes these bots live
    on.

    ``sink`` is called once per accepted file, in sorted order, with an
    :class:`ExtractedFile`; it is expected to write the bytes somewhere and
    not to keep them, since keeping them gives back exactly the memory this
    function exists to save. Anything it raises propagates out untouched --
    an unpack that cannot be written anywhere should stop, not carry on
    decompressing into a disk that is full -- and whatever earlier calls did
    has of course already happened.

    Used for firmware sets, which arrive as a handful of files and sometimes a
    folder per console. Members are handed over in sorted order with a validated
    relative path (see :func:`safe_member_path`); anything unsafe, anything
    that is not an ordinary file, and anything over ``max_file_size`` is
    skipped and counted rather than aborting the whole archive, because one
    stray symlink in an otherwise good BIOS pack should not cost the pack.

    A member whose path matches an already-taken one when case is ignored is
    skipped too: ``BIOS.bin`` and ``bios.bin`` are both individually fine, but
    on the case-insensitive filesystems of Windows and macOS hosts the second
    write would silently clobber the first, and which file survived would
    depend on nothing but sort order.

    Three separate caps apply, and the *total* one is enforced twice -- once
    against the archive's own metadata before anything is decompressed, and
    again against the bytes actually produced, since a zip bomb lies about the
    first.

    :raises ArchiveError: if the archive cannot be read or busts a cap.
    :raises NoSupportedMember: if nothing inside could be used.
    """
    # The paths handed to the sink, for the report. Names only: keeping the
    # ExtractedFile tuples would keep their payloads alive, which is the whole
    # thing this function is here to avoid.
    taken: typing.List[str] = []
    skipped: typing.List[str] = []
    # Casefolded path of every member taken so far, mapped back to the name
    # it was taken under, so a collision can say what it collided with.
    claimed_paths: typing.Dict[str, str] = {}
    total = 0

    with _open(data) as archive:
        files, names = _list_files(archive)
        if len(files) > max_files:
            raise ArchiveError(
                f"the zip holds {len(files)} files, more than the {max_files} "
                "this bot will unpack in one go. Unzip it yourself and add the "
                "files you need."
            )
        # The metadata check, before a single byte is decompressed. It is only
        # a hint -- see the running total below -- but it is what stops a
        # multi-gigabyte bomb being unpacked at all.
        claimed = sum(info.file_size for info in files)
        if claimed > max_total_size:
            raise ArchiveError(
                f"the zip unpacks to {_human_size(claimed)}, over the "
                f"{_human_size(max_total_size)} limit."
            )

        for info in files:
            path = safe_member_path(info.filename)
            if path is None or not _is_regular_file(info):
                skipped.append(info.filename)
                continue
            # Two members may differ only by case (``BIOS.bin``/``bios.bin``)
            # and still each be safe on their own; on a case-insensitive
            # disk the caller's second write would clobber the first, so the
            # later member (sorted order, so always the same one) is skipped.
            earlier = claimed_paths.get(path.casefold())
            if earlier is not None:
                skipped.append(
                    f"{info.filename} (differs from `{earlier}` only by "
                    "letter case; on a case-insensitive disk it would "
                    "overwrite it)"
                )
                continue
            if info.file_size > max_file_size:
                skipped.append(info.filename)
                continue
            # Never more than what is left of the total: a member is not worth
            # decompressing past the point where it could not be kept anyway.
            remaining = max_total_size - total
            payload = _read_member(
                archive, info, min(max_file_size, remaining), plural=True
            )
            if len(payload) > max_file_size:
                skipped.append(info.filename)
                continue
            if total + len(payload) > max_total_size:
                raise ArchiveError(
                    f"the zip unpacks to more than the "
                    f"{_human_size(max_total_size)} limit, whatever it claims."
                )
            if not payload:
                # An empty file is never firmware, and writing one would only
                # make `bios list` lie about what is installed.
                skipped.append(info.filename)
                continue
            total += len(payload)
            claimed_paths[path.casefold()] = info.filename
            taken.append(path)
            sink(ExtractedFile(path, info.filename, payload))
            # Let go before the next member is read, so the peak is one file
            # rather than the whole pack. (The sink holding on to it is its
            # own business, and its own memory.)
            del payload

    if not taken:
        raise NoSupportedMember(
            f"the zip contains no {what} this bot can use. It contains: "
            f"{describe_members(names)}.",
            names,
        )
    return ArchiveReport(tuple(taken), tuple(skipped), names, total)


def extract_all(
    data: bytes,
    *,
    max_total_size: int,
    max_file_size: int,
    max_files: int,
    what: str = "file",
) -> ExtractedArchive:
    """
    :func:`extract_each` with every file collected into one tuple.

    The convenient shape, and the expensive one: the whole unpacked archive is
    live at once, on top of the archive it came out of. Fine for a couple of
    files or a test; a caller unpacking a firmware pack that may be a hundred
    megabytes should use :func:`extract_each` and write each file as it
    arrives. Every cap, skip rule and error is that function's -- this only
    keeps what it is handed.

    :raises ArchiveError: if the archive cannot be read or busts a cap.
    :raises NoSupportedMember: if nothing inside could be used.
    """
    files: typing.List[ExtractedFile] = []
    report = extract_each(
        data,
        sink=files.append,
        max_total_size=max_total_size,
        max_file_size=max_file_size,
        max_files=max_files,
        what=what,
    )
    return ExtractedArchive(tuple(files), report.skipped, report.members, report.total_size)
