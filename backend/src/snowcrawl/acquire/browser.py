"""Browser fetch with Patchright headless Google Chrome: one navigation,
the one bounded wait (`acquire.browser_wait`), and what the render captures besides its document
(`acquire.browser_capture`), in a tab of the site's profile (`acquire.browser_session`).

As in `fetch_http`, every failure becomes an `error` outcome with a short reason that never contains the URL.
A page is read once it is ready (`settle`); every request the page makes loads, as in a user's browser (no request
interception). The caller's page steps run after that wait and before the read (`run_steps`), within the browser's
time cap plus their own timeouts; a page's fetch then scrolls it through (`acquire.browser_scroll`) within its own.
Both browser rungs, the page (`fetch_browser`) and the listing with browser actions
(`acquire.browser_tab.fetch_listing`), render through `rendered` and end through `Render`: the first document is
classified with the redirect rules, a `blocked` or `shell` one labelled with the challenge it presents
(`acquire.challenge`), the pool endpoint it went through told how it went (`core.proxy_health`), a `blocked` profile
deleted, and the outcome recorded with the process's site sessions (`acquire.session`, browser to HTTP only). Asked to
export (`CookieExport`), the fetch reads the context's cookies before the context closes, without changing them, for a
site session.
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import Page, Response
from patchright.async_api import TimeoutError as PlaywrightTimeout

from snowcrawl.acquire.browser_capture import (
    ARTIFACT_TIMEOUT_S,
    DataCalls,
    Resources,
    capture_artifacts,
    document_html,
    header_list,
    item_areas,
    json_bodies,
)
from snowcrawl.acquire.browser_scroll import SCROLL_MAX_S, scroll_back, scroll_through, watch
from snowcrawl.acquire.browser_session import (
    CLOSE_MAX_S,
    BrowserSession,
    ContextKey,
    Failed,
    OpenProfile,
    context_cookies,
    context_key,
    context_locality,
    open_context,
    present,
)
from snowcrawl.acquire.browser_wait import STEP_QUIET_S, MainFrame, called, ms_until, settle, until
from snowcrawl.acquire.challenge import CHALLENGED, block_class_of, challenge_label, count
from snowcrawl.acquire.classify import Redirect
from snowcrawl.acquire.headers import recorded
from snowcrawl.acquire.outcome import Content, Exchange, Traffic, fetch_result, report_proxy
from snowcrawl.acquire.page_steps import run_steps, steps_budget_s
from snowcrawl.acquire.public_suffix import site_of
from snowcrawl.acquire.session import CookieExport, SessionCookie, SessionScope
from snowcrawl.acquire.socks_guard import SocksGuard
from snowcrawl.acquire.ssrf import RejectReason, SafeConnector, SsrfRejected, Target
from snowcrawl.acquire.types import BlockClass, FetchPolicy, FetchResult, StepOutcome
from snowcrawl.core.capture import CaptureMethod
from snowcrawl.core.proxy import ProxyEndpoint, ProxyPool, UnknownProxyPool, pool_endpoint, proxy_pool
from snowcrawl.storage.capture_bundle import decode_html

__all__ = [
    "HeldProfile",
    "Render",
    "Rendered",
    "classified",
    "fetch_browser",
    "markup",
    "navigation_error",
    "presented",
    "read_state",
    "rendered",
    "site_profile",
    "stepped",
]

# RFC 9110 §15.5, §15.6: a 4xx or 5xx document; the profile that rendered one is not reused (`_Profiles`).
_ERROR_STATUS = 400
# Chromium's network error names (net/base/net_error_list.h) carry no URL.
_NET_ERROR = re.compile(r"net::ERR_[A-Z0-9_]+")


@dataclass
class Render:
    """One render of a page by either browser rung, as its result is made: the route it went through (`route`), and
    what its first document came to (`read`, else `failed`). `finish` ends it."""

    url: str
    policy: FetchPolicy
    engine_version: str
    scope: SessionScope | None = None  # the fetch's site state (`SiteSessions.scope_for`): None for none
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started: float = field(default_factory=time.monotonic)
    timings: dict[str, int] = field(default_factory=dict)
    traffic: Traffic = field(default_factory=Traffic)
    pool: ProxyPool | None = None
    proxy: ProxyEndpoint | None = None
    exchange: Exchange | None = None
    content: Content = field(default_factory=lambda: Content(body=b""))
    block_class: BlockClass = "error"
    error: str | None = None
    challenge: str | None = None
    cookies: tuple[SessionCookie, ...] = ()
    steps: tuple[StepOutcome, ...] = ()
    artifacts: dict[str, bytes] = field(default_factory=dict)
    temporary: bool = False  # it rendered in a temporary profile (`acquire.browser_profiles`)

    def route(self, pools: Mapping[str, ProxyPool] | None) -> bool:
        """The proxy pool `policy.proxy_pool` names and its endpoint for the URL; False (a failure) for an unknown
        pool."""
        try:
            self.pool = proxy_pool(self.policy.proxy_pool, pools)
        except UnknownProxyPool as unknown:
            self.failed(str(unknown))
            return False
        self.proxy = pool_endpoint(self.pool, site_of(self.url))
        return True

    async def read(self, page: "Rendered", *, scroll: bool = False) -> None:
        """Run the caller's page steps on the ready page and, with `scroll`, scroll it through (`scroll_through`), then
        read its document, the context's cookies, and classify it with the redirect rules of where its navigation led
        (`classified`), labelled with its challenge."""
        self.temporary = page.temporary
        if scroll:
            await watch(page.page)  # the caller's steps may scroll too
        self.steps = await stepped(page)  # kept by a failure after the steps ran too
        if scroll:
            await scroll_through(page.page, page.frame, page.calls)
        self.exchange, content = await read_state(page)
        self.cookies = await context_cookies(page.page.context)
        text = markup(content)
        self.content = replace(content, text=text)
        redirect = Redirect(self.url, self.exchange.url, self.policy.page_kind)
        self.block_class = await classified(self.exchange, text, self.policy.target_types, redirect)
        self.challenge = await challenge_of(self.block_class, self.exchange, text, self.cookies)

    def failed(self, error: str, exchange: Exchange | None = None) -> None:
        self.block_class, self.error, self.challenge = "error", error, None
        if exchange is not None:
            self.exchange = exchange

    def result(self, method: CaptureMethod = "browser") -> FetchResult:
        """The render's result; `method` is `api` for a call sent from a warm page (`acquire.browser_send`)."""
        return fetch_result(
            url=self.url,
            method=method,
            policy=self.policy,
            engine_version=self.engine_version,
            fetched_at=self.fetched_at,
            started=self.started,
            exchange=self.exchange,
            content=self.content,
            block_class=self.block_class,
            error=self.error,
            # `context` is the browser context's wall time, which `browser_seconds` usage counts.
            timings=self.timings,
            challenge=self.challenge,
            transferred=self.traffic.bytes,
            steps=self.steps,
            artifacts=self.artifacts,
        )

    async def finish(self, first: FetchResult, session: BrowserSession) -> None:
        """Record how the render went, `first` being the result of its first document: its challenge label is counted,
        the pool endpoint told, a `blocked` profile deleted, and with a `scope`, the site's sessions updated
        (`SiteSessions.after_browser`)."""
        count(self.challenge, "browser")
        report_proxy(self.pool, self.proxy, first)
        if first.block_class == "blocked" and (key := context_key(self.url, self.scope, self.proxy)) is not None:
            await session.profiles.discard(key)
        if self.scope is not None:
            await self.scope.after_browser(self.url, self.cookies, first, temporary=self.temporary)


async def fetch_browser(
    url: str,
    policy: FetchPolicy,
    *,
    session: BrowserSession,
    engine_version: str,
    connector: SafeConnector | None = None,
    pools: Mapping[str, ProxyPool] | None = None,
    export: CookieExport | None = None,
    scope: SessionScope | None = None,
) -> FetchResult:
    """`pools` are the configured proxy pools; the one `policy.proxy_pool` names carries every connection. `export`,
    when given, receives the context's cookies and the endpoint, whatever the outcome; the caller decides whether a
    session opens. `scope` is the fetch's site state (`SiteSessions.scope_for`): the site's kept profile, a temporary
    profile's starting cookies (`SiteSessions.for_browser`), and the sessions the outcome is recorded with
    (`SiteSessions.after_browser`); without one the page renders in a temporary profile that reads and writes none."""
    run = Render(url, policy, engine_version, scope)
    if run.route(pools):
        try:
            extra_s = steps_budget_s(policy.steps) + SCROLL_MAX_S + (ARTIFACT_TIMEOUT_S if policy.captures_page else 0)
            async with (
                asyncio.timeout(policy.browser_timeout_s + extra_s),
                rendered(url, policy, session, connector or SafeConnector(), run) as page,
            ):
                await run.read(page, scroll=True)
                if policy.screenshot == "viewport":
                    await scroll_back(page.page)  # the viewport the page first showed
                run.artifacts = await capture_artifacts(page.page, policy)
            if export is not None:
                export.cookies, export.proxy = run.cookies, run.proxy
        except Failed as failed:
            run.failed(failed.error, failed.exchange)
        except TimeoutError:
            run.failed("timeout")
    result = run.result()
    await run.finish(result, session)
    return result


async def stepped(page: "Rendered") -> tuple[StepOutcome, ...]:
    """Run the policy's page steps on the ready page, each change followed by the wait for the data calls it started
    (`called`), within the step's settle bound. Each step is a browser action: the calls answered after it carry its
    number (`NetworkBody.action`, 0 the page's load)."""
    if not page.policy.steps:
        return ()

    async def step_settle(within_s: float) -> None:
        answered = page.calls.answered
        begun = time.monotonic()
        deadline = begun + within_s

        def done() -> bool:
            # A step that starts no data call (a click that only shows what was loaded) is settled once quiet.
            quiet = not page.calls.in_flight and time.monotonic() - begun >= STEP_QUIET_S
            return quiet or called(page.calls, answered)

        await until(page.frame, done, within_s, deadline)

    def begun() -> None:
        page.calls.actions += 1

    return await run_steps(page.page, page.policy.steps, step_settle, lambda: len(page.calls.in_flight), begun)


def markup(content: Content) -> str:
    """The rendered document's text, decoded once (`decode_html`: the browser serializes the DOM as UTF-8)."""
    return decode_html(content.body, (), "browser")


async def challenge_of(
    block_class: BlockClass, exchange: Exchange, text: str, cookies: tuple[SessionCookie, ...]
) -> str | None:
    """The challenge label of a `CHALLENGED` document (`acquire.challenge`), from its response headers, the names of
    the cookies the context holds (its capture records no `Set-Cookie`) and its markup `text`, whose links resolve
    against the URL that served it (RFC 3986 §5), past any redirect."""
    if block_class not in CHALLENGED:
        return None
    names = tuple(cookie.name for cookie in cookies)
    status, headers = exchange.status, exchange.response_headers
    return await asyncio.to_thread(challenge_label, status, headers, text, exchange.url, names)


async def classified(
    exchange: Exchange, text: str, types: tuple[str, ...], redirect: Redirect | None = None
) -> BlockClass:
    """The block class of a rendered document from its markup `text`, the schema.org `types` the task reads it for
    (the policy's `target_types`), and from where its navigation led (`classify`'s redirect rules), when given. classify
    parses HTML synchronously; a thread keeps the event loop free. Without the page kind or `content_only`: the listing
    and content rules call a page a shell for the browser to render, and the browser is the last rung, so its page is
    extracted whatever its links (rows from captured JSON are read then)."""
    status, headers = exchange.status, exchange.response_headers
    return await asyncio.to_thread(partial(block_class_of, types=types), status, headers, text, None, redirect)


@dataclass
class Rendered:
    """A page whose navigation has settled: the tab, its main frame's navigations, the first document's response, the
    policy it was fetched with, and the data responses it has loaded so far."""

    page: Page
    frame: MainFrame
    response: Response
    policy: FetchPolicy
    calls: DataCalls = field(default_factory=DataCalls)
    resources: Resources = field(default_factory=Resources)
    temporary: bool = False  # its profile is a temporary one (`acquire.browser_profiles`)


@dataclass
class HeldProfile:
    """A site profile taken for one fetch (`site_profile`): its browser, the guard's check of the URL it was taken
    for, its key (None: a temporary profile), and whether it is kept for the site's next fetch (`keep`), else closed."""

    context: OpenProfile
    first: Target
    key: ContextKey | None
    keep: bool = False


@asynccontextmanager
async def site_profile(
    url: str, policy: FetchPolicy, session: BrowserSession, connector: SafeConnector, run: Render
) -> AsyncIterator[HeldProfile]:
    """The site's profile for a fetch of `url`, taken while the block runs: the open one its last fetch left
    (`_Profiles`), else the profile opened (a temporary one starting with the cookies of `run.scope`), through `run`'s
    proxy endpoint. Kept when the block sets `keep`, else closed; the profile's wall time is `run.timings["context"]`,
    and the bytes the guard relays meanwhile are added to `run.traffic`. Raises `Failed`."""
    try:
        first = await connector.check(url)  # also refuses `file:`, `data:` and other non-web schemes
    except SsrfRejected as e:
        raise Failed(str(e), None) from e
    key = context_key(url, run.scope, run.proxy)
    locale = context_locality(policy, session, run.pool)
    held = None if key is None else await session.profiles.take(key, locale, connector)
    if held is None:
        await session.profiles.room()
        try:
            held = await open_context(url, policy, session, connector, first, run.proxy, run.scope, key, locale)
        except BaseException:
            session.profiles.busy -= 1
            raise
    relayed = held.traffic.bytes
    opened = time.monotonic()
    taken = HeldProfile(held, first, key)
    try:
        yield taken
    finally:
        run.traffic.bytes += held.traffic.bytes - relayed
        run.timings["context"] = round((time.monotonic() - opened) * 1000)
        if taken.keep and key is not None:
            await session.profiles.keep(key, held)
        else:
            await session.profiles.release(held)


@asynccontextmanager
async def rendered(
    url: str,
    policy: FetchPolicy,
    session: BrowserSession,
    connector: SafeConnector,
    run: Render,
) -> AsyncIterator[Rendered]:
    """The page at `url`, once it is ready to read (`settle`), in a new tab of the site's profile (`site_profile`). The
    tab closes when the block ends, and the profile's browser with it unless it is kept for the site's next page: when
    the page ended on the site with no error status. Raises `Failed`."""
    async with site_profile(url, policy, session, connector, run) as held:
        page = await held.context.context.new_page()
        shown: Rendered | None = None
        try:
            await presented(page, session)
            shown = await _navigate(page, url, held.first, policy, held.context.guard, run.started)
            shown.temporary = held.context.profile.lock is None
            yield shown
            latest = shown.frame.latest or shown.response
            held.keep = latest.status < _ERROR_STATUS and context_key(latest.url, run.scope, run.proxy) == held.key
        finally:
            if shown is not None:
                shown.calls.cancel()
            try:
                async with asyncio.timeout(CLOSE_MAX_S):
                    await page.close()
            except PlaywrightError:
                pass
            except TimeoutError:
                held.keep = False  # the tab stays open in the profile: no later page shares it


async def presented(page: Page, session: BrowserSession) -> None:
    """The browser's identity set on the new tab before it navigates (`present`); a tab that cannot take it is not
    rendered."""
    try:
        await present(page, session)
    except PlaywrightError as e:
        raise Failed("render_error", None) from e


async def _navigate(
    page: Page, url: str, first: Target, policy: FetchPolicy, guard: SocksGuard, started: float
) -> Rendered:
    frame = MainFrame(page, first.url)
    calls, resources = DataCalls(mode=policy.calls), Resources()
    page.on("request", resources.on_request)
    page.on("response", resources.on_response)
    page.on("websocket", lambda _: resources.on_websocket())
    page.on("request", frame.on_request)
    page.on("response", frame.on_response)
    page.on("requestfailed", frame.on_failed)
    page.on("framenavigated", frame.on_navigated)
    page.on("load", lambda _: frame.on_load())
    page.on("domcontentloaded", lambda _: frame.on_parsed())
    page.on("request", lambda request: calls.on_request(request, page.url))
    page.on("requestfinished", calls.on_done)
    page.on("requestfailed", calls.on_done)
    page.on("response", lambda response: calls.on_response(response, page.main_frame))

    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=ms_until(started + policy.browser_timeout_s)
        )
    except PlaywrightError as e:
        raise Failed(navigation_error(e, guard.rejection_for(frame.last_hop)), None) from e
    if response is None:  # only a same-document navigation has none, and a new page has no document yet
        raise Failed("no_response", None)
    # A page that never becomes ready is read as it is when the wait ends.
    await settle(page, frame, calls, policy, started)
    return Rendered(page, frame, response, policy, calls, resources)


async def read_state(
    rendered: Rendered, *, url: str | None = None, calls: tuple[int, int | None] = (0, None)
) -> tuple[Exchange, Content]:
    """The document the page shows now, as an exchange and its content: the first document, or where a navigation
    ended (one it started itself, or a browser action's), under `url` when given (the response's URL otherwise), with
    the JSON and Flight bodies of the data responses in the `calls` slice (all of them by default) whose requests are
    done. Raises `Failed` for a body past the policy's caps, or a response the browser no longer holds."""
    policy = rendered.policy
    response = rendered.frame.latest or rendered.response
    try:
        sent, received = await asyncio.gather(response.request.headers_array(), response.headers_array())
    except PlaywrightError as e:
        raise Failed("no_response", None) from e
    exchange = Exchange(
        url=url or response.url,
        status=response.status,
        request_headers=recorded(header_list(sent)),
        response_headers=recorded(header_list(received)),
    )
    # The size the body arrived with is read before any body, so a body over `max_body_bytes` never enters
    # this process. The browser hands over bodies after content decoding, in full (Playwright has no streaming read):
    # the decompressed cap is checked once they are read, and bounds what is stored.
    if await _wire_size(response, exchange) > policy.max_body_bytes:
        raise Failed("body_cap", exchange)
    body, raw_body, network = await asyncio.gather(
        _rendered(rendered.page, exchange),
        _raw_body(response),
        json_bodies(
            [entry for entry in rendered.calls.responses[slice(*calls)] if rendered.calls.finished(entry[0])],
            rendered.response.url,
            policy.calls,
            rendered.calls.early,
        ),
    )
    if max(len(body), len(raw_body or b"")) > policy.max_decompressed_bytes:
        raise Failed("body_cap", exchange)
    areas = await item_areas(rendered.page, body) if policy.page_kind == "listing" else None
    resources = dict(sorted(rendered.resources.counts.items()))
    return exchange, Content(body=body, raw_body=raw_body, network=network, item_areas=areas, resources=resources)


async def _rendered(page: Page, exchange: Exchange) -> bytes:
    """The rendered document with its open shadow roots (`browser_capture.document_html`)."""
    try:
        return (await document_html(page)).encode()
    except PlaywrightError as e:
        raise Failed("render_error", exchange) from e


async def _raw_body(response: Response) -> bytes | None:
    """The navigation response body, or None when the browser no longer holds it."""
    try:
        return await response.body()
    except PlaywrightError:
        return None


async def _wire_size(response: Response, exchange: Exchange) -> int:
    """The navigation response body's size on the wire, before content decoding."""
    try:
        return (await response.request.sizes())["responseBodySize"]
    except PlaywrightError as e:
        raise Failed("network_error", exchange) from e


def navigation_error(error: PlaywrightError, rejected: RejectReason | None) -> str:
    if isinstance(error, PlaywrightTimeout):
        return "timeout"
    if rejected is not None:
        return str(SsrfRejected(rejected))
    match = _NET_ERROR.search(error.message)
    return f"network_error: {match.group()}" if match else "network_error"
