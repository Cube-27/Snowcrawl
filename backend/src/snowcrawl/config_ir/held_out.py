"""Held-out pages from storage: the stored captures of a (site, schema) that a config is
validated on, each parsed with its context and with the facts the engine stored for it.

The pages are a site's most recent captures with outcome `extracted` or `partial` that have records of the schema
version, each with its latest extraction. A locate run samples the surface's stored pages (`listed_nothing`):
those, and the listing pages of a job of the schema that listed nothing (no record, though a further page of its task
may have kept some), whose rows a render may show. A run may keep only the captures whose URL `keep` accepts (a URL
pattern is not SQL): the latest are read in order until `limit` are kept. A capture whose bundle is no
longer stored (captures expire) is skipped. What the engine published is the records the extraction stood for
(`task_record`; `record.data` holds published fields only): a detail page's one, or a listing's one per row in the
page's row order. A record equal to the entity's latest version stores nothing, so an extraction stands for that
version: a page captured again still has the values it published.

The references corroboration compares with (§8.3 step 4) are the valid structured-data or adapter candidates owned by
an entity that published a field of the page. Records do not say which row entity a listing record came from, so a
listing's rows have no references, and their fields stay `uncorroborated`.

`load_captures` reads named captures the same way: the pages a config was validated on (`provenance.validated_on`),
which the release check runs on.

A learning run reads the same pages, both as held-out pages and as sample pages: the published values with the
locator of each (`record.field_meta`), which induction generalizes and synthesis shows role A.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, get_args

from pydantic import JsonValue
from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.collect.currencies import with_page_declarations
from snowcrawl.config_ir.induction import Published, SamplePage
from snowcrawl.config_ir.validation import Facts, HeldOutPage
from snowcrawl.content.context import page_context
from snowcrawl.content.page import ParsedPage
from snowcrawl.db.models import (
    Candidate,
    Capture,
    CaptureId,
    Extraction,
    ExtractionId,
    Job,
    Record,
    SiteId,
    SourceKind,
    TaskRecord,
    UrlTask,
)
from snowcrawl.normalize.base import Context
from snowcrawl.normalize.dispatch import compared_form
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.storage.base import ObjectNotFound, ObjectStore
from snowcrawl.storage.capture_bundle import CaptureBundle, decode

# §6: the pages learning reads are those whose outcome is `extracted` or `partial`.
EXTRACTED: Final = ("extracted", "partial")
# Structured data or an adapter; a config's own values, the model's and a derived page URL
# are not references.
REFERENCE_KINDS: Final = frozenset(get_args(SourceKind)) - {"config", "llm", "derived"}


@dataclass(frozen=True, slots=True)
class StoredCandidate:
    """A valid candidate of an extraction: its owner, field, normalized value, source and whether it was published."""

    entity_ref: str
    field: str
    value: JsonValue
    source_kind: SourceKind
    chosen: bool


def _page_fields(schema: SchemaDef) -> set[str]:
    """A listing's page-scoped fields: every record holds them, and the first one's are the page's."""
    return {name for name, spec in schema.fields.items() if spec.scope == "page"}


def facts_of(
    schema: SchemaDef, records: Sequence[Mapping[str, JsonValue]], candidates: Iterable[StoredCandidate]
) -> tuple[Facts, tuple[Facts, ...]]:
    """The page's facts and a listing's rows' facts, from one extraction's records (`record.data`, in stored order)
    and its valid candidates."""
    if schema.page_kind == "listing":
        page_fields = _page_fields(schema)
        first = records[0] if records else {}
        page = Facts(published={name: value for name, value in first.items() if name in page_fields})
        rows = tuple(
            Facts(published={name: value for name, value in data.items() if name not in page_fields})
            for data in records
        )
        return page, rows
    candidates = list(candidates)
    owners = {c.entity_ref for c in candidates if c.chosen}
    references: dict[str, list[JsonValue]] = defaultdict(list)
    for c in candidates:
        if c.entity_ref in owners and c.source_kind in REFERENCE_KINDS:
            references[c.field].append(c.value)
    return Facts(published=dict(records[0]) if records else {}, references=dict(references)), ()


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """A record's published values (`record.data`) and their locators (`record.field_meta`)."""

    data: Mapping[str, JsonValue]
    locators: Mapping[str, str]

    def compared(self, schema: SchemaDef, ctx: Context) -> "StoredRecord":
        """The record with each value in the form learning compares (`normalize.dispatch.compared_form`): a URL is
        published as the page writes it, maybe relative, and compared resolved against the page's base URL."""
        data = {
            name: compared_form(value, spec, ctx) if (spec := schema.fields.get(name)) is not None else value
            for name, value in self.data.items()
        }
        return StoredRecord(data, self.locators)


def published_of(
    schema: SchemaDef, records: Sequence[StoredRecord]
) -> tuple[dict[str, Published], tuple[dict[str, Published], ...]]:
    """The page's published values with their locators, and a listing's rows', as `facts_of` splits them. A value
    stored without a locator is left out: induction and synthesis read a value together with where it came from."""

    def published(record: StoredRecord, names: Collection[str]) -> dict[str, Published]:
        return {
            name: Published(value, record.locators[name])
            for name, value in record.data.items()
            if name in names and name in record.locators
        }

    if schema.page_kind != "listing":
        return (published(records[0], schema.fields) if records else {}), ()
    page_fields = _page_fields(schema)
    row_fields = set(schema.fields) - page_fields
    page = published(records[0], page_fields) if records else {}
    return page, tuple(published(record, row_fields) for record in records)


@dataclass(frozen=True, slots=True)
class StoredPage:
    """A stored page as validation reads it (`held_out`) and as a learning sample (`sample`), and its capture, whose
    network bodies API discovery reads. `stated`: the fields any source gave a valid value for,
    published or not; `asked_llm`: whether its extraction called the LLM (role C, on a detail page's gaps) outside
    config mode, where it is asked only for the fields the config does not cover. Learning reads both for the fields the
    site never states (`config_ir.absent`)."""

    held_out: HeldOutPage
    sample: SamplePage
    bundle: CaptureBundle
    stated: frozenset[str] = frozenset()
    asked_llm: bool = False


async def load_held_out(
    conn: AsyncConnection, store: ObjectStore, site_id: SiteId, schema: SchemaDef, *, limit: int
) -> list[HeldOutPage]:
    """Up to `limit` held-out pages of `site_id` for `schema`'s version, most recent first."""
    return [page.held_out for page in await load_pages(conn, store, site_id, schema, limit=limit)]


async def load_pages(
    conn: AsyncConnection,
    store: ObjectStore,
    site_id: SiteId,
    schema: SchemaDef,
    *,
    limit: int,
    listed_nothing: bool = False,
    keep: Callable[[str], bool] | None = None,
) -> list[StoredPage]:
    """Up to `limit` stored pages of `site_id` for `schema`'s version, most recent first; with `listed_nothing`, a
    listing's pages that listed nothing too, and with `keep`, only those whose capture URL it accepts (module
    docstring)."""
    found = await _extractions(conn, site_id, schema, limit, listed_nothing, keep)
    return await _pages(conn, store, schema, found)


async def load_captures(
    conn: AsyncConnection, store: ObjectStore, schema: SchemaDef, captures: Sequence[CaptureId]
) -> list[HeldOutPage]:
    """The held-out pages of `captures` that have a record of `schema`'s version, in `captures` order, each with its
    latest such extraction."""
    rows = await conn.execute(_latest(schema).where(Capture.id.in_(captures)))
    found = {row.id: (row.id, row.object_key, row.extraction_id) for row in rows}
    pages = await _pages(conn, store, schema, [found[capture] for capture in captures if capture in found])
    return [page.held_out for page in pages]


async def _pages(
    conn: AsyncConnection,
    store: ObjectStore,
    schema: SchemaDef,
    extractions: Sequence[tuple[CaptureId, str, ExtractionId]],
) -> list[StoredPage]:
    """Each extraction's page, parsed, with the facts the engine stored; a capture whose bundle expired is skipped."""
    ids = [extraction_id for _, _, extraction_id in extractions]
    records = await _records(conn, ids, schema)
    candidates = await _candidates(conn, ids)
    llm_calls = await _llm_calls(conn, ids)
    # The bundles are independent reads; fetch them together.
    loaded = await asyncio.gather(*(_parsed(store, object_key) for _, object_key, _ in extractions))
    pages: list[StoredPage] = []
    for (capture_id, _, extraction_id), parsed in zip(extractions, loaded, strict=True):
        if parsed is None:
            continue
        bundle, page, ctx = parsed
        stored = [record.compared(schema, ctx) for record in records[extraction_id]]
        facts, rows = facts_of(schema, [record.data for record in stored], candidates[extraction_id])
        published, published_rows = published_of(schema, stored)
        pages.append(
            StoredPage(
                HeldOutPage(capture_id, page, ctx, facts, rows),
                SamplePage(page, ctx, published, published_rows),
                bundle,
                stated=frozenset(c.field for c in candidates[extraction_id]),
                asked_llm=llm_calls.get(extraction_id, 0) > 0,
            )
        )
    return pages


async def _llm_calls(conn: AsyncConnection, ids: Sequence[ExtractionId]) -> dict[ExtractionId, int]:
    """Each extraction's LLM calls, counted only where role C could be asked for every missing field: a config-mode
    extraction asks it only for the fields its config does not cover, so its call
    says nothing of a covered one (`config_ir.absent`)."""
    query = select(Extraction.id, Extraction.llm_calls).where(Extraction.id.in_(ids), Extraction.mode != "config")
    return {row.id: row.llm_calls for row in await conn.execute(query)}


async def _extractions(
    conn: AsyncConnection,
    site_id: SiteId,
    schema: SchemaDef,
    limit: int,
    listed_nothing: bool,
    keep: Callable[[str], bool] | None,
) -> list[tuple[CaptureId, str, ExtractionId]]:
    """The most recent captures with a record of `schema`'s version from an `extracted` or `partial` task, each with
    its latest such extraction; with `listed_nothing`, a listing's `partial` ones without a record too; with `keep`,
    only those whose URL it accepts."""
    latest = (
        _latest(schema, listed_nothing=listed_nothing)
        .add_columns(Capture.fetched_at, Capture.url)
        .join(UrlTask, UrlTask.id == Extraction.url_task_id)
        .where(Capture.site_id == site_id, UrlTask.outcome.in_(EXTRACTED))
        .subquery()
    )
    query = select(latest.c.id, latest.c.object_key, latest.c.extraction_id, latest.c.url).order_by(
        latest.c.fetched_at.desc(), latest.c.id.desc()
    )
    found: list[tuple[CaptureId, str, ExtractionId]] = []
    # Read in batches of `limit`, the latest first, until `limit` are kept or none are left.
    offset = 0
    while limit > 0 and len(found) < limit:
        batch = (await conn.execute(query.offset(offset).limit(limit))).all()
        found += [(row.id, row.object_key, row.extraction_id) for row in batch if keep is None or keep(row.url)]
        if len(batch) < limit:
            break
        offset += limit
    return found[:limit]


def _latest(schema: SchemaDef, *, listed_nothing: bool = False) -> Select[CaptureId, str, ExtractionId]:
    """Each capture's latest extraction that stood for a record of `schema` (ids are UUIDv7, so the greatest is the
    latest); with
    `listed_nothing`, or of a listing task of a job of `schema`, which listed nothing when it has none (`_extractions`
    keeps the tasks that ended `extracted` or `partial`)."""
    kept: ColumnElement[bool] = (
        select(TaskRecord.record_id)
        .join(Record, Record.id == TaskRecord.record_id)
        .where(
            TaskRecord.extraction_id == Extraction.id,
            Record.schema_id == schema.id,
        )
        .exists()
    )
    if listed_nothing and schema.page_kind == "listing":
        empty = (
            select(UrlTask.id)
            .join(Job, Job.id == UrlTask.job_id)
            .where(UrlTask.id == Extraction.url_task_id, Job.schema_id == schema.id)
            .exists()
        )
        kept = or_(kept, empty)
    return (
        select(Capture.id, Capture.object_key, Extraction.id.label("extraction_id"))
        .join(Extraction, Extraction.capture_id == Capture.id)
        .where(kept)
        .order_by(Capture.id, Extraction.id.desc())
        .ext(distinct_on(Capture.id))
    )


async def _records(
    conn: AsyncConnection, ids: Sequence[ExtractionId], schema: SchemaDef
) -> dict[ExtractionId, list[StoredRecord]]:
    query = (
        select(TaskRecord.extraction_id, Record.data, Record.field_meta)
        .join(Record, Record.id == TaskRecord.record_id)
        .where(TaskRecord.extraction_id.in_(ids), Record.schema_id == schema.id)
        .order_by(TaskRecord.position)  # the page's order
    )
    found: dict[ExtractionId, list[StoredRecord]] = defaultdict(list)
    for row in await conn.execute(query):
        locators = {name: meta["locator"] for name, meta in row.field_meta.items() if meta.get("locator")}
        found[row.extraction_id].append(StoredRecord(row.data, locators))
    return found


async def _candidates(conn: AsyncConnection, ids: Sequence[ExtractionId]) -> dict[ExtractionId, list[StoredCandidate]]:
    query = select(
        Candidate.extraction_id,
        Candidate.entity_ref,
        Candidate.field,
        Candidate.normalized_value,
        Candidate.source_kind,
        Candidate.chosen,
    ).where(Candidate.extraction_id.in_(ids), Candidate.normalize_status == "ok")
    found: dict[ExtractionId, list[StoredCandidate]] = defaultdict(list)
    for row in await conn.execute(query):
        found[row.extraction_id].append(
            StoredCandidate(row.entity_ref, row.field, row.normalized_value, row.source_kind, row.chosen)
        )
    return found


async def _parsed(store: ObjectStore, object_key: str) -> tuple[CaptureBundle, ParsedPage, Context] | None:
    try:
        blob = await asyncio.to_thread(store.get, object_key)
    except ObjectNotFound:
        return None
    # Decoding and reading the page's context are CPU work: a thread keeps the event loop serving meanwhile.
    return await asyncio.to_thread(_read, blob)


def _read(blob: bytes) -> tuple[CaptureBundle, ParsedPage, Context]:
    bundle = decode(blob)
    return bundle, *read_bundle(bundle)


def read_bundle(bundle: CaptureBundle) -> tuple[ParsedPage, Context]:
    """The bundle's page, parsed, with its context and the currencies it states, as the engine reads it."""
    page = ParsedPage.of(bundle)
    manifest = bundle.manifest
    ctx = page_context(
        page,
        manifest.final_url,
        manifest.fetched_at,
        request_url=manifest.url,
        page_url=manifest.page_url,
        headers=manifest.headers.response,
    )
    return page, with_page_declarations(ctx, page)
