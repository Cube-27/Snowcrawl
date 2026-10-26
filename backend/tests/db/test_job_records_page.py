"""A job's records, a page at a time: `list_records` with a `job_id` reads the job's tasks by id
and returns the records they stand for, each once, newest task first and newest record first within a task; its cursor
is a record id that stays valid as tasks are added."""

import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.db.models import (
    Extraction,
    Job,
    JobId,
    Project,
    Record,
    RecordId,
    SchemaId,
    Site,
    SiteId,
    TaskRecord,
    UrlTask,
    UrlTaskId,
)
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import SchemaDocument
from snowcrawl.services.records import RecordFilter, list_records
from tests.db.support import TITLE

pytestmark = pytest.mark.anyio


@dataclass
class Jobs:
    """A site and a schema, and jobs whose tasks stand for records."""

    conn: AsyncConnection
    site_id: SiteId
    schema_id: SchemaId
    host: str
    stored: dict[str, RecordId] = field(default_factory=dict)

    async def job(self) -> JobId:
        project = select(Project.id).where(Project.name == "default").scalar_subquery()
        values = {"project_id": project, "kind": "batch_scrape", "request": {}, "engine_version": "test"}
        job_id: JobId = (await self.conn.execute(insert(Job).values(values).returning(Job.id))).scalar_one()
        return job_id

    async def task(self, job_id: JobId, name: str, *records: str, schema_id: SchemaId | None = None) -> UrlTaskId:
        """A task of the job at `name` that stands for the records titled `records`, stored by it when new."""
        url = f"https://{self.host}/{name}"
        task_values = {"job_id": job_id, "url": url, "canonical_url": url, "status": "done", "outcome": "extracted"}
        task_id: UrlTaskId = (
            await self.conn.execute(insert(UrlTask).values(task_values).returning(UrlTask.id))
        ).scalar_one()
        extraction = {"url_task_id": task_id, "engine_version": "test", "mode": "adhoc"}
        extraction_id = (
            await self.conn.execute(insert(Extraction).values(extraction).returning(Extraction.id))
        ).scalar_one()
        for position, title in enumerate(records):
            if title not in self.stored:
                self.stored[title] = await self._record(job_id, extraction_id, title, schema_id or self.schema_id)
            link = {
                "url_task_id": task_id,
                "record_id": self.stored[title],
                "extraction_id": extraction_id,
                "position": position,
                "delta": "new",
            }
            await self.conn.execute(insert(TaskRecord).values(link))
        return task_id

    async def _record(self, job_id: JobId, extraction_id: object, title: str, schema_id: SchemaId) -> RecordId:
        values = {
            "extraction_id": extraction_id,
            "job_id": job_id,
            "site_id": self.site_id,
            "schema_id": schema_id,
            "entity_key": title,
            "data": {"title": title},
            "field_meta": {},
            "complete": True,
            "mode": "adhoc",
            "schema_digest": "digest",
        }
        record_id: RecordId = (await self.conn.execute(insert(Record).values(values).returning(Record.id))).scalar_one()
        return record_id

    async def titles(self, job_id: JobId, **query: object) -> list[str]:
        """The titles of the job's records in the order listed, across every page."""
        reverse = {record_id: title for title, record_id in self.stored.items()}
        listed: list[str] = []
        cursor: RecordId | None = None
        while True:
            page = await list_records(self.conn, RecordFilter(job_id=job_id, cursor=cursor, **query))  # type: ignore[arg-type]
            listed += [reverse[record.id] for record in page.items]
            if page.next_cursor is None:
                return listed
            cursor = page.next_cursor


@pytest.fixture
async def jobs(conn: AsyncConnection) -> Jobs:
    host = f"shop-{uuid.uuid4().hex[:12]}.example.test"
    project = select(Project.id).where(Project.name == "default").scalar_subquery()
    site_id: SiteId = (
        await conn.execute(insert(Site).values(project_id=project, host=host).returning(Site.id))
    ).scalar_one()
    document = {"name": f"synthetic_{uuid.uuid4().hex[:12]}", "page_kind": "detail", "fields": {"title": TITLE}}
    schema = await registry.create(conn, SchemaDocument.model_validate(document), "user")
    return Jobs(conn, site_id, schema.id, host)


async def test_pages_follow_the_jobs_tasks_newest_first_and_cover_every_record_once(jobs: Jobs) -> None:
    job_id = await jobs.job()
    await jobs.task(job_id, "a", "a1")
    await jobs.task(job_id, "b", "b1", "b2")
    await jobs.task(job_id, "c", "c1")
    other = await jobs.job()
    await jobs.task(other, "z", "z1")

    listed = await jobs.titles(job_id, limit=2)

    assert listed == ["c1", "b2", "b1", "a1"]


async def test_a_cursor_stays_valid_when_tasks_are_added_and_filters_narrow_the_pages(jobs: Jobs) -> None:
    job_id = await jobs.job()
    await jobs.task(job_id, "a", "a1")
    await jobs.task(job_id, "b", "b1")
    first = await list_records(jobs.conn, RecordFilter(job_id=job_id, limit=1))
    await jobs.task(job_id, "c", "c1")

    rest = await list_records(jobs.conn, RecordFilter(job_id=job_id, cursor=first.next_cursor, limit=5))

    reverse = {record_id: title for title, record_id in jobs.stored.items()}
    assert [reverse[record.id] for record in first.items + rest.items] == ["b1", "a1"]
    assert await jobs.titles(job_id, schema_id=uuid.uuid4()) == []


async def test_every_page_carries_the_jobs_total_whatever_its_cursor_and_size(jobs: Jobs) -> None:
    job_id = await jobs.job()
    await jobs.task(job_id, "a", "a1", "a2")
    await jobs.task(job_id, "b", "a2", "b1")
    other = await jobs.job()
    await jobs.task(other, "z", "z1")

    first = await list_records(jobs.conn, RecordFilter(job_id=job_id, limit=1))
    last = await list_records(jobs.conn, RecordFilter(job_id=job_id, cursor=first.next_cursor, limit=5))
    streamed = await list_records(jobs.conn, RecordFilter(job_id=job_id), counted=False)

    assert (first.total, last.total, len(last.items)) == (3, 3, 2)
    assert streamed.total == 0
