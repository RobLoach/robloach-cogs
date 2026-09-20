from . import commands  # noqa: F401


class Config:
    """Red's Config. The tests replace it wholesale with a fake."""

    @classmethod
    def get_conf(cls, *args, **kwargs):
        raise NotImplementedError("patched by the tests")
