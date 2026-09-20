"""Just enough of ``redbot.core.commands`` to import and drive RetroCog.

Commands keep their callback, which is how the tests invoke them
(``RetroCog.retro.callback(cog, ctx, ...)``); everything else a decorator
would normally attach (checks, permissions, cooldowns) is discarded. A test
that needs the real metadata is marked ``redbot``.
"""

import types


class Cog:
    pass


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
BucketType = types.SimpleNamespace(channel=1, guild=2, user=3, default=0)


# Red re-exports discord.ext.commands' exception hierarchy; the cog's
# cog_command_error annotates against it and isinstance-checks a few members.
from discord.ext.commands import (  # noqa: E402,F401
    BotMissingPermissions,
    CheckFailure,
    CommandError,
    CommandInvokeError,
    MaxConcurrencyReached,
)
