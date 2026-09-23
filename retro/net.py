"""
Outbound HTTP the bot is allowed to make.

`[p]retro <url>` is open to everybody who can type in a channel the bot can
see, which makes every URL in this cog a request the bot issues *from inside
its own network*. Without a guard that is a server-side request forgery hole:
``http://localhost:9000``, ``http://172.17.0.1`` (the Docker bridge),
``http://169.254.169.254/latest/meta-data/`` (cloud instance credentials) and
every host on the bot's LAN are all reachable, and the difference between
"403" and "timed out" is enough to map a private network one guess at a time.

So every outbound fetch in this cog goes through :func:`guarded_get`, which:

* refuses anything that is not ``http://`` or ``https://``;
* resolves the hostname and refuses the request if **any** resolved address is
  loopback, private, link-local, unique-local, multicast, reserved,
  unspecified, or otherwise not globally routable -- IPv6 included, and
  including an IPv4 address smuggled inside an IPv6 one (``::ffff:127.0.0.1``,
  6to4, Teredo, the NAT64 well-known prefix);
* connects to *that* resolved address rather than resolving a second time, so
  a name that answers differently on the second lookup cannot be used to slip
  past the check (see :class:`GuardedResolver` for the residual gap);
* follows redirects itself, a bounded number of times, checking every hop the
  same way -- a public URL that answers ``302 Location: http://127.0.0.1/`` is
  refused at the second hop, and so is one that redirects to ``file:///``;
* spends the caller's ``timeout`` across the *whole* chain rather than
  restarting it at every hop, so a server that dribbles out slow redirects
  cannot hold the bot for six times as long as the caller allowed.

Everything it refuses raises :class:`BlockedURL`, which deliberately carries
no detail: the caller turns it into one sentence that is the same whether the
address was blocked, the connection was refused, or the request timed out, so
the reply cannot be used as a scanner's oracle. The reason is logged instead.
"""

import asyncio
import ipaddress
import logging
import socket
import time
import typing
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger("red.robloach.retro")

#: How many ``Location:`` hops one fetch will follow before giving up. Five is
#: what browsers and aiohttp itself allow by default; a chain longer than that
#: is a loop or an attempt to exhaust the check.
MAX_REDIRECTS = 5

#: The statuses that mean "look over there instead".
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: The only schemes this cog will ever fetch. Anything else -- ``file://``,
#: ``gopher://``, ``ftp://``, ``data:`` -- is refused, including as the target
#: of a redirect, which is the interesting case: a cooperating web server can
#: answer ``302 Location: file:///etc/passwd``.
ALLOWED_SCHEMES = ("http", "https")

#: The one sentence a refused URL gets. Identical for "that address is not
#: allowed", "nothing answered" and "it timed out", on purpose: three
#: different sentences are a working port scanner.
REFUSAL = (
    "That URL points somewhere the bot will not fetch from, or nothing "
    "answered. Use a direct public `https://` link to the file."
)

#: The well-known NAT64 prefix (RFC 6052). An address inside it is a way of
#: writing an IPv4 address, so the IPv4 address inside it is checked too.
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")

#: The properties that make an address one this cog will not fetch from, in
#: the order they are reported. ``is_private`` alone covers most of them
#: (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, fc00::/7, ::1), but they
#: are listed out because each one is a thing somebody will try.
_REFUSED_PROPERTIES = (
    ("is_unspecified", "the unspecified address"),
    ("is_loopback", "a loopback address"),
    ("is_link_local", "a link-local address"),
    ("is_multicast", "a multicast address"),
    ("is_reserved", "a reserved address"),
    ("is_private", "a private address"),
)


class BlockedURL(Exception):
    """
    A URL this cog will not fetch.

    The message is for the log, never for Discord: the caller answers with
    :data:`REFUSAL` instead. See :func:`guarded_get`.
    """


def _embedded_addresses(
    address: ipaddress._BaseAddress,
) -> typing.Iterator[ipaddress._BaseAddress]:
    """
    One address and every other address hiding inside it.

    ``::ffff:127.0.0.1`` is loopback, but only if you look at the IPv4
    address it wraps: the IPv6 address itself is none of the things
    :data:`_REFUSED_PROPERTIES` asks about. The same is true of a 6to4
    address (``2002:7f00:1::``), a Teredo one, and anything inside the NAT64
    prefix, so each of those is unwrapped and checked as well.
    """
    yield address
    if not isinstance(address, ipaddress.IPv6Address):
        return
    inner: typing.List[typing.Optional[ipaddress.IPv4Address]] = [
        address.ipv4_mapped,
        address.sixtofour,
    ]
    teredo = address.teredo
    if teredo:
        # (the Teredo server, the client behind it); either may be private.
        inner.extend(teredo)
    if address in _NAT64_PREFIX:
        inner.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    for candidate in inner:
        if candidate is not None:
            yield candidate


def address_refusal(address: typing.Union[str, ipaddress._BaseAddress]) -> typing.Optional[str]:
    """
    Why this cog will not connect to an address, or None if it will.

    The answer is a phrase for the *log*. An address that cannot be parsed at
    all is refused too: a resolver that answers with something this cannot
    read is not something to hand to a socket.
    """
    if isinstance(address, str):
        try:
            # A scope id ("fe80::1%eth0") is not part of the address.
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return f"not an IP address at all ({address!r})"
    else:
        parsed = address
    for candidate in _embedded_addresses(parsed):
        for attribute, phrase in _REFUSED_PROPERTIES:
            if getattr(candidate, attribute, False):
                if candidate is parsed:
                    return phrase
                return f"{phrase} wrapped in an IPv6 address ({candidate})"
        # The catch-all, and the reason the list above is not a hand-rolled
        # set of CIDRs: `is_global` is Python's own view of "routable on the
        # public internet", so 100.64.0.0/10 (carrier NAT), the documentation
        # ranges, and whatever a future Python learns about are covered
        # without this file having to be updated.
        if not candidate.is_global:
            return f"not a globally routable address ({candidate})"
    return None


def check_scheme(url: str) -> typing.Tuple[str, str]:
    """
    ``(scheme, host)`` for a URL worth resolving, or raise :class:`BlockedURL`.

    Everything here is decided from the text of the URL: the scheme, that
    there is a hostname at all, and -- when the host is written as a literal
    IP address -- whether that address is one this cog will connect to.

    That last check is not redundant with :class:`GuardedResolver`. aiohttp
    recognises a literal address and skips the resolver entirely for it, so a
    guard that lived only in the resolver would let ``http://127.0.0.1/``
    straight through.
    """
    text = str(url).strip()
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"the scheme is {scheme or 'missing'!r}, not http or https"
        )
    try:
        host = parts.hostname or ""
    except ValueError as error:  # an unparseable IPv6 literal, e.g. "http://[::"
        raise BlockedURL(f"the host could not be read: {error}") from error
    if not host:
        raise BlockedURL("there is no hostname in it")
    host = host.rstrip(".").lower() or host
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return scheme, host
    reason = address_refusal(literal)
    if reason is not None:
        raise BlockedURL(f"{host} is {reason}")
    return scheme, host


class GuardedResolver(aiohttp.abc.AbstractResolver):
    """
    aiohttp's DNS resolver, with every answer checked before it is used.

    This is what closes the gap between "the name looked fine" and "the
    socket went somewhere else". The addresses this hands back are the
    addresses aiohttp connects to, so there is no second lookup in between
    for a DNS rebinding attack to win -- and because the URL's hostname is
    left alone, the ``Host`` header and TLS SNI are still the name the caller
    asked for, so pinning the address does not break virtual hosting or
    certificate validation.

    **Residual gap, honestly.** A name whose *first* answer is public is
    fetched; if the attacker controls the name and the record's TTL, the
    answer aiohttp gets is the answer that is validated, so the classic
    rebind (validate 93.x, connect to 127.0.0.1) does not work here. What
    this cannot defend against is a genuinely public host that *itself*
    forwards the request onwards -- an open proxy, a URL-preview service, an
    SSRF hole in somebody else's application. Nothing a client-side check can
    see distinguishes that from a normal download.

    It is also strict on purpose: a name that resolves to several addresses is
    refused if *any* of them is disallowed, rather than quietly connecting to
    whichever one happened to be acceptable.
    """

    def __init__(
        self,
        inner: typing.Optional[aiohttp.abc.AbstractResolver] = None,
        allow_private: bool = False,
    ) -> None:
        self._inner = inner if inner is not None else aiohttp.DefaultResolver()
        self._owns_inner = inner is None
        self.allow_private = bool(allow_private)
        #: Every (host, address) this resolver has handed out, for the tests
        #: and for anybody reading a debug log.
        self.resolved: typing.List[typing.Tuple[str, str]] = []

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> typing.List[typing.Dict[str, typing.Any]]:
        answers = await self._inner.resolve(host, port, family)
        for answer in answers:
            address = str(answer.get("host") or "")
            self.resolved.append((host, address))
            reason = address_refusal(address)
            if reason is None:
                continue
            if self.allow_private:
                log.warning(
                    "Fetching from %s (%s), which is %s: the owner has turned "
                    "the private-address guard off.",
                    host,
                    address,
                    reason,
                )
                continue
            raise BlockedURL(f"{host} resolves to {address}, which is {reason}")
        return answers

    async def close(self) -> None:
        if self._owns_inner:
            await self._inner.close()


def _redirect_target(response: typing.Any) -> typing.Optional[str]:
    """The absolute URL a response is pointing at, or None if it is not."""
    if response.status not in REDIRECT_STATUSES:
        return None
    location = response.headers.get("Location") or response.headers.get("location")
    if not location:
        return None
    # Relative redirects are normal ("Location: /files/rom.gb"), and urljoin
    # is the standard library's own answer for resolving one. The result goes
    # straight back through check_scheme(), so a relative redirect cannot
    # smuggle a scheme past it either.
    return urljoin(str(response.url), location.strip())


@asynccontextmanager
async def guarded_get(
    url: str,
    *,
    timeout: typing.Optional[typing.Any] = None,
    allow_private: bool = False,
    max_redirects: int = MAX_REDIRECTS,
    headers: typing.Optional[typing.Mapping[str, str]] = None,
    session_factory: typing.Optional[typing.Callable[..., typing.Any]] = None,
    resolver: typing.Optional[aiohttp.abc.AbstractResolver] = None,
) -> typing.AsyncIterator[typing.Any]:
    """
    GET a URL the bot is allowed to fetch, and hand back the response.

    Used as an async context manager; the body must be read inside it, since
    leaving it closes the connection and the session::

        async with guarded_get(url, timeout=timeout) as response:
            data = await response.read()

    Raises :class:`BlockedURL` for anything refused -- a bad scheme, an
    address the cog will not connect to (at any hop), or a redirect chain
    longer than ``max_redirects``. Everything else is aiohttp's own
    exceptions, unchanged.

    ``allow_private`` is the owner's opt-in escape hatch (see
    ``[p]retroset allowprivateurls``) and is off everywhere by default. With
    it on, the *addresses* are no longer refused -- the scheme check and the
    redirect bound still apply.

    ``timeout`` (an ``aiohttp.ClientTimeout``, or a bare number of seconds) is
    a budget for the whole fetch -- every redirect hop and the final body --
    not for each hop. Running out of it mid-chain raises
    ``asyncio.TimeoutError``, exactly what aiohttp raises when a single slow
    request runs out of time, so callers need no extra handling.
    """
    guard = GuardedResolver(inner=resolver, allow_private=allow_private)
    factory = session_factory if session_factory is not None else aiohttp.ClientSession
    connector = aiohttp.TCPConnector(
        resolver=guard,
        # No DNS cache, so every connection in this chain is resolved through
        # the guard above rather than out of a cache that was filled before
        # it. The cost is one lookup per hop on a download that happens once.
        use_dns_cache=False,
        # Nothing here reuses a connection: one fetch, then the session goes.
        force_close=True,
    )
    # One deadline for the whole chain, fixed before the first request. A
    # ClientTimeout(total=...) left on the session restarts for every request,
    # and each redirect hop is its own request -- so a hostile or broken chain
    # of slow redirects would get (max_redirects + 1) fresh budgets, pinning a
    # ROM fetch for around twelve minutes instead of two. Each hop below is
    # instead given only what remains of the caller's total; a budget that
    # runs out between hops raises asyncio.TimeoutError, the same exception
    # aiohttp raises when one request overruns, so the callers' handling (and
    # the single REFUSAL sentence in Discord) is unchanged.
    total = timeout if isinstance(timeout, (int, float)) else getattr(timeout, "total", None)
    deadline = None if total is None else time.monotonic() + total
    async with factory(connector=connector, timeout=timeout, headers=headers) as session:
        current = str(url)
        for hop in range(max_redirects + 1):
            check_scheme(current)
            request_kwargs: typing.Dict[str, typing.Any] = {}
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        f"the {total} second budget ran out after {hop} redirect(s)"
                    )
                # The per-request timeout overrides the session's, so this hop
                # gets the remainder of the budget as its total. The other
                # limits the caller set (connect, per-read) are per-attempt by
                # nature and are carried over as they are.
                request_kwargs["timeout"] = aiohttp.ClientTimeout(
                    total=remaining,
                    connect=getattr(timeout, "connect", None),
                    sock_connect=getattr(timeout, "sock_connect", None),
                    sock_read=getattr(timeout, "sock_read", None),
                )
            # allow_redirects=False: aiohttp would happily follow a redirect
            # into a private address on its own. Each hop is checked here
            # instead, which is also the only way to refuse a non-http(s)
            # redirect target with an explanation rather than a stack trace.
            response = await session.get(current, allow_redirects=False, **request_kwargs)
            target = _redirect_target(response)
            if target is None:
                try:
                    yield response
                finally:
                    response.release()
                return
            response.release()
            if hop >= max_redirects:
                break
            log.debug("Following redirect %s -> %s", current, target)
            current = target
    raise BlockedURL(f"more than {max_redirects} redirects starting at {url}")


async def refuse_reason(
    url: str, *, allow_private: bool = False, resolver: typing.Optional[typing.Any] = None
) -> typing.Optional[str]:
    """
    Why this cog would refuse a URL, without fetching anything. None if it
    would not.

    The scheme and literal-address checks plus one DNS lookup, which is
    everything :func:`guarded_get` can decide before it opens a socket. Used
    by `[p]retroset game add`, so a URL that can never work is refused when it
    is stored rather than only when somebody tries to play it.

    A lookup that fails outright is *not* a refusal: a name that does not
    resolve right now may resolve later, and the fetch will check it again.
    """
    try:
        _, host = check_scheme(url)
    except BlockedURL as error:
        return str(error)
    guard = GuardedResolver(inner=resolver, allow_private=allow_private)
    try:
        await guard.resolve(host, 0, socket.AF_UNSPEC)
    except BlockedURL as error:
        return str(error)
    except (OSError, aiohttp.ClientError):
        log.debug("Could not resolve %s while checking a stored URL.", host, exc_info=True)
        return None
    finally:
        await guard.close()
    return None
