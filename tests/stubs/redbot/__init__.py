"""A tiny stand-in for Red-DiscordBot.

The cog imports ``redbot`` at module level, so the tests need *something*
importable under that name. Installing the real Red-DiscordBot pulls in a
database, a voice stack and a hundred megabytes of dependencies, which is far
more than a unit test of a controller layout needs; this package is the few
names ``retro/*.py`` actually touches.

conftest.py puts ``tests/stubs`` on ``sys.path`` only when the real Red is not
installed, so a machine (or CI job) that *has* Red tests against the real
thing. Tests that depend on Red's own behaviour rather than merely on its
existence -- the command metadata in ``requires.bot_perms``, for instance --
are marked ``@pytest.mark.redbot`` and skip when this stub is in use.
"""

__version__ = "0.0.0-stub"
