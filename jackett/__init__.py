from redbot.core.bot import Red
from .JackettCog import JackettCog

async def setup(bot: Red) -> None:
    cog = JackettCog(bot)
    await bot.add_cog(cog)
