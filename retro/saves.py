"""
``[p]retrosaves``: a window onto the files a channel's games have saved.

A channel's progress lives in three files per game, all keyed by the channel
id and the game's slug: the cached ROM under ``roms/``, and the save state
and battery save (with one previous generation each) under ``states/``.
Nothing here invents a new place to keep things and nothing here is stored
per user; the paths all come from retro/storage.py.
"""

import asyncio
import logging
import typing
from pathlib import Path

import discord
from redbot.core import commands
from redbot.core.utils.chat_formatting import humanize_list
from redbot.core.utils.views import ConfirmView

from .abc import MixinMeta
from .emulator import MAX_SRAM_SIZE, EmulatorError, RetroEmulator
from .RetroView import RetroView, may_manage
from .storage import BACKUP_SUFFIX, MAX_CACHED_GAMES_PER_CHANNEL, ROLLBACK_SUFFIX
from .systems import system_by_key, system_for_extension

log = logging.getLogger("red.robloach.retro")

# `[p]retrosaves import`/`export` each move a file and, for an import, boot a
# core to validate it. Four a minute is more than anybody does by hand.
SAVE_COOLDOWN_RATE = 4
SAVE_COOLDOWN_SECONDS = 60.0

# -- Save management ----------------------------------------------------------

# What `[p]retrosaves import` accepts, and what it decides each attachment is.
# `.srm` is what RetroArch calls a battery save and is what this cog writes;
# `.sav` is the same thing under the name several emulators and homebrew
# toolchains use. A state keeps the `.state` RetroArch uses (`.state1`,
# `.state2` and friends are its numbered slots).
SRAM_EXTENSIONS = (".srm", ".sav")
STATE_EXTENSIONS = (".state", ".st", ".savestate")

# The ceiling on an imported battery save is the same one the emulator applies
# to a cartridge's save memory, so anything this accepts is at least the right
# order of magnitude. A save state is the whole machine and is much larger --
# a few hundred KiB for a Game Boy, a couple of MiB for a Super Nintendo --
# but it is still not a disc image.
MAX_IMPORT_SRAM_SIZE = MAX_SRAM_SIZE
MAX_IMPORT_STATE_SIZE = 16 * 1024 * 1024
# Derived, never written twice: a ceiling and a label that disagreed would
# refuse a file while telling somebody it was allowed.
MAX_IMPORT_SRAM_LABEL = f"{MAX_IMPORT_SRAM_SIZE // (1024 * 1024)} MiB"
MAX_IMPORT_STATE_LABEL = f"{MAX_IMPORT_STATE_SIZE // (1024 * 1024)} MiB"

# What to assume Discord will accept as an attachment when the server does not
# say. Every guild is allowed at least this much, so an export sized against
# it is never rejected for being too large; `discord.Guild.filesize_limit` is
# used instead wherever it can be read, which is how a boosted server gets to
# upload the bigger save states.
DEFAULT_UPLOAD_LIMIT = 8 * 1024 * 1024

# How long `[p]retrosaves delete` waits for someone to press Yes.
CONFIRM_TIMEOUT = 60.0

# What a delete removes, in the order `StorageMixin._save_paths` hands the
# four paths back, and what to call each one in a sentence. Named per file
# rather than per half, because the reply has to be able to say that three of
# them went and the fourth did not; see _delete_saves.
DELETE_LABELS = (
    "its save state",
    "the previous save state",
    "its in-game save",
    "the previous in-game save",
)

# The words `[p]retrosaves export` accepts in front of a game name to say what
# to send. Anything else is part of the name, so a game really called "state"
# is still exportable.
EXPORT_CHOICES = {
    "save": "sram",
    "sram": "sram",
    "battery": "sram",
    "srm": "sram",
    "state": "state",
    "savestate": "state",
    "both": "both",
    "all": "both",
    "everything": "both",
}


class SaveInfo(typing.NamedTuple):
    """
    Everything one channel has saved for one game, gathered in one place.

    Assembled by :meth:`Retro._saved_games` from three sources that each know
    a different part of it: the files on disk (which are the progress itself),
    the channel's live session, and the records behind its Resume buttons
    (which are where a game's proper name and console come from once it is no
    longer the one being played).
    """

    #: The filesystem-safe id the save files are keyed by, e.g. ``ucity``.
    slug: str
    #: What to call the game in a sentence, e.g. ``uCity``. Falls back to the
    #: slug for a game nothing remembers the name of any more.
    game_name: str
    #: The console, or None when neither a record nor a cached ROM says.
    system: typing.Optional[typing.Any]
    core: str
    #: Bytes and mtime of each half of the save, or None if it is not there.
    state_size: typing.Optional[int]
    state_written: typing.Optional[float]
    sram_size: typing.Optional[int]
    sram_written: typing.Optional[float]
    #: The cached ROM, if it has not been pruned yet.
    rom: typing.Optional[Path]
    #: Whether this is the channel's current game, and whether it is awake.
    current: bool
    live: bool
    #: Who started it, when anything still remembers. See _may_manage_saves.
    starter_id: typing.Optional[int]
    #: The same, for the generation before each of them. Defaulted, because
    #: every save had two files before it had four.
    state_backup_size: typing.Optional[int] = None
    state_backup_written: typing.Optional[float] = None
    sram_backup_size: typing.Optional[int] = None
    sram_backup_written: typing.Optional[float] = None

    @property
    def has_state(self) -> bool:
        return bool(self.state_size)

    @property
    def has_sram(self) -> bool:
        return bool(self.sram_size)

    @property
    def has_state_backup(self) -> bool:
        return bool(self.state_backup_size)

    @property
    def has_sram_backup(self) -> bool:
        return bool(self.sram_backup_size)

    @property
    def has_backup(self) -> bool:
        """Whether there is a previous generation to roll back to."""
        return self.has_state_backup or self.has_sram_backup

    @property
    def has_save(self) -> bool:
        return self.has_state or self.has_sram or self.has_backup

    @property
    def last_written(self) -> float:
        """When any part of this save was last written, 0.0 if never."""
        return max(
            self.state_written or 0.0,
            self.sram_written or 0.0,
            self.state_backup_written or 0.0,
            self.sram_backup_written or 0.0,
        )

    @property
    def restore(self) -> str:
        """
        How this game would come back if it were started right now.

        The same chain :func:`RetroView.restore_into` runs, read off the files
        rather than by booting anything: the save state, then the generation
        before it, then the cartridge's battery save, then the generation
        before that, then the beginning. It cannot know whether a state the
        core will reject is on disk -- only the core can say that -- so the
        answer is what would be *tried* first.
        """
        if self.has_state:
            return "state"
        if self.has_state_backup:
            return "backup-state"
        if self.has_sram:
            return "sram"
        if self.has_sram_backup:
            return "backup-sram"
        return "fresh"


class DeleteOutcome(typing.NamedTuple):
    """
    What one `[p]retrosaves delete` managed, file by file.

    A delete that goes perfectly needs none of this -- ``freed`` would do. It
    exists for the delete that goes half way: four files are removed one at a
    time and the third of them can fail on a read-only data folder, leaving a
    game with no save state and an intact in-game save. That is a state
    nothing else in this cog ever produces on purpose, so the reply has to be
    able to describe it, which means the removal has to report it.
    """

    #: Bytes that really stopped existing.
    freed: int
    #: DELETE_LABELS for the files that were there and are now gone.
    removed: typing.List[str]
    #: DELETE_LABELS for the files that were there and still are.
    failed: typing.List[str]


class SavesMixin(MixinMeta):
    """The ``[p]retrosaves`` group and everything it needs."""

    # -- Save management ----------------------------------------------------
    #
    # A channel's progress lives in three files per game, all keyed by the
    # channel id and the game's slug: the cached ROM under roms/, and the save
    # state and battery save under states/. `[p]retrosaves` is a window onto
    # exactly those files -- nothing here invents a new place to keep things,
    # and nothing here is stored per user.
    #
    # Two rules run through the whole group:
    #
    #   * no filename anybody types ever reaches the filesystem. A name is put
    #     through _slug() and then has to match a slug that is already there
    #     (see _match_save), so the paths built here are the paths the cog
    #     wrote itself.
    #   * a live emulator owns the authoritative copy of both saves and writes
    #     them out on its next automatic save, so anything that changes the
    #     files under one puts it to sleep first. See _pause_for_saves, which
    #     is the single most important thing in this section.

    @staticmethod
    def _when(written: typing.Optional[float]) -> str:
        """
        A timestamp Discord renders in the reader's own timezone.

        ``<t:...:R>`` is "3 hours ago", which is the only form of this that is
        both short and true for everybody reading the channel.
        """
        if not written:
            return "never"
        return f"<t:{int(written)}:R>"

    @staticmethod
    def _file_facts(path: Path) -> typing.Optional[typing.Tuple[int, float]]:
        """``(size, mtime)`` for a file that is there, else None."""
        try:
            info = path.stat()
        except OSError:
            return None
        return int(info.st_size), float(info.st_mtime)

    def _cached_rom(self, channel_id: int, slug: str) -> typing.Optional[Path]:
        """
        The ROM this channel has cached for one game, whatever its extension.

        The slug comes from :meth:`_slug`, so it holds nothing a glob would
        treat as a pattern. The half-written file :meth:`_write_atomic` leaves
        behind if the bot dies mid-write is skipped rather than offered as a
        playable ROM.
        """
        try:
            candidates = sorted(self._roms_dir().glob(f"{int(channel_id)}-{slug}.*"))
        except OSError:
            log.warning("Could not read the Retro ROM cache.", exc_info=True)
            return None
        for path in candidates:
            try:
                if path.suffix == ".tmp" or not path.is_file():
                    continue
            except OSError:
                continue
            return path
        return None

    def _stored_slugs(self, channel_id: int) -> typing.Set[str]:
        """Every game this channel has a ROM or a save file for, on disk."""
        prefix = f"{int(channel_id)}-"
        found: typing.Set[str] = set()
        directories = (
            # The backups are listed too: a game whose newest save state has
            # been thrown out for being unloadable still has progress worth
            # managing, and it would be a strange listing that hid the only
            # copy left.
            (
                self._data_dir("states"),
                ("*.state", "*.srm", "*.state" + BACKUP_SUFFIX, "*.srm" + BACKUP_SUFFIX),
            ),
            (self._roms_dir(), ("*",)),
        )
        for directory, patterns in directories:
            for pattern in patterns:
                try:
                    entries = list(directory.glob(prefix + pattern))
                except OSError:
                    log.warning(
                        "Could not read %s while listing saves.",
                        directory,
                        exc_info=True,
                    )
                    continue
                for path in entries:
                    if path.suffix == ".tmp":
                        # A write that was interrupted; not a game.
                        continue
                    name = path.name[len(prefix):]
                    if name.endswith(BACKUP_SUFFIX):
                        name = name[: -len(BACKUP_SUFFIX)]
                    slug = name.rsplit(".", 1)[0] if "." in name else name
                    if slug:
                        found.add(slug)
        return found

    async def _saved_games(self, channel_id: int) -> typing.List[SaveInfo]:
        """
        Everything one channel has saved, newest first, current game first.

        Names and consoles come from whatever still remembers them -- the live
        session, then the records behind the channel's Resume buttons -- and
        the files on disk supply the rest. A game whose ROM has been pruned
        and whose Resume button has been forgotten still appears, under its
        slug, because its save is still there and still worth managing.

        The files are the slow part -- several directory globs and a ``stat``
        per file -- so the whole gather runs in one worker thread rather than
        on the event loop, where a big states directory used to stall every
        press in every channel for the length of a directory listing each
        time somebody typed `[p]retrosaves`. Only reads happen in the
        thread, and only of things that are safe to read from one: the
        filesystem, and plain attributes of the session.
        """
        channel_id = int(channel_id)
        session = self.sessions.get(channel_id)
        meta: typing.Dict[str, dict] = {}
        if session is not None:
            meta[session.slug] = {
                "game_name": session.game_name,
                "system": session.system,
                "core": session.core,
                "starter_id": session.starter_id,
            }
        try:
            retired = await self.config.channel_from_id(channel_id).retired()
        except Exception:
            log.exception("Could not read channel %s's retired games.", channel_id)
            retired = {}
        for record in (retired or {}).values():
            slug = (record or {}).get("slug")
            if not slug or slug in meta:
                continue
            meta[slug] = {
                "game_name": record.get("game_name") or slug,
                "system": system_by_key(record.get("system") or ""),
                "core": record.get("core") or "",
                "starter_id": record.get("starter_id"),
            }
        def gather() -> typing.List[SaveInfo]:
            entries = [
                self._save_info(channel_id, slug, meta.get(slug) or {}, session)
                for slug in sorted(set(meta) | self._stored_slugs(channel_id))
            ]
            # The game being played first, then by when it was last saved, so
            # the top of the list is what somebody is most likely asking
            # about.
            entries.sort(key=lambda e: (not e.current, -e.last_written, e.slug))
            return entries

        return await asyncio.to_thread(gather)

    def _save_info(
        self,
        channel_id: int,
        slug: str,
        meta: dict,
        session: typing.Optional[RetroView],
    ) -> SaveInfo:
        """Gather one game's files and whatever is known about it."""
        state_path, state_backup_path, sram_path, sram_backup_path = self._save_paths(
            channel_id, slug
        )
        state = self._file_facts(state_path)
        sram = self._file_facts(sram_path)
        state_backup = self._file_facts(state_backup_path)
        sram_backup = self._file_facts(sram_backup_path)
        rom = self._cached_rom(channel_id, slug)
        system = meta.get("system")
        if system is None and rom is not None:
            # Nothing remembers this game, but its cached ROM still names its
            # console the same way `[p]retro` did when it started it.
            system = system_for_extension(rom.suffix)
        current = session is not None and session.slug == slug
        return SaveInfo(
            slug=slug,
            game_name=meta.get("game_name") or slug,
            system=system,
            core=meta.get("core") or (system.core if system is not None else ""),
            state_size=state[0] if state else None,
            state_written=state[1] if state else None,
            sram_size=sram[0] if sram else None,
            sram_written=sram[1] if sram else None,
            rom=rom,
            current=current,
            live=bool(current and session.live),
            starter_id=meta.get("starter_id"),
            state_backup_size=state_backup[0] if state_backup else None,
            state_backup_written=state_backup[1] if state_backup else None,
            sram_backup_size=sram_backup[0] if sram_backup else None,
            sram_backup_written=sram_backup[1] if sram_backup else None,
        )

    def _match_save(
        self, entries: typing.Sequence[SaveInfo], text: str
    ) -> typing.Tuple[typing.Optional[SaveInfo], typing.List[SaveInfo]]:
        """
        Turn what somebody typed into one of this channel's saved games.

        Returns ``(entry, candidates)``; exactly one is useful. Matching is
        deliberately against the list that was just read off disk rather than
        against the filesystem: the slug this produces is always one the cog
        wrote itself, so no name typed into Discord can name a path.
        """
        wanted = str(text).strip().strip("`").strip('"').strip("'")
        if not wanted:
            return None, list(entries)
        by_slug = {entry.slug: entry for entry in entries}
        slug = self._slug(wanted)
        if slug in by_slug:
            return by_slug[slug], []
        lowered = wanted.lower()
        named = [e for e in entries if e.game_name.lower() == lowered]
        if len(named) == 1:
            return named[0], []
        partial = [
            e
            for e in entries
            if slug in e.slug or lowered in e.game_name.lower()
        ]
        if len(partial) == 1:
            return partial[0], []
        return None, partial

    async def _resolve_save(
        self, ctx: commands.Context, text: str
    ) -> typing.Optional[SaveInfo]:
        """Find one game for a command, or explain why it could not."""
        entries = await self._saved_games(ctx.channel.id)
        known = [e for e in entries if e.has_save or e.rom is not None]
        if not known:
            await self._safe_send(
                ctx,
                "This channel has no saved games yet. Start one with "
                f"`{ctx.clean_prefix}retro <name or url>` and it will keep "
                "its progress from then on.",
            )
            return None
        entry, candidates = self._match_save(known, text)
        if entry is not None:
            return entry
        if candidates:
            listed = humanize_list([f"`{e.slug}`" for e in candidates])
            await self._safe_send(
                ctx, f"`{text}` matches {len(candidates)} of this channel's "
                f"games: {listed}. Use the exact name to say which you mean."
            )
            return None
        listed = humanize_list([f"`{e.slug}`" for e in known[:15]])
        await self._safe_send(
            ctx,
            f"This channel has nothing saved for `{text}`. It does have: "
            f"{listed}. See `{ctx.clean_prefix}retrosaves list`.",
        )
        return None

    async def _may_manage_saves(
        self, ctx: commands.Context, entry: typing.Optional[SaveInfo] = None
    ) -> bool:
        """
        Whether this person may destroy or replace a save.

        The same rule, from the same implementation, as `[p]retrosleep` on
        someone else's game: see :func:`RetroView.may_manage`. Playing is
        open to everybody and so are listing, inspecting and exporting --
        wiping a channel's progress is not.
        """
        return await may_manage(
            self.bot,
            getattr(ctx, "author", None),
            entry.starter_id if entry is not None else None,
        )

    async def _refuse_management(
        self, ctx: commands.Context, entry: SaveInfo, what: str
    ) -> None:
        await self._safe_send(
            ctx,
            f"Only the person who started **{entry.game_name}**, moderators "
            f"(Manage Messages), or the bot owner can {what}. Anyone can "
            f"play, look at `{ctx.clean_prefix}retrosaves list`, or export a "
            "copy.",
        )

    async def _pause_for_saves(
        self, ctx: commands.Context, entry: SaveInfo, doing: str
    ) -> bool:
        """
        Put the channel's game to sleep before its saves are touched.

        This is the correctness point of the whole group. A live emulator
        holds the *authoritative* copy of both the save state and the
        cartridge's battery memory, and writes both of them back to disk on
        its next automatic save (every SAVE_STATE_EVERY_PRESSES presses) and
        again whenever it hibernates. Deleting or replacing the files under a
        running game would therefore be silently undone by the next button
        press -- the player would press A, the core would save over the top,
        and the save they were told had gone would be back.

        Hibernating first writes the game out, frees the core, and leaves the
        files on disk as the only copy of anything, so what happens to them
        next is what the game comes back to. The session itself survives: its
        controls stay live and the next press wakes it from whatever this
        command left behind. Returns whether anything was put to sleep, so the
        reply can say so.

        The session's **undo history** goes as well, and that is the same
        point made about memory rather than about a running core. It holds
        machine states from before this command, and an undo writes the state
        it restores straight to disk (see ``Retro.run_undo``) -- so one click
        of Undo after a `reset`, a `rollback`, a `delete` or an `import`
        would put back the very save the command was asked to destroy or
        replace. It is dropped for a *sleeping* session too, which is why it
        happens before the live check below.
        """
        view = self.sessions.get(int(getattr(ctx.channel, "id", 0)))
        if view is None or view.slug != entry.slug:
            return False
        view.forget_history()
        if not view.live:
            return False
        # And, exactly as `[p]retrosleep` does, before reaching for that
        # lock: a press sitting out the clip on screen holds it, and no save
        # command should wait on a cosmetic delay. See
        # RetroView.cancel_pacing.
        view.cancel_pacing()
        # The view's own lock, exactly as `[p]retrosleep` takes it, so a
        # press that is already being emulated finishes before the core is
        # taken away from it.
        async with view.lock:
            return await self._hibernate_for_saves(view, entry, doing)

    async def _hibernate_for_saves(
        self, view: RetroView, entry: SaveInfo, doing: str
    ) -> bool:
        """
        Save a live session and free its core. Hold ``view.lock`` to call.

        The working half of :meth:`_pause_for_saves`, split out so
        :meth:`_mutate_saves` can run it again *while already holding the
        lock* just before it touches the files. Checks ``live`` itself,
        because by the time the lock has been acquired the answer may have
        changed either way: the press that held it may have been the one
        that woke the game up. Returns whether anything was put to sleep.
        """
        view.forget_history()
        if not view.live:
            return False
        reason = (
            f"Saved and put to sleep while {doing}. Press a button to pick "
            "the game back up."
        )
        try:
            await self.hibernate(view, reason)
        except Exception:
            # And the same belt and braces as `[p]retrosleep`, from the same
            # helper: everything after this assumes the files on disk are the
            # only copy.
            log.exception(
                "Could not hibernate %s cleanly before changing its saves.",
                entry.slug,
            )
            await self._force_hibernate(view)
        log.info(
            "Hibernated the live %s session in channel %s before changing its "
            "saves.",
            entry.slug,
            view.channel_id,
        )
        return True

    async def _mutate_saves(
        self,
        ctx: commands.Context,
        entry: SaveInfo,
        doing: str,
        mutate: typing.Callable[[], typing.Any],
    ) -> typing.Tuple[bool, typing.Any]:
        """
        Change one game's save files while no live core can undo the change.

        :meth:`_pause_for_saves` alone is not quite enough, because pausing
        and writing are separate awaits and the session's controls stay live
        in between: any button press in that gap wakes the game *from the
        old files*, and the woken core's next automatic save then writes
        those old files straight back over whatever the command changed --
        silently, which is the worst way for a delete, a rollback or an
        import to fail. `[p]retrosaves import` has the widest gap (a
        multi-second core boot validates the incoming save between its pause
        and its write), but every command that touches the files has one.

        So the files are only ever touched from in here: pause first, then
        take the view's own lock -- the same lock every button press holds
        for the whole of its press -- hibernate again if a press slipped in
        and woke the game, and run ``mutate`` (blocking, so it goes to a
        worker thread) before letting the lock go. Nothing can boot from, or
        save over, the files while it runs. Returns ``(anything was put to
        sleep, whatever mutate returned)``; whatever mutate raises passes
        through.
        """
        paused = await self._pause_for_saves(ctx, entry, doing)
        view = self.sessions.get(int(getattr(ctx.channel, "id", 0)))
        if view is None or view.slug != entry.slug:
            # Nothing to race with. A session for this game *appearing*
            # mid-thread means somebody started it fresh, and a fresh start
            # boots from whatever files this leaves behind, which is the
            # order every command already promises.
            return paused, await asyncio.to_thread(mutate)
        # Same as _pause_for_saves: never wait out a cosmetic delay for the
        # lock.
        view.cancel_pacing()
        async with view.lock:
            if await self._hibernate_for_saves(view, entry, doing):
                paused = True
            return paused, await asyncio.to_thread(mutate)

    async def _confirm(self, ctx: commands.Context, question: str) -> bool:
        """
        Ask before doing something that cannot be undone.

        Red's own ConfirmView, so it looks and behaves like every other
        confirmation in the bot. A prompt that could not be posted at all is a
        "no": the one thing worse than refusing to delete a save is deleting
        one nobody agreed to.
        """
        view = ConfirmView(ctx.author, timeout=CONFIRM_TIMEOUT, disable_buttons=True)
        message = await self._safe_send(ctx, question, view=view)
        if message is None:
            return False
        view.message = message
        await view.wait()
        return bool(view.result)

    @staticmethod
    def _refund_cooldown(ctx: commands.Context) -> None:
        """
        Hand this invocation's cooldown back: it cost nothing.

        `[p]retrosaves import` and `[p]retrosaves export` are limited to
        SAVE_COOLDOWN_RATE a minute because each one moves a file and an
        import boots a core on top of that. Red charges that the moment the
        command is invoked, which is before every mistake somebody makes on
        the way to a working one: a forgotten attachment, a `.zip` where a
        `.srm` should be, a file over the size ceiling, a name that matched
        two games. Every one of those is a correction away from working, and
        charging for the correction is exactly how somebody trying to fix
        their own typo locks themselves out for a minute in the middle of a
        conversation. So each path that gives up *before* an attachment has
        been downloaded or a file has been read off disk hands the slot back,
        and the limit goes on protecting the only things that cost anything.

        `Retro._forgive_cooldown` does the identical thing for `[p]retro`,
        and this is deliberately a twin of it rather than a call to it:
        retro/abc.py's MixinMeta is the whole contract of what a mixin may
        reach for on the assembled cog and `_forgive_cooldown` is not in it,
        so calling it would be reaching for something nobody wrote down (a
        test holds the contract to that -- see tests/test_mixins.py). Two
        guarded lines are not worth widening the contract for.

        Guarded because none of it is guaranteed: a command with no cooldown,
        or a context assembled by something other than Red, must not turn a
        polite refusal into a traceback.
        """
        command = getattr(ctx, "command", None)
        reset = getattr(command, "reset_cooldown", None)
        if reset is None:
            return
        try:
            reset(ctx)
        except Exception:
            log.debug("Could not hand back a Retro save cooldown.", exc_info=True)

    def _upload_limit(self, ctx: commands.Context) -> int:
        """How large an attachment this server will accept."""
        limit = getattr(getattr(ctx, "guild", None), "filesize_limit", None)
        try:
            return int(limit) if limit else DEFAULT_UPLOAD_LIMIT
        except (TypeError, ValueError):
            return DEFAULT_UPLOAD_LIMIT

    def _console_label(self, entry: SaveInfo) -> str:
        """``Game Boy Color (gambatte)``, or as much of it as is known."""
        if entry.system is not None:
            return f"{entry.system.name} (`{entry.core or entry.system.core}`)"
        if entry.core:
            return f"`{entry.core}`"
        return "console unknown"

    def _restore_sentence(self, entry: SaveInfo) -> str:
        """What starting this game right now would do, in a sentence."""
        fallback = (
            "the previous save state is tried next"
            if entry.has_state_backup
            else "the in-game save below is used instead"
            if entry.has_sram
            else "the game starts from the beginning instead"
        )
        if entry.restore == "state":
            return (
                "**from its save state** \N{EM DASH} the exact moment it was "
                f"left at. If the emulator core has been updated since, "
                f"{fallback}."
            )
        if entry.restore == "backup-state":
            return (
                "**from its previous save state** \N{EM DASH} the newer one "
                "has already been thrown out for being unloadable, so this is "
                "the exact moment a few presses before that."
            )
        if entry.restore in ("sram", "backup-sram"):
            return (
                "**from the title screen, with the in-game save in place** "
                "\N{EM DASH} load it from the game's own menu to carry on."
            )
        return "**from the very beginning** \N{EM DASH} there is nothing saved."

    def _save_lines(self, entry: SaveInfo) -> typing.List[str]:
        """Two lines describing one game, for the listing."""
        tags = []
        if entry.current:
            tags.append("**playing now**" if entry.live else "**this channel's game**")
        if entry.rom is None:
            tags.append("ROM pruned")
        elif not entry.current:
            tags.append("resumable")
        heading = f"**{entry.game_name}** \N{EM DASH} `{entry.slug}`"
        if entry.system is not None:
            heading += f" \N{BULLET} {entry.system.name}"
        if tags:
            heading += " \N{BULLET} " + ", ".join(tags)
        parts = []
        if entry.has_state:
            parts.append(
                f"save state {self._humanize_bytes(entry.state_size)}, "
                f"{self._when(entry.state_written)}"
            )
        if entry.has_sram:
            parts.append(
                f"in-game save {self._humanize_bytes(entry.sram_size)}, "
                f"{self._when(entry.sram_written)}"
            )
        if entry.has_backup:
            # Worth a word in the listing, because it is the thing somebody
            # who has just lost a save wants to know is there.
            kept = []
            if entry.has_state_backup:
                kept.append("state")
            if entry.has_sram_backup:
                kept.append("battery")
            parts.append(
                f"1 previous generation kept ({humanize_list(kept)}, "
                f"{self._when(max(entry.state_backup_written or 0.0, entry.sram_backup_written or 0.0))})"
            )
        if not parts:
            parts.append("nothing saved yet")
        return [heading, "\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}" + " \N{BULLET} ".join(parts)]

    @commands.guild_only()
    @commands.group(name="retrosaves", aliases=["saves"], invoke_without_command=True)
    async def retrosaves(
        self, ctx: commands.Context, *, game: typing.Optional[str] = None
    ) -> None:
        """
        See and manage this channel's saved games.

        Every game a channel plays keeps its progress in two pieces, and these
        are the only two names this cog uses for them:

        - a **save state** — the exact moment the game was left at, down to
          the frame. It only loads on the same build of the same emulator, so
          an emulator update can cost one.
        - an **in-game save** — what a player saved from inside the game, on
          its own menu. It is the cartridge's battery-backed memory, which is
          why the file is a `.srm` and why other emulators call it a battery
          save or SRAM. It survives an emulator update, so it is kept beside
          the save state as insurance.

        Run it with no arguments to list everything this channel has saved, or
        with a game's name to see that one in detail.

        Anyone can list, inspect and export. Deleting, dropping a save state
        and importing are limited to the person who started the game,
        moderators with the Manage Messages permission, and the bot owner.

        **Examples:**
        - `[p]retrosaves`
        - `[p]retrosaves ucity`

        **Arguments:**
        - `[game]` - A game this channel has played. Omit it for the list.
        """
        if game is None:
            await self._saves_list(ctx)
            return
        await self._saves_info(ctx, game)

    @retrosaves.command(name="list", aliases=["ls"])
    async def retrosaves_list(self, ctx: commands.Context) -> None:
        """
        List every game this channel has saved.

        Shows each game's slug (the name the other commands take), which
        halves of its save exist, how big they are and when they were last
        written, which game the channel is playing, and which of the others
        can still be resumed. Anyone can run this.

        **Examples:**
        - `[p]retrosaves list`
        """
        await self._saves_list(ctx)

    async def _saves_list(self, ctx: commands.Context) -> None:
        entries = [
            entry
            for entry in await self._saved_games(ctx.channel.id)
            if entry.has_save or entry.rom is not None
        ]
        if not entries:
            await self._safe_send(
                ctx,
                "This channel has no saved games. Start one with "
                f"`{ctx.clean_prefix}retro <name or url>` \N{EM DASH} it will "
                "keep its progress from the first button press, and pick it "
                "back up even after the channel has played something else.",
            )
            return
        saved = sum(1 for entry in entries if entry.has_save)
        total = sum(
            (entry.state_size or 0)
            + (entry.sram_size or 0)
            + (entry.state_backup_size or 0)
            + (entry.sram_backup_size or 0)
            for entry in entries
        )
        lines = [
            f"**{len(entries)} game(s)** in this channel, {saved} with saved "
            f"progress, {self._humanize_bytes(total)} in total (previous "
            "generations included).",
            "",
        ]
        for entry in entries:
            lines.extend(self._save_lines(entry))
        lines.extend(
            [
                "",
                f"`{ctx.clean_prefix}retrosaves info <game>` for one in full, "
                f"`{ctx.clean_prefix}retrosaves export <game>` to take a copy "
                f"away, `{ctx.clean_prefix}retrosaves rollback <game>` to go "
                f"back one save, `{ctx.clean_prefix}retrosaves dropstate "
                "<game>` to go back to the last in-game save.",
            ]
        )
        await self._send_pages(ctx, "\n".join(lines))

    @retrosaves.command(name="info", aliases=["show", "about"])
    async def retrosaves_info(self, ctx: commands.Context, *, game: str) -> None:
        """
        Show one game's saved progress in detail.

        Says how big each half of the save is and when it was written, which
        console and core the game needs, whether its ROM is still cached, and
        exactly what would happen if the game were started right now. Anyone
        can run this.

        **Examples:**
        - `[p]retrosaves info ucity`

        **Arguments:**
        - `<game>` - A game this channel has played.
        """
        await self._saves_info(ctx, game)

    async def _saves_info(self, ctx: commands.Context, game: str) -> None:
        entry = await self._resolve_save(ctx, game)
        if entry is None:
            return
        lines = [
            f"**{entry.game_name}** \N{EM DASH} `{entry.slug}`",
            f"Console: {self._console_label(entry)}",
        ]
        if entry.current:
            lines.append(
                "This is the channel's current game, and it is "
                + ("**awake**." if entry.live else "**asleep** \N{EM DASH} "
                   "press a button on it to carry on.")
            )
        elif entry.rom is not None:
            lines.append(
                "Not the channel's current game. Its Resume button still "
                f"works, and `{ctx.clean_prefix}retro {entry.slug}` starts it "
                "again here."
            )
        lines.append("")
        if entry.has_state:
            lines.append(
                f"**Save state**: {self._humanize_bytes(entry.state_size)}, "
                f"written {self._when(entry.state_written)}."
            )
        else:
            lines.append("**Save state**: none.")
        if entry.has_sram:
            lines.append(
                f"**Battery save**: {self._humanize_bytes(entry.sram_size)}, "
                f"written {self._when(entry.sram_written)}."
            )
        else:
            lines.append(
                "**Battery save**: none \N{EM DASH} either nobody has saved "
                "from inside the game yet, or this cartridge has no battery."
            )
        if entry.has_backup:
            kept = []
            if entry.has_state_backup:
                kept.append(
                    f"a save state of {self._humanize_bytes(entry.state_backup_size)} "
                    f"from {self._when(entry.state_backup_written)}"
                )
            if entry.has_sram_backup:
                kept.append(
                    f"an in-game save of {self._humanize_bytes(entry.sram_backup_size)} "
                    f"from {self._when(entry.sram_backup_written)}"
                )
            lines.append(
                f"**Previous generation**: {humanize_list(kept)}. It is used "
                "automatically if the newer one turns out to be unloadable, "
                f"and `{ctx.clean_prefix}retrosaves rollback {entry.slug}` "
                "swaps back to it on purpose."
            )
        else:
            lines.append(
                "**Previous generation**: none yet \N{EM DASH} one is kept "
                "from the second automatic save onwards."
            )
        if entry.rom is not None:
            size = self._file_facts(entry.rom)
            lines.append(
                f"**Cached ROM**: `{entry.rom.name}`"
                + (f", {self._humanize_bytes(size[0])}." if size else ".")
            )
        else:
            lines.append(
                "**Cached ROM**: gone \N{EM DASH} the cache keeps the "
                f"{MAX_CACHED_GAMES_PER_CHANNEL} most recent games per "
                "channel. Start the game again by name or URL and this save "
                "picks straight back up."
            )
        lines.extend(["", "Started now, it would come back " + self._restore_sentence(entry)])
        if entry.has_save:
            lines.extend(
                [
                    "",
                    f"`{ctx.clean_prefix}retrosaves export {entry.slug}` takes "
                    f"a copy away; `{ctx.clean_prefix}retrosaves dropstate "
                    f"{entry.slug}` drops the save state and goes back to the "
                    f"last in-game save; `{ctx.clean_prefix}retrosaves delete "
                    f"{entry.slug}` wipes both.",
                ]
            )
        await self._send_pages(ctx, "\n".join(lines))

    @staticmethod
    def _split_export(game: str) -> typing.Tuple[str, str]:
        """
        Pull an optional ``save``/``state``/``both`` off the front of a name.

        Only when something is left after it, so a game actually called
        "state" is still exportable by name.

        Nothing typed comes back as ``"default"`` rather than as ``"sram"``.
        The two are not the same thing: what a bare `[p]retrosaves export
        <game>` should send depends on which halves the game actually has,
        while somebody who typed a half meant that half. Resolved against the
        game in :meth:`retrosaves_export`.
        """
        text = str(game).strip()
        head, _, rest = text.partition(" ")
        choice = EXPORT_CHOICES.get(head.strip().strip("`").lower())
        if choice is not None and rest.strip():
            return choice, rest.strip()
        return "default", text

    @commands.bot_has_permissions(attach_files=True)
    # An export uploads a file per invocation; four a minute is far more than
    # anybody does by hand and is the difference between a feature and an
    # outbound-bandwidth amplifier.
    @commands.cooldown(
        SAVE_COOLDOWN_RATE, SAVE_COOLDOWN_SECONDS, commands.BucketType.user
    )
    # No `download` alias any more: `[p]retroset download` fetches emulators,
    # and one word cannot mean both "send me my save" and "install a core".
    @retrosaves.command(name="export", aliases=["backup"])
    async def retrosaves_export(self, ctx: commands.Context, *, game: str) -> None:
        """
        Post a game's save as a file you can keep.

        By default this sends the in-game save, the `.srm` file RetroArch
        and most emulators read, so a player can carry their progress
        somewhere else — or, for a game that has no in-game save because its
        cartridge has no battery, the save state instead. Put `save`, `state`
        or `both` in front of the name to ask for one of them by name; a save
        state only works on the exact emulator core that wrote it, and it is
        much larger.

        Anyone in the channel can export; nothing is changed by doing it.

        **Examples:**
        - `[p]retrosaves export ucity`
        - `[p]retrosaves export state ucity`
        - `[p]retrosaves export both ucity`

        **Arguments:**
        - `<game>` - A game this channel has played, optionally after `save`, `state` or `both`.
        """
        what, name = self._split_export(game)
        entry = await self._resolve_save(ctx, name)
        if entry is None:
            # Nothing was sent and nothing was read: an unknown or ambiguous
            # name is a typo away from a working command. See
            # _refund_cooldown.
            self._refund_cooldown(ctx)
            return
        if not entry.has_save:
            self._refund_cooldown(ctx)
            await self._safe_send(
                ctx,
                f"**{entry.game_name}** has nothing saved yet, so there is "
                "nothing to export. Play it for a few presses and it will "
                "save itself.",
            )
            return

        # What a bare `export <game>` means, decided against the game rather
        # than in the parser. The in-game save is the preference, because it
        # is the copy that travels between emulators -- but a cartridge with
        # no battery never has one, and those are common enough (most
        # homebrew, every game that saved by password) that a default which
        # could only ever name a file that cannot exist turned "export my
        # game" into a dead end: the reply said there was no in-game save and
        # said nothing at all about the save state sitting next to it. So the
        # default falls through to the state when that is the only half there
        # is, and the reply says why it did.
        #
        # Only the default falls through. `export save <game>` is somebody
        # naming a half, and it is still answered about that half.
        fell_back = what == "default" and not entry.has_sram and entry.has_state
        if what == "default":
            what = "state" if fell_back else "sram"

        limit = self._upload_limit(ctx)
        wanted: typing.List[typing.Tuple[str, Path, str, typing.Optional[int]]] = []
        if what in ("sram", "both"):
            wanted.append(
                (
                    "in-game save",
                    self._sram_path(ctx.channel.id, entry.slug),
                    f"{entry.slug}.srm",
                    entry.sram_size,
                )
            )
        if what in ("state", "both"):
            wanted.append(
                (
                    "save state",
                    self._state_path(ctx.channel.id, entry.slug),
                    f"{entry.slug}.state",
                    entry.state_size,
                )
            )

        files: typing.List[discord.File] = []
        sent: typing.List[str] = []
        notes: typing.List[str] = []
        for label, path, filename, size in wanted:
            if not size:
                notes.append(f"There is no {label} for this game.")
                continue
            if size > limit:
                notes.append(
                    f"The {label} is {self._humanize_bytes(size)}, which is "
                    f"over the {self._humanize_bytes(limit)} this server "
                    "accepts as an attachment, so it was left out."
                )
                continue
            try:
                # The path, not the payload: given a path, discord.File opens
                # the file and streams it into the upload (closing it when the
                # send is done), so a multi-megabyte save state is never
                # copied through memory just to be attached. Opening it is
                # also the readability check -- the size above came from the
                # same directory listing the command started from.
                files.append(discord.File(path, filename=filename))
            except OSError as error:
                log.warning("Could not read %s to export it.", path, exc_info=True)
                notes.append(f"The {label} could not be read: {error}")
                continue
            sent.append(f"the {label} (`{filename}`, {self._humanize_bytes(size)})")

        if not files:
            # Nothing was uploaded and no file was opened, so this invocation
            # cost the bot nothing worth rationing.
            self._refund_cooldown(ctx)
            if what == "sram" and entry.has_state:
                # Reachable only from an explicit `export save <game>` now
                # that the default falls through, and worth saying even
                # there: the answer "there is no in-game save" is true and
                # useless on its own when the game's whole progress is
                # sitting in a save state.
                notes.append(
                    "It does have a save state \N{EM DASH} "
                    f"`{ctx.clean_prefix}retrosaves export state {entry.slug}` "
                    "sends that one."
                )
            await self._safe_send(
                ctx,
                f"Nothing could be exported for **{entry.game_name}**. "
                + " ".join(notes),
            )
            return
        lines = [f"**{entry.game_name}**: {humanize_list(sent)}."]
        if fell_back:
            lines.append(
                f"**{entry.game_name}** has no in-game save \N{EM DASH} its "
                "cartridge may have no battery to keep one in, or nobody has "
                "saved from inside the game yet \N{EM DASH} so its save state "
                "is what was sent."
            )
        if any(upload.filename.endswith(".state") for upload in files):
            lines.append(
                "A save state only loads on the same build of the same "
                f"emulator core (`{entry.core or 'unknown'}`); the `.srm` is "
                "the one that travels."
            )
        lines.extend(notes)
        lines.append(
            f"Bring one back with `{ctx.clean_prefix}retrosaves import "
            f"{entry.slug}` and the file attached."
        )
        await self._safe_send(ctx, "\n".join(lines), files=files)

    @retrosaves.command(name="dropstate", aliases=["reset", "restart"])
    async def retrosaves_dropstate(self, ctx: commands.Context, *, game: str) -> None:
        """
        Delete a game's save state file, keeping its in-game save.

        This is "go back to my last in-game save": the exact moment the game
        was left at is deleted, and the next time it starts it boots from the
        title screen with the in-game save in place, so a player can load
        their own save from inside the game.

        It works on any game this channel has played, running or not, and it
        deletes a *file* — it does not touch a game that is playing. To reboot
        the game that is playing right now, use `[p]retroreboot`.

        Use `[p]retrosaves delete` instead to wipe the in-game save too.

        It used to be called `[p]retrosaves reset`, which read exactly like
        `[p]retroreset` while doing something completely different. Both old
        names still work.

        Only the person who started the game, moderators (Manage Messages) and
        the bot owner can do this.

        **Examples:**
        - `[p]retrosaves dropstate ucity`

        **Arguments:**
        - `<game>` - A game this channel has played.
        """
        entry = await self._resolve_save(ctx, game)
        if entry is None:
            return
        if not await self._may_manage_saves(ctx, entry):
            await self._refuse_management(ctx, entry, "drop its save state")
            return
        if not entry.has_state and not entry.has_state_backup:
            await self._safe_send(
                ctx,
                f"**{entry.game_name}** has no save state, so there is "
                "nothing to drop. It already "
                + (
                    "starts from the title screen with the in-game save in "
                    "place."
                    if entry.has_sram
                    else "starts from the beginning."
                ),
            )
            return

        def drop_state_files() -> None:
            # The previous generation goes with it. Leaving it would be a
            # command that appears to do nothing: the restore chain would fall
            # straight through to the backup and the game would come back at
            # almost exactly the moment that was just dropped.
            for path in self._save_paths(ctx.channel.id, entry.slug)[:2]:
                path.unlink(missing_ok=True)

        try:
            # Deleted with the session paused and its lock held, never under
            # a running core: a live core holds its own copy of the state and
            # would write it back over this on the very next press. See
            # _mutate_saves.
            paused, _ = await self._mutate_saves(
                ctx, entry, "its save state was reset", drop_state_files
            )
        except OSError as error:
            log.warning("Could not delete a Retro save state.", exc_info=True)
            await self._safe_send(ctx, f"The save state could not be deleted: {error}")
            return
        log.info(
            "Dropped the save state for %s in channel %s at %s's request.",
            entry.slug,
            ctx.channel.id,
            getattr(ctx.author, "id", "?"),
        )
        lines = [
            f"Dropped the save state for **{entry.game_name}** "
            f"({self._humanize_bytes(entry.state_size)})."
        ]
        if entry.has_state_backup:
            lines.append(
                "Its previous save state went with it, since the game would "
                "otherwise have come straight back from that instead."
            )
        if entry.has_sram:
            lines.append(
                "Its in-game save is untouched, so the game will start from "
                "the title screen with it in place \N{EM DASH} load it from "
                "the game's own menu to carry on."
            )
        else:
            lines.append(
                "This game has no in-game save, so it will start from the "
                "beginning."
            )
        if paused:
            lines.append(
                "The game was running, so it was saved and put to sleep first "
                "\N{EM DASH} otherwise the next button press would have "
                "written the old save state straight back. Press a button on "
                "it to start it again."
            )
        await self._safe_send(ctx, " ".join(lines))

    # No `undo` alias any more. There is an **Undo** button under every game
    # that does something completely different -- one press back, in memory --
    # so a player who liked that button and typed the word lost several
    # presses' worth of save instead. See RetroView._undo.
    @retrosaves.command(name="rollback", aliases=["previous"])
    async def retrosaves_rollback(self, ctx: commands.Context, *, game: str) -> None:
        """
        Go back to the save before the last one.

        Every successful save keeps the generation it replaced, so if the last
        automatic save landed somewhere useless — the moment after a game
        over, a boss room nobody can get out of, a state a fresh emulator core
        will not load — this swaps it back for the one before it, which is a
        few presses of play earlier.

        It is a **swap**, not a delete: what is being rolled back from becomes
        the new previous generation, so running it a second time puts things
        exactly as they were. Nothing here is destroyed, and nothing here is
        kept forever either — the next few button presses save the game again
        and rotate the older copy out, so roll back before carrying on.

        Only the person who started the game, moderators (Manage Messages) and
        the bot owner can do this.

        **Examples:**
        - `[p]retrosaves rollback ucity`

        **Arguments:**
        - `<game>` - A game this channel has played.
        """
        entry = await self._resolve_save(ctx, game)
        if entry is None:
            return
        if not await self._may_manage_saves(ctx, entry):
            await self._refuse_management(ctx, entry, "roll its save back")
            return
        if not entry.has_backup:
            await self._safe_send(
                ctx,
                f"**{entry.game_name}** has no previous save to go back to. "
                "One is kept from its second automatic save onwards, so play "
                "it for a few more presses and there will be. "
                f"`{ctx.clean_prefix}retrosaves dropstate {entry.slug}` goes back "
                "to the last in-game save instead, and "
                f"`{ctx.clean_prefix}retrosaves import {entry.slug}` installs "
                "a copy you exported earlier.",
            )
            return

        # Same rule as everything else in this group: the live core holds the
        # authoritative copy and would write it straight back over this, so
        # the swap runs via _mutate_saves, with the session asleep and its
        # lock held so no press can wake it mid-swap.
        try:
            paused, swapped = await self._mutate_saves(
                ctx,
                entry,
                "its save was rolled back",
                lambda: self._rollback_saves(ctx.channel.id, entry.slug),
            )
        except OSError as error:
            log.warning("Could not roll a Retro save back.", exc_info=True)
            await self._safe_send(ctx, f"The save could not be rolled back: {error}")
            return
        if not swapped:
            await self._safe_send(
                ctx,
                "Nothing could be rolled back; the bot's data folder may be "
                "read-only. The details are in the bot's log.",
            )
            return
        log.info(
            "Rolled %s back to its previous %s in channel %s at %s's request.",
            entry.slug,
            humanize_list(swapped),
            ctx.channel.id,
            getattr(ctx.author, "id", "?"),
        )
        lines = [
            f"**{entry.game_name}** has been rolled back to its previous "
            f"{humanize_list(swapped)}."
        ]
        lines.append(
            "The copy it was on is now the previous generation, so running "
            f"`{ctx.clean_prefix}retrosaves rollback {entry.slug}` again puts "
            "it back."
        )
        if paused:
            lines.append(
                "The game was running, so it was saved and put to sleep first "
                "\N{EM DASH} otherwise the next button press would have "
                "written the newer save straight back. Press a button on it to "
                "carry on from the rolled-back save."
            )
        await self._safe_send(ctx, " ".join(lines))

    def _rollback_saves(self, channel_id: int, slug: str) -> typing.List[str]:
        """
        Swap each half of a save with its previous generation. Blocking.

        Returns what was swapped, for the reply. A *swap* rather than a
        promotion, so the command is its own undo: the file being rolled back
        from lands in the backup slot instead of being deleted.

        Done through a third name (ROLLBACK_SUFFIX) so that neither file is
        ever lost if the process dies mid-swap, and so that every crash point
        is put right at the next cog load by ``_sweep_partial_writes``. Each
        rename is atomic on its own, so a crash can only land between them,
        and each gap is recoverable from the shape it leaves (the full
        reasoning is on :meth:`StorageMixin._recover_rollback`):

        * between (1) live -> spare and (2) backup -> live, the live slot is
          empty and the spare holds the newest save; the sweep puts it back.
        * between (2) and (3) spare -> backup, the backup slot is empty and
          the spare holds what used to be live; the sweep finishes the swap.

        Until that sweep runs, the worst a boot sees is one missing slot,
        which the restore chain answers by falling through to whatever it
        still has -- never a lost generation.
        """
        swapped: typing.List[str] = []
        state, state_backup, sram, sram_backup = self._save_paths(channel_id, slug)
        for live, backup, label in (
            (state, state_backup, "save state"),
            (sram, sram_backup, "in-game save"),
        ):
            if not backup.is_file():
                continue
            spare = live.with_name(live.name + ROLLBACK_SUFFIX)
            try:
                if live.is_file():
                    live.replace(spare)
                backup.replace(live)
                if spare.is_file():
                    spare.replace(backup)
            except OSError:
                log.warning(
                    "Could not swap %s with %s while rolling back.",
                    live,
                    backup,
                    exc_info=True,
                )
                continue
            swapped.append(label)
        return swapped

    @retrosaves.command(name="delete", aliases=["wipe", "erase", "clear"])
    async def retrosaves_delete(self, ctx: commands.Context, *, game: str) -> None:
        """
        Wipe a game's save data so it starts completely fresh.

        Deletes **both** the save state and the in-game save, so
        the next time the game starts it is exactly as if nobody had ever
        played it here. The in-game save goes with it, and none of it can be
        recovered — take a copy with `[p]retrosaves export` first if you might
        want it.

        The cached ROM is kept, so the game itself still starts instantly.
        To keep the in-game save and only drop the exact moment, use
        `[p]retrosaves dropstate`.

        Only the person who started the game, moderators (Manage Messages) and
        the bot owner can do this, and it asks first.

        **Examples:**
        - `[p]retrosaves delete ucity`

        **Arguments:**
        - `<game>` - A game this channel has played.
        """
        entry = await self._resolve_save(ctx, game)
        if entry is None:
            return
        if not await self._may_manage_saves(ctx, entry):
            await self._refuse_management(ctx, entry, "delete its save data")
            return
        if not entry.has_save:
            await self._safe_send(
                ctx,
                f"**{entry.game_name}** has nothing saved, so it already "
                "starts from the beginning.",
            )
            return

        # Sized here, where the sizes are still current, and only for the
        # question. What is finally deleted is measured again at the time and
        # named from that, because putting a live session to sleep below
        # rewrites both files first -- and because a file that could not be
        # removed must not be listed among the ones that were.
        pieces = []
        if entry.has_state:
            pieces.append(f"its save state ({self._humanize_bytes(entry.state_size)})")
        if entry.has_sram:
            pieces.append(
                f"its in-game save ({self._humanize_bytes(entry.sram_size)})"
            )
        if entry.has_backup:
            pieces.append("the previous generation of both")
        question = (
            f"Delete {humanize_list(pieces)} for **{entry.game_name}**?\n"
            "The game will start from the very beginning next time, and none "
            "of this can be undone."
        )
        if entry.has_sram:
            question += (
                " Whatever anyone saved from inside the game is included."
            )
        question += (
            f"\nTo keep the in-game save and only go back to it, use "
            f"`{ctx.clean_prefix}retrosaves dropstate {entry.slug}` instead."
        )
        if not await self._confirm(ctx, question):
            await self._safe_send(
                ctx, f"Left **{entry.game_name}**'s save alone."
            )
            return

        # Via _mutate_saves, so a press arriving while the confirmation sat
        # on screen cannot have woken a core that would write everything
        # straight back; see the helper.
        paused, outcome = await self._mutate_saves(
            ctx,
            entry,
            "its save data was deleted",
            lambda: self._delete_saves(ctx.channel.id, entry.slug),
        )
        log.info(
            "Deleted %s for %s in channel %s at %s's request.%s",
            humanize_list(outcome.removed) or "nothing",
            entry.slug,
            ctx.channel.id,
            getattr(ctx.author, "id", "?"),
            f" {humanize_list(outcome.failed)} could not be removed."
            if outcome.failed
            else "",
        )
        if outcome.failed:
            await self._report_partial_delete(ctx, entry, outcome, paused)
            return
        if not outcome.removed:
            # Everything was gone before the delete ran: a second delete of
            # the same game, or a prune between the listing and the
            # confirmation. Claiming to have wiped four files that were not
            # there would be the same kind of lie as the partial case.
            await self._safe_send(
                ctx,
                f"**{entry.game_name}** had nothing left to delete by the "
                "time the confirmation came back, so nothing was removed. It "
                "will start from the very beginning next time.",
            )
            return
        lines = [
            f"Wiped **{entry.game_name}**'s save data: "
            f"{humanize_list(outcome.removed)}, "
            f"{self._humanize_bytes(outcome.freed)} in all. "
            "It will start from the very beginning next time."
        ]
        if entry.rom is not None:
            lines.append(
                "The cached ROM was kept, so it still starts straight away."
            )
        if paused:
            lines.append(
                "The game was running, so it was saved and put to sleep first "
                "\N{EM DASH} otherwise the next button press would have "
                "written it all straight back. Press a button on it to start "
                "the game over."
            )
        await self._safe_send(ctx, " ".join(lines))

    async def _report_partial_delete(
        self,
        ctx: commands.Context,
        entry: SaveInfo,
        outcome: DeleteOutcome,
        paused: bool,
    ) -> None:
        """
        Say which of a game's save files went and which are still there.

        The rare half of `[p]retrosaves delete`, and the only half that can
        leave a game in a shape nobody asked for: the save state deleted and
        the in-game save surviving is neither "wiped" nor "unchanged", and
        the old reply -- "The save files could not be deleted" after three of
        the four already had been -- sent somebody off to start a game they
        had been told was untouched and find it halfway through.

        So the two lists are read out, and then the same sentence
        `[p]retrosaves info` would give is read off the files as they are
        *now* rather than guessed at, because what survived decides where the
        game comes back from.
        """
        lines = [
            (
                f"Only part of **{entry.game_name}**'s save data could be "
                f"deleted. Gone: {humanize_list(outcome.removed)}, "
                f"{self._humanize_bytes(outcome.freed)} in all."
            )
            if outcome.removed
            else f"None of **{entry.game_name}**'s save data could be deleted."
        ]
        lines.append(
            f"Still there: {humanize_list(outcome.failed)} \N{EM DASH} the "
            "bot's data folder may be read-only, and the details are in the "
            "bot's log."
        )
        left = next(
            (
                fresh
                for fresh in await self._saved_games(ctx.channel.id)
                if fresh.slug == entry.slug
            ),
            None,
        )
        if left is not None:
            lines.append(
                "Started now, it would come back " + self._restore_sentence(left)
            )
        lines.append(
            f"Running `{ctx.clean_prefix}retrosaves delete {entry.slug}` again "
            "is safe once that is sorted out \N{EM DASH} it only removes what "
            "is left."
        )
        if paused:
            lines.append(
                "The game was saved and put to sleep for the delete; press a "
                "button on it to pick it back up from whatever survived."
            )
        await self._safe_send(ctx, " ".join(lines))

    def _delete_saves(self, channel_id: int, slug: str) -> DeleteOutcome:
        """
        Remove every save file for one game. Blocking; never raises.

        All four of them: both halves and the previous generation of each.
        "Wipe this game's progress" has to mean it, and a rollback that
        resurrected what somebody had just deleted would be worse than not
        having a rollback at all.

        Every path is tried even after one of them has failed, and what
        failed comes back instead of a bare "it did not work". Giving up on
        the first OSError left the four files in a shape nobody was ever told
        about -- two deleted, two not -- under a message that said none of
        them had been; carrying on at least makes the answer describable, and
        deleting as much as can be deleted is what was asked for anyway.

        ``failed`` names the files that are *still there*, which is the only
        thing worth telling somebody. A file that was already gone and whose
        unlink failed anyway (an unwritable directory says the same thing
        about a name that is not in it) is logged and left out: "the in-game
        save survived" about a save that never existed is its own lie.
        """
        freed = 0
        removed: typing.List[str] = []
        failed: typing.List[str] = []
        paths = self._save_paths(channel_id, slug)
        # strict: DELETE_LABELS is _save_paths' tuple spelled out in words, so
        # a fifth save file added to one and not the other should stop the
        # delete rather than silently leave the new file behind.
        for path, label in zip(paths, DELETE_LABELS, strict=True):
            facts = self._file_facts(path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete the Retro save %s", path, exc_info=True)
                if facts is not None:
                    failed.append(label)
                continue
            if facts is not None:
                removed.append(label)
                freed += facts[0]
        return DeleteOutcome(freed, removed, failed)

    # An import downloads two attachments and boots a real core to check them
    # against the cartridge, which is the most expensive thing in this group.
    @commands.cooldown(
        SAVE_COOLDOWN_RATE, SAVE_COOLDOWN_SECONDS, commands.BucketType.user
    )
    # No `restore` alias any more: "restore" is what this whole cog calls
    # putting a game back on boot (see RetroView.restore_into, and every
    # sentence about a save state that could not be restored), so an alias
    # that meant "install this attachment" was the same word for two things.
    @retrosaves.command(name="import", aliases=["upload"])
    async def retrosaves_import(self, ctx: commands.Context, *, game: str) -> None:
        """
        Install a save file you attach, so a player can bring progress in.

        Attach the in-game save (`.srm` or `.sav`) and, if you want the
        exact moment back too, its save state (`.state`). Both are checked
        against the real emulator before anything is written: an in-game save
        that is the wrong size for this cartridge, or a save state this
        emulator will not load, is refused with an explanation rather than
        quietly ignored when the game next starts.

        **This overwrites whatever the channel already has for that game**,
        so it asks first. Importing an in-game save on its own also removes the
        old save state, because a save state is the whole machine and would
        otherwise be restored over the top of the save you just brought in.

        The game has to be one this channel has played, so the save can be
        matched to a cartridge. Only the person who started it, moderators
        (Manage Messages) and the bot owner can import.

        **Examples:**
        - `[p]retrosaves import ucity` (with `ucity.srm` attached)

        **Arguments:**
        - `<game>` - A game this channel has played.
        """
        entry = await self._resolve_save(ctx, game)
        if entry is None:
            # No attachment has been downloaded yet, so neither of these cost
            # anything worth rationing and both are a retry away from
            # working. See _refund_cooldown.
            self._refund_cooldown(ctx)
            return
        if not await self._may_manage_saves(ctx, entry):
            self._refund_cooldown(ctx)
            await self._refuse_management(ctx, entry, "import a save for it")
            return

        # _read_import hands the cooldown back itself on the paths that give
        # up before downloading anything, and keeps it charged on the ones
        # that have already pulled the bytes.
        incoming = await self._read_import(ctx, entry)
        if incoming is None:
            return
        state, sram = incoming

        # Two fates for what is already there, and the question must not mix
        # them up. A file that is *overwritten* is rotated into the backup
        # slot first (see _write_import), so `rollback` really can bring it
        # back. The save state a battery-save-only import removes is
        # *deleted*, both generations of it, and cannot -- so promising the
        # rollback there would be promising something the command cannot do.
        replacing = []
        if sram is not None and entry.has_sram:
            replacing.append(
                f"its in-game save ({self._humanize_bytes(entry.sram_size)})"
            )
        if state is not None and entry.has_state:
            replacing.append(
                f"its save state ({self._humanize_bytes(entry.state_size)})"
            )
        deleting_state = state is None and (
            entry.has_state or entry.has_state_backup
        )
        if replacing or deleting_state:
            sentences = []
            if replacing:
                sentences.append(
                    f"Importing this will overwrite {humanize_list(replacing)} "
                    f"for **{entry.game_name}**. What is there now is kept as "
                    f"the previous generation, so `{ctx.clean_prefix}retrosaves "
                    f"rollback {entry.slug}` can swap back to it \N{EM DASH} "
                    "but only until the next automatic save rotates it out."
                )
            if deleting_state:
                sized = (
                    f" ({self._humanize_bytes(entry.state_size)})"
                    if entry.has_state
                    else ""
                )
                sentences.append(
                    (
                        "It will also delete its"
                        if replacing
                        else f"Importing this will delete **{entry.game_name}**'s"
                    )
                    + f" save state{sized} outright, the previous generation "
                    "of it included \N{EM DASH} a save state is the whole "
                    "machine and either copy would be restored over the top "
                    "of the in-game save you are bringing in. A deleted save "
                    "state cannot be rolled back."
                )
            keep = "both " if deleting_state and entry.has_state else ""
            sentences.append(
                f"`{ctx.clean_prefix}retrosaves export {keep}{entry.slug}` "
                "first is the way to keep a copy. Go ahead?"
            )
            if not await self._confirm(ctx, " ".join(sentences)):
                await self._safe_send(
                    ctx, f"Left **{entry.game_name}**'s save alone."
                )
                return

        # Sleep first, so nothing that is written below can be overwritten by
        # a core that is still holding the old save in memory.
        paused = await self._pause_for_saves(ctx, entry, "a save was imported")
        # Asked here as well as inside the check, because the answer is worth
        # something on the way *out*: an in-game save that could not be tried
        # on the cartridge is accepted anyway (see _check_import) and the only
        # sign it did not fit is the game quietly ignoring it days later.
        # Whoever imported it deserves to be told which of the two happened.
        blocker = await self._import_check_blocker(entry)
        problem = await self._check_import(entry, state, sram, ctx.clean_prefix)
        if problem is not None:
            lines = [problem, "Nothing was changed."]
            if paused:
                lines.append(
                    "The game was put to sleep to make room for the check; "
                    "press a button on it to carry on where it was."
                )
            await self._safe_send(ctx, " ".join(lines))
            return

        try:
            # Via _mutate_saves rather than straight to a thread: the check
            # above took whole seconds of core boot, and any button press
            # during it woke the session from the old files. Written under a
            # core that came back like that, the import would be silently
            # overwritten by its very next automatic save -- so the session
            # is put back to sleep, under its own lock, and stays there until
            # the imported files are on disk.
            paused_again, written = await self._mutate_saves(
                ctx,
                entry,
                "a save was imported",
                lambda: self._write_import(ctx.channel.id, entry.slug, state, sram),
            )
            paused = paused or paused_again
        except OSError as error:
            log.warning("Could not write an imported Retro save.", exc_info=True)
            await self._safe_send(
                ctx,
                f"The save could not be written: {error}. The bot may be out "
                "of disk space.",
            )
            return
        log.info(
            "Imported %s for %s in channel %s at %s's request.",
            humanize_list(written) or "nothing",
            entry.slug,
            ctx.channel.id,
            getattr(ctx.author, "id", "?"),
        )

        lines = [f"Imported {humanize_list(written)} for **{entry.game_name}**."]
        if state is None and (entry.has_state or entry.has_state_backup):
            # Both generations go (see _write_import), so both are accounted
            # for here -- the confirmation was made precise about which files
            # survive an import and a success message that mentioned only the
            # live one would quietly take that back. A game whose newer state
            # had already been thrown out for being unloadable has only the
            # older one to lose, and it loses it just the same.
            if entry.has_state and entry.has_state_backup:
                gone = "The old save state and the previous generation of it were"
            elif entry.has_state:
                gone = "The old save state was"
            else:
                gone = "The previous save state, the only one left, was"
            lines.append(
                f"{gone} removed with it: a save state is the whole machine "
                "and would have been restored over the top of the in-game "
                "save you just brought in."
            )
        if state is not None:
            lines.append(
                "The game will pick up from the imported save state on its "
                "next start."
            )
        elif sram is not None:
            lines.append(
                "The game will start from the title screen with the imported "
                "save in place \N{EM DASH} load it from the game's own menu."
            )
        if sram is not None and blocker is not None:
            lines.append(self._unchecked_import_note(entry, blocker, ctx.clean_prefix))
        if paused:
            lines.append(
                "The game was running, so it was saved and put to sleep first "
                "\N{EM DASH} otherwise the next button press would have "
                "written the old save straight back. Press a button on it to "
                "start it with the imported save."
            )
        await self._safe_send(ctx, " ".join(lines))

    async def _read_import(
        self, ctx: commands.Context, entry: SaveInfo
    ) -> typing.Optional[
        typing.Tuple[typing.Optional[bytes], typing.Optional[bytes]]
    ]:
        """
        Sort the attachments into ``(save state, in-game save)`` bytes.

        Everything that can be checked without a core is checked here: that
        there is an attachment at all, that its extension says what it is,
        that there is at most one of each, that it is not empty, and that it
        is inside the size ceilings. Returns None (having said why) if not.

        The cooldown is handed back on every refusal above the download loop
        and kept on every refusal below it, which is the line drawn in
        :meth:`_refund_cooldown`: an attachment Discord reported as 4 MiB was
        never fetched, so the retry that renames the file should not have to
        wait a minute, while bytes that really were pulled were really paid
        for.
        """
        attachments = list(getattr(ctx.message, "attachments", ()) or ())
        if not attachments:
            self._refund_cooldown(ctx)
            await self._safe_send(
                ctx,
                "Attach the save file to your message: an in-game save "
                f"(`{'`, `'.join(SRAM_EXTENSIONS)}`) and optionally a save "
                f"state (`{'`, `'.join(STATE_EXTENSIONS)}`). Export one first "
                f"with `{ctx.clean_prefix}retrosaves export {entry.slug}` to "
                "see what they look like.",
            )
            return None
        if len(attachments) > 2:
            self._refund_cooldown(ctx)
            await self._safe_send(
                ctx,
                "Attach at most two files: one in-game save and one save "
                "state.",
            )
            return None

        found: typing.Dict[str, typing.Tuple[str, typing.Any]] = {}
        for attachment in attachments:
            name = str(getattr(attachment, "filename", "") or "")
            suffix = Path(name).suffix.lower()
            if suffix in SRAM_EXTENSIONS:
                kind = "sram"
            elif suffix in STATE_EXTENSIONS:
                kind = "state"
            else:
                self._refund_cooldown(ctx)
                await self._safe_send(
                    ctx,
                    f"`{name or 'that file'}` is not a save this cog knows. A "
                    f"in-game save ends in `{'`, `'.join(SRAM_EXTENSIONS)}` "
                    f"and a save state in `{'`, `'.join(STATE_EXTENSIONS)}`. "
                    "Rename the file to match and attach it again.",
                )
                return None
            if kind in found:
                self._refund_cooldown(ctx)
                await self._safe_send(
                    ctx,
                    f"Two {'in-game saves' if kind == 'sram' else 'save states'} "
                    "were attached; attach one of each at most.",
                )
                return None
            limit, label = (
                (MAX_IMPORT_SRAM_SIZE, MAX_IMPORT_SRAM_LABEL)
                if kind == "sram"
                else (MAX_IMPORT_STATE_SIZE, MAX_IMPORT_STATE_LABEL)
            )
            size = int(getattr(attachment, "size", 0) or 0)
            if size > limit:
                self._refund_cooldown(ctx)
                await self._safe_send(
                    ctx,
                    f"`{name}` is {self._humanize_bytes(size)}, past the "
                    f"{label} limit for a "
                    f"{'in-game save' if kind == 'sram' else 'save state'}. "
                    "That is not a save for one of these consoles.",
                )
                return None
            found[kind] = (name, attachment)

        data: typing.Dict[str, bytes] = {}
        for kind, (name, attachment) in found.items():
            try:
                payload = await attachment.read()
            except discord.HTTPException as error:
                log.warning("Could not read an imported Retro save.", exc_info=True)
                await self._safe_send(
                    ctx, f"`{name}` could not be downloaded from Discord: {error}"
                )
                return None
            if not payload:
                await self._safe_send(ctx, f"`{name}` is empty.")
                return None
            limit = MAX_IMPORT_SRAM_SIZE if kind == "sram" else MAX_IMPORT_STATE_SIZE
            if len(payload) > limit:
                await self._safe_send(
                    ctx,
                    f"`{name}` is {self._humanize_bytes(len(payload))}, which "
                    "is past the limit for that kind of save.",
                )
                return None
            data[kind] = bytes(payload)
        return data.get("state"), data.get("sram")

    async def _import_check_blocker(self, entry: SaveInfo) -> typing.Optional[str]:
        """
        Why an incoming save cannot be tried on the real core, or None.

        ``"rom"`` when the cached ROM has been pruned and ``"core"`` when the
        emulator the game needs is not installed. Either way there is nothing
        to boot, so there is nothing to offer the file to.

        One answer, two callers with opposite uses for it:
        :meth:`_check_import` turns it into a refusal for a save state, which
        is worthless unless a core has actually loaded it, and
        ``retrosaves_import`` turns it into the caveat on an accepted in-game
        save, which is merely unproven. They worked it out separately once,
        which is one edit away from a command that checks nothing and says
        nothing about it.
        """
        if entry.rom is None or not entry.rom.is_file():
            return "rom"
        if not entry.core or await self._core_path(entry.core) is None:
            return "core"
        return None

    def _unchecked_import_note(
        self, entry: SaveInfo, blocker: str, prefix: str
    ) -> str:
        """
        Own up to an in-game save that went in without being checked.

        An in-game save is the cartridge's own battery memory and is accepted
        unvalidated (see :meth:`_check_import`) because storing one costs
        nothing. Being *ignored* later does cost something, though: a `.sav`
        that is the wrong size for this cartridge is passed over at the next
        boot without a word, and from the channel's side that looks exactly
        like an import that worked and a game that lost it. The only moment
        anybody can be told is now, so this is appended to the success
        message rather than left to be discovered.
        """
        if blocker == "rom":
            why = (
                f"the cached ROM for **{entry.game_name}** has been cleaned "
                "up, so there was no cartridge to try it against"
            )
            fix = (
                f" Start the game once with `{prefix}retro <name or url>` and "
                "import it again if that happens, and it will be checked "
                "properly."
            )
        else:
            why = (
                f"the emulator **{entry.game_name}** needs "
                f"(`{entry.core or 'unknown'}`) is not installed, so there "
                "was nothing to try it against"
            )
            fix = (
                " Ask the bot owner to install it, then import again to have "
                "it checked properly."
            )
        return (
            f"It was **not** checked first: {why}. If it turns out to be the "
            "wrong size for this game's save memory, the game will ignore it "
            "when it starts and carry on as though it had never been "
            f"imported.{fix}"
        )

    async def _check_import(
        self,
        entry: SaveInfo,
        state: typing.Optional[bytes],
        sram: typing.Optional[bytes],
        prefix: str = "",
    ) -> typing.Optional[str]:
        """
        Try an incoming save on the real core, and say what is wrong with it.

        This is the only honest way to validate either file: a save state is
        loadable by exactly one build of one core, and a cartridge's battery
        size is decided by the cartridge. So the game is actually booted, the
        files are actually offered to it, and the core's own refusal is turned
        into a sentence. Returns None when everything fits.

        Loading a core is subject to the one-at-a-time rule like everything
        else, so the lock is held and anything still running is hibernated
        first.

        ``prefix`` is the bot's real command prefix. What comes back from
        here is *sent*, and Red only rewrites ``[p]`` in a docstring, so the
        command this points at has to be spelled with the prefix the caller
        was invoked with (``ctx.clean_prefix``).
        """
        if state is None and sram is None:
            return None
        blocker = await self._import_check_blocker(entry)
        # Looked up again rather than carried out of the blocker, and the gap
        # between the two closed here: a core that was uninstalled in between
        # is the "core" blocker by another route, never a None handed to an
        # emulator.
        core_path = None if blocker is not None else await self._core_path(entry.core)
        if blocker is None and core_path is None:
            blocker = "core"
        if blocker is not None:
            if state is None:
                # A battery save is the cartridge's own format and is checked
                # against the region size at boot, so storing one unvalidated
                # costs nothing worse than it being ignored later -- and the
                # reply says exactly that rather than leaving it to be found
                # out on the next start. See _unchecked_import_note.
                return None
            if blocker == "rom":
                return (
                    f"The cached ROM for **{entry.game_name}** has been "
                    "cleaned up, so a save state cannot be checked against it "
                    "\N{EM DASH} and a state that does not match its core is "
                    "refused at boot anyway. Start the game once with "
                    f"`{prefix}retro <name or url>` and import the state "
                    "after that."
                )
            return (
                f"The emulator core **{entry.game_name}** needs "
                f"(`{entry.core or 'unknown'}`) is not installed, so a "
                "save state cannot be checked against it."
            )

        emulator = RetroEmulator(
            core_path,
            entry.rom,
            system_dir=self._system_dir(),
            options=await self._core_options(entry.core),
        )
        async with self.emulator_lock:
            # One core at a time: whatever is playing elsewhere is saved and
            # put to sleep, exactly as starting a game would do.
            await self._evict_locked()
            try:
                outcome = await self.run_in_emulator_thread(
                    self._try_import, emulator, entry, state, sram
                )
            except EmulatorError as error:
                log.warning("Could not check an imported save: %s", error)
                outcome = (
                    f"**{entry.game_name}** could not be started to check the "
                    f"save against it: {error}"
                )
            finally:
                try:
                    await self.run_in_emulator_thread(emulator.stop)
                except Exception:
                    log.exception("Could not stop the import-check emulator.")
        # Whatever this check put to sleep is told so now, with the lock given
        # back rather than while it is held; see Retro._flush_refreshes.
        await self._flush_refreshes()
        return outcome

    def _try_import(
        self,
        emulator: RetroEmulator,
        entry: SaveInfo,
        state: typing.Optional[bytes],
        sram: typing.Optional[bytes],
    ) -> typing.Optional[str]:
        """Boot the game and offer it the files. Blocking. None if they fit."""
        emulator.start()
        if sram is not None:
            size = emulator.sram_size
            if not size:
                return (
                    f"**{entry.game_name}** has no battery-backed save memory "
                    "at all, so there is nowhere to put an in-game save. This "
                    "cartridge keeps its progress in save states only."
                )
            if len(sram) != size:
                return (
                    f"That in-game save is {self._humanize_bytes(len(sram))} "
                    f"({len(sram):,} bytes) but **{entry.game_name}** has "
                    f"{self._humanize_bytes(size)} ({size:,} bytes) of save "
                    "memory. It is a save for a different game, a different "
                    "revision of it, or a different emulator's layout, and "
                    "restoring part of one would hand the game a half-written "
                    "save file."
                )
        if state is not None:
            try:
                emulator.load_state(state)
            except EmulatorError as error:
                return (
                    f"That save state is not one **{entry.game_name}**'s core "
                    f"(`{entry.core or 'unknown'}`) can load: {error} A save "
                    "state only works on the exact build of the exact core "
                    "that wrote it, so one from another emulator, another "
                    "machine, or an older version of this core cannot be used "
                    "\N{EM DASH} the in-game save is the one that travels."
                )
        return None

    def _write_import(
        self,
        channel_id: int,
        slug: str,
        state: typing.Optional[bytes],
        sram: typing.Optional[bytes],
    ) -> typing.List[str]:
        """
        Put validated saves in place. Blocking. Returns what it wrote.

        Everything *overwritten* is rotated into its backup slot first, so
        `[p]retrosaves rollback` genuinely brings it back.

        A battery save imported on its own takes the existing save state with
        it -- deleted, not rotated. A state is the whole machine and is
        restored *before* SRAM is even looked at (see
        :func:`RetroView.restore_into`), so leaving the old one anywhere the
        restore chain looks would restore the game over the top of the save
        that was just brought in; the import would look as though it had done
        nothing. That makes the old state the one thing an import destroys
        for good, and the confirmation in ``retrosaves_import`` says exactly
        that instead of promising a rollback it cannot deliver.
        """
        state_path, state_backup, sram_path, _ = self._save_paths(channel_id, slug)
        written: typing.List[str] = []
        if sram is not None:
            # keep_backup: whatever was there is still the channel's own
            # progress, and an import is exactly the kind of mistake somebody
            # wants to undo. `[p]retrosaves rollback` brings it back.
            self._write_atomic(sram_path, sram, True)
            written.append(f"a {self._humanize_bytes(len(sram))} in-game save")
        if state is not None:
            self._write_atomic(state_path, state, True)
            written.append(f"a {self._humanize_bytes(len(state))} save state")
        elif sram is not None:
            # Both generations of the state go, for the reason in the
            # docstring: either of them would be restored over the top of the
            # battery save that was just imported. Deleted, not parked in the
            # backup slot -- the restore chain boots from a lone
            # ``.state.bak`` exactly as happily as from a live state, so
            # "keeping" the old state there would make this import a silent
            # no-op on the next boot.
            state_path.unlink(missing_ok=True)
            state_backup.unlink(missing_ok=True)
        return written
