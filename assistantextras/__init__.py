from .AssistantExtras import AssistantExtras

async def setup(bot):
    await bot.add_cog(AssistantExtras(bot))
