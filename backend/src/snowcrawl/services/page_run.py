"""One page task's run, the same whether it runs while its caller waits (`scrape`) or in a worker
(`batch_scrape`, `extract` and `crawl` tasks): the stored capture it reads instead of fetching
(`fetch.cache`, `max_age`), the outcome of a page that was not reached, and a reached page's extraction with its further
pages and the records they keep.

The callers adapt it. A scrape settles its task through the queue at once (`settle_task`) and answers with what the run
kept; a worker handler returns the result for the worker loop to settle. Only a worker's task is `retryable`: a fetch
error, or a vendor's challenge on a page's first try, is a transient failure the queue retries, where a scrape's caller
waits, so it ends at once. The extraction (`extract_run`) runs before the task's transaction opens, so no task holds a
connection or row lock while its page resolves; `page_run` writes in the caller's transaction (`page_store`).
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.challenge import LEGAL, vendor_block
from snowcrawl.acquire.persist import store_capture
from snowcrawl.acquire.types import AttemptRecord, FetchPolicy, FetchResult
from snowcrawl.config_ir.drift import DriftMargins
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.capture import BlockClass
from snowcrawl.core.retry import backoff_delay
from snowcrawl.db.models import CaptureId, FinishedOutcome
from snowcrawl.db.queue import ClaimedTask, Failed, Finished
from snowcrawl.resolve.pipeline import EngineData, RoleC, extract_page, states_offer
from snowcrawl.resolve.task import ActiveConfig, StoredRecord
from snowcrawl.resolve.types import ExtractionResult
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.fetch_options import CacheMode
from snowcrawl.services.page_store import (
    Further,
    SiteRow,
    StoredFurther,
    extract_further,
    records_outcome,
    store_further,
    store_page_record,
)
from snowcrawl.services.records import RecordField, page_fields
from snowcrawl.services.scrape_request import RenderOptions
from snowcrawl.services.stored_captures import NotCached, Recent, recent_capture, reread
from snowcrawl.storage.base import ObjectStore
from snowcrawl.storage.capture_bundle import CaptureBundle

# The outcomes of a page the fetch reached.
ReachedOutcome = Literal[
    "extracted",
    "partial",
    "blocked",
    "shell",
    "not_found",
    "auth_wall",
    "soft_404",
    "geo_redirect",
    "redirected_to_listing",
    "unsupported_media",
    "geo_blocked",
]
# The outcome a page task reports: a reached page's, `no_match` for a reached page that states no entity
# of the schema, or its fetch's failure (`FetchFailure`).
PageOutcome = Literal[ReachedOutcome, "no_match", "timeout", "failed"]

# The outcome of each block class but `error`, which is a fetch failure (`fetch_failure`).
BLOCK_OUTCOMES: Final[dict[BlockClass, ReachedOutcome]] = {
    # Without `records` the page is extracted once it is fetched; with them, its record decides (`partial`).
    "ok": "extracted",
    "blocked": "blocked",
    "shell": "shell",
    "not_found": "not_found",
    "auth_wall": "auth_wall",
    "soft_404": "soft_404",
    "geo_redirect": "geo_redirect",
    "redirected_to_listing": "redirected_to_listing",
    "unsupported_media": "unsupported_media",
}


def reached_outcome(result: FetchResult) -> ReachedOutcome:
    """The outcome of a page the fetch reached, from its block class (`BLOCK_OUTCOMES`), except that a `blocked` page
    whose kept attempt is labelled `LEGAL` (a 451, RFC 7725) is `geo_blocked`: withheld where the request came from."""
    outcome = BLOCK_OUTCOMES[result.block_class]
    kept = next((a for a in reversed(result.attempts) if a.method == result.method), None)
    return "geo_blocked" if outcome == "blocked" and kept is not None and kept.challenge == LEGAL else outcome


# The `AttemptRecord.error` of a rung that got no answer within its deadline.
TIMEOUT: Final = "timeout"
# The outcomes of a fetch that got no page for want of an answer (`fetch_failure`).
FetchFailure = Literal["failed", "timeout"]


def fetch_failure(result: FetchResult) -> FetchFailure | None:
    """How the fetch failed, from the kept attempt alone (one outcome model): `timeout` when its rung
    got no answer within its deadline, `failed` for any other error, None when the fetch got an answer."""
    if result.bundle.manifest.block_class != "error":
        return None
    return "timeout" if result.attempts[-1].error == TIMEOUT else "failed"


# The reason a page a vendor's challenge kept from being reached ends `blocked` with (`challenge.vendor_block`).
CHALLENGE_REASON: Final = "challenge: {vendor}"


def blocked_reason(result: FetchResult) -> str | None:
    """Why the fetched page ended `blocked`, when a vendor's challenge says (`CHALLENGE_REASON`); else None."""
    vendor = vendor_block(result)
    return None if vendor is None else CHALLENGE_REASON.format(vendor=vendor)


# The error of a reached caller-supplied page that has no record: resolution failed.
EXTRACTION_ERROR: Final = "extraction_error"
# A page a vendor's challenge kept out is fetched at most this many times (master plan 2026-10-18 §3.1), and only once
# when the browser rung met that challenge itself: a retry sends the same rungs to the same challenge.
_VENDOR_TRIES: Final = 2


@dataclass(frozen=True)
class Records:
    """What a task extracts records with: the schema, the engine data, the site's config for it (None: cold), the drift
    margins, and the `render` the request pinned (None: the job inherits its surface's)."""

    schema: SchemaDef
    data: EngineData
    config: ActiveConfig | None
    margins: DriftMargins | None
    render: RenderOptions | None = None

    def offered(self, bundle: CaptureBundle) -> bool:
        """Whether the page in `bundle` publishes a field the schema reads from the item's Offer, extracted with the
        site's config for that capture as `extract_run` extracts it, before role C (`resolve.pipeline.states_offer`)."""
        config = None if self.config is None else self.config.for_capture(bundle.manifest, self.data.adapters)
        return states_offer(bundle, self.schema, None if config is None else config.executor, data=self.data)


@dataclass(frozen=True)
class Page:
    """The task's page as fetched, or as `stored` when a stored capture was read instead (`read_stored`): its site, the
    fetch, the attempts and proxy pool its usage counts, and a listing's further pages, in order."""

    site: SiteRow
    result: FetchResult
    attempts: tuple[AttemptRecord, ...] = ()
    proxy_pool: str | None = None
    further: tuple[FetchResult, ...] = ()
    stored: Recent | None = None

    @property
    def reached(self) -> bool:
        return self.result.block_class == "ok"

    @property
    def files(self) -> frozenset[str]:
        """The names of the files stored, or to be stored, beside the capture (`storage.artifacts`)."""
        return frozenset(self.result.artifacts) if self.stored is None else self.stored.artifacts

    @cached_property
    def parsed(self) -> ParsedPage:
        """The page's one parse."""
        return ParsedPage.of(self.result.bundle)


async def read_stored(
    engine: AsyncEngine,
    store: ObjectStore,
    page: tuple[SiteRow, str],
    policy: FetchPolicy,
    cache: CacheMode,
    *,
    max_age: int,
    formats: Collection[str],
    api: bool,
) -> Page | None:
    """The page, a site and canonical URL, read from its latest stored capture made with `policy`'s steps and context
    instead of fetching ("Synchronous scrape": `max_age`, `fetch.cache`, `recent_capture`), with the
    files `formats` ask of it; an API recipe's too with `api`.
    `cache: default` reads one made within `max_age` seconds, none when it is 0; `bypass` none; `only` one within
    `max_age`, of any age when it is 0, and raises `NotCached` when there is none. A worker's job has no `max_age`."""
    if cache == "bypass" or (cache == "default" and not max_age):
        return None
    site, canonical = page
    async with engine.connect() as conn:
        age = None if cache == "only" and not max_age else max_age
        row = await recent_capture(conn, site.id, canonical, policy, max_age=age, api=api)
    recent = None if row is None else await reread(store, row, formats)
    if recent is None:
        if cache == "only":
            raise NotCached("no stored capture of the page was made the way this request fetches it")
        return None
    return Page(site, recent.result, proxy_pool=policy.proxy_pool, stored=recent)


def unreached(
    result: FetchResult, capture_id: CaptureId, task: ClaimedTask, *, retryable: bool
) -> Finished | Failed | None:
    """The result of a page that was not reached, None for a reached one: its outcome (`blocked`, `shell`,
    `not_found`), or a failure for a fetch error, transient when `retryable`.
    A page a vendor's challenge kept out ends `blocked` naming the vendor (`blocked_reason`); a `retryable` one after
    at most one retry, once the back-off of its first try is over, and with none when the browser rung met that
    vendor's challenge itself."""
    failure = fetch_failure(result)
    if failure is not None:
        error = result.attempts[-1].error or failure
        return Failed(error, transient=retryable, exhausted=failure, capture_id=capture_id)
    outcome = reached_outcome(result)
    if outcome == "extracted":
        return None
    reason = blocked_reason(result)
    if reason is not None and retryable and task.attempts < _VENDOR_TRIES and not _browser_met(result):
        after = backoff_delay(task.attempts)
        return Failed(reason, transient=True, after=after, exhausted="blocked", capture_id=capture_id)
    return Finished(outcome, reason=reason, capture_id=capture_id)


def _browser_met(result: FetchResult) -> bool:
    """Whether a browser attempt of the fetch met the challenge of the vendor that kept the page out."""
    vendor = vendor_block(result)
    return any(a.method == "browser" and a.challenge == vendor for a in result.attempts)


@dataclass(frozen=True)
class Extracted:
    """A reached page's extraction with what it extracted with, the site's config as it ran on the page's capture, and
    its further pages' (`extract_further`)."""

    records: Records
    extraction: ExtractionResult
    config: ActiveConfig | None
    further: list[Further] = field(default_factory=list)


async def extract_run(page: Page, records: Records | None, gap_fill: RoleC | None) -> Extracted | None:
    """The extraction of a reached page and its further pages with `records`: `config` mode with the
    site's config for its capture, else cold; `gap_fill`, role C charged to the task's job, fills required fields left
    missing. None for a page that was not reached, or without `records`."""
    if records is None or not page.reached:
        return None
    bundle = page.result.bundle
    config = None if records.config is None else records.config.for_capture(bundle.manifest, records.data.adapters)
    executor = None if config is None else config.executor
    extraction = await extract_page(bundle, records.schema, executor, gap_fill, data=records.data, parsed=page.parsed)
    further = await extract_further(page.further, records.schema, records.config, records.data, gap_fill)
    return Extracted(records, extraction, config, further)


# A kept record with the extraction it came from.
Kept = tuple[StoredRecord, ExtractionResult]


@dataclass(frozen=True)
class PageEnd:
    """How a page's run ends: the task's result, its capture, the records it kept (None when it extracted none), and a
    listing's page fields when it kept no record."""

    result: Finished | Failed
    capture_id: CaptureId
    kept: tuple[Kept, ...] | None = None
    fields: dict[str, RecordField] | None = None


async def page_run(
    conn: AsyncConnection,
    engine: AsyncEngine,
    store: ObjectStore,
    task: ClaimedTask,
    page: Page,
    extracted: Extracted | None,
    *,
    retryable: bool,
    caller_supplied: bool,
    reached: FinishedOutcome = "extracted",
) -> PageEnd:
    """End the task's page in the caller's transaction: its capture stored unless it was read from a stored one, the
    result of a page that was not reached (`unreached`), `reached` for a reached page that is not extracted (a crawl's
    `links_only`), else its extraction and records stored with its further pages' (`store_kept`)."""
    capture_id = (
        page.stored.capture_id
        if page.stored is not None
        else await store_capture(conn, store, page.result, page.site.id)
    )
    ended = unreached(page.result, capture_id, task, retryable=retryable)
    if ended is not None:
        return PageEnd(ended, capture_id)
    if extracted is None:
        return PageEnd(Finished(reached, capture_id=capture_id), capture_id)
    return await store_kept(
        conn, engine, store, task, (page.site, capture_id), extracted, caller_supplied=caller_supplied
    )


async def store_kept(
    conn: AsyncConnection,
    engine: AsyncEngine,
    store: ObjectStore,
    task: ClaimedTask,
    page: tuple[SiteRow, CaptureId],
    extracted: Extracted,
    *,
    caller_supplied: bool,
) -> PageEnd:
    """Store the page's extraction and record (`store_page_record`), then, unless its resolution failed, its further
    pages' under the task (`store_further`). A caller-supplied page keeps every record, complete or not; a
    crawl-discovered one only complete ones. The outcome is the page's own (`_outcome`); a listing that kept no
    record says why in the task's `last_error`."""
    site, capture_id = page
    records, extraction = extracted.records, extracted.extraction
    stored = await store_page_record(
        conn,
        engine,
        task,
        extraction,
        capture_id=capture_id,
        site_id=site.id,
        schema=records.schema,
        config=extracted.config,
        margins=records.margins,
        caller_supplied=caller_supplied,
        render=records.render,
    )
    if stored.outcome == "failed":
        return PageEnd(Failed(EXTRACTION_ERROR, transient=False, capture_id=capture_id), capture_id)
    further = await store_further(
        conn,
        engine,
        store,
        task,
        site.id,
        records.schema,
        extracted.further,
        records.margins,
        caller_supplied=caller_supplied,
    )
    # A detail page's one record, or a listing's one per row (none when it listed nothing).
    kept = {record.id: (record, extraction) for record in stored.records} | {r.id: (r, e) for r, e in further.records}
    own = stored.outcome
    reason, fields = _unlisted(records.schema, extraction) if not kept else (None, None)
    outcome = _outcome(own, further, tuple(record for record, _ in kept.values()))
    finished = Finished(outcome, reason=reason, capture_id=capture_id)
    return PageEnd(finished, capture_id, tuple(kept.values()), fields)


def _outcome(own: FinishedOutcome, further: StoredFurther, records: tuple[StoredRecord, ...]) -> FinishedOutcome:
    """The page's outcome: its own record's (`store_page_record`), `partial` when a further page's extraction failed;
    with further pages that kept records and a first page that kept all its own, every kept record decides, as one
    page's records do (`records_outcome`)."""
    if further.failed:
        return "partial"
    if not further.records or own == "partial":
        return own
    return records_outcome(records)


def _unlisted(schema: SchemaDef, extraction: ExtractionResult) -> tuple[str | None, dict[str, RecordField] | None]:
    """A listing without a kept record: why it has no rows, if it has none, and its page fields."""
    if schema.page_kind != "listing" or extraction.entity is None:
        return None, None
    return extraction.reason, page_fields(schema, extraction.entity)
