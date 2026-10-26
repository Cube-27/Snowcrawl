"""The handler of `crawl` tasks: fetch the task's page as a batch task does
(`snowcrawl.worker.handlers.steps`), settle it, and add the URLs it leads to to the frontier
(`snowcrawl.crawl.frontier`).

A reached page contributes its links and next-page candidates at its depth plus one. The scope is on the host the start
page is on: when the site redirects the start URL to another host of the same site (its `www.` subdomain, or back), the
crawl follows it there. The start task stores where its page is on the job (`job.start_location`), the way
every other task reads it. Every task stores where its page was fetched (`url_task.final_url`), and once it is stored,
the URL the task was redirected to is not added as a task of its own: that page is the redirected task's. A task queued
for that URL before then stays a task.

The start URL's task also reads the site's sitemaps (`snowcrawl.crawl.sitemaps`: robots.txt `Sitemap:` lines, else
`/sitemap.xml`) and adds their URLs as seeds, at depth 0, even when its own page was not reached, on the attempt that
ends the task: an attempt that is postponed or retried leaves them to the next, so they are read once. Sitemaps are
fetched over HTTP, politely: the worker waits for the site's rate in place, within `SITEMAP_TIMEOUT_S`, past which the
seeds read so far are kept and the job records `sitemaps_timed_out`, so that the listing diff counts the run as partial;
a sitemap the SSRF guard refused ends the reading the same way (it is never fetched).

A page whose URL matched a page-kind rule (`url_task.page_kind`) is extracted: its formats, or its record, as a
crawl-discovered page (`no_match` when incomplete). A page no rule matched, in a crawl with rules,
ends `links_only` and is never extracted. The page is fetched before the worker's transaction for the
task opens (`steps.fetch_page`). URLs are added after the page is settled, so the job's row is locked
(`Frontier.enqueue`) only for the rest of the task's transaction.
"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.types import AttemptRecord
from snowcrawl.config_ir.api_recipe.execute import MAX_PAGES
from snowcrawl.config_ir.drift import DriftMargins
from snowcrawl.config_ir.role_c import role_c
from snowcrawl.crawl.frontier import Frontier, Links, read_links
from snowcrawl.crawl.rules import located
from snowcrawl.crawl.sitemaps import SitemapReader, roots
from snowcrawl.db.jobs import record_event
from snowcrawl.db.models import FinishedOutcome, Job, UrlTask
from snowcrawl.db.queue import ClaimedTask, Failed, Postpone, Skipped, TaskResult, ends_task
from snowcrawl.llm.client import LLMClient
from snowcrawl.normalize.url_key import canonicalize
from snowcrawl.recurring.listing_diff import SITEMAPS_TIMED_OUT
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.services.crawl import CrawlRequest
from snowcrawl.services.fetch_options import fetch_policy
from snowcrawl.services.fetching import Fetching, SiteHttp, TargetRejected
from snowcrawl.services.map import TIMEOUT_S
from snowcrawl.services.page_run import Extracted, Page, Records, extract_run, page_run
from snowcrawl.services.page_store import record_fetch_usage
from snowcrawl.services.page_task import NotReached
from snowcrawl.services.polite import QUEUED, NoTurn, Turns, listed_sitemaps, rules_of
from snowcrawl.services.scrape_request import PageFormat
from snowcrawl.storage.base import ObjectStore
from snowcrawl.worker.handlers.steps import count_fetch, fetch_page, load_job
from snowcrawl.worker.loop import Settle, TaskHandler

log = logging.getLogger(__name__)

SITEMAP_TIMEOUT_S: Final = TIMEOUT_S  # as long as a `map` reads a site's sitemaps


@dataclass(frozen=True)
class _Crawl:
    """A task of a crawl: the request, what the task extracts records with (None: formats), and the client role C runs
    on."""

    task: ClaimedTask
    request: CrawlRequest
    records: Records | None
    llm: LLMClient


def crawl_handler(
    fetching: Fetching, data: EngineData | None, margins: DriftMargins | None, *, llm: LLMClient
) -> TaskHandler:
    """The `crawl` handler of a worker that fetches with `fetching`; `data` is None in a worker without the engine's
    data files, whose records tasks then fail. `margins` decide drift suspensions; role C
    runs on `llm`."""

    async def handle(task: ClaimedTask, engine: AsyncEngine) -> Settle | TaskResult:
        async with engine.connect() as conn:
            job = await load_job(conn, task, data, margins)
        if isinstance(job, Failed):
            return job
        request, records = CrawlRequest.model_validate(job[0]), job[1]
        return await _run(engine, fetching, _Crawl(task, request, records, llm))

    return handle


async def _run(engine: AsyncEngine, fetching: Fetching, crawl: _Crawl) -> Settle | TaskResult:
    task = crawl.task
    own = crawl.request.frontier()
    async with engine.connect() as conn:
        if own.strategy != "bfs" and await own.over_limit(conn, task):
            return Skipped()  # leased with the candidates before the pages that spent the limit settled
        if own.strategy == "best_first" and await own.outranked(conn, task):
            return Postpone(timedelta(0))  # a page fetched since it was leased found a better candidate
    # A page whose links the crawl reads is fetched itself: an API recipe's JSON has none.
    reads_links = task.depth < crawl.request.max_depth or task.page_kind is None
    served = None if reads_links else crawl.records
    # A listing recipe's pages count toward the crawl's page bound as its tasks do.
    pages = min(MAX_PAGES, crawl.request.limit)
    fetched = await fetch_page(
        engine,
        fetching,
        task,
        crawl.request.fetch,
        crawl.request.locale,
        served=served,
        listing_pages=pages,
        formats=_formats_of(task, crawl.request),
        # A page read for its links only is no page of the records surface: its fetch plan is not the surface's.
        planned=crawl.records if _extracted(task, crawl.request) else None,
    )
    extracted = None
    if not isinstance(fetched, NotReached):
        # The page resolves before the task's transaction opens (`page_run`); a page no rule matched is not extracted.
        records = crawl.records if _extracted(task, crawl.request) else None
        extracted = await extract_run(fetched, records, role_c(crawl.llm, job_id=task.job_id))
    return partial(_settle, engine, fetching, crawl, (fetched, extracted))


async def _settle(
    engine: AsyncEngine,
    fetching: Fetching,
    crawl: _Crawl,
    fetched: tuple[Page | NotReached, Extracted | None],
    conn: AsyncConnection,
) -> TaskResult:
    """The task's writes, in its transaction: the fetched page's run, and the URLs it leads to added."""
    task = crawl.task
    page, extracted = fetched
    if isinstance(page, NotReached):
        result, links = page.task_result, Links()
    else:
        await _record_final_url(conn, task, page)
        result, links = await _visit(conn, engine, fetching.store, crawl, page, extracted)
    is_start = task.canonical_url == crawl.request.start
    # The sitemaps are read before the task writes its job's row (usage, the frontier), which stays locked until the
    # task commits: a cancel, which locks that row, never waits for the reading (up to `SITEMAP_TIMEOUT_S`).
    reads_sitemaps = is_start and ends_task(task, result)
    # Where the start page is decides what the crawl takes; a task that adds nothing does not look it up.
    adds_urls = bool(links.urls) or reads_sitemaps
    site_url = await _site_url(conn, crawl, page) if adds_urls else crawl.request.start
    frontier = crawl.request.frontier(site_url)
    seeds, priorities = (
        await _sitemap_seeds(conn, engine, fetching, crawl, frontier, site_url) if reads_sitemaps else ([], {})
    )
    if is_start and adds_urls:
        await conn.execute(update(Job).where(Job.id == task.job_id).values(start_location=site_url))
    await count_fetch(conn, task, page)  # after the extraction, as role C needs
    await frontier.enqueue(conn, task.job_id, seeds, depth=0, parent=None, priorities=priorities)
    await frontier.enqueue(
        conn,
        task.job_id,
        links.urls,
        depth=task.depth + 1,
        parent=task.id,
        anchor_text=links.texts,
        inherited=links.relevance,
    )
    await frontier.close_if_spent(conn, task)
    return result


def _formats_of(task: ClaimedTask, request: CrawlRequest) -> Sequence[PageFormat]:
    """The formats the task's page is fetched for: a page no rule matched is read for its links only, so it needs no
    file made of it."""
    return request.formats if _extracted(task, request) else ()


def _extracted(task: ClaimedTask, request: CrawlRequest) -> bool:
    """Whether the task's page is extracted: one a page-kind rule matched, or any page of a crawl without rules."""
    return task.page_kind is not None or not request.page_kind_rules


async def _record_final_url(conn: AsyncConnection, task: ClaimedTask, page: Page) -> None:
    """Store where the task's page was fetched, in canonical form, before the task adds any URL. An adapter's or API
    recipe's capture (method `api`) stands for the page it was asked
    for: its final URL is the endpoint, which is no page of the crawl (as `rules.located` reads it)."""
    manifest = page.result.bundle.manifest
    final_url = canonicalize(manifest.page_url)
    await conn.execute(update(UrlTask).where(UrlTask.id == task.id).values(final_url=final_url))


async def _site_url(conn: AsyncConnection, crawl: _Crawl, page: Page | NotReached) -> str:
    """Where the crawl's start page is (`rules.located`), which its scope follows: for the start task, from its own
    page's capture; for every other, as the start task stored it on the job."""
    request = crawl.request
    if crawl.task.canonical_url == request.start:
        manifest = None if isinstance(page, NotReached) else page.result.bundle.manifest
        return located(request.start, None if manifest is None else (manifest.method, manifest.final_url))
    stored = (await conn.execute(select(Job.start_location).where(Job.id == crawl.task.job_id))).scalar_one()
    return stored or request.start


async def _visit(
    conn: AsyncConnection,
    engine: AsyncEngine,
    store: ObjectStore,
    crawl: _Crawl,
    page: Page,
    extracted: Extracted | None,
) -> tuple[TaskResult, Links]:
    """The result of the fetched page's run (`page_run`: `links_only` for a page no rule matched) and the links it
    contributes (none when it was not reached)."""
    task, request = crawl.task, crawl.request
    links = Links()
    # A page at `max_depth` leads nowhere the crawl goes, so its links are not read.
    if page.reached and task.depth < request.max_depth:
        # One parse serves the links and the extraction; parsing is CPU work, so it runs in a thread.
        wanted = request.frontier().wanted if request.strategy == "best_first" else None
        links = await asyncio.to_thread(read_links, page.parsed, page.result.bundle.manifest.final_url, wanted)
    reached: FinishedOutcome = "extracted" if _extracted(task, request) else "links_only"
    run = await page_run(
        conn, engine, store, task, page, extracted, retryable=True, caller_supplied=False, reached=reached
    )
    return run.result, links


async def _sitemap_seeds(
    conn: AsyncConnection, engine: AsyncEngine, fetching: Fetching, crawl: _Crawl, frontier: Frontier, site_url: str
) -> tuple[list[str], dict[str, float]]:
    """The URLs of the sitemaps of the site the start page is on, `site_url` there, that the crawl takes, in the order
    read, up to its capacity, with the priorities the sitemaps state for them. The fetches count as the job's usage."""
    task = crawl.task
    policy = fetch_policy(crawl.request.fetch, crawl.request.locale)
    attempts: list[AttemptRecord] = []
    # The page's fetch has just stored a fresh snapshot, so this reads it rather than fetching, unless the page
    # redirected to another host.
    # The reading waits in place for the host's turns, as a further request of a task does.
    waiting = replace(QUEUED, postpone=False)
    read = rules_of(engine, fetching.rungs.http, attempts, Turns(fetching.limiter, waiting))
    try:
        rules = await read(site_url, policy)
    except NoTurn:
        rules = None
    if rules is None:
        return [], {}
    polite = SiteHttp(fetching, rules, policy, attempts, waiting)
    reader = SitemapReader(polite.sitemap_body, frontier.scope.host)
    seeds: dict[str, None] = {}
    capped = False
    try:
        # The time cap covers robots.txt's `Sitemap:` lines too, read here when the crawl does not obey robots.txt.
        async with asyncio.timeout(SITEMAP_TIMEOUT_S):
            start = roots(site_url, await listed_sitemaps(read, site_url, policy, rules))
            async with aclosing(reader.urls(start)) as urls:
                async for url in urls:
                    if frontier.admits(url):
                        seeds[url] = None
                    if len(seeds) >= frontier.capacity:
                        break
    except TimeoutError:
        capped = True
    except TargetRejected:
        pass  # a refused sitemap ends the reading: it is never fetched
    await record_fetch_usage(conn, task.job_id, attempts, policy.proxy_pool)
    if capped:  # the run saw part of its listing only
        await record_event(conn, task.job_id, SITEMAPS_TIMED_OUT, {"url": task.url, "seeds": len(seeds)})
    # The task's id and job, never a URL, which `SNOWCRAWL_LOG_REDACT_URLS` may keep out of the logs.
    stop = f"; stopped at the {SITEMAP_TIMEOUT_S:g} s time cap" if capped else ""
    log.info("url_task %s: sitemaps of job %s: %d seeds%s", task.id, task.job_id, len(seeds), stop)
    return list(seeds), {url: reader.priorities[url] for url in seeds if url in reader.priorities}
