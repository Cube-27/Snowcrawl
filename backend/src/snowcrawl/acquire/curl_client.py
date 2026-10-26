"""The one curl_cffi client of the HTTP and JSON rungs: a session impersonating Chrome,
every request pinned to the address the SSRF guard checked, directly or through a proxy pool's CONNECT tunnel to it,
bodies capped
while they stream, redirects followed by hand through the guard with the fetch's own cookies, and every
failure a short reason that never contains the URL.

A request's headers are merged into the impersonation's defaults, the headers `acquire.impersonate` takes from the
browser rung in place of the profile's: a header the profile sends keeps the profile's position with the request's
value, a `None` value removes it, and any other header follows the profile's.
"""

import asyncio
import io
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Final, Literal, cast
from urllib.parse import urlsplit

from curl_cffi import Curl, CurlECode, CurlHttpVersion, CurlOpt, ffi, lib
from curl_cffi.curl import (
    CURL_WRITEFUNC_ERROR,
    CURLINFO_DATA_IN,
    CURLINFO_DATA_OUT,
    CURLINFO_HEADER_IN,
    CURLINFO_HEADER_OUT,
)
from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException

from snowcrawl.acquire.cookies import FetchCookies, fetch_jar
from snowcrawl.acquire.headers import recorded
from snowcrawl.acquire.impersonate import Impersonation
from snowcrawl.acquire.outcome import Exchange, Route, Traffic
from snowcrawl.acquire.public_suffix import site_of
from snowcrawl.acquire.redirects import MAX_REDIRECTS
from snowcrawl.acquire.redirects import location as redirect_location
from snowcrawl.acquire.session import SessionCookie, SiteSession
from snowcrawl.acquire.ssrf import SafeConnector, SsrfRejected, Target, bracketed
from snowcrawl.acquire.types import FetchPolicy, RequestContext
from snowcrawl.core.proxy import ProxyEndpoint, ProxyPool, UnknownProxyPool, pool_endpoint, proxy_pool
from snowcrawl.storage.capture_bundle import HeaderList

__all__ = [
    "BODY_CAP",
    "Failed",
    "Hop",
    "Limits",
    "Redirected",
    "Sent",
    "Transport",
    "Wire",
    "curl_transport",
    "follow",
    "pinned",
    "reason",
    "send",
    "set_field",
]

_CURLPROXY_HTTP = 0  # curl's `curl_proxytype` for an HTTP proxy (CURLOPT_PROXYTYPE)
# The debug callback's kinds that carry HTTP message bytes (curl_debug_callback, CURLOPT_DEBUGFUNCTION): the headers and
# the body as transferred, proxy CONNECT exchanges included, without the TLS records or HTTP/2 framing around them.
_MESSAGE_BYTES = frozenset({CURLINFO_HEADER_IN, CURLINFO_HEADER_OUT, CURLINFO_DATA_IN, CURLINFO_DATA_OUT})
# The reason of a body past `max_body_bytes` or `max_decompressed_bytes`.
BODY_CAP: Final = "body_cap"
_CURL_ERRORS: dict[int, str] = {
    CurlECode.OPERATION_TIMEDOUT: "timeout",
    CurlECode.FILESIZE_EXCEEDED: BODY_CAP,  # the compressed body passed max_body_bytes
    CurlECode.BAD_CONTENT_ENCODING: "decode_error",
}


@dataclass(frozen=True)
class Hop:
    """What one request sends. `headers` are merged into the impersonation's defaults; `None` removes a default.
    `order` replaces the profile's header order (lowercase names), for a request the profile does not describe."""

    method: Literal["GET", "POST"]
    headers: Mapping[str, str | None]
    body: bytes | None = None
    order: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Wire:
    """One response as it arrived: the headers sent and received, cookies included, and the decoded body. `status` is
    0 when no status line arrived."""

    url: str
    status: int
    request_headers: HeaderList
    response_headers: HeaderList
    body: bytes

    @property
    def exchange(self) -> Exchange:
        """As a capture records it: without credentials (`headers.recorded`)."""
        return Exchange(self.url, self.status, recorded(self.request_headers), recorded(self.response_headers))


@dataclass(frozen=True)
class Limits:
    """The caps and the fetch's deadline, on `time.monotonic()`."""

    max_body_bytes: int
    max_decompressed_bytes: int
    deadline: float


class Failed(Exception):
    """A request got no usable response. `last` is the last response that did arrive, if any."""

    def __init__(self, error: str, last: Wire | None) -> None:
        super().__init__(error)
        self.error = error
        self.last = last


# Sends one hop to a checked target; tests stand in for the network here.
Transport = Callable[[Target, Hop], Awaitable[Wire]]
# The hop that follows a redirect with `status` to the checked target.
Redirected = Callable[[Hop, int, Target], Hop]


@dataclass(frozen=True)
class Sent:
    """What a fetch's requests got: the last response (None when none arrived), the failure, if any, and how they went
    out (`Route`)."""

    last: Wire | None
    error: str | None
    route: Route = field(default_factory=Route)


async def send(
    url: str,
    first: Hop,
    policy: FetchPolicy,
    *,
    impersonate: Impersonation,
    connector: SafeConnector,
    pools: Mapping[str, ProxyPool] | None,
    redirected: Redirected,
    transport: Transport | None = None,
    session: SiteSession | None = None,
    page_url: str | None = None,
) -> Sent:
    """Fetch `url` with `first` through the pool `policy.proxy_pool` names (an endpoint picked for the URL's host),
    within the policy's caps and time budget; `transport` replaces the network (tests). A site `session` is carried only
    through its own endpoint while the pool has it available: its cookies seed the fetch's jar, and the hops send the
    headers of the browser that earned them (`Impersonation.replaying`). The caller's context (`policy.context`) belongs
    to the page: it goes with the hops to the host of `page_url` (the page a JSON request is made for; `url` itself by
    default), and to no other host (`follow`)."""
    started = time.monotonic()
    try:
        pool = proxy_pool(policy.proxy_pool, pools)
    except UnknownProxyPool as unknown:
        return Sent(None, str(unknown))
    session = _usable(session, pool)
    # A page's own requests go out through its site's exit, as the browser rung's render of it does.
    proxy = session.proxy if session is not None else pool_endpoint(pool, site_of(page_url or url))
    limits = Limits(policy.max_body_bytes, policy.max_decompressed_bytes, started + policy.timeout_s)
    traffic = Traffic()
    try:
        async with AsyncExitStack() as stack:
            if transport is None:
                # A session's cookies go with the headers of the browser that earned them (`acquire.impersonate`).
                identity = impersonate.replaying(session is not None)
                transport = await stack.enter_async_context(curl_transport(identity, proxy, limits, traffic))
            last = await follow(
                url,
                first,
                connector=connector,
                transport=transport,
                deadline=limits.deadline,
                redirected=redirected,
                session=session,
                context=policy.context,
                page_url=page_url,
            )
    except Failed as failed:
        return Sent(failed.last, failed.error, Route(pool, proxy, session, traffic.bytes))
    return Sent(last, None, Route(pool, proxy, session, traffic.bytes))


def _usable(session: SiteSession | None, pool: ProxyPool | None) -> SiteSession | None:
    """`session` while the pool still serves its site through its endpoint (`ProxyPool.carries`: not quarantined, a
    sticky exit not ended, one of its fetches spent): its cookies were earned from that endpoint's address. A session
    left out gets back the fetch `SiteSessions.take` spent on it, as it serves none."""
    if session is None or pool is None or session.proxy is None:
        return session
    if pool.carries(session.registrable_domain, session.proxy):
        return session
    session.fetches_left += 1
    return None


async def follow(
    url: str,
    first: Hop,
    *,
    connector: SafeConnector,
    transport: Transport,
    deadline: float,
    redirected: Redirected,
    session: SiteSession | None = None,
    context: RequestContext | None = None,
    page_url: str | None = None,
) -> Wire:
    """The final response for `url`, every hop checked by the guard first, at most `MAX_REDIRECTS` followed.
    Cookies set by one hop go with the next whose host and path they match, and none outlive the fetch (a shared
    jar's fetch, `cookies.shared`, spans several requests); a
    site `session`'s cookies are held from the start. The caller's `context` cookies are held from the start as
    host-only cookies of the page's host (`page_url`'s, else `url`'s), and its headers go with every hop to that host
    only. Raises `Failed`."""
    target = await _checked(connector.check(url), deadline, None)
    cookies, shared = await fetch_jar()
    # In a shared jar, what earlier requests' responses set is newer than this request's seeds.
    keep = cookies.held() if shared else frozenset()
    if session is not None:
        cookies.seed(session.cookies, keep)
    caller = _Caller.of(page_url or url, context)
    cookies.seed(caller.cookies, keep)
    hop = first
    response = await _send(transport, target, hop, cookies, caller)
    for hops in range(MAX_REDIRECTS + 1):
        location = redirect_location(response.status, response.response_headers)
        if location is None:
            break
        if hops == MAX_REDIRECTS:
            raise Failed("too_many_redirects", response)
        target_next = await _checked(connector.redirect(target, location), deadline, response)
        hop, target = redirected(hop, response.status, target_next), target_next
        response = await _send(transport, target, hop, cookies, caller)
    return response


# An origin (RFC 6454 §4): the scheme and host, lowercased, and the port, the scheme's default when the URL names none.
type _Origin = tuple[str, str, int]
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}  # RFC 9110 §4.2.1, §4.2.2


def _origin_of(url: str) -> _Origin | None:
    parts = urlsplit(url)
    scheme, host = parts.scheme.lower(), parts.hostname
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    return None if host is None or port is None else (scheme, host, port)


@dataclass(frozen=True)
class _Caller:
    """The caller's context as a fetch applies it: the origins it goes to, its headers and its cookies."""

    origins: frozenset[_Origin] = frozenset()
    headers: HeaderList = ()
    cookies: tuple[SessionCookie, ...] = ()

    @classmethod
    def of(cls, url: str, context: RequestContext | None) -> "_Caller":
        origin = _origin_of(url)
        if context is None or origin is None:
            return cls()
        scheme, host, _ = origin
        headers = tuple((name, value.get_secret_value()) for name, value in context.headers)
        # Host-only cookies of the fetched URL's host (RFC 6265 §5.3 step 6), for every path, ending with the fetch;
        # given for an https page, they go over https only (`secure`, RFC 6265 §5.4 step 1).
        cookies = tuple(
            SessionCookie(
                name=name,
                value=value.get_secret_value(),
                domain=host,
                path="/",
                secure=scheme == "https",
                host_only=True,
                expires=None,
            )
            for name, value in context.cookies
        )
        # An http page's own upgrade to https (RFC 9110 §4.2.2) is its host still, over a channel that only adds
        # protection.
        upgrade = {("https", host, 443)} if scheme == "http" else set()
        return cls(frozenset({origin, *upgrade}), headers, cookies)

    def headers_for(self, url: str) -> HeaderList:
        """The caller's headers for a request to `url`: only to the origin they were given for (RFC 6454 §4: scheme,
        host and port) or its https upgrade, so a redirect to another host, port or a cleartext scheme carries none of
        them (as RFC 9110 §15.4 leaves credentials to the user agent's judgement)."""
        return self.headers if _origin_of(url) in self.origins else ()


async def _checked(check: Awaitable[Target], deadline: float, last: Wire | None) -> Target:
    """Run one guard check within the fetch's time budget: name resolution can take as long as the request."""
    try:
        async with asyncio.timeout(deadline - time.monotonic()):
            return await check
    except SsrfRejected as e:
        raise Failed(str(e), last) from e
    except TimeoutError as e:
        raise Failed("timeout", last) from e


async def _send(transport: Transport, target: Target, hop: Hop, cookies: FetchCookies, caller: _Caller) -> Wire:
    # The fetch sends only its own cookies, and the guard's host is the request's (RFC 9110 §7.2).
    headers = {name: value for name, value in hop.headers.items() if name.lower() not in ("cookie", "host")}
    # The caller's headers replace the profile's and the hop's of the same name in any case, keeping the profile's
    # position.
    for name, value in caller.headers_for(target.url):
        set_field(headers, name, value)
    if (cookie := cookies.header(target.url)) is not None:
        headers["Cookie"] = cookie
    response = await transport(target, replace(hop, headers=headers))
    cookies.extract(target.url, response.status, response.response_headers)
    return response


class _Session(AsyncSession[Response]):
    """curl_cffi's session, releasing a request's handle from the event loop's multi handle before its callbacks.

    curl_cffi 0.16 frees a handle's callback data first and removes the handle from the multi handle second. For a
    request still in flight (cancelled by an outer time cap), the removal closes its connection, and curl reports
    that to the debug callback (`DEBUGFUNCTION`, which records the headers sent), whose data is gone by then: the
    process dies in native code. Removed first, the handle's events reach live callbacks; curl_cffi's own removal
    then finds it removed, which `curl_multi_remove_handle` answers with success.
    """

    def release_curl(self, curl: Curl) -> None:
        if not self._closed:
            self.acurl.remove_handle(curl)
        super().release_curl(curl)


@asynccontextmanager
async def curl_transport(
    impersonate: Impersonation, proxy: ProxyEndpoint | None, limits: Limits, traffic: Traffic
) -> AsyncIterator[Transport]:
    """A transport over one curl_cffi session for one fetch, through `proxy` when a pool carries it, adding the bytes
    each request moves to `traffic`; every request sends the impersonation's headers unless it sets its own."""
    # Cookies are kept by `FetchCookies`, not libcurl's engine, which this build runs without the public-suffix list
    # (RFC 6265 §5.3 step 5): the session discards what curl parsed, and each request clears curl's list before
    # sending only the cookies passed in. `trust_env=False` stops curl_cffi reading the proxy environment variables;
    # libcurl reads them itself, so each request also sets CURLOPT_PROXY, and a pool request CURLOPT_NOPROXY:
    # proxies come only from the pools. HTTP/2 only over TLS, negotiated by ALPN (RFC 9113 §3.2), as Chrome speaks it:
    # curl's default HTTP/2 would also offer a cleartext request the `h2c` upgrade (RFC 7540 §3.2, which RFC 9113 §3.1
    # deprecates), sending `Upgrade`, `HTTP2-Settings` and `Connection` headers no browser sends.
    async with _Session(
        impersonate=impersonate.target, http_version=CurlHttpVersion.V2TLS, trust_env=False, discard_cookies=True
    ) as session:
        yield partial(_request, session, impersonate.headers, proxy, limits, traffic)


async def _request(
    session: AsyncSession[Response],
    defaults: HeaderList,
    proxy: ProxyEndpoint | None,
    limits: Limits,
    traffic: Traffic,
    target: Target,
    hop: Hop,
) -> Wire:
    remaining = limits.deadline - time.monotonic()
    if remaining <= 0:
        raise Failed("timeout", None)

    body = io.BytesIO()
    received = 0

    def on_body(chunk: bytes) -> int:
        # curl hands over the body after content decoding, so this counts decompressed bytes: a compression bomb
        # stops here, holding at most the cap in memory.
        nonlocal received
        received += len(chunk)
        if received > limits.max_decompressed_bytes:
            return CURL_WRITEFUNC_ERROR
        return body.write(chunk)

    sent: list[bytes] = []

    def on_debug(kind: int, data: bytes) -> None:
        if kind in _MESSAGE_BYTES:
            traffic.bytes += len(data)
        if kind == CURLINFO_HEADER_OUT:
            sent.append(data)

    options: dict[CurlOpt, object] = {
        **(pinned(target) if proxy is None else _through(proxy, target)),
        # curl speaks only the schemes the guard admits, whatever URL reaches it (CURLOPT_PROTOCOLS_STR).
        CurlOpt.PROTOCOLS_STR: "http,https",
        CurlOpt.MAXFILESIZE_LARGE: limits.max_body_bytes,  # counts body bytes as received, before decoding
        # The debug callback is the only place curl reports the request headers it sent (impersonation sets them in
        # C). It also sees every other debug event, a cost accepted for recording what was sent.
        CurlOpt.VERBOSE: 1,
        CurlOpt.DEBUGFUNCTION: on_debug,
    }
    if hop.order is not None:
        # Applied after the impersonation, whose order it replaces (CURLOPT_HTTPHEADER_ORDER, curl-impersonate).
        options[CurlOpt.HTTPHEADER_ORDER] = ",".join(hop.order)
    # curl_cffi types option values as str, but passes each to curl_easy_setopt with the option's own type.
    session.curl_options = cast(dict[CurlOpt, str], options)
    try:
        response = await session.request(
            hop.method,
            target.url,
            headers=_merged(defaults, hop.headers),
            data=hop.body,
            allow_redirects=False,
            timeout=remaining,
            content_callback=on_body,
        )
    except RequestException as e:
        error = BODY_CAP if received > limits.max_decompressed_bytes else reason(e)
        # The exception carries what arrived before the failure: the status and headers, if any.
        partial_response: Response | None = e.response
        last = None if partial_response is None else _wire(target, partial_response, sent, b"")
        raise Failed(error, last) from e
    return _wire(target, response, sent, body.getvalue())


def set_field(headers: dict[str, str | None], name: str, value: str | None) -> None:
    """Set `name` in `headers`, replacing a field of the same name in any case (RFC 9110 §5.1) in its place and
    spelling, as the impersonated browser sends it."""
    same = [key for key in headers if key.lower() == name.lower()]
    for duplicate in same[1:]:
        del headers[duplicate]
    headers[same[0] if same else name] = value


def _merged(defaults: HeaderList, headers: Mapping[str, str | None]) -> dict[str, str | None]:
    """`defaults` with `headers` set over them, a field of the same name in any case replaced in the default's spelling
    (RFC 9110 §5.1). A name goes out as spelled here, which HTTP/1.1 keeps (RFC 9112 §5) and HTTP/2 lowercases
    (RFC 9113 §8.2.1)."""
    given = {name.lower() for name in headers}
    spelled = {name.lower(): name for name, _ in defaults}
    return {
        **{name: value for name, value in defaults if name.lower() not in given},
        **{spelled.get(name.lower(), name): value for name, value in headers.items()},
    }


def reason(error: RequestException) -> str:
    """A curl failure's reason: its kind, never its message, which can hold the URL."""
    return _CURL_ERRORS.get(error.code, f"network_error: curl {int(error.code)}")


def pinned(target: Target) -> dict[CurlOpt, object]:
    addresses = ",".join(bracketed(str(ip)) for ip in target.addresses)
    return {
        # Every connection of this request goes to the checked addresses, whatever host name curl derives from the
        # URL (IDNA, case, percent-encoding): CURLOPT_RESOLVE's `*` host matches every name on the port, and curl
        # tries the next address when one can't be reached.
        CurlOpt.RESOLVE: [f"*:{target.port}:{addresses}"],
        CurlOpt.PROXY: "",  # an empty proxy disables proxies, including the environment's (CURLOPT_PROXY)
    }


def _through(proxy: ProxyEndpoint, target: Target) -> dict[CurlOpt, object]:
    ip = bracketed(str(target.ip))
    options: dict[CurlOpt, object] = {
        # The proxy's host alone, with its type and port set apart: curl's CURLPROXY_HTTP (0) speaks plain HTTP to
        # the proxy, the only kind a pool is (`snowcrawl.core.proxy`); pages inside the tunnel keep their own TLS.
        CurlOpt.PROXY: bracketed(proxy.host),
        CurlOpt.PROXYPORT: proxy.port,
        CurlOpt.PROXYTYPE: _CURLPROXY_HTTP,
        # A CONNECT tunnel for http pages too (RFC 9110 §9.3.6): the proxy relays bytes and never rewrites a request.
        CurlOpt.HTTPPROXYTUNNEL: 1,
        # The CONNECT names the first checked address, not the host: a proxy would resolve a name itself, to an address
        # the guard never checked. CURLOPT_CONNECT_TO's empty host matches every name on the port, and the request's
        # Host and TLS server name keep the URL's host. No CURLOPT_RESOLVE: it would also redirect the connection to
        # the proxy.
        CurlOpt.CONNECT_TO: _slist(f":{target.port}:{ip}:{target.port}"),
        # An empty list overrides the environment's `no_proxy`, which would otherwise send matching hosts direct,
        # resolved by curl rather than the guard (CURLOPT_NOPROXY).
        CurlOpt.NOPROXY: "",
    }
    if proxy.username is not None:
        # Set apart from the proxy URL, so the credentials need no percent-encoding (CURLOPT_PROXYUSERNAME).
        options[CurlOpt.PROXYUSERNAME] = proxy.username
        options[CurlOpt.PROXYPASSWORD] = proxy.password or ""
    return options


def _slist(*items: str) -> object:
    """A `curl_slist` of `items`, freed once unreferenced, for a list option curl_cffi builds no list for (it does for
    CURLOPT_RESOLVE and the header lists). The session's options hold it while the request runs, and the handle is
    reset when the request ends (`AsyncSession.release_curl`), so curl keeps no pointer to it after."""
    head = ffi.NULL
    for item in items:
        head = lib.curl_slist_append(head, item.encode())
    return ffi.gc(head, lib.curl_slist_free_all)


def _wire(target: Target, response: Response, sent: list[bytes], body: bytes) -> Wire:
    return Wire(
        url=target.url,
        status=response.status_code,
        request_headers=_parse_request_headers(sent[-1] if sent else b""),
        response_headers=tuple((name, value or "") for name, value in response.headers.multi_items()),
        body=body,
    )


def _parse_request_headers(block: bytes) -> HeaderList:
    # curl reports the header block it sent in HTTP/1.1 form for every protocol version: a request line, then
    # `name: value` lines (RFC 9112 §3).
    lines = block.decode("latin-1").split("\r\n")[1:]
    return tuple((name, value.strip()) for name, _, value in (line.partition(":") for line in lines if ":" in line))
