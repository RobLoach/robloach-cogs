from redbot.core.bot import Red

from .Retro import Retro


async def setup(bot: Red) -> None:
    cog = Retro(bot)
    await bot.add_cog(cog)
