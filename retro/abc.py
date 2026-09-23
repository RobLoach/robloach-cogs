"""
The shapes that let the cog be assembled from mixins.

`Retro` is one cog class made of several: the storage layer, the cores and
their options, the `[p]retrosaves` group and the one-off namespace migration
each live in their own module and are mixed in (see retro/Retro.py). Two
pieces of scaffolding make that legal:

* :class:`MixinMeta` is what a mixin may assume about the cog it ends up on.
  It is an ABC purely so the annotations have somewhere to live -- nothing
  here is abstract, and nothing here is implemented either. It is the whole
  contract: a mixin that reaches for something not declared below is a
  mixin whose requirement nobody wrote down, so anything a mixin starts
  using has to be added here too. A test holds it to that
  (tests/test_mixins.py).
* :class:`CompositeMetaClass` is the metaclass the cog itself is declared
  with. ``commands.Cog`` has ``CogMeta`` and an ABC has ``ABCMeta``, so a
  class that is both needs a metaclass deriving from both. It adds nothing.

Red's own cogs are built this way, and it is the reason the cog class can
stay a single `Retro` -- which it must, because Red derives both the Config
namespace and the data directory from the class name.
"""

import asyncio
import typing
from abc import ABC, ABCMeta

from redbot.core import Config, commands
from redbot.core.bot import Red


class MixinMeta(ABC):
    """What every mixin can rely on the assembled cog providing."""

    bot: Red
    config: Config
    #: channel id -> the RetroView driving that channel's game.
    sessions: typing.Dict[int, typing.Any]
    #: message id -> the lone Resume button left on a retired message.
    retired: typing.Dict[int, typing.Any]
    #: Serializes every core operation across all channels.
    emulator_lock: asyncio.Lock
    #: The per-channel half of the start rate limit.
    start_buckets: typing.Any
    #: The detached task that fetches missing cores shortly after the cog
    #: loads, or None. Read by CoresMixin._cores_downloading, which is how
    #: "no cores are installed" stops being said while the download the
    #: install message promised is still in flight.
    _download_task: typing.Optional[asyncio.Task]
    #: Drops the session and Resume-button records that pointed at a cached
    #: ROM which has just been deleted, keeping the saves. Called by the disk
    #: budget after it prunes (retro/storage.py).
    _forget_pruned_roms: typing.Callable[..., typing.Awaitable[None]]

    # -- ...and the rest of what the cog itself provides.
    #
    # Everything below is implemented on `Retro` (retro/Retro.py) and called
    # from a mixin. They are declared here for the same reason the attributes
    # above are: a requirement that is only visible as a `self.` somewhere in
    # retro/saves.py is a requirement that can be broken by editing
    # retro/Retro.py alone.

    #: Save a session's progress, free its core, and leave the controls
    #: usable. Taken by `[p]retrosaves` before it touches a save on disk
    #: (retro/saves.py), because a live core holds the authoritative copy.
    hibernate: typing.Callable[..., typing.Awaitable[None]]
    #: The same, for a session whose ordinary hibernate raised: it must not
    #: leave a core loaded. Also retro/saves.py.
    _force_hibernate: typing.Callable[..., typing.Awaitable[None]]
    #: Put every *other* live session to sleep, so loading a core here cannot
    #: make two. Called before a save is inspected with a real core
    #: (retro/saves.py), which is the only caller left outside this class:
    #: reading a core's options used to evict every session in every channel
    #: to do it, and now answers from what is already known instead.
    _evict_locked: typing.Callable[..., typing.Awaitable[typing.List[typing.Any]]]
    #: Run one blocking call into a libretro core, on the single thread every
    #: core call is made from. Used wherever a mixin drives a core -- probing
    #: a core's options and setting one (retro/cores.py), checking an
    #: uploaded save against the real core (retro/saves.py). Never
    #: ``asyncio.to_thread``: see Retro.run_in_emulator_thread for what a
    #: core does when the thread under it changes.
    run_in_emulator_thread: typing.Callable[..., typing.Awaitable[typing.Any]]
    #: Make the message edits that were put off while the emulator lock was
    #: held. Called by anything that takes that lock and lets it go again --
    #: _evict_locked records an edit per session it puts to sleep.
    _flush_refreshes: typing.Callable[..., typing.Awaitable[None]]
    #: Reply to a command without letting a missing permission raise.
    _safe_send: typing.Callable[..., typing.Awaitable[typing.Any]]
    #: Reply with something long enough to need paging.
    _send_pages: typing.Callable[..., typing.Awaitable[typing.Any]]


class CompositeMetaClass(commands.CogMeta, ABCMeta):
    """``CogMeta`` and ``ABCMeta`` at once, so the cog can be both."""
