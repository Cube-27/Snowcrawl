"""What a page task stores the same way wherever it runs (`scrape`, a worker's tasks): the
task's site, the usage its fetches count, a reached page's extraction and the records it keeps, a listing's further
pages under the task, and what a page with a record teaches its surface (progressive state, render mode, locate run).

These steps write in the caller's transaction, but for site memory (`acquire.memory`), which they learn through the
engine in short transactions of their own, so a rolled-back task leaves what its pages taught. They never settle the
task themselves (`page_run`).
"""

import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from pydantic import JsonValue
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.memory import Coverage, Remembered, learn_extraction, learn_fetch, remembered
from snowcrawl.acquire.persist import store_capture
from snowcrawl.acquire.rungs import NO_PLAN, FetchPlan
from snowcrawl.acquire.types import AttemptRecord, FetchResult
from snowcrawl.collect.base import Candidate
from snowcrawl.config_ir.active import fetch_plan, record_page
from snowcrawl.config_ir.drift import DriftMargins
from snowcrawl.config_ir.locate import Extract
from snowcrawl.config_ir.progressive import LocateRequest, StateKey, render_mode, request_locate, set_render_mode
from snowcrawl.config_ir.role_c import role_c
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.capture import CaptureMethod
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.db.models import Candidate as CandidateRow
from snowcrawl.db.models import (
    CaptureId,
    Extraction,
    ExtractionId,
    FinishedOutcome,
    JobId,
    NormalizeStatus,
    PageKind,
    ProgressiveState,
    Project,
    RenderMode,
    Site,
    SiteId,
    UsageResource,
)
from snowcrawl.db.queue import ClaimedTask
from snowcrawl.db.usage import record_usage
from snowcrawl.llm.client import LLMClient
from snowcrawl.normalize.base import Ambiguous, Invalid, Ok
from snowcrawl.normalize.url_key import canonicalize
from snowcrawl.recurring.versions import store_records
from snowcrawl.resolve.pipeline import EngineData, RoleC, extract_page
from snowcrawl.resolve.task import ActiveConfig, RecordSource, StoredRecord, page_records
from snowcrawl.resolve.types import ExtractionResult, FieldOutcome
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.scrape_request import RenderOptions
from snowcrawl.storage.base import ObjectStore
from snowcrawl.storage.capture_bundle import CaptureBundle

# The usage a fetch attempt counts: a request per HTTP or API attempt, the seconds of a browser's.
_REQUEST_METHODS: Final[frozenset[CaptureMethod]] = frozenset({"http", "api"})
# Jobs and sites belong to the project migrations create (single tenant).
DEFAULT_PROJECT: Final = select(Project.id).where(Project.name == "default").scalar_subquery()


@dataclass(frozen=True, slots=True)
class SiteRow:
    """A site as a fetch reads it: its id, what its memory says of the page kind fetched (`acquire.memory`), its
    robots.txt snapshot, and the adapters that declined its host (`Registry.for_host`)."""

    id: SiteId
    remembered: Remembered
    robots_snapshot: str | None
    adapter_declined: dict[str, str] = field(default_factory=dict)


async def site_row(conn: AsyncConnection, host: str, page_kind: PageKind | None = None) -> SiteRow:
    """The site of `host`, created in the default project the first time one of its pages is fetched, with what its
    memory says of its pages of `page_kind` (`acquire.memory.remembered`)."""
    await conn.execute(
        pg_insert(Site).values(project_id=DEFAULT_PROJECT, host=host).on_conflict_do_nothing(index_elements=[Site.host])
    )
    columns = (Site.id, Site.robots_snapshot, Site.adapter_declined)
    row = (await conn.execute(select(*columns).where(Site.host == host))).one()
    return SiteRow(row.id, await remembered(conn, row.id, page_kind), row.robots_snapshot, row.adapter_declined)


async def record_fetch_usage(
    conn: AsyncConnection, job_id: JobId, attempts: Iterable[AttemptRecord], proxy_pool: str | None
) -> None:
    """Count the fetch `attempts` as the job's usage: a request or the browser's seconds per attempt, and, for a fetch
    through `proxy_pool`, the bytes each attempt moved through it (`proxy_bytes`).
    Attempts, not fetch results, so that a caller keeps no page body only to count it."""
    for attempt in attempts:
        resource: UsageResource
        if attempt.method in _REQUEST_METHODS:
            resource, quantity = "http_requests", 1
        else:
            resource, quantity = "browser_seconds", math.ceil(attempt.duration_ms / 1000)
        metadata = {"method": attempt.method}
        await record_usage(
            conn, job_id, resource, provider=None, model_or_pool=proxy_pool, quantity=quantity, metadata=metadata
        )
        if proxy_pool is not None and attempt.transferred:
            await record_usage(
                conn,
                job_id,
                "proxy_bytes",
                provider=None,
                model_or_pool=proxy_pool,
                quantity=attempt.transferred,
                metadata=metadata,
            )


@dataclass(frozen=True, slots=True)
class Stored:
    """The task's outcome (`url_task.outcome`; a resolution `error` is `failed`), the rows written for it, and each
    record's content: at most one record for a detail page, one per kept row for a listing."""

    outcome: FinishedOutcome | Literal["failed"]
    extraction_id: ExtractionId
    records: tuple[StoredRecord, ...]
    result: ExtractionResult


async def store_extraction(
    conn: AsyncConnection,
    task: ClaimedTask,
    result: ExtractionResult,
    *,
    capture_id: CaptureId,
    site_id: SiteId,
    schema: SchemaDef,
    caller_supplied: bool,
    config: ActiveConfig | None = None,
    page_url: str | None = None,
) -> Stored:
    """Store `result`, `extract_page`'s for the task's capture, in `conn`'s transaction. `caller_supplied` is true for
    `scrape`, `batch_scrape` and `extract` URLs: an incomplete page then keeps its record (`partial`), where a
    crawl-discovered one gets `no_match` and none. The caller settles the task with the returned outcome.
    `scrape` extracts before it opens the transaction, so it holds no connection while the page resolves. `config` is
    the config a `config`-mode result ran; `page_url` the canonical URL of the document it read, when that is not the
    task's (a listing's further page)."""
    page_url = page_url or task.canonical_url
    extraction_id: ExtractionId = (
        await conn.execute(
            insert(Extraction)
            .values(
                url_task_id=task.id,
                capture_id=capture_id,
                engine_version=ENGINE_VERSION,
                config_version=None if config is None else config.version,
                mode=result.mode,
                llm_calls=len(result.llm_calls),
                diagnostics=list(result.diagnostics),
            )
            .returning(Extraction.id)
        )
    ).scalar_one()
    if result.candidates:
        await conn.execute(insert(CandidateRow), list(_candidate_rows(extraction_id, result)))
    if result.entity is None:
        return Stored("failed" if result.outcome == "error" else result.outcome, extraction_id, (), result)
    outcome, drafts = page_records(
        schema,
        result.entity,
        result.rows,
        site_id=site_id,
        page_url=page_url,
        caller_supplied=caller_supplied,
        diagnostics=result.diagnostics,
    )
    if not drafts:
        return Stored(outcome, extraction_id, (), result)
    source = RecordSource(extraction_id, task.id, task.job_id, site_id, result.mode, page_url)
    records = await store_records(conn, schema, drafts, source)
    return Stored(outcome, extraction_id, records, result)


def _candidate_rows(extraction_id: ExtractionId, result: ExtractionResult) -> Iterator[dict[str, object]]:
    chosen, reasons = _marks(result)
    for candidate in result.candidates:
        status, value = _normalized(candidate)
        key = _key(candidate)
        yield {
            "extraction_id": extraction_id,
            "entity_ref": candidate.entity_id,
            "field": candidate.field,
            "raw_value": storable(candidate.raw),
            "normalized_value": value,
            "normalize_status": status,
            "source_kind": candidate.source_kind,
            "independent": candidate.independent,
            "locator": candidate.locator,
            "chosen": key in chosen,
            "reject_reason": None if key in chosen else reasons.get(key),
        }


# PostgreSQL `jsonb` holds no U+0000 (its text type cannot) and no lone surrogate (not a Unicode scalar value, so no
# UTF-8 string holds it; RFC 8259 §8.2 lets JSON text escape one). A page's raw value may carry either.
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


def storable(value: JsonValue) -> JsonValue:
    """`value` with each U+0000 and lone surrogate in its strings and member names replaced by U+FFFD, the Unicode
    replacement character, so that `jsonb` takes it. A raw value is kept for triage; the normalized one is what
    publishes, and normalizers decline such strings."""
    if isinstance(value, str):
        return _UNSTORABLE.sub("\ufffd", value)
    if isinstance(value, list):
        return [storable(item) for item in value]
    if isinstance(value, dict):
        return {_UNSTORABLE.sub("\ufffd", key): storable(item) for key, item in value.items()}
    return value


# A candidate's identity on the page: one field's value, from one source, at one place, of one entity.
_CandidateKey = tuple[str, str, str, str]


def _key(candidate: Candidate) -> _CandidateKey:
    return candidate.field, candidate.entity_id, candidate.source_kind, candidate.locator


def _marks(result: ExtractionResult) -> tuple[set[_CandidateKey], dict[_CandidateKey, str]]:
    """The candidates some entity's field published, and why each other one did not. A listing resolves every row
    against all candidates, so a row's candidate is `not_owned` in every other row: the reason its own row gave says
    more, and is kept."""
    chosen: set[_CandidateKey] = set()
    reasons: dict[_CandidateKey, str] = {}
    for fields in (result.entity or {}, *result.rows):
        for outcome in _with_sub_fields(fields.values()):
            if outcome.chosen is not None:
                chosen.add(_key(outcome.chosen))
            for candidate, reason in outcome.rejected:
                if reasons.get(_key(candidate), "not_owned") == "not_owned":
                    reasons[_key(candidate)] = reason
    return chosen, reasons


def _with_sub_fields(outcomes: Iterable[FieldOutcome]) -> Iterator[FieldOutcome]:
    """Each outcome, and the sub-field outcomes of a row-mapped field's published rows."""
    for outcome in outcomes:
        yield outcome
        for item in outcome.items or ():
            yield from item.fields.values()


def _normalized(candidate: Candidate) -> tuple[NormalizeStatus, JsonValue]:
    match candidate.normalized:
        case Ok(value=value):
            return "ok", value
        case Ambiguous():
            return "ambiguous", None
        case Invalid():
            return "invalid", None


async def store_page_record(
    conn: AsyncConnection,
    engine: AsyncEngine,
    task: ClaimedTask,
    extraction: ExtractionResult,
    *,
    capture_id: CaptureId,
    site_id: SiteId,
    schema: SchemaDef,
    config: ActiveConfig | None,
    margins: DriftMargins | None,
    caller_supplied: bool = True,
    page_url: str | None = None,
    render: RenderOptions | None = None,
) -> Stored:
    """Store the extraction of a page and what it keeps: a caller-supplied page keeps its records, complete or not
        (`partial`), and a crawl-discovered one only complete records (else `no_match`); none when resolution failed.
    The outcome is `failed` then; a caller-supplied detail page that states no entity of the schema
        is `no_match` (`resolve.records.states_entity`); a caller-supplied listing without rows is `partial` with none
        (its
        page was read, and listed nothing). A page with a record, or a caller-supplied listing that listed nothing,
        counts
        toward its progressive state (an `extracted` or `partial` page: a locate run renders it) and applies the job's
        `render` (None: the job inherits the surface's) to its surface (`render_surface`): a listing's rows the HTTP
        page
        does not carry are for a locate run to find, as any field's are. A page with a record freshens the site's
        contract
        with its coverage; a caller-supplied page without one counts towards its staleness
        (`acquire.memory.learn_extraction`, through `engine`, in a transaction of its own). A page `config` ran on joins
        the config's drift window.
        `page_url` is `store_extraction`'s."""
    stored = await store_extraction(
        conn,
        task,
        extraction,
        capture_id=capture_id,
        site_id=site_id,
        schema=schema,
        caller_supplied=caller_supplied,
        config=config,
        page_url=page_url,
    )
    if config is not None:
        await record_page(conn, config, extraction, margins=margins, job_id=task.job_id)
    if stored.records:
        await learn_extraction(engine, site_id, schema.page_kind, coverage(schema, extraction))
        await _count_page(conn, site_id, schema)
        await render_surface(conn, task.job_id, site_id, schema, render, extraction)
    elif caller_supplied:
        # A caller-supplied page is of the schema's kind by the caller's word, so no record is a quality failure; a
        # crawl-discovered page may be of no kind the schema reads.
        await learn_extraction(engine, site_id, schema.page_kind, None)
        # A listing read whole that held no row; one whose rows state no item is `no_match` as a detail page is.
        read = stored.outcome != "failed" and extraction.entity is not None
        listed_nothing = schema.page_kind == "listing" and read and not extraction.rows
        if listed_nothing:
            await _count_page(conn, site_id, schema)
            await render_surface(conn, task.job_id, site_id, schema, render, extraction)
            return Stored("partial", stored.extraction_id, stored.records, stored.result)
        # A page that states no entity of the schema is `no_match`; one whose
        # resolution failed, `failed`.
        return stored if stored.outcome == "no_match" else replace(stored, outcome="failed")
    return stored


# A listing's further page (a kept state of its browser actions, or a page its recipe served), its extraction, and the
# site's active config as it ran on it.
Further = tuple[FetchResult, ExtractionResult, ActiveConfig | None]


@dataclass(frozen=True)
class StoredFurther:
    """A listing's further pages as stored: each record with the extraction it came from, and whether any page's
    extraction failed."""

    records: tuple[tuple[StoredRecord, ExtractionResult], ...] = ()
    failed: bool = False


async def extract_further(
    pages: Iterable[FetchResult],
    schema: SchemaDef,
    config: ActiveConfig | None,
    data: EngineData,
    gap_fill: RoleC | None,
) -> list[Further]:
    """The extraction of each further page of a listing that was reached, with the first page's schema, each with the
    site's config for its own capture."""
    extracted: list[Further] = []
    for page in pages:
        if page.block_class != "ok":
            continue
        own = None if config is None else config.for_capture(page.bundle.manifest, data.adapters)
        executor = None if own is None else own.executor
        parsed = ParsedPage.of(page.bundle)
        extraction = await extract_page(page.bundle, schema, executor, gap_fill, data=data, parsed=parsed)
        extracted.append((page, extraction, own))
    return extracted


async def store_further(
    conn: AsyncConnection,
    engine: AsyncEngine,
    store: ObjectStore,
    task: ClaimedTask,
    site_id: SiteId,
    schema: SchemaDef,
    extracted: Iterable[Further],
    margins: DriftMargins | None,
    *,
    caller_supplied: bool = True,
) -> StoredFurther:
    """Store each further page: its capture, its extraction and its records, under the listing's task, each under the
    canonical URL of the document it read (a row without its own URL is keyed by its page and position). What each
    fetch taught is remembered on the site through `engine` (`acquire.memory.learn_fetch`)."""
    records: list[tuple[StoredRecord, ExtractionResult]] = []
    failed = False
    for page, extraction, config in extracted:
        await learn_fetch(engine, site_id, page)
        capture_id = await store_capture(conn, store, page, site_id)
        kept = await store_page_record(
            conn,
            engine,
            task,
            extraction,
            capture_id=capture_id,
            site_id=site_id,
            schema=schema,
            config=config,
            margins=margins,
            caller_supplied=caller_supplied,
            page_url=canonicalize(page.bundle.manifest.page_url),
        )
        failed = failed or kept.outcome == "failed"
        records.extend((record, extraction) for record in kept.records)
    return StoredFurther(tuple(records), failed)


def coverage(schema: SchemaDef, extraction: ExtractionResult) -> Coverage:
    """The schema's fields an extraction published, on its entity or on any of its rows, and those it did not."""
    sets = (extraction.entity or {}, *extraction.rows)
    found = {name for fields in sets for name, outcome in fields.items() if outcome.status == "published"}
    return {"found": sorted(found & schema.fields.keys()), "missing": sorted(schema.fields.keys() - found)}


def records_outcome(records: tuple[StoredRecord, ...]) -> Literal["extracted", "partial"]:
    """A caller-supplied page's outcome: `extracted` when it kept records and every one is complete."""
    return "extracted" if records and all(record.draft.complete for record in records) else "partial"


async def surface_plan(
    conn: AsyncConnection,
    site_id: SiteId,
    schema: SchemaDef | None,
    config: ActiveConfig | None,
    render: RenderOptions | None,
) -> FetchPlan:
    """How a page of the surface is fetched: by its `config`'s field sources, in the
    job's `render` mode, else the surface's. A page read for no schema has no plan."""
    if schema is None:
        return NO_PLAN
    key = StateKey(site_id, schema.id, schema.page_kind)
    return fetch_plan(config, render.mode if render is not None else await render_mode(conn, key))


async def render_surface(
    conn: AsyncConnection,
    job_id: JobId,
    site_id: SiteId,
    schema: SchemaDef,
    render: RenderOptions | None,
    extraction: ExtractionResult,
) -> RenderMode:
    """Apply a job's `render` to its surface, and return the mode its pages are fetched
    by: the job's mode is stored as the surface's, and a job without one inherits it. In `locate` or `always` the
    surface's locate run is started once per job (`request_locate`), for the job's `render.fields`, else the fields
    `extraction` left unpublished (`always` locates even when none is: the sources of every field the config does not
    read, so the surface can fall back to HTTP)."""
    key = StateKey(site_id, schema.id, schema.page_kind)
    if render is not None:
        await set_render_mode(conn, key, render.mode)
    mode = render.mode if render is not None else await render_mode(conn, key)
    fields = list(render.fields) if render is not None and render.fields else unpublished(extraction, schema)
    if mode == "always" or (mode == "locate" and fields):
        await request_locate(conn, key, LocateRequest(job_id=job_id, fields=fields))
    return mode


def learning_extract(data: EngineData, llm: LLMClient) -> Extract:
    """A locate run's extraction of a render: the page's pipeline, cold,
    with role C on `llm` charged to the run's job."""

    async def extract(bundle: CaptureBundle, schema: SchemaDef, job_id: JobId) -> ExtractionResult:
        return await extract_page(bundle, schema, None, role_c(llm, job_id=job_id), data=data)

    return extract


def unpublished(extraction: ExtractionResult, schema: SchemaDef) -> list[str]:
    """The fields `extraction` publishes no value of: the entity's, and a listing's row fields no row publishes (each
    of `schema`'s row fields when it has no row)."""
    entity = extraction.entity or {}
    names = [name for name, outcome in entity.items() if outcome.status != "published"]
    rows = list(extraction.rows)
    row_names = {name for row in rows for name in row} or {
        name for name, spec in schema.fields.items() if spec.scope == "row"
    }
    names += sorted(n for n in row_names if all(row.get(n) is None or row[n].status != "published" for row in rows))
    return names


async def _count_page(conn: AsyncConnection, site_id: SiteId, schema: SchemaDef) -> None:
    """An extracted or partial page counts toward the progressive state of its site, schema and page kind
    (config learning reads it)."""
    state = ProgressiveState
    await conn.execute(
        pg_insert(state)
        .values(
            site_id=site_id,
            schema_id=schema.id,
            page_kind=schema.page_kind,
            pages_extracted=1,
        )
        .on_conflict_do_update(
            index_elements=[state.site_id, state.schema_id, state.page_kind],
            set_={"pages_extracted": state.pages_extracted + 1},
        )
    )
