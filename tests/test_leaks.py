"""What the cog holds on to, and for how long.

A Discord bot is a long-running process. Everything in here is about the
same question asked of a different container: after N of something has come
and gone, is N still in memory? The tests are deliberately written as
"do the work many times, then assert the container did not grow" rather than
as assertions about one call, because a leak is a shape a single call cannot
show.

Three kinds of proof are used:

* a counted container (``len(...)`` before and after);
* ``weakref`` plus ``gc.collect()``, for "is this object really released";
* adding up the bytes every reachable session is holding, which used to be
  where the megabytes actually were (a replay buffer of up to 8 MiB apiece,
  before the Replay button was removed, and one clip each after that) and is
  now nothing at all.

Nothing here needs a real core or the network. The one thing it does need is
the *real* discord.py view store, because the leak that motivated this file
lives inside it: see test_a_retired_view_is_released_by_discord_py below.
"""

import asyncio
import gc
import types
import weakref

import pytest

from .loader import load_standalone

pytest.importorskip("discord", reason="the leak tests need discord.py")

import discord  # noqa: E402
from discord.ui.view import ViewStore  # noqa: E402

from .fakes import footage_bytes  # noqa: E402

C = load_standalone("retro_clips_for_leaks", "clips.py")


# -- A real discord.py view store behind the fake bot -------------------------
#
# tests/fakes.py's FakeBot.add_view only records the call, which is the right
# thing for every other test and exactly wrong for this file: the container
# under test *is* discord.py's. So these tests give the fake bot a real
# ViewStore and drive it the way discord.py itself does.
#
# discord.py 2.7.1 stores a view on every send and every edit that carries
# one -- Messageable.send (discord/abc.py:1710), Message.edit
# (discord/message.py:1418 and :2992), the interaction response and followup
# paths (discord/interactions.py:618, :1105, :1251) and Client.add_view
# (discord/client.py:3201) -- all of them landing in
# ConnectionState.store_view (discord/state.py:415) and from there in
# ViewStore.add_view. Nothing in discord.py ever removes an entry except
# ViewStore.remove_view, which is reached only from View.stop().


class Store:
    """A real ViewStore standing in for the one inside a real Client.

    The fake bot's ``add_view`` is redirected into it, which is the route
    the cog itself uses (``Retro._register_view`` and ``Retro._arm_retired``).
    Tests that want to model discord.py storing a view because a *message*
    carried one call :meth:`add` by hand at that point.
    """

    def __init__(self, env):
        self.env = env
        self.store = ViewStore.__new__(ViewStore)
        self.store._views = {}
        self.store._synced_message_views = {}
        self.store._modals = {}
        self.store._dynamic_items = {}
        self.store._state = None
        env.bot.add_view = self.add

    def add(self, view, message_id=None):
        # The same two guards Client.add_view applies (discord/client.py
        # :3195-3198), so a test cannot register something Discord would
        # have refused.
        if not view.is_persistent():
            raise ValueError("View is not persistent.")
        if view.is_finished():
            raise ValueError("View is already finished.")
        self.store.add_view(view, message_id)

    # -- what the store is holding
    @property
    def views(self):
        return self.store._views

    @property
    def synced(self):
        return self.store._synced_message_views

    def items(self):
        """Every Item the store would dispatch a click to."""
        return [item for info in self.views.values() for item in info.values()]

    def held_views(self):
        """Every View object the store can still reach, by either route."""
        found = {id(v): v for v in self.synced.values()}
        for item in self.items():
            view = getattr(item, "_view", None)
            if view is not None:
                found[id(view)] = view
        return list(found.values())


@pytest.fixture
def store(retro):
    return Store(retro)


def collect():
    """Several passes, because a view and its children are a cycle."""
    for _ in range(3):
        gc.collect()


def drop_the_test_doubles(env):
    """Forget what tests/fakes.py recorded, so only the cog is measured.

    FakeMessage keeps the kwargs it was sent with and a list of every edit
    (tests/fakes.py:374 and :381), and both of those contain ``view=...``.
    FakeChannel keeps every message. That is exactly what those doubles are
    for -- other tests assert on them -- but it means a fake channel holds a
    strong reference to every view ever posted in it, which would mask the
    thing under test here.

    Real discord.py does not do this: ConnectionState keeps a *bounded*
    deque of Messages (max_messages, 1000 by default) and a Message holds
    decoded Components, not the View. So clearing these is removing the
    double's own bookkeeping, not papering over a leak.
    """
    for channel in env.bot.channels.values():
        for message in channel.messages.values():
            message.kwargs = {}
            message.edits = []
        channel.messages.clear()
        channel.sent.clear()


# -- 1. Views registered with discord.py ---------------------------------------


async def test_a_retired_view_is_released_by_discord_py(retro, store):
    """
    The leak this file exists for.

    Every game posts a new message and every message's view is registered
    with discord.py, keyed by message id. A channel that plays ten games
    leaves ten messages behind, and before this was fixed all ten views --
    each holding a clip and a stack of save states, and in those days a
    replay buffer of up to 8 MiB besides -- stayed reachable from ViewStore
    for the life of the process, because nothing ever called View.stop().

    Note that ViewStore.add_view *merges* into any dispatch table already
    registered for the same message id (discord/ui/view.py:944), so putting
    a Resume button on a retired message does not displace the old
    controls: their Items, and through ``Item._view`` the whole RetroView,
    stay in the table. That is why this asserts on held_views() rather than
    on _synced_message_views alone.
    """
    await retro.install_cores("gambatte")
    channel = retro.channel(9600)
    dead = []

    for index in range(8):
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"game{index}")
        assert view is not None
        retro.cog._register_view(view)
        dead.append(weakref.ref(view))
        # The next game retires this one, which is the ordinary way a
        # session ends.
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        del view, ctx

    # The store is the container under test, and it must hold no RetroView
    # at all: every one of them has been retired.
    held = store.held_views()
    assert not [v for v in held if isinstance(v, retro.viewmod.RetroView)], held

    drop_the_test_doubles(retro)
    collect()
    alive = [ref for ref in dead if ref() is not None]
    assert not alive, (
        f"{len(alive)} of {len(dead)} retired RetroViews are still reachable. "
        f"Held by the view store: {held}"
    )


async def test_the_view_store_does_not_grow_with_every_game(retro, store):
    await retro.install_cores("gambatte")
    channel = retro.channel(9601)

    sizes = []
    for index in range(10):
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"game{index}")
        retro.cog._register_view(view)
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        sizes.append(len(store.items()))

    # Ten games in one channel leave ten messages, so an unbounded store
    # would be ten games' worth of controls -- twelve dispatch entries each.
    # What it settles at instead is one entry per *remembered* retired
    # message, which is what MAX_RETIRED_PER_CHANNEL bounds.
    cap = retro.cogmod.MAX_RETIRED_PER_CHANNEL
    assert sizes[-1] <= cap, f"the dispatch table kept growing: {sizes}"
    assert sizes[-1] == sizes[cap], f"it had not settled: {sizes}"
    assert len(store.synced) <= cap, dict(store.synced)
    # And not one of them is a live control surface.
    assert all(
        isinstance(view, retro.viewmod.RetiredView) for view in store.held_views()
    ), store.held_views()


async def test_unloading_the_cog_releases_every_view(retro, store):
    """
    A `[p]reload retro` must not cost the process a generation of views.

    cog_load builds fresh views from Config, so anything the old cog left
    registered with discord.py is pure retention.
    """
    await retro.install_cores("gambatte")
    dead = []
    for index in range(6):
        view, _, _ = await retro.posted_game(9610 + index, f"unload{index}")
        retro.cog._register_view(view)
        dead.append(weakref.ref(view))
        del view

    await retro.cog.cog_unload()
    assert not store.held_views(), store.held_views()
    assert retro.cog.sessions == {} and retro.cog.retired == {}

    drop_the_test_doubles(retro)
    collect()
    alive = [ref for ref in dead if ref() is not None]
    assert not alive, f"{len(alive)} of 6 views survived cog_unload"


async def test_a_replaced_resume_button_does_not_stack_up(retro, store):
    """
    _arm_retired replaces the RetiredView on a message it already armed.

    The old one is marked `alive = False` so a stale click is answered
    politely, but it must not also stay registered.
    """
    await retro.install_cores("gambatte")
    record = {"channel_id": 9620, "message_id": 555, "game_name": "armed", "slug": "armed"}
    dead = []
    for _ in range(10):
        armed = retro.cog._arm_retired(dict(record))
        dead.append(weakref.ref(armed))
        del armed

    assert len(store.held_views()) == 1, store.held_views()
    drop_the_test_doubles(retro)
    collect()
    alive = [ref for ref in dead if ref() is not None]
    # The current one is legitimately alive; the nine before it are not.
    assert len(alive) == 1, f"{len(alive)} of 10 Resume buttons are still reachable"


async def test_forgetting_a_retired_record_releases_its_view(retro, store):
    await retro.install_cores("gambatte")
    dead = []
    for index in range(8):
        record = {
            "channel_id": 9630,
            "message_id": 700 + index,
            "game_name": f"forget{index}",
            "slug": f"forget{index}",
        }
        armed = retro.cog._arm_retired(dict(record))
        dead.append(weakref.ref(armed))
        del armed
        await retro.cog._forget_retired(9630, 700 + index)

    assert not store.held_views(), store.held_views()
    drop_the_test_doubles(retro)
    collect()
    alive = [ref for ref in dead if ref() is not None]
    assert not alive, f"{len(alive)} of 8 forgotten Resume buttons are still reachable"


# -- 2. The footage a session holds --------------------------------------------
#
# This section used to be about the replay buffer, which was the one thing in
# a session big enough to matter: up to MAX_REPLAY_BYTES -- 8 MiB -- of
# footage apiece, bounded by a count cap, a byte cap and a seconds cap that
# all had to be enforced as clips arrived. Removing the Replay button removed
# all of it, leaving the single clip that was on the message
# (`RetroView.last_clip`); nothing read that either, so it has gone too and
# the number these tests are about is now zero.
#
# `fakes.footage_bytes` is deliberately attribute-agnostic -- every
# bytes-like thing a view holds, bar the compressed undo history -- because
# the thing being guarded against is a clip coming back under a new name.


async def test_a_session_holds_no_footage_however_long_it_is_played(retro):
    """No container to grow, and no clip either: it is built and let go of.

    The buffer this replaces kept fifteen seconds of footage, which at the
    0.2s clip floor was 76 clips (MAX_REPLAY_CLIPS) and needed a byte cap of
    its own to bound. Sixty presses here would have filled it.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9640, "oneclip")
    view.clip_seconds = retro.clipsmod.MIN_CLIP_SECONDS

    held, clips = [], []
    for _ in range(60):
        interaction = retro.interaction(view, message=view.message)
        await view._press(interaction, "a")
        clips.append(interaction.clip())
        held.append(footage_bytes(view))

    # Sixty clips recorded, all of them different, and the session is holding
    # none of them at any point along the way.
    assert len(set(clips)) == 60, "the fake produced the same clip twice"
    assert all(isinstance(clip, bytes) and clip for clip in clips)
    assert held == [0] * 60, held
    assert not hasattr(view, "clips"), "the replay buffer came back"
    assert not hasattr(view, "last_clip"), "the per-session clip came back"


async def test_a_discarded_session_holds_no_footage_either(retro, store):
    """
    The same statement at the other end of a session's life.

    It used to need saying twice, because the clip was held until
    ``_release_view`` dropped it: even if something else kept the view (a
    stale reference in a traceback, say) the expensive part had to be gone.
    Now there is nothing to drop, so what is checked is that retiring a
    session leaves it holding neither footage nor undo history.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9642, "clipdrop")
    for _ in range(4):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert view.history, "nothing was played, so this proves nothing"

    await retro.cog._retire(view, "replaced")
    assert footage_bytes(view) == 0, "a retired session kept a clip"
    assert not view.history and view.history_bytes == 0


async def test_the_footage_a_bot_holds_does_not_grow_with_the_games_played(retro, store):
    """
    The bytes, counted rather than inferred.

    This plays thirty games across ten channels, presses buttons in each, and
    adds up the payload bytes still reachable from the cog and from discord.py
    at the end. With the views retained it grew with every game, at up to
    MAX_REPLAY_BYTES a session and then at one clip a session; the answer is
    now zero throughout, and it is measured rather than assumed because a
    clip is exactly the kind of thing that comes back by accident.
    """
    await retro.install_cores("gambatte")

    def footage():
        views = list(retro.cog.sessions.values())
        views += list(retro.cog.retired.values())
        views += store.held_views()
        seen, total = set(), 0
        for view in views:
            if id(view) in seen:
                continue
            seen.add(id(view))
            total += footage_bytes(view)
        return total

    held = []
    for index in range(30):
        channel = retro.channel(9660 + (index % 10))
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"footage{index}")
        retro.cog._register_view(view)
        for _ in range(8):
            interaction = retro.interaction(view, message=view.message)
            await view._press(interaction, "a")
            assert interaction.clip(), "no clip was posted, so this proves nothing"
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        held.append(footage())
        del view, ctx

    assert held == [0] * 30, f"footage was retained: {held}"


# -- 3. Module level containers ------------------------------------------------


def test_the_slow_grab_log_cannot_grow_without_bound():
    """
    Two of the fallback reasons interpolate numbers the core chose.

    ``a pitch of %s is too small for %s pixels`` and ``a %s byte framebuffer
    where %s was needed`` are both built from the frame geometry, and a core
    that changes geometry (the SNES really does, mid-game) while on the slow
    path would mint a new string every time. The set is a "log this once"
    marker, so losing the oldest entries costs a duplicate log line and
    nothing else.
    """
    C._SLOW_GRAB_LOGGED.clear()
    try:
        for index in range(10_000):
            C._note_slow_frame_grab(f"a pitch of {index} is too small for {index} pixels")
        assert len(C._SLOW_GRAB_LOGGED) <= C.MAX_SLOW_GRAB_REASONS, len(C._SLOW_GRAB_LOGGED)
        assert C.MAX_SLOW_GRAB_REASONS < 1000, "a cap nobody can reach is not a cap"
    finally:
        C._SLOW_GRAB_LOGGED.clear()


def test_the_slow_grab_log_still_logs_each_reason_once(caplog):
    """The cap must not break what the set is for."""
    import logging

    C._SLOW_GRAB_LOGGED.clear()
    try:
        with caplog.at_level(logging.DEBUG, logger="red.robloach.retro.emulator"):
            for _ in range(20):
                C._note_slow_frame_grab("rotation NINETY")
        lines = [r for r in caplog.records if "framebuffer directly" in r.message]
        assert len(lines) == 1, [r.message for r in lines]
    finally:
        C._SLOW_GRAB_LOGGED.clear()


def test_no_module_level_container_is_keyed_by_user_input():
    """
    An inventory, so a new cache has to be thought about.

    Every mutable module-level container in the cog is listed here with why
    it is bounded. A new one shows up as a failure rather than as a slow
    leak in production.
    """
    import sys

    known = {
        # name -> why it cannot grow without bound
        ("retro.clips", "FAST_RAW_MODES"): "three literal pixel formats",
        ("retro.clips", "FAST_ROTATIONS"): "three literal rotations",
        ("retro.clips", "FAST_POINT_TABLES"): "three literal pixel formats",
        ("retro.clips", "_SLOW_GRAB_LOGGED"): "capped at MAX_SLOW_GRAB_REASONS",
        ("retro.systems", "EMOJI_CODEPOINTS"): "one entry per button emoji",
        ("retro.systems", "CORES"): "built once from SYSTEMS",
        ("retro.systems", "_BY_EXTENSION"): "built once from SYSTEMS",
        ("retro.RetroView", "_STYLES"): "two literal Discord styles",
        ("retro.RetroView", "ACTION_NOTES"): "four literal action lines",
        ("retro.RetroView", "MARKDOWN_ESCAPES"): "one str.translate table",
        ("retro.saves", "EXPORT_CHOICES"): "literal",
        ("retro.Retro", "DEFAULT_GLOBALS"): "the Config schema",
        ("retro.Retro", "DEFAULT_CHANNEL"): "the Config schema",
    }
    modules = [
        "retro.Retro", "retro.RetroView", "retro.emulator", "retro.clips",
        "retro.systems", "retro.archives", "retro.net", "retro.storage",
        "retro.cores", "retro.saves", "retro.migration", "retro.abc",
    ]
    found = {}
    for name in modules:
        module = sys.modules.get(name)
        if module is None:
            continue
        for attribute, value in vars(module).items():
            if attribute.startswith("__"):
                continue
            if isinstance(value, (dict, set, list)) and not isinstance(value, type):
                # A container defined somewhere else and merely imported here
                # is that module's business, not this one's.
                if getattr(value, "__module__", None) not in (None, name):
                    continue
                found[(name, attribute)] = value

    unexplained = {
        key: type(value).__name__
        for key, value in found.items()
        if key not in known and not isinstance(value, (frozenset,))
    }
    # Imports of a known container into another module are the same object.
    by_id = {id(value): key for key, value in found.items() if key in known}
    unexplained = {
        key: kind
        for key, kind in unexplained.items()
        if id(found[key]) not in by_id
    }
    assert not unexplained, (
        "new module level container(s) with no bound recorded in this test: "
        f"{unexplained}"
    )

    # functools caches: an unbounded one keyed by anything a user supplies
    # is a leak by construction, so only a bounded one is allowed. The cog
    # defines none of its own; urllib.parse.urlsplit, imported by
    # retro/net.py, is the standard library's own and is capped.
    for name in modules:
        module = sys.modules.get(name)
        if module is None:
            continue
        for attribute, value in vars(module).items():
            info = getattr(value, "cache_info", None)
            if info is None:
                continue
            assert info().maxsize is not None, (
                f"{name}.{attribute} is an unbounded functools cache; give it "
                "a maxsize or record its bound in this test"
            )


# -- 4. Cooldown buckets -------------------------------------------------------


async def test_the_channel_start_cooldown_evicts_expired_buckets(retro):
    """
    discord.py's own behaviour, asserted because the cog relies on it.

    CooldownMapping._cache would otherwise hold an entry per channel for
    ever. discord.py sweeps it at the top of every get_bucket
    (discord/ext/commands/cooldowns.py:135), so it can only ever hold the
    keys seen inside one window.
    """
    import time

    buckets = retro.cog.start_buckets
    buckets._cache.clear()
    for index in range(500):
        ctx = retro.context(retro.channel(9700 + index))
        buckets.update_rate_limit(ctx)
    assert len(buckets._cache) == 500, "the buckets were not created at all"

    # One window later, the very next access must sweep the lot.
    window = retro.cogmod.CHANNEL_START_COOLDOWN_SECONDS
    buckets.get_bucket(
        retro.context(retro.channel(9699)), current=time.time() + window + 1
    )
    assert len(buckets._cache) <= 1, len(buckets._cache)


# -- 5. Emulators on failure paths --------------------------------------------


async def test_a_cancelled_start_still_frees_the_core(retro, monkeypatch):
    """
    `[p]unload retro` while a game is booting used to leak the core.

    A libretro core is a dlopen'd shared object with process-global state and
    MAX_LIVE_EMULATORS is 1, so one leaked core stops the cog working at all
    rather than merely costing memory. The start path caught ``Exception``,
    which is not what cancelling a task raises.

    The cancellation is raised *after* the core is up, which is the case that
    matters and the only case that can prove anything: a core that never
    loaded needs no freeing.
    """
    await retro.install_cores("gambatte")
    fake = retro.fakes["RetroEmulator"]
    fake.reset_all()

    async def boot_then_cancel(self, ctx, emulator, *args, **kwargs):
        emulator.start()
        assert emulator.started
        raise asyncio.CancelledError

    channel = retro.channel(9800)
    ctx = retro.context(channel)
    monkeypatch.setattr(retro.viewmod.RetroView, "start", boot_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await retro.start_game(ctx, "cancelme")

    assert fake.instances, "no emulator was built, so nothing was proved"
    assert not any(e.started for e in fake.instances), (
        "a cancelled start left a libretro core loaded"
    )
    assert retro.cog.sessions.get(channel.id) is None
    # And the cancellation was not paid for out of the player's progress:
    # _write_state_now banks the save state on the way out, without awaiting
    # anything (a cancelled task cannot).
    slug = retro.cog._slug("cancelme")
    assert retro.cog._state_path(channel.id, slug).is_file(), (
        "a cancelled start threw the emulated progress away"
    )


async def test_a_cancelled_wake_still_frees_the_core(retro, monkeypatch):
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9801, "wakecancel")
    await retro.cog.hibernate(view, None)
    fake = retro.fakes["RetroEmulator"]
    fake.reset_all()

    def boot_then_cancel(emulator, progress, slug):
        emulator.start()
        assert emulator.started
        raise asyncio.CancelledError

    monkeypatch.setattr(retro.cogmod, "restore_into", boot_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        async with retro.cog.emulator_lock:
            await retro.cog._wake_locked(view)

    assert fake.instances, "no emulator was built, so nothing was proved"
    assert not any(e.started for e in fake.instances), (
        "a cancelled wake left a libretro core loaded"
    )
    assert view.emulator is None


async def test_a_cancelled_resume_still_frees_the_core(retro, monkeypatch):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9802, "resumecancel")
    await retro.cog._retire(view, "replaced")
    retro.cog.sessions.pop(channel.id, None)
    retired = retro.cog.retired.get(view.message_id)
    assert retired is not None

    fake = retro.fakes["RetroEmulator"]
    fake.reset_all()

    def boot_then_cancel(self, emulator, progress=None):
        emulator.start()
        assert emulator.started
        raise asyncio.CancelledError

    monkeypatch.setattr(retro.viewmod.RetroView, "_boot", boot_then_cancel)
    interaction = retro.interaction(retired, message=view.message)
    with pytest.raises(asyncio.CancelledError):
        await retro.cog.resume_retired(retired, interaction)

    assert fake.instances, "no emulator was built, so nothing was proved"
    assert not any(e.started for e in fake.instances), (
        "a cancelled Resume left a libretro core loaded"
    )


@pytest.mark.parametrize("when", ["load", "run"])
async def test_a_rom_the_core_will_not_digest_leaves_nothing_behind(retro, when):
    """A player's broken ROM must not cost the bot its one emulator slot.

    The fast half of tests/test_malformed_roms.py, which does the same thing
    with real cores and genuinely corrupted cartridges: here the fake is
    simply made to fail the way a core does, at the two points it can --
    refusing the content (``start``) and falling over while the first clip is
    being recorded (``record``). Both land in ``_start_session``'s
    EmulatorError branch and ``_abandon_session``.
    """
    await retro.install_cores("gambatte")
    fake = retro.fakes["RetroEmulator"]
    fake.reset_all()

    def refuse_the_rom(self, *args, **kwargs):
        raise retro.emumod.EmulatorError("The core could not load this ROM.")

    def die_mid_run(self, *args, **kwargs):
        # The case with something to clean up: the core is loaded by now.
        assert self.started, "the core was not loaded, so this is the wrong case"
        raise retro.emumod.EmulatorError("The core crashed while running: boom")

    method, replacement = {
        "load": ("start", refuse_the_rom),
        "run": ("record", die_mid_run),
    }[when]
    original = getattr(fake, method)
    setattr(fake, method, replacement)
    try:
        channel = retro.channel(9805)
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name="unplayable")
    finally:
        setattr(fake, method, original)

    assert view is None, "a session survived a ROM that could not be started"
    assert channel.id not in retro.cog.sessions
    assert not await retro.cog.config.channel_from_id(channel.id).session()
    assert "could not be started" in ctx.said(), ctx.said()
    assert fake.instances, "no emulator was built, so nothing is proved"
    assert not any(e.started for e in fake.instances), (
        "a failed start left a libretro core loaded, and MAX_LIVE_EMULATORS is 1"
    )

    # And the next game plays, which is the consequence worth proving.
    fine = await retro.start_game(retro.context(retro.channel(9806)), name="fine")
    assert fine is not None and fine.live


async def test_many_starts_and_stops_leave_no_emulator_running(retro):
    """The ordinary path, repeated, as a backstop for all of the above."""
    await retro.install_cores("gambatte")
    fake = retro.fakes["RetroEmulator"]
    fake.reset_all()
    for index in range(12):
        view, _, channel = await retro.posted_game(9810 + index, f"churn{index}")
        await retro.cog.hibernate(view, None)
        retro.cog.sessions.pop(channel.id, None)
    running = [e for e in fake.instances if e.started]
    assert not running, f"{len(running)} of {len(fake.instances)} cores are still loaded"


# -- 6. Frames and images ------------------------------------------------------


def test_the_fast_frame_grab_does_not_alias_the_framebuffer():
    """
    The returned image must own its pixels.

    retro/clips.py builds it from a memoryview onto the driver's array and
    releases the view on the way out, swallowing BufferError if something is
    still holding it. If Pillow ever kept that view, an image handed back to
    the recorder would pin the driver's buffer -- and the swallowed
    BufferError would be the only sign.
    """
    pytest.importorskip("libretro")
    pytest.importorskip("PIL")
    from array import array

    from libretro import ArrayVideoDriver, PixelFormat, Rotation
    from PIL import Image

    width, height = 17, 11
    for name in sorted(C.FAST_RAW_MODES):
        pixel_format = getattr(PixelFormat, name)
        pitch = width * pixel_format.bytes_per_pixel
        payload = bytes(range(256)) * ((pitch * height) // 256 + 1)
        payload = payload[: pitch * height]

        driver = ArrayVideoDriver()
        driver._pixel_format = pixel_format
        driver._rotation = Rotation.NONE
        driver._frame = array("B", payload)
        driver.refresh(memoryview(payload), width, height, pitch)

        image = C.fast_frame_image(driver, Image)
        assert image is not None, name
        before = image.tobytes()
        # Scribble over the driver's framebuffer. An image that aliased it
        # would change; one that owns its pixels cannot.
        for index in range(len(driver._frame)):
            driver._frame[index] = 0
        assert image.tobytes() == before, f"{name}: the image aliases the framebuffer"


async def test_a_session_keeps_no_image_or_memoryview_after_a_clip(retro):
    """A clip is uploaded as bytes; nothing decoded survives the press.

    The picture goes out as an attachment and the Pillow images and the
    framebuffer views it was built from are not kept anywhere -- neither on
    the session nor on the emulator.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9820, "noimages")
    interaction = retro.interaction(view, message=view.message)
    await view._press(interaction, "a")
    assert isinstance(interaction.clip(), bytes), "no clip was posted at all"

    held = {}
    for owner in (view, view.emulator):
        if owner is None:
            continue
        for name, value in vars(owner).items():
            if isinstance(value, memoryview):
                held[f"{type(owner).__name__}.{name}"] = "memoryview"
            elif type(value).__module__.startswith("PIL"):
                held[f"{type(owner).__name__}.{name}"] = type(value).__name__
    assert not held, held


# -- 7. The core option definitions cache --------------------------------------


async def test_the_option_cache_is_keyed_by_core_and_not_by_game(retro):
    """
    It is persisted, so a per-game key would grow the on-disk config for
    ever. One entry per installed core is the whole budget.
    """
    await retro.install_cores()
    for index in range(20):
        view, _, channel = await retro.posted_game(9840 + index, f"optgame{index}")
        await retro.cog._learn_options(view.core, view.emulator)
        await retro.cog.hibernate(view, None)
        retro.cog.sessions.pop(channel.id, None)

    definitions = await retro.cog.config.core_option_definitions()
    options = await retro.cog.config.core_options()
    assert set(definitions) <= set(retro.sysmod.CORES), set(definitions)
    assert set(options) <= set(retro.sysmod.CORES), set(options)
    assert len(definitions) <= len(retro.sysmod.CORES)


# -- 8. The in-memory session dictionaries -------------------------------------


async def test_the_retired_dictionary_stays_capped_per_channel(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9870)
    for index in range(4 * retro.cogmod.MAX_RETIRED_PER_CHANNEL):
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"retiree{index}")
        await retro.cog._retire(view, "replaced")
        await retro.cog._remember_retired(view.to_record())
        retro.cog.sessions.pop(channel.id, None)

    assert len(retro.cog.retired) <= retro.cogmod.MAX_RETIRED_PER_CHANNEL, len(
        retro.cog.retired
    )
    stored = await retro.cog.config.channel_from_id(channel.id).retired()
    assert len(stored) <= retro.cogmod.MAX_RETIRED_PER_CHANNEL


async def test_one_channel_keeps_one_session_however_many_games_it_plays(retro):
    await retro.install_cores("gambatte")
    channel = retro.channel(9880)
    for index in range(15):
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"onechannel{index}")
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        del view
    assert len(retro.cog.sessions) <= 1, len(retro.cog.sessions)


async def test_restoring_reads_at_most_the_capped_number_per_channel(retro):
    """
    _restore_sessions arms a Resume button per stored record, so the in-memory
    dictionary inherits the Config cap rather than having one of its own.
    """
    await retro.install_cores("gambatte")
    channel_id = 9890
    async with retro.cog.config.channel_from_id(channel_id).retired() as stored:
        for index in range(50):
            stored[str(2000 + index)] = {
                "channel_id": channel_id,
                "message_id": 2000 + index,
                "game_name": f"stored{index}",
                "slug": f"stored{index}",
                "last_active": float(index),
            }

    cog2, bot2 = retro.make_cog()
    await cog2._restore_sessions()
    assert len(cog2.retired) <= retro.cogmod.MAX_RETIRED_PER_CHANNEL, len(cog2.retired)
    await cog2.cog_unload()


async def test_discord_py_still_has_no_public_way_to_unregister_a_view():
    """
    Why the fix is View.stop() rather than bot.remove_view().

    If discord.py ever grows a real remove_view on the Client, this fails and
    the cog can use the supported call instead of relying on stop()'s cancel
    callback.

    Async on purpose: discord.py only gives a View its ``__stopped`` future
    when it is built inside a running loop (discord/ui/view.py:243-248), and
    without that future ``is_finished()`` is always False. The cog always
    builds its views on the loop, so this matches.
    """
    assert hasattr(discord.Client, "add_view")
    assert not hasattr(discord.Client, "remove_view"), (
        "discord.py has a public remove_view now; prefer it to View.stop()"
    )
    assert hasattr(ViewStore, "remove_view"), "stop() has nothing to call any more"
    view = discord.ui.View(timeout=None)
    assert not view.is_finished()
    view.stop()
    assert view.is_finished(), "stop() no longer finishes a view"

    # And that stop() is really what reaches remove_view, which is the whole
    # mechanism _release_view depends on.
    store = ViewStore.__new__(ViewStore)
    store._views, store._synced_message_views = {}, {}
    store._modals, store._dynamic_items = {}, {}
    store._state = None
    removed = []
    store.remove_view = removed.append
    registered = discord.ui.View(timeout=None)
    registered.add_item(discord.ui.Button(custom_id="leaktest", label="x"))
    store.add_view(registered, 12345)
    assert store._views and store._synced_message_views
    registered.stop()
    assert removed == [registered], "stop() no longer calls ViewStore.remove_view"


# -- 9. Session records in Config ----------------------------------------------
#
# The per-channel `session` record and the `retired` records behind a
# channel's Resume buttons used to be written and never deleted: there was no
# `session.clear()` anywhere in the cog and no channel or guild listeners, so
# `_restore_sessions` rebuilt a RetroView for every channel that had *ever*
# played, on every load, including channels that no longer existed and games
# whose cached ROM had been pruned months earlier. Bounded per channel
# (one session, MAX_RETIRED_PER_CHANNEL buttons) and unbounded in channels.
#
# Four things drop a record now, and every one of them keeps the saves:
#
#   * the cached ROM it pointed at was pruned, by either pruner;
#   * the channel (or thread) was deleted;
#   * the bot left the guild;
#   * the bot can no longer see the channel, noticed by the sweep that runs
#     once the bot is ready.
#
# The *semantics* are what make that safe, and they are asserted as hard as
# the counting is: a record is only a "resume from this message" pointer,
# while the `.state` and `.srm` (and the previous generation of each) are
# keyed by channel and game. Dropping a record costs the button and nothing
# else -- `[p]retro <name>` re-fetches the ROM and picks the progress back
# up.


def saves_of(retro, channel_id, slug):
    """Which of a game's four save files are on disk, by name."""
    return [p.name for p in retro.cog._save_paths(channel_id, slug) if p.is_file()]


async def test_a_rom_pruned_by_the_disk_budget_takes_its_resume_button_with_it(retro):
    """The pointer goes with the file; the progress does not.

    This is the disk budget's prune, which deliberately deletes cached ROMs
    and never a save. So the Resume button -- which can only apologise once
    its ROM is gone -- is dropped, and all four save files stay exactly
    where they are.
    """
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9900, "budgeted")
    slug = view.slug
    await view._press(retro.interaction(view, message=view.message), "a")
    await retro.cog._write_state(view)
    retro.cog._sram_path(channel.id, slug).write_bytes(b"battery")
    # Rotate both, so the previous generation exists too.
    await retro.cog._write_state(view)
    retro.cog._write_atomic(retro.cog._sram_path(channel.id, slug), b"battery2", True)
    before = saves_of(retro, channel.id, slug)
    assert len(before) == 4, before

    # Retire it, so its ROM is no longer protected by a live session, and
    # then squeeze the budget until the ROM has to go.
    await retro.cog._retire(view, "replaced")
    retro.cog.sessions.pop(channel.id, None)
    assert await retro.cog.config.channel_from_id(channel.id).retired()
    rom = retro.cog._rom_path(view.rom_filename)
    assert rom.is_file()

    # A 1 MiB budget and a download that only fits once the 128 KiB ROM
    # above has gone, so the prune really runs and is not merely refused.
    await retro.cog.config.disk_budget_mb.set(1)
    room, note = await retro.cog._make_room(950 * 1024)

    assert not rom.is_file(), "the ROM was not pruned, so nothing is proved"
    assert not await retro.cog.config.channel_from_id(channel.id).retired()
    assert not retro.cog.retired, "the Resume button was left armed"
    # And the player's progress is untouched, which is the whole point.
    assert saves_of(retro, channel.id, slug) == before
    assert room and "Nothing anyone had saved was touched" in note, note


async def test_starting_a_forgotten_game_again_still_picks_up_its_save(retro):
    """Why dropping the record is allowed to be automatic.

    A save state and a battery save are keyed by channel *and* game, never
    by message, so the pointer is the only thing a forgotten record costs:
    `[p]retro <name>` re-fetches the ROM and restores exactly as it always
    did.
    """
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9901, "comeback")
    for _ in range(3):
        await view._press(retro.interaction(view, message=view.message), "a")
    assert retro.cog._state_path(channel.id, view.slug).is_file()
    played = view.emulator.frame

    # Forget everything the channel could be resumed *from*, and throw the
    # cached ROM away as a prune would.
    await retro.cog._forget_channel(channel.id, "a test said so")
    retro.cog._rom_path(view.rom_filename).unlink()
    assert not await retro.cog.config.all_channels()
    assert not retro.cog.sessions and not retro.cog.retired

    again = await retro.start_game(retro.context(channel), name="comeback")
    assert again is not None and again.live
    assert again.boot_outcome == "state", "the save state was not picked up"
    assert again.emulator.loaded_from == played


async def test_a_deleted_channel_is_forgotten_and_keeps_its_saves(retro):
    await retro.install_cores("gambatte")
    view, ctx, channel = await retro.posted_game(9902, "deletedchan")
    await view._press(retro.interaction(view, message=view.message), "a")
    await retro.cog._write_state(view)
    retro.cog._sram_path(channel.id, view.slug).write_bytes(b"battery")
    saved = saves_of(retro, channel.id, view.slug)
    assert len(saved) == 2, saved

    await retro.cog.on_guild_channel_delete(channel)

    assert channel.id not in retro.cog.sessions
    assert not await retro.cog.config.all_channels(), "the Config row survived"
    assert view.closed and view.is_finished(), "the view was not released"
    # Kept, deliberately: they are small, the disk budget prunes ROMs rather
    # than saves, and an archived thread is indistinguishable from a deleted
    # channel here. See the note above Retro._channel_is_gone.
    assert saves_of(retro, channel.id, view.slug) == saved


async def test_deleting_a_channel_that_never_played_writes_nothing(retro):
    """The common case, and it must cost nothing.

    ``on_guild_channel_delete`` fires for every channel and category in
    every server the bot is in, and almost none of them have ever played a
    game. A listener that wrote to Config (and logged) for each of them
    would be worse than the leak it replaced.
    """
    await retro.install_cores("gambatte")
    played, _, busy = await retro.posted_game(9904, "played")
    quiet = retro.channel(9905)

    await retro.cog.on_guild_channel_delete(quiet)

    # (`FakeConfig.channel_from_id` creates its dict on read, which Red's does
    # not, so the statement is about the *rows* -- what `all_channels()`
    # answers and therefore what `_restore_sessions` would walk on load.)
    assert quiet.id not in await retro.cog.config.all_channels()
    # ...and the channel that *is* playing was not touched by it.
    assert retro.cog.sessions.get(busy.id) is played
    assert await retro.cog.config.channel_from_id(busy.id).session()


async def test_a_deleted_thread_is_forgotten_the_same_way(retro):
    """A thread is a channel a game can be played in, with its own event."""
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9903, "deletedthread")
    await retro.cog.on_thread_delete(channel)
    assert channel.id not in retro.cog.sessions
    assert not await retro.cog.config.all_channels()


async def test_leaving_a_guild_forgets_every_channel_it_had(retro):
    await retro.install_cores("gambatte")
    guild = types.SimpleNamespace(id=777)
    mine, theirs = [], []
    for index in range(4):
        view, _, channel = await retro.posted_game(9910 + index, f"ours{index}")
        mine.append((view, channel))
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
    # A channel in another guild, which must be left completely alone.
    other = retro.channel(9950)
    elsewhere = await retro.start_game(
        retro.context(other, guild_id=888), name="elsewhere"
    )
    theirs.append((elsewhere, other))

    await retro.cog.on_guild_remove(guild)

    rows = await retro.cog.config.all_channels()
    assert set(rows) == {other.id}, rows
    assert set(retro.cog.sessions) == {other.id}
    assert not retro.cog.retired, "the departed guild's Resume buttons are still armed"
    assert elsewhere is retro.cog.sessions[other.id] and not elsewhere.closed
    for view, channel in mine:
        assert view.closed and view.is_finished(), "a view was left live"
        # The saves of a game the departed guild was playing are kept, exactly
        # as they are for a deleted channel.
        assert retro.cog._state_path(channel.id, view.slug).is_file(), view.slug


async def test_a_record_whose_channel_is_gone_is_not_rebuilt_on_load(retro):
    """Rather than arming a persistent view against a message nobody can see.

    This is the case that made the records grow for ever: every load built a
    RetroView for every channel that had ever played, deleted or not.
    """
    await retro.install_cores("gambatte")
    alive, _, alive_channel = await retro.posted_game(9920, "stillhere")
    gone, _, gone_channel = await retro.posted_game(9921, "longgone")
    await retro.cog._retire(gone, "replaced")
    retro.cog.sessions.pop(gone_channel.id, None)

    cog2, bot2 = retro.make_cog()
    cog2.config.channels.update(
        {k: dict(v) for k, v in retro.cog.config.channels.items()}
    )
    # The restarted bot can see one of the two channels.
    bot2.channels[alive_channel.id] = alive_channel

    await cog2._restore_sessions()

    assert set(cog2.sessions) == {alive_channel.id}
    assert not [v for v in cog2.retired.values() if v.channel_id == gone_channel.id]
    assert set(await cog2.config.all_channels()) == {alive_channel.id}
    assert not bot2.added_views or all(
        getattr(view, "channel_id", None) == alive_channel.id
        for view, _ in bot2.added_views
    )
    await cog2.cog_unload()


async def test_nothing_is_forgotten_while_the_bot_is_still_connecting(retro):
    """The hazard in the whole idea, pinned.

    Red loads its cogs *before* the bot connects, so during a startup load
    ``bot.get_channel`` answers None for every channel that exists. Acting
    on that would delete every record on every restart, so
    ``_channel_is_gone`` answers False until the bot says it is ready.
    """
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9930, "connecting")
    await retro.cog.hibernate(view, None)

    cog2, bot2 = retro.make_cog()
    cog2.config.channels.update(
        {k: dict(v) for k, v in retro.cog.config.channels.items()}
    )
    bot2.ready = False           # still logging in; the cache is empty
    assert bot2.get_channel(channel.id) is None

    await cog2._restore_sessions()
    assert set(cog2.sessions) == {channel.id}, "a record was dropped too early"
    assert set(await cog2.config.all_channels()) == {channel.id}

    # Once it is connected and the channel really is not there, the sweep
    # that runs after wait_until_red_ready() drops it.
    bot2.ready = True
    await cog2._forget_unreachable_sessions()
    assert not cog2.sessions
    assert not await cog2.config.all_channels()
    await cog2.cog_unload()


async def test_a_listener_that_hits_a_broken_config_cannot_break_the_cog(retro):
    """discord.py does not await a listener, so a raise is an orphan error."""
    await retro.install_cores("gambatte")
    view, _, channel = await retro.posted_game(9940, "brokenconfig")

    def explode(channel_id):
        raise RuntimeError("Config is unavailable")

    retro.cog.config.channel_from_id = explode

    # None of the three may raise, and the in-memory half must still happen.
    await retro.cog.on_guild_channel_delete(channel)
    await retro.cog.on_thread_delete(channel)
    await retro.cog.on_guild_remove(types.SimpleNamespace(id=777))
    assert channel.id not in retro.cog.sessions
    assert view.closed


async def test_the_records_do_not_grow_with_the_channels_a_bot_has_seen(retro):
    """The whole point, counted: N channels in, nothing left behind.

    Twelve channels each play two games -- so each one ends with a session
    record and a Resume button -- and then go away the two ways a channel
    does: deleted while the bot is up (the listener) or simply invisible by
    the time it next loads (the sweep). Both the Config rows and the two
    in-memory dictionaries come back to zero, and not one save is deleted.
    """
    await retro.install_cores("gambatte")
    channels, slugs = [], []
    for index in range(12):
        channel = retro.channel(9960 + index)
        ctx = retro.context(channel)
        first = await retro.start_game(ctx, name=f"first{index}")
        await retro.cog._retire(first, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        second = await retro.start_game(retro.context(channel), name=f"second{index}")
        await retro.cog._write_state(second)
        channels.append(channel)
        slugs.append((channel.id, first.slug, second.slug))

    rows = await retro.cog.config.all_channels()
    assert len(rows) == 12
    assert all(row["session"] and row["retired"] for row in rows.values())
    assert len(retro.cog.sessions) == 12 and len(retro.cog.retired) == 12
    grew = sum(len(row["retired"]) + 1 for row in rows.values())
    assert grew == 24, grew

    # Half are deleted while the bot is watching.
    for channel in channels[:6]:
        await retro.cog.on_guild_channel_delete(channel)
    assert len(await retro.cog.config.all_channels()) == 6
    assert len(retro.cog.sessions) == 6 and len(retro.cog.retired) == 6

    # The other half quietly stop existing, and the sweep notices.
    for channel in channels[6:]:
        retro.bot.channels.pop(channel.id, None)
    await retro.cog._forget_unreachable_sessions()

    assert not await retro.cog.config.all_channels()
    assert not retro.cog.sessions and not retro.cog.retired
    # Twelve channels came and went and nobody's progress did.
    for channel_id, _first_slug, second_slug in slugs:
        assert retro.cog._state_path(channel_id, second_slug).is_file(), second_slug
