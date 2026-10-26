"""Domain memory: two tiers, kept apart, because a
durable strategy and a short-lived safety bias age differently.

- The acquisition contract (`site_contract`), durable, per site and page kind (`remembered_as`): the rung the pages
  start with, the browser engine that taught it, whether the browser was needed only to pass a block (`handoff`: the
  pages stay on HTTP with the cookies that browser `ok` held, `acquire.session`), and the field coverage of the last
  extraction with a record; the site's platform is on `site.platform`. It is learned from a fetch that tried HTTP in
  `auto` (`learn_fetch`), the browser only from HTTP evidence about the method (`lesson`), and goes stale after
  `STALE_FAILURES` consecutive extractions without a record (`learn_extraction`); a block is never one. A stale
  contract is kept but not used; an extraction with a record makes it fresh again. The 7-day re-check
  (`scheduler.recheck`) still sets a `browser` contract back to `http` when HTTP passes, and a browser start that
  fails asks HTTP once more (`rungs`), whose `ok` teaches HTTP again.
- The block history (`site_block`), short-lived, per site: each HTTP request the site refused (`refused`) counts, the
  count starting over when the last one is older than `BLOCK_TTL`; at `BROWSER_FIRST_BLOCKS`, once the browser has
  passed (a browser `ok` after the refusal), the site's pages start in the browser for one `BLOCK_TTL`: a browser the
  site refuses too gains nothing by going first. A browser the site refuses ends a browser-first window. An HTTP `ok`
  kept for the site clears it.

Precedence (`remembered`): a job's own setting (`FetchPolicy.method_hint` other than `auto`) beats memory, which the
caller applies; the block history's browser-first beats the contract's method; a fresh contract beats the default
(HTTP). Both tiers are rows, so every process reads the same memory.

Memory is advisory, and is learned in short transactions of its own (`learn_fetch`, `learn_extraction`), never in a
task's: an upsert locks its row until commit, so a contract row a task's transaction held would make every other task
of the site and page kind wait for that task to end.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.fingerprint import platform_shown
from snowcrawl.acquire.politeness import SLOW_DOWN
from snowcrawl.acquire.session import ENGINE
from snowcrawl.acquire.stored_fetch import stored_fetch
from snowcrawl.acquire.types import AttemptRecord, FetchResult
from snowcrawl.db.models import (
    BrowserEngine,
    FetchMethod,
    PageKind,
    Site,
    SiteBlock,
    SiteContract,
    SiteId,
)

__all__ = [
    "BLOCK_TTL",
    "BROWSER_FIRST_BLOCKS",
    "STALE_FAILURES",
    "Coverage",
    "Lesson",
    "Remembered",
    "learn_extraction",
    "learn_fetch",
    "lesson",
    "refused",
    "remembered",
    "remembered_as",
]

# Owner plan 2026-10-19 P4 (domain memory): unmeasured starting points.
BLOCK_TTL: Final = timedelta(minutes=15)
BROWSER_FIRST_BLOCKS: Final = 2
STALE_FAILURES: Final = 2

# The last extraction with a record: the schema's field names it published (`found`) and those it did not (`missing`).
Coverage = dict[str, list[str]]


@dataclass(frozen=True)
class Remembered:
    """What memory says a fetch of a site's page of one kind starts with: the defaults without memory."""

    method: FetchMethod = "http"


@dataclass(frozen=True)
class Lesson:
    """What one fetch teaches the contract (`lesson`)."""

    method: FetchMethod
    engine: BrowserEngine | None = None
    handoff: bool = False


def remembered_as(kind: PageKind | None) -> PageKind:
    """The page kind whose memory a fetch of `kind` reads and writes. A fetch with no page kind is classified as a
    detail page is (`classify` applies the listing rule to listings only), so it shares the detail pages' memory."""
    return kind or "detail"


async def remembered(
    conn: AsyncConnection, site_id: SiteId, kind: PageKind | None, *, blocks: bool = True
) -> Remembered:
    """The memory of the site's pages of `kind` (module docstring, precedence): browser-first while the block history
    says so, else the fresh contract's method, else the default. Without `blocks`, the durable
    contract alone, for what outlives the block history (a learned config's `Target.fetch`)."""
    fresh = (
        SiteContract.site_id == site_id,
        SiteContract.page_kind == remembered_as(kind),
        SiteContract.failures < STALE_FAILURES,
    )
    browser_first = exists().where(SiteBlock.site_id == site_id, SiteBlock.browser_first_until > func.now())
    query = select(select(SiteContract.method).where(*fresh).scalar_subquery(), browser_first)
    method, first = (await conn.execute(query)).one()
    if first and blocks:
        return Remembered("browser")
    return Remembered(method or "http")


def refused(attempt: AttemptRecord) -> bool:
    """Whether an HTTP request was refused by the site (`classify`): `blocked` other than a 429 or 503, which ask for
    fewer requests and are the politeness step's (RFC 6585 §4, RFC 9110 §15.6.4), or a `shell` a challenge names
    (`acquire.challenge`). A block needs this corroboration; an error or a shell alone is not one."""
    if attempt.method != "http":
        return False
    if attempt.block_class == "blocked":
        return attempt.status not in SLOW_DOWN
    return attempt.block_class == "shell" and attempt.challenge is not None


def lesson(result: FetchResult) -> Lesson | None:
    """What an `auto` fetch that tried HTTP and kept an `ok` teaches; None for any other fetch (a job's own method, a
    page that started in the browser or was served by an API: nothing new about HTTP). An HTTP `ok` teaches the
    default. A browser `ok` teaches the browser only when the last HTTP attempt was evidence about the method
    (`_method_evidence`: a `shell`, or `refused`), not a transient failure (a timeout, a network error, a 429 or 503,
    a server error), which says nothing of the next request. One its attempt classed `shell`, kept as the last rung's
    page by `rungs`, teaches nothing: the browser passed nothing HTTP did not. A browser that passed a refusal is a
    `handoff` instead when the render ran no browser steps and its cookies went on to HTTP (an attempt's
    `site_session`): it was needed only to pass the block, and the pages stay on HTTP."""
    http = [attempt for attempt in result.attempts if attempt.method == "http"]
    if stored_fetch(result.bundle.manifest).method_hint != "auto" or result.block_class != "ok" or not http:
        return None
    if result.method == "http":
        return Lesson("http")
    rendered = [attempt.block_class for attempt in result.attempts if attempt.method == "browser"]
    if result.method != "browser" or rendered[-1:] != ["ok"] or not _method_evidence(http[-1]):
        return None
    stepped = any(attempt.steps for attempt in result.attempts if attempt.method == "browser")
    carried = any(attempt.site_session for attempt in result.attempts)
    handoff = refused(http[-1]) and carried and not stepped
    return Lesson(method="http" if handoff else "browser", engine=ENGINE, handoff=handoff)


def _method_evidence(attempt: AttemptRecord) -> bool:
    """Whether an HTTP attempt says HTTP does not get the page: a `shell` (the page needs a script) or `refused`."""
    return attempt.block_class == "shell" or refused(attempt)


async def learn_fetch(engine: AsyncEngine, site_id: SiteId, result: FetchResult) -> None:
    """Remember what the fetch `result` of a site's page taught, in a transaction of its own: its block or HTTP
    success in the block history, and its `lesson` in the contract of the kind it was fetched as. A plain HTTP `ok`
    leaves a contract on HTTP as it is (a handoff's HTTP `ok` is the handoff working); a contract the lesson would not
    change is not rewritten. The platform an `ok` page shows (`adapters.platforms`) is the site's (`site.platform`)."""
    async with engine.begin() as conn:
        await _learn_fetch(conn, site_id, result)


async def _learn_fetch(conn: AsyncConnection, site_id: SiteId, result: FetchResult) -> None:
    await _record_block(conn, site_id, result)
    await _record_platform(conn, site_id, result)
    learned = lesson(result)
    if learned is None:
        return
    values = {"method": learned.method, "engine": learned.engine, "handoff": learned.handoff}
    kind = remembered_as(stored_fetch(result.bundle.manifest).page_kind)
    insert = pg_insert(SiteContract).values(site_id=site_id, page_kind=kind, checked_at=func.now(), **values)
    changed = (
        SiteContract.method != "http"
        if learned == Lesson("http")
        else or_(*(getattr(SiteContract, name).is_distinct_from(value) for name, value in values.items()))
    )
    await conn.execute(
        insert.on_conflict_do_update(
            index_elements=[SiteContract.site_id, SiteContract.page_kind],
            set_={**values, "checked_at": func.now()},
            where=changed,
        )
    )


async def _record_platform(conn: AsyncConnection, site_id: SiteId, result: FetchResult) -> None:
    """Set the site's platform to the one its `ok` page shows; a page that shows none leaves it."""
    if result.block_class != "ok" or result.method == "api":
        return
    platform = platform_shown(result)
    if platform is not None:
        query = update(Site).where(Site.id == site_id, Site.platform.is_distinct_from(platform))
        await conn.execute(query.values(platform=platform))


def _browser_refused(result: FetchResult) -> bool:
    """Whether the fetch's browser attempt was refused as an HTTP request is (`refused`): `blocked`, or a `shell` a
    challenge names; a fetch that kept a browser `ok` was not."""
    if result.method == "browser" and result.block_class == "ok":
        return False
    return any(
        attempt.method == "browser"
        and (attempt.block_class == "blocked" or (attempt.block_class == "shell" and attempt.challenge is not None))
        for attempt in result.attempts
    )


async def _record_block(conn: AsyncConnection, site_id: SiteId, result: FetchResult) -> None:
    """Count the fetch's last `refused` HTTP request in the site's block history, or clear the history when the fetch
    kept an HTTP `ok`. Browser-first starts only from a fetch that kept a browser `ok`, and a browser attempt the site
    refused ends it (module docstring)."""
    block = next((attempt for attempt in reversed(result.attempts) if refused(attempt)), None)
    browser_refused = _browser_refused(result)
    if block is None:
        if result.block_class == "ok" and result.method == "http":
            await conn.execute(delete(SiteBlock).where(SiteBlock.site_id == site_id))
        elif browser_refused:
            await conn.execute(update(SiteBlock).where(SiteBlock.site_id == site_id).values(browser_first_until=None))
        return
    blocks = case((SiteBlock.last_block_at > func.now() - BLOCK_TTL, SiteBlock.blocks + 1), else_=1)
    kept = None if browser_refused else SiteBlock.browser_first_until
    passed = result.method == "browser" and result.block_class == "ok"
    first_until = case((blocks >= BROWSER_FIRST_BLOCKS, func.now() + BLOCK_TTL), else_=kept) if passed else kept
    insert = pg_insert(SiteBlock).values(
        site_id=site_id, blocks=1, last_block_at=func.now(), vendor=block.challenge, browser_first_until=None
    )
    await conn.execute(
        insert.on_conflict_do_update(
            index_elements=[SiteBlock.site_id],
            set_={
                "blocks": blocks,
                "last_block_at": func.now(),
                "vendor": insert.excluded.vendor,
                "browser_first_until": first_until,
            },
        )
    )


async def learn_extraction(engine: AsyncEngine, site_id: SiteId, kind: PageKind, coverage: Coverage | None) -> None:
    """Remember how an extraction of a reached page of the site's `kind` went, in a transaction of its own: with a
    record, its `coverage`, which makes the contract fresh (a row with the defaults when there is none); without one
    (None), one more consecutive failure towards `STALE_FAILURES`. An unchanged contract is not rewritten."""
    async with engine.begin() as conn:
        await _learn_extraction(conn, site_id, kind, coverage)


async def _learn_extraction(conn: AsyncConnection, site_id: SiteId, kind: PageKind, coverage: Coverage | None) -> None:
    key = (SiteContract.site_id == site_id, SiteContract.page_kind == kind)
    if coverage is None:
        await conn.execute(update(SiteContract).where(*key).values(failures=SiteContract.failures + 1))
        return
    insert = pg_insert(SiteContract).values(
        site_id=site_id, page_kind=kind, method="http", checked_at=func.now(), coverage=coverage
    )
    await conn.execute(
        insert.on_conflict_do_update(
            index_elements=[SiteContract.site_id, SiteContract.page_kind],
            set_={"coverage": insert.excluded.coverage, "failures": 0},
            where=or_(SiteContract.failures != 0, SiteContract.coverage.is_distinct_from(insert.excluded.coverage)),
        )
    )
