"""
Putting a game back: what a channel has saved, and the order a boot tries it
in.

Pulled out of retro/RetroView.py, which had grown to 2,700 lines with a third
of them not about the view at all. This is the half of starting a game that
has nothing to do with buttons: it is reached from both doors a game comes
back through -- starting one the channel has played before
(:meth:`RetroView._boot`) and waking a hibernated session
(``Retro._wake_locked``) -- and it used to be a copy each, kept in step by
the tests rather than by construction.

Everything here is blocking and thread-safe to the extent its emulator is:
:func:`restore_into` runs in the worker thread that owns the core, and the
lazy backup reads it may do are ordinary blocking file reads in the thread
that exists for them. Nothing here awaits, and nothing here touches Discord.

RetroView re-exports every name in here, so `retro.RetroView.Progress` still
resolves; see the note at the top of that file.
"""

import logging
import typing
from pathlib import Path

from .emulator import EmulatorError, RetroEmulator
from .timing import BOOT_SECONDS

log = logging.getLogger("red.robloach.retro")

#: Where one of :class:`Progress`'s backup blobs comes from: the bytes
#: themselves, a file to read them out of, something to call for them, or
#: nothing at all. See Progress, which is where the reasoning is.
BackupSource = typing.Union[
    bytes,
    bytearray,
    Path,
    typing.Callable[[], typing.Optional[bytes]],
    None,
]


def backup_offered(source: BackupSource) -> bool:
    """
    Whether a backup is probably there, without reading a byte of it.

    The question :attr:`Progress.has_state` has to answer, and it has to
    answer it cheaply -- reading a megabyte to decide whether there is a
    megabyte would undo the whole point of a lazy backup.

    A path is answered with one ``stat``, and an empty file counts as
    nothing: a zero-length save state is not a save state, and the eager form
    has always treated ``b""`` the same way. A callable is taken at its word,
    because there is no way to ask one that is not calling it; see
    :attr:`Progress.has_state` for why the optimistic answer is the safe one
    here. Never raises: a backup that cannot be looked at is a backup that
    is not there.
    """
    if source is None:
        return False
    if isinstance(source, (bytes, bytearray)):
        return bool(source)
    if isinstance(source, Path):
        try:
            return source.is_file() and source.stat().st_size > 0
        except OSError:
            return False
    return callable(source)


def read_backup(
    source: BackupSource, what: str = "save"
) -> typing.Optional[bytes]:
    """
    Resolve a backup to bytes, at the moment somebody actually wants it.

    Blocking, and deliberately so: the only caller is :func:`restore_into`,
    which already runs in a worker thread precisely because loading a game is
    file-sized work. ``what`` is only for the log line.

    Never raises, for the same reason the rest of this chain does not: a
    backup is the thing that is tried *because* the newer file failed, so a
    backup that cannot be read means "carry on down the chain" -- the
    cartridge's battery save next, and the title screen after that -- rather
    than a boot that dies holding a perfectly good ROM.
    """
    if source is None:
        return None
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    try:
        data = source.read_bytes() if isinstance(source, Path) else source()
    except FileNotFoundError:
        # Not an event. A game that has been saved once has no previous
        # generation yet, and every boot of it asks anyway -- so this is the
        # ordinary answer, not a failure, and it must not put a traceback in
        # the log every time somebody starts a game. The eager form this
        # replaced read the file with a plain "missing is None" helper and
        # said nothing either.
        return None
    except Exception:
        # A file deleted between the stat and the read (a save import, a
        # prune, a `[p]retrosaves` command), a permission that changed under
        # us, or a caller's own reader that threw. Worth a line: something
        # is there and unreadable, which is different from nothing existing.
        log.warning("Could not read the previous %s.", what, exc_info=True)
        return None
    return bytes(data) if data else None


class Progress(typing.NamedTuple):
    """
    One game's saved progress, in the order a boot is willing to try it.

    Four files rather than two, because every save keeps one previous
    generation (see BACKUP_SUFFIX in storage.py): a successful write of a *bad*
    save is not something an atomic write can protect anybody from, and a save
    state is the thing players care most about. The backups are only ever
    reached when the newer file cannot be used.

    **The two backups may be promises rather than bytes**, and that is the
    point of them being separate fields. Every start, every resume and every
    wake builds one of these, and the newest save state loads on almost all
    of them -- so the two backup reads are almost always wasted work. A save
    state is not small: measured on the real cores, a Game Boy state is 178
    KiB, a GBA one 516 KiB and a Genesis one 1012 KiB, and both backups were
    read off disk, in full, before every single boot.

    So a backup field takes any of:

    * ``bytes`` -- the eager form, exactly as it always was. Every existing
      caller and every test that builds one by hand still works;
    * a :class:`~pathlib.Path` -- read when, and only when,
      :func:`restore_into` reaches for it;
    * a zero-argument callable returning ``Optional[bytes]`` -- the same,
      for a caller whose backup does not come from a plain file;
    * ``None`` -- there is no backup.

    Reading happens in the worker thread :func:`restore_into` already runs
    in, so it is a blocking read in a place where blocking is what the thread
    is for. Nothing here does async I/O and nothing here touches the event
    loop.
    """

    #: The most recent save state, always read up front: the boot needs it
    #: unless the channel has never played this game.
    state: typing.Optional[bytes] = None
    #: The generation before it -- bytes, a Path, or a callable. See above.
    state_backup: "BackupSource" = None
    #: The cartridge's battery memory, and the generation before it.
    sram: typing.Optional[bytes] = None
    sram_backup: "BackupSource" = None

    @property
    def has_state(self) -> bool:
        """
        Whether there is any save state at all to try.

        Answered from *existence* rather than contents, which is what keeps
        it cheap: a Path is a ``stat`` and a callable is taken at its word
        (see :func:`backup_offered`). The alternative -- reading the backup
        to find out whether there is one -- would spend the megabyte this
        laziness exists to avoid, on the question rather than on the answer.

        The one caller is ``Retro._settle_boot``, which asks "was there a
        state that should have worked?" before deleting the files that did
        not. Taking a callable at its word can therefore only cost a delete
        of a file that was not there, which is a no-op; it can never cost
        anybody a save that was.
        """
        return bool(self.state) or backup_offered(self.state_backup)

    @property
    def has_sram(self) -> bool:
        """
        Whether there is any in-game save to try, by the same reckoning.

        Here because a lazy backup is a Path or a callable, and both of those
        are truthy whether or not there is a file behind them: a caller that
        used to write ``progress.sram or progress.sram_backup`` would be
        reading "there is an in-game save" off the fact that somebody passed
        a path.
        """
        return bool(self.sram) or backup_offered(self.sram_backup)

    def read_state_backup(self) -> typing.Optional[bytes]:
        """The previous generation of the save state, read now if need be."""
        return read_backup(self.state_backup, "save state")

    def read_sram_backup(self) -> typing.Optional[bytes]:
        """The previous generation of the in-game save, read now if need be."""
        return read_backup(self.sram_backup, "in-game save")


def restore_into(
    emulator: RetroEmulator,
    progress: typing.Optional[Progress] = None,
    slug: str = "",
) -> str:
    """
    Start a core and put back as much of a game as is restorable.

    The one implementation of the save state -> previous save state -> battery
    save -> previous battery save -> cold boot chain. Both paths that bring a
    game up go through it: starting a game the channel has played before
    (:meth:`RetroView._boot`) and waking a hibernated session
    (``Retro._wake_locked``). They used to have a copy each and were kept in
    step by the tests rather than by construction.

    The order is the order of confidence. A save state is the exact moment and
    is tried first; its previous generation is the same thing one autosave
    older, which is worth far more than the title screen. The cartridge's
    battery save is not a moment at all but it survives a core update, so it
    comes after both states, and its own previous generation after it.

    Returns what actually happened, which is what the cog reads to decide
    whether to say anything and whether to throw a save state away:

    * ``"state"`` - the exact moment came back;
    * ``"backup-state"`` - the newest state was unusable, the one before it
      was not;
    * ``"sram"`` - no state could be used, but the cartridge's battery save
      went in;
    * ``"backup-sram"`` - the same, from the previous generation of it;
    * ``"fresh"`` - there was nothing to restore, or none of it could be used.

    Blocking, so callers run it in a worker thread -- which is also where the
    two backups are *read*, if they are read at all. Each rung of the chain
    is a callable rather than a blob so that reaching for the previous
    generation is what costs the read: on the overwhelmingly common boot,
    where the newest save state loads, neither backup is touched at all. See
    :class:`Progress`. Blocking file reads are exactly what this thread is
    for; nothing here may await.
    """
    progress = progress if progress is not None else Progress()
    emulator.start()
    for read, outcome in (
        (lambda: progress.state, "state"),
        (progress.read_state_backup, "backup-state"),
    ):
        data = read()
        if not data:
            continue
        try:
            emulator.load_state(data)
            # Straight back to the exact moment, so none of the boot frames
            # below are wanted: the game is already past its title screen.
            return outcome
        except EmulatorError as error:
            # A state from a different build of the core, a truncated file, or
            # -- the case the battery save exists for -- a state whose size no
            # longer matches because the core was updated. That should cost
            # the exact moment, not the session and not the player's own
            # in-game save.
            log.warning(
                "Discarding an unusable Libretro save state (%s) for %s: %s",
                outcome,
                slug,
                error,
            )
    # Cold boot. The core only allocates the cartridge's save memory once it
    # has loaded the game, so the battery save goes in after start(), and the
    # boot frames run afterwards so the game reaches its own title screen with
    # the save already in place.
    restored = "fresh"
    for read, outcome in (
        (lambda: progress.sram, "sram"),
        (progress.read_sram_backup, "backup-sram"),
    ):
        # load_sram() answers False for a save that is the wrong size for this
        # cartridge, which is exactly when the generation before it is worth a
        # try: an import or a core update can leave a mismatched newest file.
        # And the previous generation is only read at all once the newest one
        # has been refused; see Progress.
        data = read()
        if data and emulator.load_sram(data):
            restored = outcome
            break
    emulator.advance(emulator.frames_for_seconds(BOOT_SECONDS))
    return restored
