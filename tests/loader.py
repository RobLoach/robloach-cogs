"""Load a single cog module without importing the ``retro`` package.

``retro/__init__.py`` imports Red, and ``retro/Retro.py`` imports discord.
``systems.py`` and ``archives.py`` deliberately import nothing but the
standard library, so the tests that cover them load the files directly and
run anywhere Python does.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_standalone(module_name, filename):
    """Load ``retro/<filename>`` under ``module_name``, with no package."""
    path = REPO_ROOT / "retro" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
