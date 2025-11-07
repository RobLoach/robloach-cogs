
import discord

from redbot.core import commands
from redbot.core.bot import Red
from redbot.core import Config
from redbot.core.utils.views import SetApiView, SimpleMenu
import aiohttp

class JackettCog(commands.Cog):
    """
    Use Jackett to search your favorite torrent trackers
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config: Config = Config.get_conf(
            self,
            identifier=114+111+98+108+111+97+99+104+45+99+111+103+115+47+106+97+99+107+101+116+116,
            force_registration=True
        )
        self.config.register_global(
            api_key="",
            url="http://127.0.0.1:9117"
        )

    @commands.bot_has_permissions(embed_links=True)
    @commands.command()
    async def jackett(self, ctx: commands.Context, *, query: str) -> None:
        """
        Search for a Torrent using Jackett

        **Examples:**
        - `[p]jackett Ubuntu MATE`

        **Arguments:**
        - `<query>` - The query you're looking for.
        """
        token = await self.bot.get_shared_api_tokens("jackett")
        api_key = token.get("api_key")
        url = await self.config.url()
        xml_data = ""

        params = {
            "apikey": api_key,
            "t": "search",
            "q": query
        }
        search_url = f"{url}/api/v2.0/indexers/all/results/torznab"

        #await ctx.send(f"Searching Jackett at: `{search_url}`")

        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, params=params, ssl=False) as resp:
                if resp.status != 200:
                    await ctx.send(f"Error: Jackett API returned status {resp.status}")
                    return
                xml_data = await resp.text()

        if "error code" in xml_data.lower():
            await ctx.send("Error: Jackett API returned an error in the response.")
            return

        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")

        if not items:
            await ctx.send("No results found.")
            return

        for item in items[:20]:  # Limit to first 5 results
            title = item.findtext("title", default="No Title")
            link = item.findtext("link", default="No Link")
            if title and link and "magnet:" in link:
                embed = discord.Embed(
                    title=title,
                    description=f"```\n{link}\n```",
                    colour=await ctx.embed_colour(),
                )
                await ctx.send(embed=embed)
                break

    @commands.group()
    @commands.admin_or_permissions(manage_guild=True)
    async def jackettset(self, ctx: commands.Context):
        """
        Configure Jackett cog settings.
        """

    @commands.is_owner()
    @jackettset.command(name="creds")
    @commands.bot_has_permissions(embed_links=True)
    async def jackettset_creds(self, ctx: commands.Context):
        """
        Guide to setting up the Jackett API key.

        This command will give you information on how to set up the API key.
        """
        msg = (
            "To use this cog, you need to get an API key from Jackett.\n"
            f"`{ctx.clean_prefix}set api jackett api_key <your api key>`\n"
        )
        default_keys = {"api_key": ""}
        view = SetApiView("jackett", default_keys)
        embed = discord.Embed(
            title="Jackett API Key",
            description=msg,
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=embed, view=view)

    @commands.is_owner()
    @jackettset.command(name="url")
    async def set_url(self, ctx: commands.Context, new_url: str) -> None:
        """
        Set the Jackett server URL.
        """
        await self.config.url.set(new_url)
        await ctx.send(f"Jackett URL has been set to: {new_url}")
