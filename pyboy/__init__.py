from redbot.core.bot import Red
from .PyBoyCog import PyBoyCog

async def setup(bot: Red) -> None:
    cog = PyBoyCog(bot)
    await bot.add_cog(cog)
