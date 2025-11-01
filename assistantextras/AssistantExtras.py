import logging
import typing as t

from redbot.core import commands
from redbot.core.bot import Red

from .abc import CompositeMetaClass
from .common import schemas
from .common.functions import Functions

log = logging.getLogger("red.robloach.assistantextras")

class MockAssistantCog:
    async def register_function(
        self,
        cog_name: str,
        schema: dict,
        permission_level: t.Literal["user", "mod", "admin", "owner"] = "user",
    ) -> bool:
        raise NotImplementedError("This is a mock class for testing purposes.")

class AssistantExtras(Functions, commands.Cog, metaclass=CompositeMetaClass):
    """
    Assistant Extras adds additional functions to the Assistant from your existing cogs.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot

    @commands.Cog.listener()
    async def on_assistant_cog_add(self, cog: MockAssistantCog):
        await cog.register_function(self.qualified_name, schemas.WORDLE)
        log.info("AssistantExtras Functions registered")

    @commands.command()
    async def assistantextras(self, ctx: commands.Context) -> None:
        """Does something."""
        await ctx.send("Nothing")
