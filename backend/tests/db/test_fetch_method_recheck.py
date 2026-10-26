"""The 7-day fetch-method re-check (`scheduler.recheck`) fetches the latest `ok` capture's page as that capture was
classified: its stored policy's target types and content-only reading go with the HTTP request."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.db.models import Capture, Project, Site, SiteContract
from snowcrawl.scheduler.recheck import RECHECK_AFTER, recheck_fetch_method
from tests.support.captures import fetched

pytestmark = pytest.mark.anyio

_HOST = "recheck.example.test"
_URL = f"https://{_HOST}/item/1"


async def test_the_recheck_fetches_with_the_latest_captures_stored_policy(engine: AsyncEngine) -> None:
    stored = FetchPolicy(page_kind="detail", target_types=("Product",), content_only=True)
    project = select(Project.id).where(Project.name == "default").scalar_subquery()
    async with engine.begin() as conn:
        site_id = (
            await conn.execute(insert(Site).values(project_id=project, host=_HOST).returning(Site.id))
        ).scalar_one()
        await conn.execute(
            insert(SiteContract).values(
                site_id=site_id, page_kind="detail", method="browser", checked_at=datetime.now(UTC) - 2 * RECHECK_AFTER
            )
        )
        await conn.execute(
            insert(Capture).values(
                site_id=site_id,
                url=_URL,
                fetched_at=datetime.now(UTC) - timedelta(days=1),
                method="browser",
                status_code=200,
                final_url=_URL,
                block_class="ok",
                object_key=f"{_HOST}/recheck",
                html_sha256="0" * 64,
                size_bytes=4,
                duration_ms=5,
                fetch_policy=stored.model_dump(mode="json"),
            )
        )
    asked: list[tuple[str, FetchPolicy]] = []

    async def http(url: str, policy: FetchPolicy) -> FetchResult:
        asked.append((url, policy))
        return fetched(url, 200, b"<html><body><h1>Item</h1></body></html>", block_class="ok", policy=policy)

    async with engine.begin() as conn:
        assert await recheck_fetch_method(conn, http, batch=10) >= 1
        method = select(SiteContract.method).where(SiteContract.site_id == site_id)
        assert (await conn.execute(method)).scalar_one() == "http"

    assert asked == [(_URL, FetchPolicy(page_kind="detail", target_types=("Product",), content_only=True))]
