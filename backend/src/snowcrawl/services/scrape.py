"""The scrape service: one URL fetched while the caller waits, and returned in the content formats
it asks for. `POST /v1/scrape` calls it; the MCP `scrape` tool will.

A scrape is a `job` of kind `scrape` with one `url_task`, run here rather than through the queue, so its usage and
events are kept like any job's. The task is fetched as a worker fetches one: robots.txt, the site's rate, the rungs,
and its page runs as a worker's does (`page_run`), but not retried: the caller waits. A
fetch that does not reach the page is an outcome, not an error. The whole scrape is bounded
by `timeout_s`; past it the job fails and nothing is returned. A target the SSRF guard refuses is never fetched.

A `schema.prompt` is drafted into a schema by role S from the cleaned text of the fetched page, and the
draft is returned; the same prompt returns the stored draft without a call. A page that was not reached is not drafted
from. A draft the LLM could not give (no provider, no budget, an invalid reply) fails the job, like a timeout.
`schema.fields` is drafted from the names before the job starts, so a draft that fails stores nothing.

`records` extracts the page with `extract_page` against the schema named by id, given inline, or
drafted, and stores the extraction and the records (`page_store`: a listing has one per row). A scrape URL is
caller-supplied, so a page whose required fields don't all publish keeps its record, `complete=false`, with outcome
`partial`; a page that states no entity of the schema (a maintenance or error page served 200) is `no_match` with no
record, as in a batch. A site with an active config for the schema extracts in `config` mode,
and the page joins the config's drift window. A scrape that asks for the record only is served by the config's
API recipe when it has one: a JSON body has no markdown, links or metadata of the page, so other formats
fetch the page. The formats, the draft and the records all read one parse of the page. With `max_age`, a
capture of the same canonical URL that reached its page within that many seconds is read again instead of fetching.
"""

import asyncio
import os
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Final, Self, TypedDict
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, field_validator, model_serializer, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.acquire.types import ActionsReport, BrowserActions, CallsMode, FetchResult
from snowcrawl.collect.run import Adapters
from snowcrawl.config_ir.active import active_config
from snowcrawl.config_ir.drift import DriftMargins
from snowcrawl.config_ir.role_c import role_c
from snowcrawl.content.chunks import Chunk, ChunkOptions
from snowcrawl.content.fit import FitOptions
from snowcrawl.content.metadata import PageMetadata
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.egress import Egress
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.core.errors import Refusal
from snowcrawl.db.models import CaptureId, Job, JobId, PageKind, SchemaId, SiteId, TaskOutcome
from snowcrawl.db.queue import ClaimedTask, Failed, Finished, complete_task, fail_task, insert_claimed, settle_task
from snowcrawl.llm.client import NO_LLM, LLMClient, LLMFailure
from snowcrawl.llm.roles.s_labels import with_labels
from snowcrawl.llm.roles.s_schema import draft_schema
from snowcrawl.normalize.url_key import canonicalize
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.resolve.task import ActiveConfig
from snowcrawl.resolve.types import Probability
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.fetch_options import CAPTURE_FORMATS, FetchOptions, check_proxy_pool
from snowcrawl.services.fetching import (
    Fetching,
    Growth,
    TargetRejected,
    check_target,
)
from snowcrawl.services.location import job_egress
from snowcrawl.services.page_formats import (
    FetchSummary,
    FormatOptions,
    Formats,
    fetch_summary,
    format_member,
    page_formats,
)
from snowcrawl.services.page_run import (
    Extracted,
    Page,
    PageEnd,
    PageOutcome,
    Records,
    extract_run,
    page_run,
)
from snowcrawl.services.page_store import (
    DEFAULT_PROJECT,
    record_fetch_usage,
    site_row,
)
from snowcrawl.services.page_task import NotReached, PageTask, fetch_task
from snowcrawl.services.polite import IN_PLACE
from snowcrawl.services.records import RecordField, ScrapedRecord
from snowcrawl.services.scrape_paging import Actions, actions_deadline, check_browser_actions
from snowcrawl.services.scrape_request import (
    MIN_CONFIDENCE_DESCRIPTION,
    RENDER_AS_GIVEN,
    RENDER_DESCRIPTION,
    Input,
    PageFormat,
    RenderOptions,
    SchemaPrompt,
    SchemaRequest,
    SchemaUnusable,
    check_formats,
    check_records,
    check_render,
    check_usable,
    named_schema,
    render_as_given,
    stored_request,
)
from snowcrawl.services.stored_captures import NOT_CACHED, NotCached
from snowcrawl.storage.artifacts import Artifact
from snowcrawl.storage.capture_bundle import CaptureManifest

MAX_TIMEOUT_S: Final = 120
# A scrape task's `last_error` when it failed for no reason the service declares.
INTERNAL_ERROR: Final = "internal_error"
# The process that runs a scrape's task, named as a worker names itself (`snowcrawl.worker.loop`).
_CLAIMANT: Final = f"api:{socket.gethostname()}:{os.getpid()}"


class Budget(Input):
    """A request's own bound on its job's LLM calls. A request without one takes the project's `llm_call_cap`, and a
    project without a cap bounds none (owner, 2026-10-16; `db.usage.budget_remaining`)."""

    llm_calls: NonNegativeInt


class ScrapeRequest(Input):
    """The body of `POST /v1/scrape`. `records` needs a schema and a `page_kind`, and the schema's page kind
    must be that one. A `schema.prompt` is drafted and returned, and `budget.llm_calls` (else the project's cap) bounds
    its draft; `schema.fields` is drafted before the job, which no budget counts. `max_age` reuses a capture fetched
    within that many seconds. `browser_actions` collects more of a listing's rows in the browser; a listing
    scrape for `records` in `auto` without it takes the default actions where its page is rendered."""

    url: str
    formats: list[PageFormat] = Field(min_length=1)
    schema_: SchemaRequest | None = Field(default=None, alias="schema")
    page_kind: PageKind | None = None
    max_age: NonNegativeInt = 0  # seconds
    fetch: FetchOptions = FetchOptions()
    locale: str | None = None
    timeout_s: float = Field(default=60, gt=0, le=MAX_TIMEOUT_S)
    budget: Budget | None = None
    browser_actions: BrowserActions | None = None
    fit: FitOptions | None = None  # `markdown_fit`'s query and block limit
    chunking: ChunkOptions | None = None  # `chunks`' strategy, size and overlap
    min_confidence: Probability | None = Field(default=None, description=MIN_CONFIDENCE_DESCRIPTION)
    render: RenderOptions = Field(default=RenderOptions(), description=RENDER_DESCRIPTION)

    model_config = RENDER_AS_GIVEN
    _render_as_given = model_serializer(mode="wrap")(render_as_given)

    @field_validator("url")
    @classmethod
    def _absolute_http_url(cls, url: str) -> str:
        if canonicalize(url) is None:
            raise ValueError("not an absolute http or https URL")
        return url

    @model_validator(mode="after")
    def _records_have_a_schema(self) -> Self:
        check_records(self.formats, self.schema_, self.page_kind)
        check_browser_actions(self.browser_actions, self.page_kind, self.max_age, self.fetch)
        check_formats(self.formats, self.fetch, self.fit, self.chunking)
        if self.browser_actions is not None and CAPTURE_FORMATS.intersection(self.formats):
            raise ValueError("screenshot and pdf are of the page as first rendered, so browser_actions cannot ask them")
        return self

    @property
    def format_options(self) -> FormatOptions:
        return FormatOptions(fit=self.fit, chunking=self.chunking)


class Usage(BaseModel):
    """What the scrape's job used."""

    # The API sends every member, defaulted ones too, so its document marks them all `required` (OpenAPI 3.1).
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    browser_seconds: int = 0
    http_requests: int = 0


class ScrapeResult(BaseModel):
    """The answer to a scrape. A page that robots.txt kept from being fetched has no capture, so
    `capture_id`, `final_url`, `fetched_at` and `fetch` are None, and `reason` says why, as the task's `last_error`
    does: `robots` for a disallow, `robots_unreachable` for a robots.txt that could not be read (RFC
    9309 §2.3.1.4), `slow_down` for a host whose turn at its rate was more than a minute off; for a fetched page a
    vendor's challenge kept out, `challenge: <vendor>`; for a fetched listing whose extraction gave no rows,
    `no_rows: <cause>` (`no_rows` in `resolve.pipeline`, "Scrape outcomes, not errors, for page
    failures"); None for any other page that was fetched. `schema` is the draft of a `schema.prompt`."""

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    job_id: JobId
    capture_id: CaptureId | None
    url: str
    final_url: str | None
    fetched_at: datetime | None
    outcome: PageOutcome
    reason: str | None
    fetch: FetchSummary | None
    markdown: str | None = format_member()
    markdown_fit: str | None = format_member()
    chunks: list[Chunk] | None = format_member()
    html: str | None = format_member()
    raw_html: str | None = format_member()
    links: list[str] | None = format_member()
    metadata: PageMetadata | None = format_member()
    screenshot: Artifact | None = format_member()
    pdf: Artifact | None = format_member()
    schema_: SchemaDef | None = format_member(alias="schema")
    records: list[ScrapedRecord] | None = format_member()
    # A listing that kept no record: its page-scope fields (`next_page_url`), in the record field shape.
    page: dict[str, RecordField] | None = format_member()
    browser_actions: ActionsReport | None = format_member()
    usage: Usage


class ScrapeTimedOut(Refusal):
    def __init__(self, timeout_s: float, what: str = "scrape") -> None:
        super().__init__(f"the {what} did not finish within {timeout_s:g} s")


class RecordsUnavailable(Refusal):
    """`records` was asked of a process without the engine's data files (`EngineData`)."""


@dataclass(frozen=True)
class _Work:
    """What a scrape works with besides fetching: the LLM client, the engine's data and drift margins, and the schema
    `records` extracts with when the request names it (a prompt's is drafted after the fetch)."""

    llm: LLMClient
    data: EngineData | None
    margins: DriftMargins | None
    schema: SchemaDef | None
    deadline: float | None = None  # on `time.monotonic`: when `request.timeout_s` runs out
    render: RenderOptions | None = None  # the request's; None: its surface's
    calls: CallsMode = "data"  # the calls its capture keeps


@dataclass(frozen=True)
class _Derived:
    """What a scrape makes of a page it reached: the formats, the draft of a prompt, and the page's extraction with
    what it extracted with."""

    formats: Formats
    drafted: SchemaDef | None = None
    extracted: Extracted | None = None


_NOTHING: Final = _Derived(Formats())


class _FetchMembers(TypedDict):
    capture_id: CaptureId | None
    final_url: str | None
    fetched_at: datetime | None
    fetch: FetchSummary | None


_NOT_FETCHED = _FetchMembers(capture_id=None, final_url=None, fetched_at=None, fetch=None)


async def scrape(
    engine: AsyncEngine,
    fetching: Fetching,
    request: ScrapeRequest,
    *,
    llm: LLMClient = NO_LLM,
    data: EngineData | None = None,
    margins: DriftMargins | None = None,
    calls: CallsMode = "data",
) -> ScrapeResult:
    """Run `request` as a scrape job; `records` reads `data`, and an active config's drift `margins`; its capture
    keeps `calls` (`FetchPolicy.calls`: `all` for an engineer capture, `services.captures`). Raises, before
    any job is created: `TargetRejected` for a target refused outright, `RecordsUnavailable` for `records` without
    `data`, `SchemaUnusable` for a schema id `records` cannot use, and the schemas service's `SchemaConflict` or
    `InvalidSchema` for an inline schema, and the LLM client's `LLMFailure` for field names it could not draft.
    Raises, failing the job: `TargetRejected` for a refused redirect,
    `ScrapeTimedOut` past `request.timeout_s`, the LLM client's `LLMFailure` when a prompt could not be drafted, and
    `SchemaUnusable` for a draft of another page kind. Any other exception fails the job with `internal_error` and
    propagates."""
    await check_target(fetching.connector, request.url)
    check_proxy_pool(request.fetch, fetching.egresses.pools)
    pinned = None if data is None else data.with_min_confidence(request.min_confidence)
    render = request.render if "render" in request.model_fields_set else None
    schema = await _named_schema(engine, request, data, llm)
    work = _Work(llm, pinned, margins, schema, render=render, calls=calls)
    egress = job_egress(fetching.egresses, request.fetch)
    task = await _start(engine, request, work.schema, egress)
    # The browser's budget is the timeout's, which starts here (`acquire.rungs.BROWSER_MIN_S`).
    work = replace(work, deadline=time.monotonic() + request.timeout_s)
    try:
        async with asyncio.timeout(request.timeout_s):
            return await _run(engine, fetching, work, request, task)
    except TimeoutError as e:
        await _fail(engine, task, "timeout")
        raise ScrapeTimedOut(request.timeout_s) from e
    except TargetRejected as e:
        await _fail(engine, task, e.reason)
        raise
    except SchemaUnusable:
        await _fail(engine, task, "invalid_schema")
        raise
    except LLMFailure as e:
        await _fail(engine, task, e.code)
        raise
    except NotCached:
        await _fail(engine, task, NOT_CACHED)
        raise
    except Exception:
        # Anything else is this process's fault; the job still ends now, not when the task's lease runs out.
        await _fail(engine, task, INTERNAL_ERROR)
        raise


async def _named_schema(
    engine: AsyncEngine, request: ScrapeRequest, data: EngineData | None, llm: LLMClient
) -> SchemaDef | None:
    """The schema `records` extracts with when the request names it by id, inline or by field names; None without
    `records`, and for a prompt, whose draft needs the page."""
    if "records" not in request.formats:
        return None
    if data is None:
        raise RecordsUnavailable("this process has not loaded the engine's data files")
    named = await named_schema(engine, request.schema_, request.page_kind, llm)
    check_render(request.render, named)
    return named


async def _start(engine: AsyncEngine, request: ScrapeRequest, schema: SchemaDef | None, egress: Egress) -> ClaimedTask:
    """The running job, with its task claimed by this process."""
    stored = stored_request(request)
    [task] = await start_job(
        engine, stored, None if schema is None else schema.id, egress, [request.url], request.page_kind
    )
    return task


async def start_job(
    engine: AsyncEngine,
    request: dict[str, Any],
    schema_id: SchemaId | None,
    egress: Egress,
    urls: Sequence[str],
    page_kind: PageKind | None,
) -> list[ClaimedTask]:
    """A running `scrape` job storing `request`, with a task per URL of `urls` claimed by this process, in order.
    Committed on its own, so that a scrape that fails or times out can still record that on them."""
    async with engine.begin() as conn:
        job_id = (
            await conn.execute(
                pg_insert(Job)
                .values(
                    project_id=DEFAULT_PROJECT,
                    kind="scrape",
                    request=request,
                    schema_id=schema_id,
                    status="running",
                    started_at=func.now(),
                    engine_version=ENGINE_VERSION,
                    egress_country=egress.country,
                    egress_basis=egress.basis,
                )
                .returning(Job.id)
            )
        ).scalar_one()
        return [await insert_claimed(conn, job_id, url, canonicalize(url) or url, page_kind, _CLAIMANT) for url in urls]


async def _fail(engine: AsyncEngine, task: ClaimedTask, error: str) -> None:
    async with engine.begin() as conn:
        await fail_task(conn, task, error, transient=False)


async def _run(
    engine: AsyncEngine, fetching: Fetching, work: _Work, request: ScrapeRequest, task: ClaimedTask
) -> ScrapeResult:
    """The fetch runs outside any transaction, so a slow page holds no connection or row lock; what it found is
    written in one transaction after it, by the page's run (`page_run`). A stored capture `fetch.cache` and `max_age`
    allow is read instead of fetching (`read_stored`)."""
    async with engine.begin() as conn:
        site = await site_row(conn, urlsplit(request.url).hostname or "", request.page_kind)
        config = await _config(conn, site.id, work, request.url)
    records = None
    if work.schema is not None and work.data is not None:
        records = Records(work.schema, work.data, config, work.margins, work.render)
    actions = request.browser_actions
    paging = None if actions is None else Actions(actions, request.timeout_s)
    growth = None if paging is not None else _growth(request, fetching)
    page_task = PageTask(
        task,
        request.fetch,
        request.locale,
        work.deadline,
        request.formats,
        records=records,
        # A JSON body has no markdown, links or metadata of the page.
        served=set(request.formats) == {"records"},
        planned=True,
        max_age=request.max_age,
        growth=growth,
        paging=paging,
        calls=work.calls,
    )
    fetched = await fetch_task(engine, fetching, site, page_task, IN_PLACE)
    page = _reached(fetched)
    report = paging.report if paging is not None else _grew(growth, page)
    # Formats, the draft and the extraction come before the write, so a timeout or an LLM failure while they are made
    # still finds the task claimed. They are made only from a page that was reached (§4.8: no partial formats).
    derived = await _derive_reached(engine, work, request, task, page, config)
    async with engine.begin() as conn:
        end = None
        if isinstance(page, str):
            await complete_task(conn, task, "blocked", reason=page)
        else:
            store = fetching.store
            end = await page_run(
                conn, engine, store, task, page, derived.extracted, retryable=False, caller_supplied=True
            )
            await settle_task(conn, task, end.result)
        await record_fetch_usage(conn, task.job_id, fetched.attempts, fetched.proxy_pool)
        usage = await job_usage(conn, task.job_id)
    return _answer(request, task, (page, end), derived, report, usage, fetching.rungs.adapters)


def _reached(fetched: Page | NotReached) -> Page | str:
    """The page, or why nothing was sent (`blocked`). Raises `TargetRejected` when the guard refused a request of
    the fetch."""
    if isinstance(fetched, Page):
        return fetched
    match fetched.task_result:
        case Finished(reason=str(reason)):
            return reason
        case Failed(error=error):
            raise TargetRejected(error)
    raise TypeError(f"a fetch the caller waits for ended {fetched.task_result!r}")


def _grew(growth: Growth | None, page: Page | str) -> ActionsReport | None:
    """What the default browser actions did, when the page kept is the listing they grew."""
    grew = None if growth is None or isinstance(page, str) else growth.of(page.result)
    return None if grew is None else grew.report


def _answer(
    request: ScrapeRequest,
    task: ClaimedTask,
    run: tuple[Page | str, PageEnd | None],
    derived: _Derived,
    report: ActionsReport | None,
    usage: Usage,
    adapters: Adapters,
) -> ScrapeResult:
    """The answer to the scrape: how its page's run ended (`blocked` with the reason when nothing was sent), the
    formats, draft and records it made, and the job's usage. A failed page answers nothing derived from it."""
    page, end = run
    if isinstance(page, str) or end is None:
        reason = page if isinstance(page, str) else None
        return ScrapeResult(
            job_id=task.job_id, url=request.url, outcome="blocked", reason=reason, usage=usage, **_NOT_FETCHED
        )
    result = end.result
    if isinstance(result, Failed):
        outcome, reason, derived = _page_outcome(result.exhausted), None, _NOTHING
    else:
        outcome, reason = _page_outcome(result.outcome), result.reason
    return ScrapeResult(
        job_id=task.job_id,
        url=request.url,
        outcome=outcome,
        reason=reason,
        usage=usage,
        schema_=derived.drafted,
        records=_scraped(end, derived.extracted),
        page=end.fields,
        browser_actions=report,
        **_fetch_members(page.result, end.capture_id, adapters),
        **derived.formats,
    )


def _page_outcome(outcome: TaskOutcome) -> PageOutcome:
    """A scrape's task outcome, as its answer states it: a scrape reads its page whatever rules a crawl has."""
    if outcome == "links_only":
        raise ValueError("a scrape's page is never links_only")
    return outcome


async def _derive_reached(
    engine: AsyncEngine,
    work: _Work,
    request: ScrapeRequest,
    task: ClaimedTask,
    page: Page | str,
    config: ActiveConfig | None,
) -> _Derived:
    """What `_derive` makes of the page when it was reached, else nothing."""
    if isinstance(page, str) or not page.reached:
        return _NOTHING
    return await _derive(engine, work, request, task, page, config)


def _growth(request: ScrapeRequest, fetching: Fetching) -> Growth | None:
    """The default browser actions of a listing scrape for `records` in `auto` that asks for none, used where its page
    is rendered: `records`' default, within the actions' share
    of its `timeout_s`; none for a page captured as a file (its first render) or without the listing rung. Batch and
    crawl tasks never grow (`worker.handlers`)."""
    reads_rows = request.page_kind == "listing" and "records" in request.formats
    first_render = bool(CAPTURE_FORMATS.intersection(request.formats))
    if not reads_rows or first_render or request.fetch.method_hint != "auto" or fetching.rungs.listing is None:
        return None
    return Growth(BrowserActions(), actions_deadline(request.timeout_s))


async def _config(conn: AsyncConnection, site_id: SiteId, work: _Work, url: str) -> ActiveConfig | None:
    """The site's active config for the schema `records` names that serves `url`; a prompt's draft is new, so it has
    none."""
    if work.schema is None or work.data is None:
        return None
    return await active_config(conn, site_id, work.schema, work.data.vocabulary, url)


async def _derive(
    engine: AsyncEngine, work: _Work, request: ScrapeRequest, task: ClaimedTask, page: Page, config: ActiveConfig | None
) -> _Derived:
    """The requested formats, the draft of a prompt, and the extraction for `records` (`extract_run`), from the page's
    one parse, with the site's active `config` when there is one; the formats name the files stored for the
    capture."""
    prompt = request.schema_.prompt if isinstance(request.schema_, SchemaPrompt) else None
    manifest = page.result.bundle.manifest
    content = partial(_content, page.parsed, manifest, request, page.files, sample=prompt is not None)
    formats, sample = await asyncio.to_thread(content)
    drafted = None
    if prompt is not None and sample is not None:
        # Role S's one sample page is the page's cleaned text.
        drafted = await draft_schema(engine, work.llm, prompt=prompt, samples=[sample], job_id=task.job_id)
    schema = work.schema if drafted is None else drafted
    if "records" not in request.formats or schema is None or work.data is None:
        return _Derived(formats, drafted)
    if drafted is not None:
        check_usable(drafted, request.page_kind)
        # The drafted schema extracts here; its label synonyms are drafted in the background, for later uses
        # (`s_labels`).
        schema = drafted = with_labels(engine, work.llm, drafted)
    # §4.4 step 3: role C fills required fields left missing, charged to the job. No write to the job's row is open
    # here: the job was committed before the fetch, and the task is written after.
    records = Records(schema, work.data, config, work.margins, work.render)
    extracted = await extract_run(page, records, role_c(work.llm, job_id=task.job_id))
    return _Derived(formats, drafted, extracted)


def _fetch_members(result: FetchResult, capture_id: CaptureId, adapters: Adapters) -> _FetchMembers:
    manifest = result.bundle.manifest
    return _FetchMembers(
        capture_id=capture_id,
        final_url=manifest.final_url,
        fetched_at=manifest.fetched_at,
        fetch=fetch_summary(
            manifest.method,
            manifest.status_code or None,
            manifest.block_class,
            result.attempts,
            adapters=adapters,
            url=manifest.url,
            final_url=manifest.final_url,
        ),
    )


def _scraped(end: PageEnd, extracted: Extracted | None) -> list[ScrapedRecord] | None:
    """The records the page kept, served in the schema they were extracted with; None when it kept none."""
    if end.kept is None or extracted is None:
        return None
    schema = extracted.records.schema
    return [
        ScrapedRecord.of(schema, record.id, record.draft, mode=of.mode, confidence=of.record_confidence)
        for record, of in end.kept
    ]


def _content(
    page: ParsedPage, manifest: CaptureManifest, request: ScrapeRequest, files: frozenset[str], *, sample: bool
) -> tuple[Formats, str | None]:
    """The requested content formats of the page and, when `sample`, its cleaned text."""
    formats = page_formats(page, manifest, request.formats, request.format_options, files)
    return formats, page.cleaned_text.text if sample else None


async def job_usage(conn: AsyncConnection, job_id: JobId) -> Usage:
    """What the job `job_id` used so far."""
    used: dict[str, int] = (await conn.execute(select(Job.usage).where(Job.id == job_id))).scalar_one()
    return Usage.model_validate({name: used[name] for name in Usage.model_fields if name in used})
