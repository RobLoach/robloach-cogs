"""Load a single cog module without importing the ``retro`` package.

``retro/__init__.py`` imports Red, and ``retro/Retro.py`` imports discord.
``systems.py`` and ``archives.py`` deliberately import nothing but the
standard library, and ``emulator.py``/``clips.py`` nothing but libretro.py
and Pillow, so the tests that cover them load the files directly and run
anywhere Python does.

The modules are loaded into a synthetic package whose ``__path__`` is
``retro/`` rather than as bare top-level modules, because ``emulator.py``
imports its own sibling (``from .clips import ...``) and a relative import
needs a parent package to be relative *to*. The synthetic package has no
``__init__.py`` of its own, so ``retro/__init__.py`` -- and with it Red --
still never runs.
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The name of the package the standalone modules are loaded under. Not
#: ``retro``: that one has an ``__init__.py`` that imports Red.
STANDALONE_PACKAGE = "retro_standalone"


def _standalone_package():
    """A package rooted at ``retro/`` that runs no ``__init__``."""
    package = sys.modules.get(STANDALONE_PACKAGE)
    if package is None:
        package = types.ModuleType(STANDALONE_PACKAGE)
        package.__path__ = [str(REPO_ROOT / "retro")]
        sys.modules[STANDALONE_PACKAGE] = package
    return package


def load_standalone(module_name, filename):
    """Load ``retro/<filename>`` under ``module_name``, outside the cog package."""
    _standalone_package()
    qualified = f"{STANDALONE_PACKAGE}.{module_name}"
    path = REPO_ROOT / "retro" / filename
    spec = importlib.util.spec_from_file_location(qualified, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, so a sibling that imports it back
    # gets the module being built rather than a second copy of it.
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module
