"""A listing's browser actions on the browser rung: the rendered page as a `Tab` that a
paging driver reads and acts on, and the document states it keeps, each a fetch result of its own.

The page is opened, navigated, settled and read exactly as `fetch_browser` does it (`browser.rendered`,
`browser.Render`), in the process's shared browser session, so the launch, the profile and the identity the page sees
are the same, and the render ends as a page's does (`Render.finish`). Everything the tab reads or
does runs in Patchright's isolated world, which shares the document but not the page's globals, and it writes nothing
into the page's DOM: elements are named by element-index XPath paths into the serialized document (`/*[1]/*[2]`), which
the driver computes from its own parse. Nothing is typed and no form is filled; a click is a pointer click on an element
the driver chose, and one an overlay intercepts is reported, not worked around.

What to do is the driver's (`crawl.paging`, above `acquire` in the layering): `fetch_listing` hands it the tab.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, cast

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import Request
from patchright.async_api import TimeoutError as PlaywrightTimeout

from snowcrawl.acquire.browser import Render, Rendered, classified, markup, read_state, rendered
from snowcrawl.acquire.browser_capture import AREAS_JS, DATA_REQUESTS, JSON_BODIES_MAX, JSON_PAGE_MAX_BYTES, json_bodies
from snowcrawl.acquire.browser_session import BrowserSession, Failed, context_cookies
from snowcrawl.acquire.outcome import Content, Exchange, fetch_result
from snowcrawl.acquire.page_steps import steps_budget_s
from snowcrawl.acquire.session import SessionScope
from snowcrawl.acquire.ssrf import SafeConnector
from snowcrawl.acquire.types import ActionsReport, BlockClass, FetchPolicy, FetchResult, PagedFetch, StepOutcome
from snowcrawl.core.proxy import ProxyPool
from snowcrawl.storage.capture_bundle import NetworkBody

__all__ = ["Click", "Drive", "ListingRung", "Ref", "Tab", "fetch_listing"]

# An element of the serialized document: its element-index XPath path (`/*[1]/*[2]`) and its tag.
Ref = tuple[str, str]

# What a click came to: done, refused by something over the element (an overlay) or by the element not becoming
# clickable in time, or the element is no longer in the document.
Click = Literal["clicked", "intercepted", "gone"]

# Playwright's actionability wait for a click (visible, stable, receiving events) ends after this long.
_CLICK_TIMEOUT_MS: Final = 4_000
# A browser action's navigation (to a next page, or back) waits this long for its document.
_NAVIGATION_TIMEOUT_MS: Final = 15_000
# HTML Standard §8.1.6 (navigation), RFC 9110 §15.5: a document answered 4xx or 5xx is no next page.
_ERROR_STATUS: Final = 400
# The element an element-index XPath path names (DOM Standard `document.evaluate`, XPath 1.0), when it has the tag the
# driver read there, else null: libxml2 may build a serialized document's tree differently from the browser.
_ELEMENT_JS: Final = """([path, tag]) => {
  const node = document.evaluate(path, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  return node !== null && node.nodeType === Node.ELEMENT_NODE && node.localName === tag ? node : null;
}"""
# How many changes to the document's structure and text there have been (DOM Standard §4.3, a MutationObserver on
# `childList` and `characterData`), counted from the first call in each document.
_MUTATIONS_JS: Final = """() => {
  if (globalThis.__mutations === undefined) {
    globalThis.__mutations = 0;
    new MutationObserver((records) => { globalThis.__mutations += records.length; })
      .observe(document, {subtree: true, childList: true, characterData: true});
  }
  return globalThis.__mutations;
}"""
# CSSOM View `scrollTo`: first to a viewport and a half above the end, then to the end, so that content which loads
# when a sentinel near the end enters the viewport sees it enter.
_NEAR_END_JS: Final = (
    "() => window.scrollTo(0, Math.max(0, document.scrollingElement.scrollHeight - 1.5 * innerHeight))"
)
_END_JS: Final = "() => window.scrollTo(0, document.scrollingElement.scrollHeight)"
_SCROLL_STEP_S: Final = 0.2


class Tab(Protocol):
    """The rendered page as the paging driver sees it."""

    @property
    def url(self) -> str:
        """The URL of the document the page shows now."""
        ...

    @property
    def landed(self) -> str:
        """The URL where the listing's own navigation ended (its redirects followed), before the caller's steps ran."""
        ...

    async def html(self) -> str | None:
        """The document serialized as it is now; None while it is being replaced."""
        ...

    async def mutations(self) -> int | None:
        """The changes to the document's structure and text so far (another document counts afresh); None while the
        document is being replaced."""
        ...

    async def areas(self, elements: Sequence[Ref]) -> list[float]:
        """The rendered area of each element; 0 for one not found or not rendered visible."""
        ...

    async def click(self, element: Ref) -> Click: ...

    async def scroll(self) -> None:
        """Scroll the document to its end."""
        ...

    async def goto(self, url: str, timeout_s: float) -> bool:
        """Navigate to `url`, waiting at most `timeout_s` (and the tab's own navigation cap); whether a document other
        than an error arrived."""
        ...

    async def back(self) -> None:
        """Go back to the previous document, after a click that left it."""
        ...

    def data_requests(self) -> int:
        """How many fetch, XMLHttpRequest and document requests the page has sent so far."""
        ...

    async def keep(self) -> bool:
        """Keep the document as it is now as a capture of its own; False when it could not be read within the caps."""
        ...

    def discard(self) -> None:
        """Drop the state kept last, which a later one holds whole: its data responses go to the next state kept."""
        ...

    async def calls(self) -> tuple[tuple[NetworkBody, bytes], ...]:
        """The JSON bodies of the page's latest data responses, as many as a capture keeps at most."""
        ...


# The driver: what to do with the tab, answering what it did. The caller binds the request's settings to it.
Drive = Callable[[Tab], Awaitable[ActionsReport]]
# `fetch_listing` bound to a process's shared browser, guard, proxy pools and site sessions: a listing's URL, its
# policy and the driver.
ListingRung = Callable[[str, FetchPolicy, Drive, float], Awaitable[PagedFetch]]


class _PatchrightTab:
    def __init__(
        self,
        page: Rendered,
        *,
        url: str,
        engine_version: str,
        started: float,
        steps: tuple[StepOutcome, ...],
        landed: str | None = None,
    ) -> None:
        self._page = page
        self.landed = page.page.url if landed is None else landed
        self._steps = steps  # the caller's page steps, run on the first document; its state's attempt reports them
        self._url = url  # the URL the shown document was asked for; `_at` is where that navigation ended
        self._at = page.page.url
        self._engine_version = engine_version
        self._since = started  # the time the next kept state's attempt is counted from
        self._calls_from = 0
        self._requests = 0
        self.kept: list[FetchResult] = []
        # Each kept state's start: the time its attempt is counted from and its first data response, for `discard`.
        self._starts: list[tuple[float, int]] = []
        self._bodies: dict[int, tuple[NetworkBody, bytes] | None] = {}  # the latest data responses read, by index
        page.page.on("request", self._on_request)

    def _on_request(self, request: Request) -> None:
        # Images and fonts are not counted (`DATA_REQUESTS`): items shown from the DOM still load theirs.
        if request.resource_type in DATA_REQUESTS:
            self._requests += 1

    @property
    def url(self) -> str:
        return self._page.page.url

    async def html(self) -> str | None:
        try:
            return await self._page.page.content()
        except PlaywrightError:
            return None

    async def mutations(self) -> int | None:
        try:
            return cast(int, await self._page.page.evaluate(_MUTATIONS_JS))
        except PlaywrightError:
            return None

    async def areas(self, elements: Sequence[Ref]) -> list[float]:
        try:
            areas = cast(list[float], await self._page.page.evaluate(AREAS_JS, [list(e) for e in elements]))
        except PlaywrightError:
            return [0.0] * len(elements)
        return [max(area, 0.0) for area in areas]  # an element no longer found has no area here

    def _act(self) -> None:
        """Count a browser action: the data responses that arrive from now on followed it (`NetworkBody.action`)."""
        self._page.calls.actions += 1

    async def click(self, element: Ref) -> Click:
        self._act()
        try:
            handle = await self._page.page.evaluate_handle(_ELEMENT_JS, list(element))
        except PlaywrightError:
            return "gone"
        found = handle.as_element()
        try:
            if found is None:
                return "gone"
            await found.click(timeout=_CLICK_TIMEOUT_MS)
            return "clicked"
        except PlaywrightTimeout:
            return "intercepted"
        except PlaywrightError:
            return "gone"
        finally:
            with contextlib.suppress(PlaywrightError):
                await handle.dispose()

    async def scroll(self) -> None:
        self._act()
        with contextlib.suppress(PlaywrightError):
            await self._page.page.evaluate(_NEAR_END_JS)
            await asyncio.sleep(_SCROLL_STEP_S)
            await self._page.page.evaluate(_END_JS)

    async def goto(self, url: str, timeout_s: float) -> bool:
        timeout_ms = max(1.0, min(_NAVIGATION_TIMEOUT_MS, 1000 * timeout_s))  # 0 would mean no timeout
        self._act()
        try:
            response = await self._page.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError:
            return False
        finally:
            self._url, self._at = url, self._page.page.url
        return response is not None and response.status < _ERROR_STATUS

    async def back(self) -> None:
        self._act()
        with contextlib.suppress(PlaywrightError):
            await self._page.page.go_back(wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT_MS)

    def data_requests(self) -> int:
        return self._requests

    async def keep(self) -> bool:
        fetched_at = datetime.now(UTC)
        # The data responses up to now belong to this state; one arriving while it is read goes to the next.
        calls_to = len(self._page.calls.responses)
        try:
            exchange, content = await read_state(self._page, url=self.url, calls=(self._calls_from, calls_to))
        except (Failed, PlaywrightError):
            return False
        block_class = await classified(exchange, markup(content), self._page.policy.target_types)
        self.kept.append(self._result(fetched_at, exchange, content, block_class))
        self._starts.append((self._since, self._calls_from))
        self._since = time.monotonic()
        self._calls_from = calls_to
        return True

    def discard(self) -> None:
        if self.kept:
            self.kept.pop()
            self._since, self._calls_from = self._starts.pop()

    async def calls(self) -> tuple[tuple[NetworkBody, bytes], ...]:
        responses = self._page.calls.responses
        # The page goes on answering calls while bodies are read: this read covers the responses there are now.
        count = len(responses)
        first = max(0, count - JSON_BODIES_MAX)
        for index in [index for index in self._bodies if index < first]:
            del self._bodies[index]
        for index in range(first, count):
            # A body still arriving is left for a later read (`DataCalls.done`).
            if index not in self._bodies and self._page.calls.finished(responses[index][0]):
                self._bodies[index] = self._within_cap(
                    next(iter(await json_bodies([responses[index]], self._page.response.url)), None)
                )
        found = (self._bodies.get(index) for index in range(first, count))
        return tuple(body for body in found if body is not None)

    def _within_cap(self, body: tuple[NetworkBody, bytes] | None) -> tuple[NetworkBody, bytes] | None:
        """`body`, unless the bodies held with it would pass the page's cap (`JSON_PAGE_MAX_BYTES`), which
        `json_bodies` applies within one read only: each response is read on its own here."""
        if body is None:
            return None
        held = sum(len(data) for _, data in filter(None, self._bodies.values()))
        return body if held + len(body[1]) <= JSON_PAGE_MAX_BYTES else None

    def _result(
        self, fetched_at: datetime, exchange: Exchange, content: Content, block_class: BlockClass
    ) -> FetchResult:
        # A document a click or a script navigated to was asked for under its own URL.
        return fetch_result(
            url=self._url if self.url == self._at else self.url,
            method="browser",
            policy=self._page.policy,
            engine_version=self._engine_version,
            fetched_at=fetched_at,
            started=self._since,
            exchange=exchange,
            content=content,
            block_class=block_class,
            error=None,
            steps=() if self.kept else self._steps,
        )


async def fetch_listing(
    url: str,
    policy: FetchPolicy,
    drive: Drive,
    deadline: float,
    *,
    session: BrowserSession,
    engine_version: str,
    connector: SafeConnector | None = None,
    pools: Mapping[str, ProxyPool] | None = None,
    scope: SessionScope | None = None,
) -> PagedFetch:
    """Render the listing at `url` as `fetch_browser` does and, when it is reached (`ok`), let `drive` act on it; the
    answer holds the document states the driver kept (at least one) and its report. A page not reached is the one
    page, with no report, labelled with the challenge it presents. The navigation is bounded by
    `policy.browser_timeout_s`; the driver stops acting at `deadline` (`time.monotonic()`), and what follows the
    navigation ends by then and a page's own time to render and be read: a browser that stops answering ends the
    listing as a `timeout` rather than holding its slot. The render ends as `fetch_browser`'s does
    (`Render.finish`, with its `scope`), and the navigation's attempt (the first
    page's) carries the bytes the whole run moved (`Traffic`)."""
    run = Render(url, policy, engine_version, scope)
    paged = await _listing(run, drive, deadline, session, connector or SafeConnector(), pools)
    if paged is None:
        first = run.result()
        paged = PagedFetch(pages=(first,), report=None)
    else:
        first = paged.pages[0]
        navigation = first.attempts[0].model_copy(update={"transferred": run.traffic.bytes})
        first = first.model_copy(update={"attempts": (navigation, *first.attempts[1:])})
        paged = paged.model_copy(update={"pages": (first, *paged.pages[1:])})
    await run.finish(first, session)
    return paged


async def _listing(
    run: Render,
    drive: Drive,
    deadline: float,
    session: BrowserSession,
    connector: SafeConnector,
    pools: Mapping[str, ProxyPool] | None,
) -> PagedFetch | None:
    """The document states the driver kept and its report; None when the page was not reached or not kept, `run`
    then holding its one outcome."""
    url, policy = run.url, run.policy
    if not run.route(pools):
        return None
    try:
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(policy.browser_timeout_s + steps_budget_s(policy.steps)):
                page = await stack.enter_async_context(rendered(url, policy, session, connector, run))
                landed = page.page.url
                await run.read(page)  # the caller's steps first, then the paging driver reads the page
            if run.block_class != "ok":
                return None
            tab = _PatchrightTab(
                page, url=url, engine_version=run.engine_version, started=run.started, steps=run.steps, landed=landed
            )
            async with asyncio.timeout(max(0.0, deadline - time.monotonic()) + policy.browser_timeout_s):
                report = await _driven(drive, tab)
                # The driver's actions may have set cookies: the site's sessions are told those the context holds now.
                run.cookies = await context_cookies(page.page.context)
                if not tab.kept and not await tab.keep():
                    return None
            return PagedFetch(pages=tuple(tab.kept), report=report.model_copy(update={"pages": len(tab.kept)}))
    except Failed as failed:
        run.failed(failed.error, failed.exchange)
    except TimeoutError:
        run.failed("timeout")
    return None


async def _driven(drive: Drive, tab: _PatchrightTab) -> ActionsReport:
    """The driver's report; a browser failure the tab did not absorb (the page crashed) ends it with `error`, keeping
    what was kept."""
    try:
        return await drive(tab)
    except PlaywrightError:
        return ActionsReport(mechanism=None, stop="error", actions=0, pages=len(tab.kept), items=0)
