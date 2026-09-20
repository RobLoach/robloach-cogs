class SimpleMenu:
    """Enough of Red's paginator for the Retro cog's tests."""

    last_pages = []

    def __init__(self, pages, timeout=180.0, **kwargs):
        self.pages = list(pages)
        SimpleMenu.last_pages = self.pages

    async def start(self, ctx, **kwargs):
        for page in self.pages:
            await ctx.send(page)
