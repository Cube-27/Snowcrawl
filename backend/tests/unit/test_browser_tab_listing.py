"""A listing's browser fetch (`fetch_listing`) when reading its first state fails after its page steps ran: the
failure's attempt still says how each step ended; and when its browser stops answering, it ends by its deadline."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from snowcrawl.acquire import browser, browser_tab
from snowcrawl.acquire.browser_session import Failed
from snowcrawl.acquire.types import FetchPolicy, StepOutcome

URL = "https://shop.example.test/c/1"


def test_a_failed_first_read_keeps_the_outcomes_of_the_steps_that_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked = (StepOutcome(action="click", outcome="done"),)

    @asynccontextmanager
    async def rendered(*_: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(page=SimpleNamespace(url="https://shop.example.test/list"), temporary=False)

    async def stepped(_page: Any) -> tuple[StepOutcome, ...]:
        return clicked

    async def read_state(_page: Any) -> Any:
        raise Failed("body_too_large", None)

    async def drive(_tab: Any) -> Any:
        raise AssertionError("a page not read is not driven")

    monkeypatch.setattr(browser_tab, "rendered", rendered)
    monkeypatch.setattr(browser, "stepped", stepped)
    monkeypatch.setattr(browser, "read_state", read_state)
    session: Any = SimpleNamespace()

    listing = browser_tab.fetch_listing(
        URL, FetchPolicy(), drive, time.monotonic(), session=session, engine_version="test"
    )
    paged = asyncio.run(listing)

    (page,) = paged.pages
    assert (page.block_class, page.attempts[0].error) == ("error", "body_too_large")
    assert page.attempts[0].steps == clicked


def test_a_driver_whose_browser_stops_answering_ends_the_listing_by_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def rendered(*_: Any) -> AsyncIterator[Any]:
        page = SimpleNamespace(url="https://shop.example.test/list", on=lambda *_: None)
        yield SimpleNamespace(page=page, temporary=False)

    async def read(run: browser.Render, _page: Any, **_: Any) -> None:
        run.block_class = "ok"

    async def drive(_tab: Any) -> Any:
        await asyncio.Event().wait()  # a browser call that never returns

    monkeypatch.setattr(browser_tab, "rendered", rendered)
    monkeypatch.setattr(browser.Render, "read", read)
    session: Any = SimpleNamespace()
    policy = FetchPolicy(browser_timeout_s=0.1)

    listing = browser_tab.fetch_listing(URL, policy, drive, time.monotonic(), session=session, engine_version="test")
    paged = asyncio.run(asyncio.wait_for(listing, 5))

    (page,) = paged.pages
    assert (page.block_class, page.attempts[0].error) == ("error", "timeout")
