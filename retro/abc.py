"""
The shapes that let the cog be assembled from mixins.

`Retro` is one cog class made of several: the storage layer, the cores and
their options, the `[p]retrosaves` group and the one-off namespace migration
each live in their own module and are mixed in (see retro/Retro.py). Two
pieces of scaffolding make that legal:

* :class:`MixinMeta` is what a mixin may assume about the cog it ends up on.
  It is an ABC purely so the annotations have somewhere to live -- nothing
  here is abstract, and nothing here is implemented either.
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
    #: ROM which has just been deleted, keeping the saves. Implemented on the
    #: cog itself (retro/Retro.py) and called by the disk budget after it
    #: prunes (retro/storage.py), which is the one place a mixin reaches back
    #: into the cog for something that is not storage.
    _forget_pruned_roms: typing.Callable[..., typing.Awaitable[None]]


class CompositeMetaClass(commands.CogMeta, ABCMeta):
    """``CogMeta`` and ``ABCMeta`` at once, so the cog can be both."""
