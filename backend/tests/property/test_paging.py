"""The paging driver's stop and keep rules over a simulated tab: whatever the list's length, its batch size,
the row target and the action cap, the run stops for a stated reason, overshoots the target by less than one batch,
never passes its cap, and keeps the grown document once. A list that recycles its items while it grows keeps every
item in some kept state; a list whose items hold no link is counted by the rows of its calls.

The simulated tabs serve listings as markup the driver parses like any page; nothing runs a browser.
"""

import asyncio
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from snowcrawl.acquire.browser_tab import Click, Drive, Ref
from snowcrawl.acquire.politeness import HostLimiter
from snowcrawl.acquire.rungs import Rungs
from snowcrawl.acquire.ssrf import SafeConnector
from snowcrawl.acquire.types import BrowserActions, FetchPolicy, FetchResult, PagedFetch
from snowcrawl.config_ir.api_recipe.execute import unserved
from snowcrawl.crawl.paging import Caps, collect
from snowcrawl.services.fetching import Fetching, grown
from snowcrawl.services.polite import HostRules
from snowcrawl.storage.capture_bundle import NetworkBody
from snowcrawl.storage.fs import FsStore

_URL = "https://shop.example.test/list"
_WAIT_S, _POLL_S = 0.02, 0.001  # fast waits: the simulated tab changes at once


class _LoadMoreTab:
    def __init__(self, total: int, batch: int) -> None:
        self.total, self.batch = total, batch
        self.shown = min(total, batch)
        self.changes = 0
        self.requests = 0
        self.kept: list[int] = []  # the items each kept state showed

    @property
    def url(self) -> str:
        return _URL

    @property
    def landed(self) -> str:
        return _URL

    async def html(self) -> str:
        cards = "".join(f'<li class="card"><a href="/item/{i}">{i}</a></li>' for i in range(self.shown))
        control = "<button>+</button>" if self.shown < self.total else ""
        return f"<html><body><main><ul>{cards}</ul>{control}</main></body></html>"

    async def mutations(self) -> int:
        return self.changes

    async def areas(self, elements: Sequence[Ref]) -> list[float]:
        return [1.0] * len(elements)

    async def click(self, element: Ref) -> Click:
        if element[1] != "button" or self.shown >= self.total:
            return "gone"
        self.shown = min(self.total, self.shown + self.batch)
        self.changes += 1
        self.requests += 1
        return "clicked"

    async def scroll(self) -> None:
        return None

    async def goto(self, url: str, timeout_s: float) -> bool:
        return False

    async def back(self) -> None:
        return None

    def data_requests(self) -> int:
        return self.requests

    async def keep(self) -> bool:
        self.kept.append(self.shown)
        return True

    def discard(self) -> None:
        self.kept.pop()

    async def calls(self) -> tuple[tuple[NetworkBody, bytes], ...]:
        return ()


class _ScrollTab(_LoadMoreTab):
    """A listing that grows by `batch` items per scroll and shows at most `window` of them, the last ones (a
    virtualised list recycles the elements of items scrolled past), or all of them when `window` is None. Each kept
    state records the items it showed."""

    def __init__(self, total: int, batch: int, window: int | None) -> None:
        super().__init__(total, batch)
        self.window = window
        self.states: list[set[int]] = []

    def _range(self) -> range:
        start = 0 if self.window is None else max(0, self.shown - self.window)
        return range(start, self.shown)

    async def html(self) -> str:
        cards = "".join(f'<li class="card"><a href="/item/{i}">{i}</a></li>' for i in self._range())
        return f"<html><body><main><ul>{cards}</ul></main></body></html>"

    async def click(self, element: Ref) -> Click:
        return "gone"

    async def scroll(self) -> None:
        if self.shown < self.total:
            self.shown = min(self.total, self.shown + self.batch)
            self.changes += 1

    async def keep(self) -> bool:
        self.states.append(set(self._range()))
        return True

    def discard(self) -> None:
        self.states.pop()


async def _open(url: str | None) -> bool:
    return True


@settings(deadline=None, max_examples=60)
@given(
    total=st.integers(3, 120),
    batch=st.integers(3, 30),
    records=st.integers(1, 150),
    cap=st.integers(1, 50),
)
def test_a_load_more_run_stops_for_its_reason_within_one_batch_and_its_cap(
    total: int, batch: int, records: int, cap: int
) -> None:
    tab = _LoadMoreTab(total, batch)
    caps = Caps(actions=cap, wait_s=_WAIT_S, poll_s=_POLL_S)

    report = asyncio.run(collect(tab, BrowserActions(records=records), _open, time.monotonic() + 60, caps))

    assert report.actions <= cap
    assert report.items == tab.shown
    assert tab.kept == [tab.shown]
    assert report.pages == 1
    match report.stop:
        case "records":
            assert records <= report.items < records + batch
        case "end":
            assert report.items == total
        case "action_cap":
            assert report.actions == cap
        case "no_mechanism":  # the first batch was the whole list: there is no control to click
            assert report.items == total <= batch
        case other:
            raise AssertionError(other)
    if report.stop != "records" or report.items > batch:
        assert report.mechanism in (None, "load_more")


class _ElsewhereTab(_ScrollTab):
    """A scroll listing the caller's steps navigated to another host before the driver runs."""

    @property
    def url(self) -> str:
        return "https://elsewhere.example.test/list"


def test_a_listing_whose_tab_left_its_host_is_not_driven(tmp_path: Path) -> None:
    tab = _ElsewhereTab(30, 5, None)

    async def listing(url: str, policy: FetchPolicy, drive: Drive, _deadline: float) -> PagedFetch:
        report = await drive(tab)
        return PagedFetch(pages=(unserved(url, policy, "test", "error", "unused"),), report=report)

    async def unused(url: str, policy: FetchPolicy) -> FetchResult:
        raise AssertionError("no page is fetched")

    async def admit(*, after: timedelta = timedelta(0)) -> bool:
        return True

    fetching = Fetching(
        Rungs(http=unused, browser=unused, listing=listing), SafeConnector(), FsStore(tmp_path), HostLimiter()
    )
    actions = BrowserActions(records=10_000, hint="scroll")

    paged = asyncio.run(grown(fetching, _URL, FetchPolicy(), actions, time.monotonic() + 60, admit, HostRules()))

    # The listing's host's robots.txt and rate say nothing of another host's (RFC 9309 §2.3: rules are per origin).
    assert paged.report is not None
    assert (paged.report.stop, tab.shown) == ("not_admitted", 5)
