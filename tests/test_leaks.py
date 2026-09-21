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
* adding up the bytes every reachable session is holding, for the replay
  buffers, which are where the megabytes actually are.

Nothing here needs a real core or the network. The one thing it does need is
the *real* discord.py view store, because the leak that motivated this file
lives inside it: see test_a_retired_view_is_released_by_discord_py below.
"""

import asyncio
import gc
import weakref

import pytest

from .loader import load_standalone

pytest.importorskip("discord", reason="the leak tests need discord.py")

import discord  # noqa: E402
from discord.ui.view import ViewStore  # noqa: E402

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
    each holding its replay buffer of up to MAX_REPLAY_BYTES -- stayed
    reachable from ViewStore for the life of the process, because nothing
    ever called View.stop().

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


# -- 2. The replay buffer ------------------------------------------------------


async def test_the_replay_byte_cap_is_enforced_as_clips_arrive(retro):
    """
    Not only at stitch time: the buffer itself has to stay under the cap.

    MAX_REPLAY_CLIPS can be 76 (REPLAY_SECONDS / MIN_CLIP_SECONDS), so a
    count cap alone would allow 76 clips of whatever size the encoder
    produced.
    """
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9640, "replaycap")
    view.clip_seconds = retro.clipsmod.MIN_CLIP_SECONDS

    chunk = b"x" * (512 * 1024)
    sizes = []
    for _ in range(60):
        view.remember_clip(chunk)
        sizes.append(sum(len(data) for data, _ in view.clips))

    assert max(sizes) <= retro.clipsmod.MAX_REPLAY_BYTES, max(sizes)
    assert len(view.clips) <= retro.clipsmod.MAX_REPLAY_CLIPS
    # 60 x 512 KiB is 30 MiB offered; the cap must have thrown most of it out.
    assert sizes[-1] < 30 * 1024 * 1024 // 2


async def test_one_clip_bigger_than_the_whole_cap_is_still_kept(retro):
    """The documented exception, pinned so it cannot become accidental."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9641, "oneclip")
    huge = b"y" * (retro.clipsmod.MAX_REPLAY_BYTES + 1024)
    view.remember_clip(huge)
    assert len(view.clips) == 1, "a replay is never emptied to nothing"
    view.remember_clip(b"z" * 1024)
    assert sum(len(d) for d, _ in view.clips) <= len(huge), "the huge clip was dropped"


async def test_a_discarded_session_does_not_keep_its_clips(retro, store):
    """
    The 8 MiB is what makes a retained view expensive.

    Freeing it where the session is discarded means that even if something
    else does hold the view (a stale reference in a traceback, say) the
    footage is not what is held.
    """
    await retro.install_cores("gambatte")
    view, ctx, _ = await retro.posted_game(9642, "clipdrop")
    for _ in range(4):
        view.remember_clip(b"q" * (256 * 1024))
    assert view.clips

    await retro.cog._retire(view, "replaced")
    assert not view.clips, "a retired session kept its replay footage"


async def test_the_footage_a_bot_holds_does_not_grow_with_the_games_played(retro, store):
    """
    The megabytes, counted rather than inferred.

    A replay buffer is the one thing in a session big enough to matter: up
    to MAX_REPLAY_BYTES apiece. This plays thirty games across ten channels,
    fills each one's buffer, and adds up the footage still reachable from
    the cog and from discord.py at the end. With the views retained it grew
    with every game; now it is bounded by the channels that still have a
    live session.
    """
    await retro.install_cores("gambatte")
    chunk = b"f" * (256 * 1024)

    def footage():
        views = list(retro.cog.sessions.values())
        views += list(retro.cog.retired.values())
        views += store.held_views()
        seen, total = set(), 0
        for view in views:
            if id(view) in seen:
                continue
            seen.add(id(view))
            for data, _ in getattr(view, "clips", ()) or ():
                total += len(data)
        return total

    held = []
    for index in range(30):
        channel = retro.channel(9660 + (index % 10))
        ctx = retro.context(channel)
        view = await retro.start_game(ctx, name=f"footage{index}")
        retro.cog._register_view(view)
        for _ in range(8):
            view.remember_clip(chunk)
        assert view.clips
        await retro.cog._retire(view, "replaced")
        retro.cog.sessions.pop(channel.id, None)
        held.append(footage())
        del view, ctx

    assert held[-1] == 0, f"footage retained after every game was retired: {held}"
    assert max(held) <= retro.clipsmod.MAX_REPLAY_BYTES, (
        f"more than one session's worth of footage was held at once: {max(held)}"
    )


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
    fake.reset()

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
    fake.reset()

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
    fake.reset()

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


async def test_many_starts_and_stops_leave_no_emulator_running(retro):
    """The ordinary path, repeated, as a backstop for all of the above."""
    await retro.install_cores("gambatte")
    fake = retro.fakes["RetroEmulator"]
    fake.reset()
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
    """Clips are held as bytes; nothing decoded survives the press."""
    await retro.install_cores("gambatte")
    view, _, _ = await retro.posted_game(9820, "noimages")
    await view._press(retro.interaction(view, message=view.message), "a")

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
    assert view.clips and all(isinstance(data, bytes) for data, _ in view.clips)


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
