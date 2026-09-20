from redbot.core.bot import Red
from .RetroCog import RetroCog

async def setup(bot: Red) -> None:
    cog = RetroCog(bot)
    await bot.add_cog(cog)
