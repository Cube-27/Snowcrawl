"""Record versions, against a real Postgres: a re-extracted entity equal to its latest version adds no
record row, and one with any other value adds the next version, pointing at the one it supersedes and naming its changed
fields."""

import uuid
from typing import Any

import pytest
from pydantic import JsonValue
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.core.egress import UNKNOWN, Egress
from snowcrawl.db.models import (
    Extraction,
    ExtractionId,
    Job,
    JobId,
    Project,
    Record,
    RecordId,
    Site,
    SiteId,
    TaskRecord,
)
from snowcrawl.db.queue import insert_claimed
from snowcrawl.recurring.record_fp import record_fingerprint
from snowcrawl.recurring.versions import store_records
from snowcrawl.resolve.records import Delta, FieldChange, FieldMeta, RecordDraft, entity_key
from snowcrawl.resolve.task import RecordSource, StoredRecord
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef, SchemaDocument
from snowcrawl.services.records import RecordFilter, list_records

pytestmark = pytest.mark.anyio

PAGE_URL = "https://shop.example.test/p/1"
DOCUMENT = SchemaDocument(
    name="synthetic_versioned_product",
    page_kind="detail",
    fields={
        "title": FieldSpec(type=FieldType.TEXT, description="d", required=True),
        "price": FieldSpec(type=FieldType.MONEY, description="d", required=True),
        "colour": FieldSpec(type=FieldType.TEXT, description="d"),
    },
)
KETTLE: dict[str, JsonValue] = {"title": "Synthetic kettle", "price": {"amount": "19.90", "currency": "EUR"}}
CHEAPER: dict[str, JsonValue] = {**KETTLE, "price": {"amount": "17.50", "currency": "EUR"}}
META = FieldMeta(
    status="published",
    reason=None,
    evidence=None,
    confidence=None,
    calibrated=False,
    source_kind="jsonld",
    locator=None,
    derived_by=None,
)


class _Site:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self.jobs: list[JobId] = []
        self.extractions: list[ExtractionId] = []

    async def prepare(self, document: SchemaDocument = DOCUMENT) -> None:
        project = select(Project.id).where(Project.name == "default").scalar_subquery()
        self.project = project
        host = f"shop-{uuid.uuid4().hex[:12]}.example.test"
        insert_site = insert(Site).values(project_id=project, host=host).returning(Site.id)
        self.site_id: SiteId = (await self.conn.execute(insert_site)).scalar_one()
        self.schema: SchemaDef = await registry.create(self.conn, document.model_copy(update={"name": _name()}), "user")

    def draft(self, data: dict[str, JsonValue], url: str = PAGE_URL) -> RecordDraft:
        meta = dict.fromkeys(data, META)
        return RecordDraft(data=data, field_meta=meta, complete=True, entity_key=entity_key(self.site_id, url))

    async def run(self, *drafts: RecordDraft, egress: Egress = UNKNOWN) -> tuple[StoredRecord, ...]:
        """One extraction of the page in a job of its own, as a re-run of the crawl stores it."""
        job = insert(Job).values(
            project_id=self.project,
            kind="scrape",
            request={},
            schema_id=self.schema.id,
            engine_version="e",
            egress_country=egress.country,
            egress_basis=egress.basis,
        )
        job_id: JobId = (await self.conn.execute(job.returning(Job.id))).scalar_one()
        self.jobs.append(job_id)
        task = await insert_claimed(self.conn, job_id, PAGE_URL, PAGE_URL, self.schema.page_kind, "test-worker")
        extraction = insert(Extraction).values(url_task_id=task.id, engine_version="e", mode="adhoc")
        extraction_id = (await self.conn.execute(extraction.returning(Extraction.id))).scalar_one()
        self.extractions.append(extraction_id)
        source = RecordSource(
            extraction_id=extraction_id,
            task_id=task.id,
            job_id=job_id,
            site_id=self.site_id,
            mode="adhoc",
            page_url=PAGE_URL,
        )
        return await store_records(self.conn, self.schema, drafts, source)

    async def stood_for(self, extraction_id: ExtractionId) -> list[RecordId]:
        """The records the extraction stood for, in the page's order."""
        query = select(TaskRecord.record_id).where(TaskRecord.extraction_id == extraction_id)
        return list((await self.conn.execute(query.order_by(TaskRecord.position))).scalars())

    async def rows(self) -> list[dict[str, Any]]:
        columns = (
            Record.id,
            Record.version,
            Record.supersedes_record_id,
            Record.changed_fields,
            Record.fingerprint,
            Record.data,
        )
        query = select(*columns).where(Record.site_id == self.site_id).order_by(Record.id)
        return [dict(row) for row in (await self.conn.execute(query)).mappings()]


def _name() -> str:
    return f"synthetic_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def site(conn: AsyncConnection) -> _Site:
    prepared = _Site(conn)
    await prepared.prepare()
    return prepared


async def test_a_first_extraction_is_version_1_with_its_fingerprint(site: _Site) -> None:
    (stored,) = await site.run(site.draft(KETTLE))

    assert await site.rows() == [
        {
            "id": stored.id,
            "version": 1,
            "supersedes_record_id": None,
            "changed_fields": [],
            "fingerprint": record_fingerprint(KETTLE, site.schema),
            "data": KETTLE,
        }
    ]


async def test_an_equal_record_is_its_latest_version_and_a_changed_optional_value_adds_one(site: _Site) -> None:
    (first,) = await site.run(site.draft(KETTLE))
    (again,) = await site.run(site.draft(KETTLE))
    (coloured,) = await site.run(site.draft({**KETTLE, "colour": "Blue"}))

    assert again.id == first.id
    assert coloured.draft.data == {**KETTLE, "colour": "Blue"}
    assert [(row["version"], row["changed_fields"]) for row in await site.rows()] == [(1, []), (2, ["colour"])]


async def test_an_extraction_stands_for_its_records_in_page_order_the_equal_ones_included(site: _Site) -> None:
    other = "https://shop.example.test/p/2"
    (kettle,) = await site.run(site.draft(KETTLE))

    second = await site.run(site.draft(CHEAPER, other), site.draft(KETTLE))

    assert await site.stood_for(site.extractions[1]) == [second[0].id, kettle.id]


async def test_a_changed_price_adds_version_2_naming_price(site: _Site) -> None:
    (first,) = await site.run(site.draft(KETTLE))

    (second,) = await site.run(site.draft(CHEAPER))

    assert second.id != first.id
    assert second.draft.data == CHEAPER
    assert (await site.rows())[1] == {
        "id": second.id,
        "version": 2,
        "supersedes_record_id": first.id,
        "changed_fields": ["price"],
        "fingerprint": record_fingerprint(CHEAPER, site.schema),
        "data": CHEAPER,
    }


async def test_a_record_states_the_egress_its_job_pinned(site: _Site) -> None:
    (stored,) = await site.run(site.draft(KETTLE), egress=Egress("GB", "pool"))
    (unknown,) = await site.run(site.draft(CHEAPER))

    query = select(Record.id, Record.egress_country, Record.egress_basis).where(Record.site_id == site.site_id)
    found = {row.id: (row.egress_country, row.egress_basis) for row in await site.conn.execute(query)}
    assert found == {stored.id: ("GB", "pool"), unknown.id: (None, "unknown")}


async def test_each_jobs_record_carries_its_delta_against_the_previous_record(site: _Site) -> None:
    for data in (KETTLE, KETTLE, CHEAPER, CHEAPER):
        await site.run(site.draft(data))

    deltas = [(await list_records(site.conn, RecordFilter(job_id=job))).items[0].delta for job in site.jobs]
    # Served as JSON numbers.
    price = FieldChange(
        field="price", previous={"amount": 19.9, "currency": "EUR"}, value={"amount": 17.5, "currency": "EUR"}
    )
    assert deltas == [Delta(kind="new"), Delta(kind="same"), Delta(kind="changed", changes=[price]), Delta(kind="same")]
