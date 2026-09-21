class SimpleMenu:
    """Enough of Red's paginator for the Retro cog's tests."""

    last_pages = []

    def __init__(self, pages, timeout=180.0, **kwargs):
        self.pages = list(pages)
        SimpleMenu.last_pages = self.pages

    async def start(self, ctx, **kwargs):
        for page in self.pages:
            await ctx.send(page)


class ConfirmView:
    """Enough of Red's yes/no prompt to import the cog.

    The real one is a ``discord.ui.View`` with two buttons. The cog tests
    replace it with :class:`tests.fakes.FakeConfirm`, which answers
    immediately; this only has to exist so ``from redbot.core.utils.views
    import ConfirmView`` works when the real Red is not installed. The
    ``redbot``-marked tests check the real class against this contract.
    """

    def __init__(self, author=None, *, timeout=180.0, disable_buttons=False):
        self.author = author
        self.timeout = timeout
        self.disable_buttons = disable_buttons
        self.result = None
        self.message = None

    async def wait(self):
        return True
