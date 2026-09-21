"""
The URL guard.

`[p]retro <url>` is open to everyone who can type in a channel the bot can
see, so it is the one place where a stranger chooses what the bot connects
to. These tests are the fence around that: every address family that must be
refused, the schemes, and the redirect chain -- because a public URL that
302s to 127.0.0.1 is the interesting attack, not a private URL typed
directly.

Nothing here touches the network: DNS is answered by a fake resolver and the
HTTP session is injected.
"""

import contextlib

import pytest

from .loader import load_standalone

net = load_standalone("retro_net_standalone", "net.py")


# -- Addresses ----------------------------------------------------------------

REFUSED = [
    ("127.0.0.1", "loopback"),
    ("127.1.2.3", "loopback, anywhere in 127/8"),
    ("::1", "IPv6 loopback"),
    ("10.0.0.7", "private, 10/8"),
    ("192.168.1.1", "private, 192.168/16"),
    ("172.16.9.9", "private, 172.16/12"),
    ("169.254.169.254", "link-local: the cloud metadata address"),
    ("fe80::1", "IPv6 link-local"),
    ("fd00::1", "IPv6 unique-local"),
    ("0.0.0.0", "unspecified"),
    ("::", "IPv6 unspecified"),
    ("224.0.0.1", "multicast"),
    ("ff02::1", "IPv6 multicast"),
    ("100.64.0.1", "carrier NAT, caught by is_global"),
    ("192.0.2.1", "documentation range, caught by is_global"),
    ("::ffff:127.0.0.1", "loopback wrapped in IPv6"),
    ("::ffff:10.0.0.1", "private wrapped in IPv6"),
    ("2002:7f00:1::", "6to4 wrapping 127.0.0.1"),
    ("64:ff9b::7f00:1", "NAT64 wrapping 127.0.0.1"),
    ("not-an-address", "unparseable"),
    ("", "empty"),
]

ALLOWED = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]


@pytest.mark.parametrize("address, why", REFUSED, ids=[a or "empty" for a, _ in REFUSED])
def test_an_address_the_bot_must_not_connect_to_is_refused(address, why):
    refusal = net.address_refusal(address)
    assert refusal, f"{address} ({why}) was allowed"
    assert isinstance(refusal, str) and refusal


@pytest.mark.parametrize("address", ALLOWED)
def test_a_public_address_is_allowed(address):
    assert net.address_refusal(address) is None


def test_a_scope_id_does_not_smuggle_a_link_local_address_through():
    assert net.address_refusal("fe80::1%eth0")


# -- Schemes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/rom.gb",
        "gopher://example.com",
        "http://",
        "https://",
        "not a url at all",
        "//example.com/rom.gb",
    ],
)
def test_a_url_worth_no_lookup_is_refused_outright(url):
    with pytest.raises(net.BlockedURL):
        net.check_scheme(url)


@pytest.mark.parametrize("url", ["http://example.com/rom.gb", "https://example.com/rom.zip"])
def test_an_ordinary_public_url_passes_the_scheme_check(url):
    scheme, host = net.check_scheme(url)
    assert scheme in {"http", "https"}
    assert host == "example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/rom.gb",
        "http://[::1]/rom.gb",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5:8080/rom.gb",
    ],
)
def test_a_literal_private_address_is_refused_without_a_lookup(url):
    with pytest.raises(net.BlockedURL):
        net.check_scheme(url)


# -- The fetch, with DNS and HTTP faked ---------------------------------------


class FakeResolver:
    """Answers whatever the test says, so no DNS leaves the machine."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def resolve(self, host, port=0, family=0):
        self.asked.append(host)
        address = self.answers.get(host, "93.184.216.34")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self):
        return None


class FakeResponse:
    def __init__(self, status=200, location=None, body=b"rom", url="https://example.com/rom.gb"):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self.content_length = len(body)
        # The guard resolves a relative Location against the response's own
        # URL, so a stand-in needs one.
        self.url = url
        self._body = body
        self.released = False

    async def read(self):
        return self._body

    def release(self):
        self.released = True


def session_factory_for(hops):
    """A session whose GETs replay `hops`, recording what was asked for."""
    asked = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.connector = kwargs.get("connector")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            # aiohttp closes the connector with the session; the guard's
            # resolver lives on it, so make sure we do not leak one.
            connector = getattr(self, "connector", None)
            if connector is not None:
                await connector.close()
            return False

        async def get(self, url, allow_redirects=False):
            asked.append(url)
            assert allow_redirects is False, "the guard must check hops itself"
            return hops[len(asked) - 1]

    return FakeSession, asked


async def fetch(url, hops, answers=None, **kwargs):
    factory, asked = session_factory_for(hops)
    resolver = FakeResolver(answers or {})
    async with net.guarded_get(
        url, session_factory=factory, resolver=resolver, **kwargs
    ) as response:
        body = await response.read()
    return body, asked, resolver


async def test_a_public_url_is_fetched():
    body, asked, resolver = await fetch("https://example.com/rom.gb", [FakeResponse()])
    assert body == b"rom"
    assert asked == ["https://example.com/rom.gb"]


async def test_a_redirect_to_a_public_url_is_followed():
    hops = [FakeResponse(302, "https://cdn.example.com/rom.gb"), FakeResponse()]
    body, asked, _ = await fetch("https://example.com/rom.gb", hops)
    assert body == b"rom"
    assert asked == ["https://example.com/rom.gb", "https://cdn.example.com/rom.gb"]


async def test_a_redirect_into_loopback_is_refused():
    """The attack: a public URL that bounces the bot onto its own host."""
    hops = [FakeResponse(302, "http://127.0.0.1:6379/"), FakeResponse()]
    with pytest.raises(net.BlockedURL):
        await fetch("https://example.com/rom.gb", hops)


async def test_a_redirect_to_a_file_url_is_refused():
    hops = [FakeResponse(302, "file:///etc/passwd"), FakeResponse()]
    with pytest.raises(net.BlockedURL):
        await fetch("https://example.com/rom.gb", hops)


async def test_a_redirect_loop_gives_up():
    hops = [FakeResponse(302, "https://example.com/rom.gb") for _ in range(12)]
    with pytest.raises(net.BlockedURL):
        await fetch("https://example.com/rom.gb", hops, max_redirects=3)


async def test_a_hostname_that_resolves_into_a_private_address_is_refused():
    """DNS is the other way in: the URL looks fine, the answer does not."""
    guard = net.GuardedResolver(inner=FakeResolver({"rom.example": "10.1.2.3"}))
    with pytest.raises(net.BlockedURL):
        await guard.resolve("rom.example", 443, 0)


async def test_a_hostname_that_resolves_publicly_is_allowed():
    guard = net.GuardedResolver(inner=FakeResolver({"rom.example": "93.184.216.34"}))
    answers = await guard.resolve("rom.example", 443, 0)
    assert answers and answers[0]["host"] == "93.184.216.34"


async def test_one_private_answer_among_several_refuses_the_lot():
    class Multi(FakeResolver):
        async def resolve(self, host, port=0, family=0):
            return [
                {"hostname": host, "host": "93.184.216.34", "port": port,
                 "family": family, "proto": 0, "flags": 0},
                {"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": family, "proto": 0, "flags": 0},
            ]

    guard = net.GuardedResolver(inner=Multi({}))
    with pytest.raises(net.BlockedURL):
        await guard.resolve("rom.example", 443, 0)


async def test_the_owners_opt_in_lets_a_private_address_through():
    guard = net.GuardedResolver(inner=FakeResolver({"nas.local": "192.168.1.10"}), allow_private=True)
    answers = await guard.resolve("nas.local", 80, 0)
    assert answers[0]["host"] == "192.168.1.10"


async def test_even_the_opt_in_does_not_allow_a_bad_scheme():
    with pytest.raises(net.BlockedURL):
        async with net.guarded_get(
            "file:///etc/passwd",
            session_factory=session_factory_for([FakeResponse()])[0],
            resolver=FakeResolver({}),
            allow_private=True,
        ):
            pass


async def test_the_refusal_shown_in_discord_says_nothing_useful_to_a_scanner():
    """
    One generic sentence, so a blocked host, a refused connection and a
    timeout are indistinguishable from the outside.
    """
    assert "not fetch" in net.REFUSAL.lower() or "won't fetch" in net.REFUSAL.lower()
    for leak in ("127.0.0.1", "private", "loopback", "10.", "192.168", "refused", "timeout"):
        assert leak not in net.REFUSAL, f"{leak!r} leaks in the user-facing refusal"


async def test_refuse_reason_reports_why_for_the_log_only():
    reason = await net.refuse_reason(
        "http://127.0.0.1/rom.gb", resolver=FakeResolver({})
    )
    assert reason and isinstance(reason, str)
    assert await net.refuse_reason(
        "https://example.com/rom.gb", resolver=FakeResolver({})
    ) is None


def test_the_response_is_released_even_when_the_caller_raises():
    """A body left open would leak the connection and the session."""

    async def run():
        response = FakeResponse()
        factory, _ = session_factory_for([response])
        with contextlib.suppress(RuntimeError):
            async with net.guarded_get(
                "https://example.com/rom.gb",
                session_factory=factory,
                resolver=FakeResolver({}),
            ):
                raise RuntimeError("the caller blew up mid-download")
        return response

    import asyncio

    assert asyncio.run(run()).released
