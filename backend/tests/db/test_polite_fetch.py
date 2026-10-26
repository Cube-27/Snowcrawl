"""Fetching a task's page politely: robots.txt, the site's rate and backoff through the queue (`polite_fetch`
waiting as a worker does, `QUEUED`, and what the task ends as: `page_task.queued_end`)."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.politeness import Admit, HostLimiter
from snowcrawl.acquire.robots import RobotsSnapshot
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.db.models import Job, JobId, Project, Site, SiteId, UrlTask
from snowcrawl.db.queue import ClaimedTask, Failed, Finished, TaskResult, claim_tasks
from snowcrawl.services.page_task import queued_end
from snowcrawl.services.polite import QUEUED, HostRules, PageFetch, Turns, polite_fetch, rules_of
from tests.db.support import Web as SharedWeb

pytestmark = pytest.mark.anyio

ORIGIN = "https://shop.example.test"
ROBOTS = "User-agent: *\nDisallow: /private/\n"
PAGE = b"<html><body><h1>Item</h1></body></html>"
DEFAULT_PROJECT = select(Project.id).where(Project.name == "default").scalar_subquery()
OBEYS = FetchPolicy(respect_robots=True)


@dataclass
class Web(SharedWeb):
    """`SharedWeb`, which is a page fetch too: its `Admit` goes unused, it makes one request."""

    async def __call__(self, url: str, _admit: Admit | None = None, _known: HostRules | None = None) -> FetchResult:
        return await self.fetch("http", url, FetchPolicy())


@pytest.fixture
def web(fetch_result: Callable[..., FetchResult]) -> Web:
    site = Web(fetch_result)
    site.robots(ORIGIN, ROBOTS.encode())
    return site


@pytest.fixture
async def site_id(engine: AsyncEngine) -> SiteId:
    """The site of `ORIGIN`: its row and robots.txt snapshot are written in transactions of their own (`rules_of`)."""
    values = {"project_id": DEFAULT_PROJECT, "host": urlsplit(ORIGIN).hostname}
    async with engine.begin() as conn:
        site: SiteId = (await conn.execute(insert(Site).values(**values).returning(Site.id))).scalar_one()
    return site


async def _tasks(engine: AsyncEngine, *paths: str) -> list[ClaimedTask]:
    values = {"project_id": DEFAULT_PROJECT, "kind": "scrape", "request": {}, "engine_version": "0+cal.0"}
    async with engine.begin() as conn:
        job_id: JobId = (await conn.execute(insert(Job).values(**values).returning(Job.id))).scalar_one()
        rows = [{"job_id": job_id, "url": ORIGIN + path, "canonical_url": ORIGIN + path} for path in paths]
        await conn.execute(insert(UrlTask), rows)
        tasks = await claim_tasks(conn, "w1", len(paths))
    return sorted(tasks, key=lambda task: paths.index(task.url.removeprefix(ORIGIN)))


async def _polite(
    engine: AsyncEngine,
    task: ClaimedTask,
    policy: FetchPolicy,
    page: PageFetch,
    limiter: HostLimiter,
    *,
    robots: "Web | None" = None,
    by_adapter: bool = False,
) -> FetchResult | TaskResult:
    """The task's queued polite fetch with `page`, and robots.txt fetched from `robots`, else from `page` (then a
    `Web`)."""
    web = robots or page
    assert isinstance(web, Web)
    rules = rules_of(engine, web.rung("http"), [], Turns(limiter, QUEUED))
    result = await polite_fetch(
        task.url, policy, rules, limiter, QUEUED, page, by_adapter=by_adapter, tries=task.attempts
    )
    return queued_end(result, task)


async def _snapshot(engine: AsyncEngine, site_id: SiteId, origin: str = ORIGIN) -> RobotsSnapshot | None:
    async with engine.connect() as conn:
        stored = (await conn.execute(select(Site.robots_snapshot).where(Site.id == site_id))).scalar_one()
    snapshot = None if stored is None else json.loads(stored).get(f"{origin}/robots.txt")
    return None if snapshot is None else RobotsSnapshot.model_validate(snapshot)


async def test_a_disallowed_page_is_blocked_by_robots_without_being_fetched(
    engine: AsyncEngine, site_id: SiteId, web: Web
) -> None:
    [task] = await _tasks(engine, "/private/a")

    result = await _polite(engine, task, OBEYS, web, HostLimiter())

    assert result == Finished("blocked", reason="robots")
    assert web.urls() == [f"{ORIGIN}/robots.txt"]
    snapshot = await _snapshot(engine, site_id)
    assert snapshot is not None
    assert (snapshot.status_code, snapshot.body) == (200, ROBOTS)


@pytest.mark.usefixtures("site_id")
async def test_by_default_a_disallowed_page_is_fetched_without_reading_robots(engine: AsyncEngine, web: Web) -> None:
    web.serve(f"{ORIGIN}/private/a", PAGE)
    [task] = await _tasks(engine, "/private/a")

    result = await _polite(engine, task, FetchPolicy(), web, HostLimiter())

    assert isinstance(result, FetchResult)
    assert web.urls() == [f"{ORIGIN}/private/a"]
    assert result.bundle.manifest.url == f"{ORIGIN}/private/a"


async def test_an_unreachable_robots_file_fails_the_attempts_and_is_asked_once_while_kept(
    engine: AsyncEngine, site_id: SiteId, web: Web
) -> None:
    web.serve(f"{ORIGIN}/robots.txt", b"", 503, "blocked")
    first, second = await _tasks(engine, "/item/1", "/item/2")
    limiter = HostLimiter()

    results = [await _polite(engine, task, OBEYS, web, limiter) for task in (first, second)]

    assert results == [Failed("robots_unreachable", transient=True, exhausted="blocked")] * 2
    assert web.urls() == [f"{ORIGIN}/robots.txt"]
    snapshot = await _snapshot(engine, site_id)
    assert snapshot is not None
    assert (snapshot.status_code, snapshot.body) == (503, None)
