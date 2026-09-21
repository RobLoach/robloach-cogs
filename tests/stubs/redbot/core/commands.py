"""Just enough of ``redbot.core.commands`` to import and drive Retro.

Commands keep their callback, which is how the tests invoke them
(``Retro.retro.callback(cog, ctx, ...)``); everything else a decorator
would normally attach (checks, permissions, cooldowns) is discarded. A test
that needs the real metadata is marked ``redbot``.
"""

import types


class CogMeta(type):
    """Red re-exports discord.py's CogMeta; the cog needs it to exist.

    The real one collects a cog's commands out of the whole MRO, which is
    what lets `Retro` be assembled from mixins. Nothing here collects
    anything -- the tests reach a command through the class attribute the
    decorator left behind -- but `retro/abc.py` derives its metaclass from
    this and `ABCMeta`, so it has to be a metaclass rather than a name.
    """


class Cog(metaclass=CogMeta):
    @classmethod
    def listener(cls, name=None):
        """Mark a coroutine as a Discord event handler.

        The real decorator records the event name so discord.py can wire the
        method up when the cog is added to the bot. Nothing here wires
        anything: the tests call a listener directly, exactly as they call a
        command's callback, so all this has to do is leave the function
        alone. ``tests/test_mixins.py`` pins the set of listeners against
        the real Red.
        """

        def decorator(func):
            return func

        return decorator


class Context:
    pass


class Command:
    def __init__(self, func, name=None):
        self.callback = func
        self.name = name or func.__name__
        self.__doc__ = func.__doc__

    def __get__(self, obj, objtype=None):
        return self


class Group(Command):
    def command(self, name=None, **kwargs):
        def decorator(func):
            return Command(getattr(func, "callback", func), name=name)

        return decorator

    def group(self, name=None, **kwargs):
        def decorator(func):
            return Group(getattr(func, "callback", func), name=name)

        return decorator


def command(name=None, **kwargs):
    def decorator(func):
        return Command(getattr(func, "callback", func), name=name)

    return decorator


def group(name=None, **kwargs):
    def decorator(func):
        return Group(getattr(func, "callback", func), name=name)

    return decorator


def _passthrough(*args, **kwargs):
    def decorator(func):
        return func

    return decorator


guild_only = is_owner = bot_has_permissions = max_concurrency = _passthrough
cooldown = dynamic_cooldown = _passthrough


# Red re-exports discord.ext.commands' exception hierarchy; the cog's
# cog_command_error annotates against it and isinstance-checks a few members.
# BucketType, Cooldown and CooldownMapping are the real ones rather than
# stand-ins: the cog builds a CooldownMapping itself for the per-channel half
# of the start rate limit, and a fake would only ever prove itself right.
from discord.ext.commands import (  # noqa: E402,F401
    BotMissingPermissions,
    BucketType,
    CheckFailure,
    CommandError,
    CommandInvokeError,
    CommandOnCooldown,
    Cooldown,
    CooldownMapping,
    MaxConcurrencyReached,
)
