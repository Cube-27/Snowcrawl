"""A page task's fetch, the one way a scrape (the caller waits: `IN_PLACE`) and a worker's page task (`QUEUED`) fetch
their page: the fetch policy, the stored capture read instead when `fetch.cache` allows it (`read_stored`), the fetch
plan of the surface, the site's rungs, the page through the polite fetch, the SSRF guard's refusals, and what the fetch
taught the site's memory (`acquire.memory.learn_fetch`).

Each read and write goes through `engine` in a short transaction of its own, outside any transaction of the task, so no
task holds a site's rows while it fetches, and the tasks of one site run side by side.

How long the task waits decides what a fetch that kept no page ends as: a queued task is postponed, or retried after
the back-off (`queued_end`); a caller who waits gets `blocked`. A listing's growth, the default browser actions where
its page is rendered (`fetching.Growth`), and a request's browser actions (`Paging`) are a scrape's only: they are
bounded by the caller's `timeout_s` and records target, which a queued task does not have, and a queued listing's
further pages come from its config's listing recipe within the task's lease (`RECIPE_PAGES_SHARE`).
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.memory import learn_fetch
from snowcrawl.acquire.politeness import backoff, slowed_down
from snowcrawl.acquire.rungs import NO_PLAN
from snowcrawl.acquire.types import AttemptRecord, CallsMode, FetchResult
from snowcrawl.config_ir.active import custom_of, recipe_of
from snowcrawl.config_ir.api_recipe.model import ApiRecipe
from snowcrawl.db.queue import LEASE, ClaimedTask, Failed, Finished, Postpone, TaskResult
from snowcrawl.services.fetch_options import FetchOptions, fetch_policy
from snowcrawl.services.fetching import (
    Fetching,
    Growth,
    PagePlan,
    RecipePages,
    TargetRejected,
    fetch_planned,
    in_place,
    refusal,
    site_rungs,
)
from snowcrawl.services.page_run import Page, Records, read_stored
from snowcrawl.services.page_store import SiteRow, surface_plan
from snowcrawl.services.polite import ROBOTS_UNREACHABLE, Later, Refused, Waiting
from snowcrawl.services.scrape_request import PageFormat

log = logging.getLogger(__name__)

# A listing recipe's page requests start within this share of the task's lease (`LEASE`): each waits in place for its
# turn at most `QUEUED.bound` (half the lease), so the last one ends inside the lease, its timeout and the extraction
# after it included, and no other worker claims the task and sends its requests again.
RECIPE_PAGES_SHARE: Final = LEASE / 4
# A page a 429 kept out (RFC 6585 §4) is fetched at most this many times: one retry after the back-off, from the
# pool's next address, then `blocked`; once its fetch went on to the browser (`acquire.rungs`), at once.
_TOO_MANY_TRIES: Final = 2


class Paging(Protocol):
    """A scrape's browser actions (`scrape_paging.Actions`): the listing fetched by the listing rung instead of
    `acquire`, with its further kept states, or why nothing was sent."""

    async def fetch(
        self, engine: AsyncEngine, fetching: Fetching, url: str, plan: PagePlan
    ) -> tuple[FetchResult, tuple[FetchResult, ...]] | Refused: ...


@dataclass(frozen=True)
class PageTask:
    """A claimed page task to fetch: the request's fetch `options`, `locale` and the `formats` its page is read for;
    the `records` it extracts with, whose schema the policy reads, by whose surface's fetch plan (`surface_plan`) it is
    fetched when `planned`, and whose config's API recipe serves it when the records are all it reads (`served`); a
    listing recipe with pagination then serves up to `listing_pages` pages, and only the first without. A stored
    capture is read within `max_age` seconds; the fetch is bounded by `deadline`, on `time.monotonic`. `growth` and
    `paging` are a scrape's (module docstring); `calls`, the calls its capture keeps (`FetchPolicy.calls`). A `trial`
    (a recipe test) teaches the site nothing (`PagePlan.trial`)."""

    task: ClaimedTask
    options: FetchOptions
    locale: str | None
    deadline: float | None
    formats: Sequence[PageFormat] = ()
    records: Records | None = None
    served: bool = False
    planned: bool = False
    listing_pages: int | None = None
    max_age: int = 0
    growth: Growth | None = None
    paging: Paging | None = None
    calls: CallsMode = "data"
    trial: bool = False


@dataclass(frozen=True)
class NotReached:
    """A page task whose fetch kept no page this run: how the task ends (`blocked` when nothing was sent, a failure
    when the SSRF guard refused a request, postponed or retried when queued), and the fetch attempts it made and the
    proxy pool they went through, which count as the job's usage."""

    task_result: TaskResult
    attempts: tuple[AttemptRecord, ...] = ()
    proxy_pool: str | None = None


async def fetch_task(
    engine: AsyncEngine, fetching: Fetching, site: SiteRow, page_task: PageTask, waiting: Waiting
) -> Page | NotReached:
    """The task's page on `site`, read from its stored capture or fetched politely as `waiting` says, or how the task
    ends without one (`NotReached`). Raises `stored_captures.NotCached` when `fetch.cache` is `only` and no capture
    was stored."""
    task, records = page_task.task, page_task.records
    policy = fetch_policy(
        page_task.options,
        page_task.locale,
        page_kind=task.page_kind,
        schema=None if records is None else records.schema,
        vocabulary=None if records is None else records.data.vocabulary,
        formats=page_task.formats,
        calls=page_task.calls,
    )
    config = None if records is None else records.config
    recipe = recipe_of(config) if page_task.served and config is not None else None
    custom = None if recipe is None or config is None else custom_of(config)
    # A page the config's API recipe serves was stored as its JSON.
    where, cache, formats = (site, task.canonical_url), page_task.options.cache, page_task.formats
    stored = await read_stored(
        engine, fetching.store, where, policy, cache, max_age=page_task.max_age, formats=formats, api=bool(recipe)
    )
    if stored is not None:
        return stored
    by_sources = NO_PLAN
    if page_task.planned and records is not None:
        async with engine.connect() as conn:
            by_sources = await surface_plan(conn, site.id, records.schema, config, records.render)
    rungs = site_rungs(fetching, urlsplit(task.url).hostname or "", site)
    attempts: list[AttemptRecord] = []
    pages = _recipe_pages(page_task, recipe, attempts)
    growth, deadline = page_task.growth, page_task.deadline
    plan = PagePlan(
        rungs,
        site,
        policy,
        attempts,
        recipe,
        pages,
        growth,
        deadline=deadline,
        fetch=by_sources,
        custom=custom,
        trial=page_task.trial,
        offered=None if records is None else records.offered,
    )
    try:
        fetched, further = await _fetched(engine, fetching, page_task, plan, waiting)
    except TargetRejected as e:  # a scrape's growth or browser actions met a refused request
        fetched, further = Failed(e.reason, transient=False), ()
    return await _kept(engine, plan, fetched, further)


def _recipe_pages(page_task: PageTask, recipe: ApiRecipe | None, attempts: list[AttemptRecord]) -> RecipePages | None:
    """The config's API `recipe` serving a listing in pages, their attempts added to `attempts`, when it has
    pagination and the task bounds them (`listing_pages`); None otherwise."""
    records, bound = page_task.records, page_task.listing_pages
    if recipe is None or recipe.pagination is None or bound is None:
        return None
    if records is None or records.schema.page_kind != "listing":
        return None
    deadline = time.monotonic() + RECIPE_PAGES_SHARE.total_seconds()
    return RecipePages(bound, attempts, deadline=deadline)


async def _fetched(
    engine: AsyncEngine, fetching: Fetching, page_task: PageTask, plan: PagePlan, waiting: Waiting
) -> tuple[FetchResult | TaskResult, tuple[FetchResult, ...]]:
    """The page and a listing's further pages, or how the task ends without them. Raises `TargetRejected` when the
    guard refused a request of a scrape's growth or browser actions."""
    task = page_task.task
    if page_task.paging is not None:
        paged = await page_task.paging.fetch(engine, fetching, task.url, plan)
        return (Finished("blocked", reason=paged.reason), ()) if isinstance(paged, Refused) else paged
    polite = await fetch_planned(engine, fetching, task.url, plan, waiting, tries=task.attempts)
    if isinstance(polite, FetchResult):
        log.info("url_task %s of job %s: fetched", task.id, task.job_id)
    fetched = queued_end(polite, task) if waiting.postpone else _in_place_end(polite)
    return fetched, (_further(plan, fetched) if isinstance(fetched, FetchResult) else ())


def _in_place_end(result: FetchResult | Refused | Later) -> FetchResult | Finished:
    """The page the caller waits for, or `blocked` with the reason nothing was sent."""
    kept = in_place(result)
    return Finished("blocked", reason=kept.reason) if isinstance(kept, Refused) else kept


def queued_end(result: FetchResult | Refused | Later, task: ClaimedTask) -> FetchResult | TaskResult:
    """The queued task's page as the polite fetch (`QUEUED`) answered it, or the result the task ends this run with:
    - a robots.txt disallow: `blocked`, reason `robots`, without fetching the page; or, when an active adapter serves
      the URL first, from an API whose own robots.txt its requests go by, its answer when it is `ok`, else `blocked`,
      reason `robots`, with no fallback to the page rungs admitted;
    - robots.txt unreachable: a transient failure, `blocked` once attempts run out (RFC 9309 §2.3.1.4: unreachable
      allows nothing);
    - the site not admitted yet, or the host's turn for a further request further off than the worker waits:
      postponed until it is, or `blocked`;
    - a 429 or 503 to any request of the fetch, unless the outcome kept is `ok` (the page an adapter's endpoint fell
      back to): a transient failure, retried after the back-off (`politeness.backoff`), `blocked` once
      attempts run out (a 429's after one retry, or as soon as the fetch tried it in the browser), and at once when
      the server asks for more than the longest backoff."""
    match result:
        case Later(wait=wait):
            return Postpone(wait)
        case Refused(reason=reason) if reason == ROBOTS_UNREACHABLE:
            return Failed(reason, transient=True, exhausted="blocked")
        case Refused(reason=reason):
            return Finished("blocked", reason=reason)
    return result if result.block_class == "ok" else _backed_off(result, task)


def _backed_off(result: FetchResult, task: ClaimedTask) -> FetchResult | TaskResult:
    """A fetch that kept no `ok` page: the result, or, after a 429 or 503 to any of its requests, the task retried
    after the back-off of its try (`politeness.backoff`), or `blocked` when the server asks for longer than the longest
    backoff (the polite fetch paused the host), or a 429 came on the retry (`_TOO_MANY_TRIES`) or was answered in the
    browser already: its one attempt is the retry."""
    slowed = slowed_down(result.attempts)
    if slowed is None:
        return result
    error = f"http {slowed.status}"
    after = backoff(slowed, task.attempts)
    too_many = _rendered_after(result.attempts) or (
        slowed.status == HTTPStatus.TOO_MANY_REQUESTS and task.attempts >= _TOO_MANY_TRIES
    )
    if after is None or too_many:
        return Finished("blocked", reason=error)
    return Failed(error, transient=True, after=after, exhausted="blocked")


def _rendered_after(attempts: tuple[AttemptRecord, ...]) -> bool:
    """Whether the page's HTTP 429 (RFC 6585 §4) was answered in the browser by a later attempt of the fetch (its one
    browser attempt, `acquire.rungs`), whatever the browser answered: a 429 or 503 of its own is that attempt's
    outcome, and a retry would send the HTTP request and the browser attempt again. An adapter's or a recipe's 429 is
    its endpoint's (method `api`), which the browser does not retry."""
    page = next(
        (i for i, a in enumerate(attempts) if a.method == "http" and a.status == HTTPStatus.TOO_MANY_REQUESTS), None
    )
    return page is not None and any(attempt.method == "browser" for attempt in attempts[page + 1 :])


def _further(plan: PagePlan, kept: FetchResult) -> tuple[FetchResult, ...]:
    """A listing's further pages: those its recipe served after the first (their attempts are the plan's already), or
    the further states its growth kept, whose attempts are added to the plan's (the first state's came with `kept`)."""
    if plan.pages is not None:
        return tuple(plan.pages.further)
    grew = None if plan.growth is None else plan.growth.of(kept)
    if grew is None:
        return ()
    further = grew.pages[1:]
    plan.attempts.extend(attempt for page in further for attempt in page.attempts)
    return further


async def _kept(
    engine: AsyncEngine, plan: PagePlan, fetched: FetchResult | TaskResult, further: tuple[FetchResult, ...]
) -> Page | NotReached:
    """The page, with what its fetch taught remembered on its site (`acquire.memory.learn_fetch`) unless it is a
    trial's, or how the task ends: a request the guard refused, a further page's too, fails it, and no page of it is
    stored."""
    attempts, pool = tuple(plan.attempts), plan.policy.proxy_pool
    if isinstance(fetched, FetchResult):
        refused = next((reason for page in (fetched, *further) if (reason := refusal(page)) is not None), None)
        if refused is None:
            if not plan.trial:
                await learn_fetch(engine, plan.site.id, fetched)
            return Page(plan.site, fetched, attempts, pool, further)
        fetched = Failed(refused, transient=False)
    return NotReached(fetched, attempts, pool)
