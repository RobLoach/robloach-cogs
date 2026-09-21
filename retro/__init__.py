from redbot.core.bot import Red

from .Retro import Retro
from .version import VERSION as __version__

__all__ = ["Retro", "setup", "__version__"]


async def setup(bot: Red) -> None:
    cog = Retro(bot)
    await bot.add_cog(cog)
