"""
Retro: play retro console games in Discord, emulated with libretro.

Copyright (C) 2025-2026 Rob Loach

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

The notice above is the one the GPL itself asks for, and this is the file it
goes in: the package every other module in the cog is reached through. The
LICENSE file at the repository root is the licence text verbatim and is
deliberately not edited to hold a copyright line -- it is a legal document the
FSF asks distributors not to change.
"""

from redbot.core.bot import Red

from .Retro import Retro
from .version import VERSION as __version__

__all__ = ["Retro", "setup", "__version__"]


async def setup(bot: Red) -> None:
    cog = Retro(bot)
    await bot.add_cog(cog)
