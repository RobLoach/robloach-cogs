
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core import Config

from .PyBoyView import PyBoyView

class PyBoyCog(commands.Cog):
    """Play Gameboy"""
    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)
        self.config: Config = Config.get_conf(
            self,
            identifier=11411198108111979910445991111031154711212198111121,
            force_registration=True
        )
        #self.config.register_global(servers={})

    @commands.max_concurrency(1, commands.BucketType.member)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    @commands.command()
    async def pyboy(self, ctx: commands.Context) -> None:
        """Loads the given Gameboy game."""
        if not ctx.message.attachments:
            await ctx.send("Attach a Gameboy rom")
            return
        
        attachment = ctx.message.attachments[0]
        filename = attachment.filename
        await attachment.save(filename)

        await PyBoyView(
            self,
            filename
        ).start(ctx)
