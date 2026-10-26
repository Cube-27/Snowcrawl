"""JSON over HTTP, for adapters and API recipes (the acquisition rung), over the HTTP
rung's client (`curl_client`): the same Chrome impersonation, SSRF guard, caps, cookie jar, proxy pools and
reasons. The capture is recorded under the page the JSON stands for.

The request is what that page's own script would send with `fetch()`: the impersonation's headers, less the
two only a navigation sends (Upgrade Insecure Requests §3.2; Fetch Metadata §2.4 `Sec-Fetch-User`), with the Fetch
Metadata of a CORS request for no destination (W3C Fetch Metadata Request Headers §2: `Sec-Fetch-Mode: cors`,
`Sec-Fetch-Dest: empty`, `Sec-Fetch-Site` from the page to the endpoint), the `Referer` the default referrer policy
gives (W3C Referrer Policy §3, `strict-origin-when-cross-origin`) and an `Origin` where Fetch §3.1 sends one (a CORS
request to another origin, or any request that is not a GET). A recipe's own headers come last and win. The page's
site session, if any (`acquire.session`), lends its cookies to a request in the
site's registrable domain.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit, urlunsplit

from snowcrawl.acquire.challenge import CHALLENGED, challenge_label
from snowcrawl.acquire.classify import classify
from snowcrawl.acquire.curl_client import DEFAULT_PORTS, Hop, Transport, origin, send, set_field
from snowcrawl.acquire.impersonate import Impersonation
from snowcrawl.acquire.locality import exit_place, language_headers
from snowcrawl.acquire.outcome import Content, Sending
from snowcrawl.acquire.public_suffix import default_suffixes
from snowcrawl.acquire.session import SessionScope
from snowcrawl.acquire.ssrf import SafeConnector, Target
from snowcrawl.acquire.types import BlockClass, FetchPolicy, FetchResult
from snowcrawl.core import json_text
from snowcrawl.core.proxy import UNDECLARED, ExitPlace, ProxyPool
from snowcrawl.storage.capture_bundle import HeaderList, decode_html

__all__ = ["ACCEPT", "JsonFetcher", "JsonRequest", "answer_class", "answer_label", "fetch_json"]

# RFC 8259 §11: the JSON media type.
ACCEPT = "application/json"
# Fetch §5.2 "extract a body": a string body's type when the request names none.
_TEXT_BODY = "text/plain;charset=UTF-8"
_CONTENT_TYPE = "content-type"
# Fetch §4.4 step 12 (HTTP-redirect fetch): the request-body-header names, dropped with the body.
_BODY_HEADERS = frozenset({"content-encoding", "content-language", "content-location", _CONTENT_TYPE})
SiteRelation = Literal["same-origin", "same-site", "cross-site"]
# Fetch Metadata §2.1: across a redirect chain the value is the least related hop's.
_RELATION_ORDER: Final[tuple[SiteRelation, ...]] = ("same-origin", "same-site", "cross-site")
# The order in which Chromium sends a script's `fetch()` headers, as the browser rung's Chromium sends them to a
# header echo service; the impersonation profile describes a navigation's. A header absent from a request is skipped.
FETCH_HEADER_ORDER: Final = (
    "host",
    "content-length",
    "sec-ch-ua-platform",
    "user-agent",
    "sec-ch-ua",
    "content-type",
    "sec-ch-ua-mobile",
    "accept",
    "origin",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "referer",
    "accept-encoding",
    "accept-language",
    "cookie",
    "priority",
)


@dataclass(frozen=True)
class JsonRequest:
    url: str  # the endpoint
    page_url: str  # the page the JSON stands for: the capture's `url`; the endpoint is its `final_url`
    method: Literal["GET", "POST"] = "GET"
    # Sent after the fetch's own headers, replacing a field of the same name; never Host or Cookie.
    headers: HeaderList = ()
    body: str | None = None  # UTF-8 text, a POST's only
    # The endpoint answers XML or HTML, not JSON (a feed, a page embedding data): a 2xx answer is `ok` whatever its
    # body, and the adapter that asked reads it into the JSON its shapes describe (`adapters.answer`).
    raw: bool = False


JsonFetcher = Callable[[JsonRequest, FetchPolicy], Awaitable[FetchResult]]


async def fetch_json(
    request: JsonRequest,
    policy: FetchPolicy,
    *,
    engine_version: str,
    impersonate: Impersonation,
    connector: SafeConnector | None = None,
    pools: Mapping[str, ProxyPool] | None = None,
    transport: Transport | None = None,
    scope: SessionScope | None = None,
    direct: ExitPlace = UNDECLARED,
) -> FetchResult:
    """`pools` are the configured proxy pools; the one `policy.proxy_pool` names carries every request, from its place
    (`direct` without a pool), whose locale the request's language falls back to. `transport`
    replaces the network (tests). `scope` is the fetch's site state (`SiteSessions.scope_for`): the page's site's
    session goes with a request in its registrable domain, and is dropped when it is `blocked`."""
    sending = Sending(request.page_url, "api", policy, engine_version, scope)
    sent = await send(
        request.url,
        _first_hop(request, language_headers(policy.locale_hint, exit_place(policy.proxy_pool, pools, direct))),
        policy,
        impersonate=impersonate,
        connector=connector or SafeConnector(),
        pools=pools,
        redirected=_Redirect(request.page_url),
        transport=transport,
        session=sending.session(request.url),
        page_url=request.page_url,
    )
    last, error, challenge = sent.last, sent.error, None
    block_class: BlockClass = "error"
    body = b""
    if error is None and last is not None:
        block_class, error = await asyncio.to_thread(answer_class, last.status, last.body, request.raw)
        body = last.body
        if block_class in CHALLENGED:
            challenge = await asyncio.to_thread(answer_label, last.status, last.response_headers, body, request.url)
    return sending.finish(
        exchange=None if last is None else last.exchange,
        content=Content(body=body),
        block_class=block_class,
        error=error,
        challenge=challenge,
        route=sent.route,
    )


def answer_label(status: int, headers: HeaderList, body: bytes, url: str) -> str | None:
    """The challenge label of a refused answer to a request to `url`, read from its markup, decoded as a page's is
    (`decode_html`)."""
    return challenge_label(status, headers, decode_html(body, headers, "api"), url)


def answer_class(status: int, body: bytes, raw: bool) -> tuple[BlockClass, str | None]:
    """The block class of an answer, and its error: a 2xx answer is `ok` when it is JSON (RFC 8259), or whatever its
    body for a `raw` request; any other status follows the status rules."""
    if 200 <= status < 300:
        if raw:
            return "ok", None
        try:
            json_text.parse(body)
        except ValueError:
            return "error", "decode_error"
        return "ok", None
    # A 1xx or an unfollowed 3xx (RFC 9110 §15.2, §15.4) answers nothing; otherwise the status rules apply, and the
    # body is not HTML, so it is never a shell.
    if status < 400:
        return "error", "unexpected_status"
    return classify(status, (), ""), None


def _first_hop(request: JsonRequest, language: Mapping[str, str]) -> Hop:
    # The page's language goes with its script's calls, as the browser rung's context sends it (`acquire.locality`).
    headers: dict[str, str | None] = {**language}
    headers.update(_fetch_headers(request.page_url, request.url, request.method, "same-origin"))
    for name, value in request.headers:
        set_field(headers, name, value)
    if request.body is not None and not any(name.lower() == _CONTENT_TYPE for name in headers):
        headers["Content-Type"] = _TEXT_BODY
    body = None if request.body is None else request.body.encode()
    return Hop(request.method, headers, body, FETCH_HEADER_ORDER)


def _fetch_headers(page_url: str, url: str, method: str, before: SiteRelation) -> dict[str, str | None]:
    """The headers a `fetch()` from `page_url` sends to `url` (module docstring); `before` is the least related hop
    of the redirect chain so far."""
    relation = max(before, site_relation(page_url, url), key=_RELATION_ORDER.index)
    headers: dict[str, str | None] = {
        "Accept": ACCEPT,
        "Sec-Fetch-Site": relation,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-User": None,
        "Upgrade-Insecure-Requests": None,
        "Referer": referrer(page_url, url),
        # RFC 9218 §4: urgency 1, incremental, as Chromium sends for a script's fetch (a navigation is urgency 0).
        "Priority": "u=1, i",
    }
    if method != "GET" or origin(page_url) != origin(url):
        headers["Origin"] = _serialized_origin(page_url)
    return headers


@dataclass(frozen=True)
class _Redirect:
    """The hop after a redirect: WHATWG Fetch §4.4 (HTTP-redirect fetch) step 12 makes a 303, or a 301 or 302 after a
    POST, a GET without its body and body headers; 307 and 308 repeat the request. The Fetch Metadata, `Referer` and
    `Origin` are those of the new target."""

    page_url: str

    def __call__(self, hop: Hop, status: int, target: Target) -> Hop:
        method, body = hop.method, hop.body
        headers = dict(hop.headers)
        if status == 303 or (status in (301, 302) and hop.method == "POST"):
            method, body = "GET", None
            headers = {name: value for name, value in headers.items() if name.lower() not in _BODY_HEADERS}
        before = _relation_of(headers.get("Sec-Fetch-Site"))
        for name, value in _fetch_headers(self.page_url, target.url, method, before).items():
            set_field(headers, name, value)
        if method == "GET" and origin(self.page_url) == origin(target.url):
            set_field(headers, "Origin", None)
        return Hop(method, headers, body, FETCH_HEADER_ORDER)


def _relation_of(value: str | None) -> SiteRelation:
    return next((relation for relation in _RELATION_ORDER if relation == value), "same-origin")


def _serialized_origin(url: str) -> str:
    """RFC 6454 §6.2: scheme "://" host, and ":" port unless it is the scheme's default."""
    scheme, host, port = origin(url)
    netloc = f"[{host}]" if ":" in host else host
    return f"{scheme}://{netloc}" if port == DEFAULT_PORTS.get(scheme) else f"{scheme}://{netloc}:{port}"


def site_relation(page_url: str, url: str) -> SiteRelation:
    """Fetch Metadata §2.1: `same-origin` for equal origins, `same-site` for the same scheme and site (HTML Standard
    §7.1.1 "same site": the registrable domain of the Public Suffix List, or the host itself where it has none),
    else `cross-site`."""
    if origin(page_url) == origin(url):
        return "same-origin"
    (scheme_a, host_a, _), (scheme_b, host_b, _) = origin(page_url), origin(url)
    suffixes = default_suffixes()
    if scheme_a == scheme_b and suffixes.site(host_a) == suffixes.site(host_b):
        return "same-site"
    return "cross-site"


def referrer(page_url: str, url: str) -> str | None:
    """The `Referer` under `strict-origin-when-cross-origin` (Referrer Policy §3.9, the default since its §8.3):
    the page's URL without fragment or credentials (§8.4 "strip url") to its own origin, only its origin elsewhere,
    and none from an https page to an http URL."""
    page = urlsplit(page_url)
    if page.scheme.lower() == "https" and urlsplit(url).scheme.lower() == "http":
        return None
    if origin(page_url) == origin(url):
        netloc = page.netloc.rpartition("@")[2]
        return urlunsplit((page.scheme, netloc, page.path or "/", page.query, ""))
    return _serialized_origin(page_url) + "/"
