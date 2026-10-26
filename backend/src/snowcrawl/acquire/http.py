"""HTTP fetch with curl_cffi and Chrome TLS impersonation, through `curl_client`, with a
new curl session per fetch.

Redirects are followed by hand so the SSRF guard checks every hop, and each request connects only to the
address the guard checked. Through a proxy pool, which resolves names itself, the guard still checks each hop's host
before it is requested. Every failure becomes an `error` outcome with a short reason that never
contains the URL. The request is a navigation, sent with the impersonation's headers (`acquire.impersonate`), in
the request locale's language as the browser rung asks for it (`acquire.locality`), and with a site session's cookies
where the site has one (`acquire.session`).
"""

import asyncio
from collections.abc import Mapping
from functools import partial
from typing import cast

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from snowcrawl.acquire.challenge import CHALLENGED, block_class_of, challenge_label
from snowcrawl.acquire.classify import Redirect, unread_media
from snowcrawl.acquire.curl_client import BODY_CAP, Hop, pinned, reason, send
from snowcrawl.acquire.impersonate import Impersonation
from snowcrawl.acquire.locality import exit_place, language_headers
from snowcrawl.acquire.outcome import Content, Sending
from snowcrawl.acquire.session import SessionScope
from snowcrawl.acquire.ssrf import SafeConnector, Target
from snowcrawl.acquire.types import BlockClass, FetchPolicy, FetchResult
from snowcrawl.core.proxy import UNDECLARED, ExitPlace, ProxyPool
from snowcrawl.storage.capture_bundle import decode_html


async def fetch_http(
    url: str,
    policy: FetchPolicy,
    *,
    engine_version: str,
    impersonate: Impersonation,
    connector: SafeConnector | None = None,
    pools: Mapping[str, ProxyPool] | None = None,
    scope: SessionScope | None = None,
    direct: ExitPlace = UNDECLARED,
) -> FetchResult:
    """`pools` are the configured proxy pools; the one `policy.proxy_pool` names carries every request, from its place
    (`direct` without a pool: the machine's), whose locale the request's language falls back to. `scope` is the fetch's
    site state (`SiteSessions.scope_for`): the site's session, if any, goes with the fetch, and is dropped when it is
    `blocked`."""
    sending = Sending(url, "http", policy, engine_version, scope)
    sent = await send(
        url,
        Hop("GET", language_headers(policy.locale_hint, exit_place(policy.proxy_pool, pools, direct))),
        policy,
        impersonate=impersonate,
        connector=connector or SafeConnector(),
        pools=pools,
        redirected=_follow,
        session=sending.session(url),
    )
    last, challenge, text = sent.last, None, None
    block_class: BlockClass = "error"
    if sent.error is None and last is not None:
        # The body is decoded once (`decode_html`), and every rule reads that text. Classifying parses HTML
        # synchronously; a thread keeps the event loop free.
        text = decode_html(last.body, last.response_headers, "http")
        redirect = Redirect(url, last.exchange.url, policy.page_kind)
        block_class = await asyncio.to_thread(
            partial(block_class_of, types=policy.target_types, content_only=policy.content_only),
            last.status,
            last.response_headers,
            text,
            policy.page_kind,
            redirect,
        )
        if block_class in CHALLENGED:
            # The document's links resolve against the URL that served it (RFC 3986 §5), the last hop's.
            served = last.exchange.url
            label = partial(challenge_label, block_class=block_class)
            challenge = await asyncio.to_thread(label, last.status, last.response_headers, text, served)
    elif sent.error == BODY_CAP and last is not None and unread_media(last.status, last.response_headers):
        # The body was stopped, which no page reader would read: its status and media type class the answer.
        block_class = "unsupported_media"
    return sending.finish(
        exchange=None if last is None else last.exchange,
        content=Content(body=b"" if last is None else last.body, text=text),
        block_class=block_class,
        error=sent.error,
        challenge=challenge,
        route=sent.route,
    )


def _follow(hop: Hop, _status: int, _target: Target) -> Hop:
    """A navigation's redirect is followed with a plain GET (RFC 9110 §15.4), in the navigation's language."""
    return Hop("GET", hop.headers)


class RequestFailed(Exception):
    """A request got no response: the reason never contains the URL."""


async def post(url: str, body: bytes, headers: Mapping[str, str], *, connector: SafeConnector, timeout_s: float) -> int:
    """POST `body` to `url` and return the response status. The guard checks `url` and the request connects only to
    the checked address; a redirect is returned as its status, never followed, so the body goes to no other target.
    The response body is discarded as it arrives. Raises `SsrfRejected`, or `RequestFailed`."""
    target = await connector.check(url)
    async with AsyncSession(trust_env=False) as session:
        options: dict[CurlOpt, object] = {**pinned(target), CurlOpt.PROTOCOLS_STR: "http,https"}
        session.curl_options = cast(dict[CurlOpt, str], options)
        try:
            response = await session.post(
                target.url,
                data=body,
                headers=dict(headers),
                allow_redirects=False,
                timeout=timeout_s,
                content_callback=len,
            )
        except RequestException as e:
            raise RequestFailed(reason(e)) from e
    status: int = response.status_code
    return status
