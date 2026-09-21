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


class CompositeMetaClass(commands.CogMeta, ABCMeta):
    """``CogMeta`` and ``ABCMeta`` at once, so the cog can be both."""
