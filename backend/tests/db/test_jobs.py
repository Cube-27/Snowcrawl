"""Jobs: a job's summary, its cancel and its results, including the clean `json_values` items (the UI), through the jobs
service and then over HTTP (`GET /v1/jobs`, `GET/DELETE /v1/jobs/{id}`,
`GET /v1/jobs/{id}/results`)."""

import csv
import io
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import (
    Capture,
    CaptureId,
    Extraction,
    Job,
    JobId,
    Project,
    Record,
    RecordId,
    SchemaId,
    Site,
    SiteId,
    TaskOutcome,
    TaskStatus,
    UrlTask,
    UrlTaskId,
)
from snowcrawl.recurring.versions import link_records
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef, SchemaDocument, document_digest
from snowcrawl.services import jobs
from snowcrawl.services.job_outcomes import job_outcomes
from snowcrawl.services.jobs import (
    ExportPage,
    JsonValuesPage,
    PageOutputPage,
    ResultsQuery,
)
from snowcrawl.services.records import RecordPage
from snowcrawl.storage.fs import FsStore
from tests.db.support import api

pytestmark = pytest.mark.anyio


OUTCOMES = {
    "extracted",
    "partial",
    "links_only",
    "blocked",
    "shell",
    "not_found",
    "auth_wall",
    "soft_404",
    "geo_redirect",
    "redirected_to_listing",
    "unsupported_media",
    "geo_blocked",
    "no_match",
    "timeout",
    "failed",
}


def _document(name: str) -> SchemaDocument:
    text = FieldSpec(type=FieldType.TEXT, description="d")
    return SchemaDocument(
        name=name,
        page_kind="detail",
        fields={"title": text.model_copy(update={"required": True}), "colour": text},
    )


def meta(status: str) -> dict[str, Any]:
    """A `field_meta` entry; only a published field has a source."""
    published = status == "published"
    return {
        "status": status,
        "reason": None if published else "no candidate",
        "evidence": "structured" if published else None,
        "confidence": None,
        "calibrated": False,
        "source_kind": "jsonld" if published else None,
        "locator": "jsonld:0:$.name" if published else None,
        "derived_by": None,
    }


@dataclass
class World:
    """One site and one schema; the `engine`, `own_origin` and `schema_name` fixtures delete what it made."""

    engine: AsyncEngine
    origin: str
    site_id: SiteId
    schema: SchemaDef

    async def job(self, *, schema_id: SchemaId | None = None, status: str = "queued", **columns: Any) -> JobId:
        async with self.engine.begin() as conn:
            project = select(Project.id).where(Project.name == "default").scalar_subquery()
            values = {"kind": "batch_scrape", "request": {}, "engine_version": "e", "status": status} | columns
            statement = insert(Job).values(project_id=project, schema_id=schema_id, **values).returning(Job.id)
            job_id: JobId = (await conn.execute(statement)).scalar_one()
        return job_id

    async def tasks(
        self, job_id: JobId, rows: Sequence[tuple[TaskStatus, TaskOutcome | None]], *, last_error: str | None = None
    ) -> None:
        async with self.engine.begin() as conn:
            for status, outcome in rows:
                url = f"{self.origin}/p/{uuid7().hex}"
                values = {"job_id": job_id, "url": url, "canonical_url": url, "status": status, "outcome": outcome}
                await conn.execute(insert(UrlTask).values(**values, last_error=last_error))

    async def record(
        self,
        job_id: JobId,
        data: Mapping[str, Any],
        field_meta: Mapping[str, Any],
        *,
        capture_id: CaptureId | None = None,
    ) -> RecordId:
        """A record of the world's schema, stored as a page task stores it, from the capture `capture_id` if given."""
        async with self.engine.begin() as conn:
            task_id, url = await self._task(conn, job_id, "extracted", capture_id)
            [record_id] = await self._store(conn, job_id, task_id, [(url, data, field_meta)])
        return record_id

    async def capture(self, method: str, attempts: Sequence[str] = ()) -> CaptureId:
        """A capture of the site's by `method`, its fetch having tried the rungs `attempts` (methods) in order."""
        values = {
            "site_id": self.site_id,
            "url": self.origin,
            "fetched_at": func.now(),
            "method": method,
            "status_code": 200,
            "final_url": self.origin,
            "block_class": "ok",
            "object_key": f"captures/{uuid7().hex}",
            "html_sha256": "0" * 64,
            "size_bytes": 1,
            "duration_ms": 1,
            "fetch_policy": {"attempts": [{"method": m} for m in attempts or (method,)]},
        }
        async with self.engine.begin() as conn:
            capture_id: CaptureId = (
                await conn.execute(insert(Capture).values(**values).returning(Capture.id))
            ).scalar_one()
        return capture_id

    async def page(
        self,
        job_id: JobId,
        outcome: TaskOutcome,
        rows: Sequence[Mapping[str, Any]] = (),
        *,
        capture_id: CaptureId | None = None,
    ) -> tuple[UrlTaskId, str]:
        """A finished task whose page stored one record per entry of `rows` (a listing's rows), each publishing the
        fields it names, from the capture `capture_id` if given; its id and URL."""
        async with self.engine.begin() as conn:
            task_id, url = await self._task(conn, job_id, outcome, capture_id)
            records = [
                (f"{url}#{i}", data, {n: meta(_status(n, data)) for n in self.schema.fields})
                for i, data in enumerate(rows)
            ]
            await self._store(conn, job_id, task_id, records)
        return task_id, url

    async def _task(
        self, conn: AsyncConnection, job_id: JobId, outcome: TaskOutcome, capture_id: CaptureId | None
    ) -> tuple[UrlTaskId, str]:
        url = f"{self.origin}/p/{uuid7().hex}"
        status = "failed" if outcome == "failed" else "done"
        values = {"job_id": job_id, "url": url, "canonical_url": url, "status": status, "outcome": outcome}
        task = await conn.execute(insert(UrlTask).values(**values, capture_id=capture_id).returning(UrlTask.id))
        return task.scalar_one(), url

    async def _store(
        self,
        conn: AsyncConnection,
        job_id: JobId,
        task_id: UrlTaskId,
        records: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ) -> list[RecordId]:
        """Store `records` (entity key, data, field meta) as the task's page's, in order, and link them to it."""
        if not records:
            return []
        extraction = await conn.execute(
            insert(Extraction).values(url_task_id=task_id, engine_version="e", mode="adhoc").returning(Extraction.id)
        )
        extraction_id = extraction.scalar_one()
        made: list[RecordId] = []
        for entity_key, data, field_meta in records:
            values = {
                "extraction_id": extraction_id,
                "job_id": job_id,
                "site_id": self.site_id,
                "schema_id": self.schema.id,
                "schema_digest": document_digest(self.schema),
                "entity_key": entity_key,
                "data": dict(data),
                "field_meta": dict(field_meta),
                "complete": "title" in data,
                "mode": "adhoc",
            }
            made.append((await conn.execute(insert(Record).values(**values).returning(Record.id))).scalar_one())
        await link_records(conn, task_id, extraction_id, [(record_id, "new") for record_id in made])
        return made

    async def records(self, job_id: JobId, n: int) -> list[RecordId]:
        """`n` records, oldest first, the even ones without a colour."""
        made = []
        for i in range(n):
            data = {"title": f"Kettle {i}"} | ({"colour": "blue"} if i % 2 else {})
            field_meta = {"title": meta("published"), "colour": meta("published" if i % 2 else "missing")}
            made.append(await self.record(job_id, data, field_meta))
        return made


def _status(name: str, data: Mapping[str, Any]) -> str:
    return "published" if name in data else "missing"


@pytest.fixture
async def world(engine: AsyncEngine, own_origin: str, schema_name: str) -> World:
    async with engine.begin() as conn:
        project = select(Project.id).where(Project.name == "default").scalar_subquery()
        site = insert(Site).values(project_id=project, host=own_origin.removeprefix("https://"))
        site_id = (await conn.execute(site.returning(Site.id))).scalar_one()
        schema = await registry.create(conn, _document(schema_name), "user")
    return World(engine, own_origin, site_id, schema)


async def _summary(engine: AsyncEngine, job_id: JobId) -> jobs.JobSummary:
    async with engine.connect() as conn:
        return await jobs.get_job(conn, job_id)


async def _results(
    engine: AsyncEngine, job_id: JobId, **query: Any
) -> RecordPage | ExportPage | PageOutputPage | JsonValuesPage:
    async with engine.connect() as conn:
        # These jobs have no captures: the store is never read.
        return await jobs.job_results(conn, job_id, ResultsQuery(**query), store=FsStore(Path("unused")))


# The summary.


async def test_the_summary_counts_tasks_by_status_and_by_outcome(world: World) -> None:
    job_id = await world.job(status="running", usage={"llm_calls": 3})
    await world.tasks(
        job_id,
        [
            ("queued", None),
            ("queued", None),
            ("claimed", None),
            ("done", "extracted"),
            ("done", "extracted"),
            ("done", "links_only"),
            ("done", "blocked"),
            ("failed", "failed"),
            ("cancelled", None),
        ],
    )

    summary = await _summary(world.engine, job_id)

    assert summary.counts.model_dump() == {"queued": 2, "claimed": 1, "done": 4, "failed": 1, "cancelled": 1}
    expected = dict.fromkeys(OUTCOMES, 0) | {"extracted": 2, "links_only": 1, "blocked": 1, "failed": 1}
    assert summary.outcomes.model_dump() == expected
    assert summary.usage == {"llm_calls": 3}


async def test_the_outcomes_count_pages_by_outcome_and_fields_by_status_and_reason(world: World) -> None:
    job_id = await world.job()
    await world.tasks(job_id, [("failed", "timeout"), ("done", "soft_404")])
    stock = {**meta("missing"), "reason": "out_of_stock"}
    await world.record(job_id, {"title": "A"}, {"title": meta("published"), "colour": stock})
    await world.record(job_id, {"title": "B"}, {"title": meta("published"), "colour": meta("missing")})
    await world.record(
        job_id, {}, {"title": meta("conflict"), "colour": {**meta("interaction_required"), "reason": "x"}}
    )
    tasks = select(UrlTask.id).where(UrlTask.job_id == job_id).scalar_subquery()
    async with world.engine.begin() as conn:
        signals = ["signal:websocket", "js_state:/x: invalid JSON", "signal:amp_alternate"]
        await conn.execute(update(Extraction).where(Extraction.url_task_id.in_(tasks)).values(diagnostics=signals))
        first = select(Extraction.id).where(Extraction.url_task_id.in_(tasks)).limit(1).scalar_subquery()
        await conn.execute(update(Extraction).where(Extraction.id == first).values(diagnostics=["signal:wasm"]))

    async with world.engine.connect() as conn:
        outcomes = await job_outcomes(conn, job_id)

    assert outcomes.pages.timeout == 1
    assert outcomes.pages.soft_404 == 1
    assert outcomes.pages.extracted == 3
    assert outcomes.fields.model_dump() == {
        "published": 2,
        "missing": 2,
        "conflict": 1,
        "abstained": 0,
        "interaction_required": 1,
    }
    assert outcomes.reasons == {"no candidate": 2, "out_of_stock": 1, "x": 1}
    assert (outcomes.missing_unrendered, outcomes.hint) == ({}, None)
    assert outcomes.signals == {"wasm": 1, "websocket": 2, "amp_alternate": 2}
    assert outcomes.records.model_dump() == {"new": 3, "changed": 0, "same": 0}


@pytest.mark.parametrize(("mode", "hint"), [("off", "render.mode: locate"), ("locate", None)])
async def test_the_outcomes_count_fields_http_pages_did_not_carry(world: World, mode: str, hint: str | None) -> None:
    job_id = await world.job(request={"render": {"mode": mode, "fields": []}})
    unfound = {**meta("missing"), "reason": "no_candidates"}
    fields = {"title": meta("published"), "colour": unfound}
    for capture in (await world.capture("http"), await world.capture("http")):
        await world.record(job_id, {"title": "A"}, fields, capture_id=capture)
    await world.record(job_id, {"title": "B"}, fields, capture_id=await world.capture("browser"))
    await world.record(job_id, {"title": "C"}, fields, capture_id=await world.capture("http", ["browser", "http"]))

    async with world.engine.connect() as conn:
        outcomes = await job_outcomes(conn, job_id)

    assert (outcomes.missing_unrendered, outcomes.hint) == ({"colour": 2}, hint)


@pytest.mark.parametrize(("method", "hint"), [("http", "render.mode: locate"), ("browser", None)])
async def test_a_listing_that_listed_nothing_over_http_names_the_locate_hint(
    world: World, method: str, hint: str | None
) -> None:
    job_id = await world.job(request={"render": {"mode": "off", "fields": []}})
    capture_id = await world.capture(method)
    url = f"{world.origin}/c/{uuid7().hex}"
    values = {"job_id": job_id, "url": url, "canonical_url": url, "status": "done", "outcome": "partial"}
    async with world.engine.begin() as conn:
        await conn.execute(insert(UrlTask).values(**values, page_kind="listing", capture_id=capture_id))

    async with world.engine.connect() as conn:
        outcomes = await job_outcomes(conn, job_id)

    assert (outcomes.missing_unrendered, outcomes.hint) == ({}, hint)


# The list.


# Cancel.


# Results.


async def test_results_export_the_records_in_the_schemas_columns(world: World) -> None:
    job_id = await world.job(schema_id=world.schema.id, status="completed")
    await world.records(job_id, 2)

    page = await _results(world.engine, job_id, format="csv_records", limit=1)
    rest = await _results(world.engine, job_id, format="json_records", cursor=getattr(page, "next_cursor", None))
    markdown = await _results(world.engine, job_id, format="markdown")

    assert isinstance(page, ExportPage)
    assert isinstance(rest, ExportPage)
    assert isinstance(markdown, ExportPage)
    assert list(csv.reader(io.StringIO(page.content))) == [
        ["colour", "title", "_status_colour", "_status_title", "_delta", "_previous"],
        ["blue", "Kettle 1", "published", "published", "new", ""],
    ]
    assert json.loads(rest.content) == [
        {
            "colour": None,
            "title": "Kettle 0",
            "_status_colour": "missing",
            "_status_title": "published",
            "_delta": "new",
            "_previous": None,
        }
    ]
    assert rest.next_cursor is None
    assert markdown.content.count("\n") == 4  # header, delimiter, two records


# Over HTTP.


@pytest.fixture
async def client(app_env: pytest.MonkeyPatch, world: World) -> AsyncIterator[httpx.AsyncClient]:
    """The app with no LLM provider key."""
    app_env.setenv("SNOWCRAWL_LLM_PROVIDERS", "anthropic")
    async with api(world.engine) as (_, client):
        yield client


async def test_job_problems(client: httpx.AsyncClient, world: World) -> None:
    unknown = uuid7()
    ended = await world.job(status="completed")

    responses = {
        "get unknown": await client.get(f"/v1/jobs/{unknown}"),
        "cancel unknown": await client.delete(f"/v1/jobs/{unknown}"),
        "results unknown": await client.get(f"/v1/jobs/{unknown}/results"),
        "outcomes unknown": await client.get(f"/v1/jobs/{unknown}/outcomes"),
        "cancel ended": await client.delete(f"/v1/jobs/{ended}"),
        "export without schema": await client.get(f"/v1/jobs/{ended}/results", params={"format": "csv_records"}),
        "unknown format": await client.get(f"/v1/jobs/{ended}/results", params={"format": "xml"}),
        "bad id": await client.get("/v1/jobs/not-an-id"),
        "list unknown kind": await client.get("/v1/jobs", params={"kind": "search"}),
        "list unknown status": await client.get("/v1/jobs", params={"status": "paused"}),
        "list bad cursor": await client.get("/v1/jobs", params={"cursor": "not-an-id"}),
        "list over the page limit": await client.get("/v1/jobs", params={"limit": 201}),
    }

    statuses = {name: (r.status_code, r.json()["type"]) for name, r in responses.items()}
    assert statuses == {
        "get unknown": (404, "not_found"),
        "cancel unknown": (404, "not_found"),
        "results unknown": (404, "not_found"),
        "outcomes unknown": (404, "not_found"),
        "cancel ended": (409, "conflict"),
        "export without schema": (409, "conflict"),
        "unknown format": (422, "validation"),
        "bad id": (422, "validation"),
        "list unknown kind": (422, "validation"),
        "list unknown status": (422, "validation"),
        "list bad cursor": (422, "validation"),
        "list over the page limit": (422, "validation"),
    }


async def test_a_csv_safe_export_quotes_formula_cells_and_the_default_keeps_them(
    client: httpx.AsyncClient, world: World
) -> None:
    job_id = await world.job(schema_id=world.schema.id, status="completed")
    await world.record(job_id, {"title": "=SUM(1)"}, {"title": meta("published"), "colour": meta("missing")})

    plain = await client.get(f"/v1/jobs/{job_id}/results", params={"format": "csv_records"})
    safe = await client.get(f"/v1/jobs/{job_id}/results", params={"format": "csv_records", "csv_safe": "true"})

    header = "colour,title,_status_colour,_status_title,_delta,_previous\r\n"
    assert plain.json()["content"] == header + ",=SUM(1),missing,published,new,\r\n"
    assert safe.json()["content"] == header + ",'=SUM(1),missing,published,new,\r\n"


async def test_jobs_need_a_key(client: httpx.AsyncClient, world: World) -> None:
    job_id = await world.job()
    del client.headers["authorization"]

    responses = [
        await client.get("/v1/jobs"),
        await client.get(f"/v1/jobs/{job_id}"),
        await client.delete(f"/v1/jobs/{job_id}"),
        await client.get(f"/v1/jobs/{job_id}/results"),
    ]

    assert [r.status_code for r in responses] == [401, 401, 401, 401]
    assert (await _summary(world.engine, job_id)).status == "queued"
