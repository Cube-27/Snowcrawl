"""The captures service: an engineer capture of a sample page, replay a stored capture with the current engine,
for debugging, and read a stored capture's markdown again. Nothing a replay or a read makes is stored.

An engineer capture (`capture`) is a scrape of the page for `records` of the schema, run while the caller waits, as
`POST /v1/scrape` runs one (a `scrape` job, `services.scrape`), in the browser with the caller's page steps, cookies
and proxy pool, keeping every call the page makes (`FetchPolicy.calls` `all`): the capture and its record are stored,
a sample page an engineer chooses a custom recipe's calls from (`services.recipe_calls`).

A capture carries no schema of its own, so it replays against each schema its stored records follow, with the
schema's current document (a schema is edited in place). A schema the site has an active config for replays
with that config (`config` mode), as a page fetched now would extract; nothing joins its drift
window. No replay calls an LLM: `extract_page` gets no role C.
"""

import asyncio
from collections import OrderedDict
from typing import Annotated, Final

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.types import AttemptRecord
from snowcrawl.config_ir.active import active_config
from snowcrawl.config_ir.page_actions import MAX_ACTIONS, Action, WaitFor, wait_for_action
from snowcrawl.content.markdown import markdown
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.errors import Refusal
from snowcrawl.db.models import (
    Capture,
    CaptureId,
    Extraction,
    JobId,
    PageKind,
    Record,
    SchemaId,
    SiteId,
    TaskRecord,
    UrlTask,
)
from snowcrawl.normalize.url_key import canonicalize
from snowcrawl.resolve.pipeline import EngineData, extract_page
from snowcrawl.resolve.records import record_confidence
from snowcrawl.resolve.task import ActiveConfig, page_records
from snowcrawl.resolve.types import ExtractionResult
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services import scrape
from snowcrawl.services.fetch_options import FetchOptions, Secrets
from snowcrawl.services.page_run import PageOutcome
from snowcrawl.services.records import ExtractedRecord, ScrapedRecord
from snowcrawl.services.runtime import Caller
from snowcrawl.services.scrape import MAX_TIMEOUT_S, ScrapeRequest
from snowcrawl.services.scrape_request import Input, SchemaById
from snowcrawl.storage.artifacts import ARTIFACT_TYPES, artifact_key
from snowcrawl.storage.base import ObjectNotFound, ObjectStore
from snowcrawl.storage.capture_bundle import CaptureBundle, HeaderList, decode

_COLUMNS = (Capture.id, Capture.site_id, Capture.url, Capture.object_key)
_Capture = Row[CaptureId, SiteId, str, str]

# The markdown of the captures paged most recently, least recent first: enough for a few clients paging at once.
_MARKDOWN_KEPT = 8
_MARKDOWN: OrderedDict[CaptureId, str] = OrderedDict()


class CaptureNotFound(Refusal, LookupError):
    """No capture has the id, or its bundle is no longer stored (captures expire)."""


class NotReplayable(Exception):
    """The capture exists but no record was made from it, so no schema names what to extract."""


class CaptureRequest(Input):
    """The body of `POST /v1/captures`: a sample page of `schema_id`'s page kind, rendered in the browser after the
    page steps (`actions`, `wait_for`, as `fetch` names them), with the caller's `cookies`, through `proxy_pool`."""

    url: str
    schema_id: SchemaId
    page_kind: PageKind
    actions: Annotated[list[Action], Field(max_length=MAX_ACTIONS, default_factory=list)]
    wait_for: WaitFor | None = None
    cookies: Secrets = Field(default_factory=dict)
    proxy_pool: str | None = None
    locale: str | None = None
    timeout_s: float = Field(default=60, gt=0, le=MAX_TIMEOUT_S)

    @field_validator("url")
    @classmethod
    def _absolute_http_url(cls, url: str) -> str:
        if canonicalize(url) is None:
            raise ValueError("not an absolute http or https URL")
        return url

    @field_validator("wait_for")
    @classmethod
    def _translatable(cls, wait_for: WaitFor | None) -> WaitFor | None:
        if wait_for is not None:
            wait_for_action(wait_for)  # raises for a selector cssselect does not translate
        return wait_for

    def scrape_request(self) -> ScrapeRequest:
        fetch = FetchOptions(
            method_hint="browser",
            cookies=self.cookies,
            actions=self.actions,
            wait_for=self.wait_for,
            proxy_pool=self.proxy_pool,
            cache="bypass",
        )
        return ScrapeRequest(
            url=self.url,
            formats=["records"],
            schema=SchemaById(id=self.schema_id),
            page_kind=self.page_kind,
            fetch=fetch,
            locale=self.locale,
            timeout_s=self.timeout_s,
        )


class CaptureResult(BaseModel):
    """An engineer capture: the stored capture (None when nothing was sent: `reason` says why, as a scrape's does),
    how its page ended, the records stored from it, and every fetch attempt."""

    capture_id: CaptureId | None
    url: str
    outcome: PageOutcome
    reason: str | None
    records: list[ScrapedRecord]
    attempts: tuple[AttemptRecord, ...]


async def capture(caller: Caller, request: CaptureRequest) -> CaptureResult:
    """An engineer capture of `request.url` (module docstring). Raises as `services.scrape.scrape` does."""
    runtime = caller.runtime
    scraped = await scrape.scrape(
        runtime.database,
        runtime.fetching,
        request.scrape_request(),
        llm=runtime.llm,
        data=runtime.engine_data,
        margins=runtime.drift_margins,
        calls="all",
    )
    return CaptureResult(
        capture_id=scraped.capture_id,
        url=request.url,
        outcome=scraped.outcome,
        reason=scraped.reason,
        records=scraped.records or [],
        attempts=() if scraped.fetch is None else scraped.fetch.attempts,
    )


async def replay(
    engine: AsyncEngine, store: ObjectStore, data: EngineData, capture_id: CaptureId, job_id: JobId | None = None
) -> list[ExtractedRecord]:
    """The records the current engine makes from the capture, one schema after another (by id), against the schemas
    of the records stored from it, those of `job_id` when given. A page that resolves nothing (blocked, an error) adds
    none; an incomplete one adds its record with `complete=false`, as for any caller-supplied URL. It reads the
    capture's stored bundle and the database, and reaches no network (`reextract_job` runs it for a job's captures)."""
    async with engine.connect() as conn:
        capture = await _capture(conn, capture_id)
        if job_id is None:
            # The records the capture's extractions stood for, equal records stored from an earlier capture included.
            stored = select(Record.schema_id).distinct().join(TaskRecord, TaskRecord.record_id == Record.id)
            stored = stored.join(Extraction, Extraction.id == TaskRecord.extraction_id)
            stored = stored.where(Extraction.capture_id == capture_id)
        else:
            # The records the job's tasks stand for, equal records of an earlier job's included.
            stored = select(Record.schema_id).distinct().join(TaskRecord, TaskRecord.record_id == Record.id)
            stored = stored.join(UrlTask, UrlTask.id == TaskRecord.url_task_id)
            stored = stored.where(UrlTask.job_id == job_id, UrlTask.capture_id == capture_id)
        schema_ids = (await conn.execute(stored.order_by(Record.schema_id))).scalars()
        schemas = [await registry.get_by_id(conn, schema_id) for schema_id in schema_ids]
        configs = [
            await active_config(conn, capture.site_id, schema, data.vocabulary, capture.url) for schema in schemas
        ]
    if not schemas:
        raise NotReplayable(f"no record was made from capture {capture_id}, so no schema says what to extract")
    bundle = await _bundle(store, capture)
    return [
        record
        for schema, config in zip(schemas, configs, strict=True)
        for record in await _replay(bundle, schema, config, data, capture)
    ]


async def markdown_of(engine: AsyncEngine, store: ObjectStore, capture_id: CaptureId) -> str:
    """The capture's markdown, made again from its bundle: a format is a function of the capture alone, so it
    equals the markdown the capture's scrape answered with. The MCP `scrape` tool pages it, one call per page, so the
    last few captures' markdown is kept and a later page is not made again; the capture row is still read each time, so
    a capture that is gone is not found."""
    async with engine.connect() as conn:
        capture = await _capture(conn, capture_id)
    made = _MARKDOWN.get(capture_id)
    if made is None:
        bundle = await _bundle(store, capture)
        manifest = bundle.manifest
        made = await asyncio.to_thread(lambda: markdown(ParsedPage.of(bundle), manifest.final_url))
    _MARKDOWN[capture_id] = made
    _MARKDOWN.move_to_end(capture_id)
    while len(_MARKDOWN) > _MARKDOWN_KEPT:
        _MARKDOWN.popitem(last=False)
    return made


async def artifact(engine: AsyncEngine, store: ObjectStore, capture_id: CaptureId, name: str) -> bytes:
    """The artifact `name` of the capture (`storage.artifacts`): the screenshot or PDF the browser made of its page.
    Raises `CaptureNotFound` for a capture that is gone, and for one that has no such artifact."""
    if name not in ARTIFACT_TYPES:
        raise CaptureNotFound(f"no artifact {name!r}")
    async with engine.connect() as conn:
        capture = await _capture(conn, capture_id)
    try:
        return await asyncio.to_thread(store.get, artifact_key(capture.object_key, name))
    except (ObjectNotFound, ValueError) as e:  # a ValueError: the bundle is outside `captures/`, so has none
        raise CaptureNotFound(f"capture {capture_id} has no artifact {name!r}") from e


class CallDetail(BaseModel):
    """One call a capture kept (`GET /v1/captures/{id}/calls/{n}`): the request as sent (headers as recorded: no
    cookies, the caller's own values masked), the answer's status, headers and text (its UTF-8 decoding, at most
    `ANSWER_MAX_BYTES`), the page step it followed and whether it went to the page's own site, the guard prefix its JSON
    opens with, and why its body was not kept (past a cap), when it was not."""

    index: int
    method: str | None
    url: str
    status: int
    content_type: str | None
    resource_type: str | None
    step: int | None
    same_site: bool | None
    guard_prefix: str | None
    request_headers: HeaderList | None
    request_body: str | None
    response_headers: HeaderList | None
    size: int
    answer: str
    omitted: str | None


# A capture keeps no body past 2 MB (`acquire.browser_capture`); the answer an API reply holds is bounded the same.
ANSWER_MAX_BYTES: Final = 2_000_000


async def call(engine: AsyncEngine, store: ObjectStore, capture_id: CaptureId, index: int) -> CallDetail:
    """The `index`th call the capture kept. Raises `CaptureNotFound` for a capture that is gone or has no such call."""
    bundle = await bundle_of(engine, store, capture_id)
    entries = bundle.manifest.network_bodies
    if not 0 <= index < len(entries):
        raise CaptureNotFound(f"capture {capture_id} has no call {index}")
    entry, data = entries[index], bundle.network[index]
    return CallDetail(
        index=index,
        method=entry.method,
        url=entry.url,
        status=entry.status,
        content_type=entry.content_type,
        resource_type=entry.resource_type,
        step=entry.action,
        same_site=entry.same_site,
        guard_prefix=entry.guard_prefix,
        request_headers=entry.request_headers,
        request_body=entry.request_body,
        response_headers=entry.response_headers,
        size=len(data),
        answer=data[:ANSWER_MAX_BYTES].decode("utf-8", errors="replace"),
        omitted=entry.omitted,
    )


async def bundle_of(engine: AsyncEngine, store: ObjectStore, capture_id: CaptureId) -> CaptureBundle:
    """The capture's stored bundle. Raises `CaptureNotFound` for a capture that is gone."""
    async with engine.connect() as conn:
        capture = await _capture(conn, capture_id)
    return await _bundle(store, capture)


async def _capture(conn: AsyncConnection, capture_id: CaptureId) -> _Capture:
    capture = (await conn.execute(select(*_COLUMNS).where(Capture.id == capture_id))).one_or_none()
    if capture is None:
        raise CaptureNotFound(f"no capture {capture_id}")
    return capture


async def _bundle(store: ObjectStore, capture: _Capture) -> CaptureBundle:
    try:
        blob = await asyncio.to_thread(store.get, capture.object_key)
    except ObjectNotFound as e:
        raise CaptureNotFound(f"capture {capture.id}: its bundle is no longer stored") from e
    return await asyncio.to_thread(decode, blob)


async def _replay(
    bundle: CaptureBundle, schema: SchemaDef, config: ActiveConfig | None, data: EngineData, capture: _Capture
) -> list[ExtractedRecord]:
    if config is not None:
        config = config.for_capture(bundle.manifest, data.adapters)
    result = await extract_page(bundle, schema, None if config is None else config.executor, None, data=data)
    # Their entity keys are not returned, so the capture's URL stands in for the task's canonical one.
    return unstored_records(schema, result, site_id=capture.site_id, page_url=capture.url)


def unstored_records(
    schema: SchemaDef, result: ExtractionResult, *, site_id: SiteId, page_url: str
) -> list[ExtractedRecord]:
    """The records a page task would store from `result`, the extraction of the page at `page_url`, without storing
    them: a listing's rows are deduplicated by their URLs."""
    if result.entity is None:
        return []
    _, drafts = page_records(
        schema,
        result.entity,
        result.rows,
        site_id=site_id,
        page_url=page_url,
        caller_supplied=True,
        diagnostics=result.diagnostics,
    )
    return [
        ExtractedRecord.of_content(
            schema, draft, mode=result.mode, confidence=record_confidence(schema, draft.field_meta)
        )
        for draft in drafts
    ]
