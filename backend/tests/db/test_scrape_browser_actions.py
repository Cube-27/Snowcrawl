"""A scrape's browser actions: the listing rung's kept document states each become a capture with its own
extraction, the answer lists their records once each with the actions' report, and robots.txt is asked first."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.browser_tab import Drive
from snowcrawl.acquire.rungs import Rungs
from snowcrawl.acquire.types import ActionsReport, FetchPolicy, FetchResult, PagedFetch
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.services.fetching import Fetching
from snowcrawl.services.scrape import ScrapeRequest, scrape
from tests.db.conftest import listing_schema
from tests.db.support import fetching_from

pytestmark = pytest.mark.anyio

REPORT = ActionsReport(mechanism="pagination", stop="records", actions=1, pages=2, items=4)


@dataclass
class Listing:
    """The listing rung: answers with `pages` and `REPORT`, and records what it was asked."""

    fetch_result: Callable[..., FetchResult]
    pages: list[tuple[str, bytes]] = field(default_factory=list)
    asked: list[tuple[str, FetchPolicy]] = field(default_factory=list)
    drives: list[Drive] = field(default_factory=list)

    async def __call__(self, url: str, policy: FetchPolicy, drive: Drive, _deadline: float) -> PagedFetch:
        self.asked.append((url, policy))
        self.drives.append(drive)
        results = tuple(
            self.fetch_result(url, 200, body, method="browser", policy=policy, final_url=final)
            for final, body in self.pages
        )
        return PagedFetch(pages=results, report=REPORT)


@pytest.fixture
def listing(fetch_result: Callable[..., FetchResult]) -> Listing:
    return Listing(fetch_result)


@pytest.fixture
def fetching(fetch_result: Callable[..., FetchResult], listing: Listing, own_origin: str, tmp_path: Path) -> Fetching:
    robots = f"{own_origin}/robots.txt"

    async def http(url: str, policy: FetchPolicy) -> FetchResult:
        assert url == robots, "a listing with browser actions is fetched by the listing rung only"
        return fetch_result(url, 200, b"User-agent: *\nDisallow: /private\n")

    return fetching_from(Rungs(http=http, browser=http, listing=listing), tmp_path)


def _request(url: str, schema_id: object, **fields: Any) -> ScrapeRequest:
    body = {"url": url, "formats": ["records"], "schema": {"id": str(schema_id)}, "page_kind": "listing", **fields}
    return ScrapeRequest.model_validate(body)


async def test_a_listing_robots_txt_disallows_is_not_fetched(
    engine: AsyncEngine,
    fetching: Fetching,
    listing: Listing,
    own_origin: str,
    schema_name: str,
    engine_data: EngineData,
) -> None:
    schema = await listing_schema(engine, schema_name)

    result = await scrape(
        engine,
        fetching,
        _request(f"{own_origin}/private/kettles", schema.id, browser_actions={}, fetch={"respect_robots": True}),
        data=engine_data,
    )

    assert (result.outcome, result.capture_id, result.browser_actions, listing.asked) == ("blocked", None, None, [])
