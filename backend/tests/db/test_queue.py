from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.db.models import Job, JobEvent, JobId, JobStatus, Project, UrlTask, UrlTaskId
from snowcrawl.db.queue import (
    ClaimedTask,
    LeaseLost,
    claim_tasks,
    complete_task,
    fail_task,
    release_expired_leases,
)

pytestmark = pytest.mark.anyio


async def _job(conn: AsyncConnection, status: JobStatus = "running", **columns: Any) -> JobId:
    default_project = select(Project.id).where(Project.name == "default").scalar_subquery()
    values = {"project_id": default_project, "kind": "crawl", "request": {}, "engine_version": "0+cal.0"}
    job_id: JobId = (
        await conn.execute(insert(Job).values({**values, "status": status, **columns}).returning(Job.id))
    ).scalar_one()
    return job_id


async def _tasks(conn: AsyncConnection, job_id: JobId, count: int, **columns: Any) -> list[UrlTaskId]:
    urls = [f"https://example.test/p/{i}" for i in range(count)]
    rows = [{"job_id": job_id, "url": url, "canonical_url": url, **columns} for url in urls]
    ids = (await conn.execute(insert(UrlTask).returning(UrlTask.id, sort_by_parameter_order=True), rows)).scalars()
    return list(ids)


async def _claim_one(conn: AsyncConnection, worker: str = "w1") -> ClaimedTask:
    [task] = await claim_tasks(conn, worker, 1)
    return task


async def _row(conn: AsyncConnection, task_id: UrlTaskId) -> Any:
    return (await conn.execute(select(UrlTask).where(UrlTask.id == task_id))).one()


async def _job_state(conn: AsyncConnection, job_id: JobId) -> tuple[str, list[str]]:
    status = (await conn.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
    query = select(JobEvent.kind).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
    events = (await conn.execute(query)).scalars().all()
    return status, list(events)


async def _now(conn: AsyncConnection) -> Any:
    return (await conn.execute(select(func.now()))).scalar_one()


async def _expire(conn: AsyncConnection, task: ClaimedTask) -> ClaimedTask:
    """Let `task`'s lease run out, as it would after `LEASE` with no word from its worker."""
    expired = await _now(conn) - timedelta(seconds=1)
    await conn.execute(update(UrlTask).where(UrlTask.id == task.id).values(lease_until=expired))
    return replace(task, lease_until=expired)


async def test_claim_takes_due_queued_tasks_by_priority_then_age(conn: AsyncConnection) -> None:
    job_id = await _job(conn)
    old, new, urgent = await _tasks(conn, job_id, 3)
    await conn.execute(update(UrlTask).where(UrlTask.id == urgent).values(priority=5))
    [not_due] = await _tasks(
        conn, job_id, 1, url="u", canonical_url="u", next_attempt_at=await _now(conn) + timedelta(1)
    )
    [done] = await _tasks(conn, job_id, 1, url="d", canonical_url="d", status="done")

    claimed = await claim_tasks(conn, "w1", 10)

    assert [task.id for task in claimed] == [urgent, old, new]
    assert not_due not in {task.id for task in claimed}
    assert done not in {task.id for task in claimed}


async def test_claim_takes_at_most_n_and_never_the_same_task_twice(conn: AsyncConnection) -> None:
    job_id = await _job(conn)
    ids = await _tasks(conn, job_id, 5)

    first = await claim_tasks(conn, "w1", 2)
    second = await claim_tasks(conn, "w2", 2)
    third = await claim_tasks(conn, "w3", 2)
    fourth = await claim_tasks(conn, "w4", 2)

    assert [[task.id for task in batch] for batch in (first, second, third, fourth)] == [
        ids[0:2],
        ids[2:4],
        ids[4:5],
        [],
    ]


async def test_completing_the_last_task_completes_the_job_once(conn: AsyncConnection) -> None:
    job_id = await _job(conn)
    first_id, second_id = await _tasks(conn, job_id, 2)
    first, second = await claim_tasks(conn, "w1", 2)

    await complete_task(conn, first, "extracted")
    assert await _job_state(conn, job_id) == ("running", [])

    await complete_task(conn, second, "no_match")

    assert await _job_state(conn, job_id) == ("completed", ["completed"])
    rows = [await _row(conn, task_id) for task_id in (first_id, second_id)]
    assert [(row.status, row.outcome, row.claimed_by) for row in rows] == [
        ("done", "extracted", "w1"),
        ("done", "no_match", "w1"),
    ]


async def test_a_transient_failure_requeues_with_exponential_backoff(conn: AsyncConnection) -> None:
    job_id = await _job(conn)
    [task_id] = await _tasks(conn, job_id, 1)
    now = await _now(conn)
    delays = []

    for _ in range(2):
        task = await _claim_one(conn)
        await fail_task(conn, task, "timeout", transient=True)
        row = await _row(conn, task_id)
        assert (row.status, row.outcome, row.last_error, row.claimed_by, row.lease_until) == (
            "queued",
            None,
            "timeout",
            None,
            None,
        )
        delays.append(row.next_attempt_at - now)
        # Make the task due again without waiting for the backoff.
        await conn.execute(update(UrlTask).values(next_attempt_at=func.now()))

    # Full jitter (`core.retry.backoff_delay`): at most the doubled backoff.
    assert timedelta(0) <= delays[0] <= timedelta(seconds=30)
    assert timedelta(0) <= delays[1] <= timedelta(seconds=60)
    assert await _job_state(conn, job_id) == ("running", [])


async def test_a_task_whose_lease_was_lost_cannot_be_finished(conn: AsyncConnection) -> None:
    job_id = await _job(conn)
    [task_id] = await _tasks(conn, job_id, 1)
    stale = await _claim_one(conn, "w1")
    # The lease expired, the task went back to the queue, and another worker claimed it.
    await conn.execute(update(UrlTask).values(status="queued", claimed_by=None, lease_until=None))
    await _claim_one(conn, "w2")

    with pytest.raises(LeaseLost, match=str(task_id)):
        await complete_task(conn, stale, "extracted")
    with pytest.raises(LeaseLost):
        await fail_task(conn, stale, "boom", transient=True)

    row = await _row(conn, task_id)
    assert (row.status, row.claimed_by, row.attempts) == ("claimed", "w2", 2)


async def test_an_expired_lease_is_requeued_with_backoff_and_the_old_claim_is_fenced_out(
    conn: AsyncConnection,
) -> None:
    job_id = await _job(conn)
    expired_id, live_id = await _tasks(conn, job_id, 2)
    expiring, live = await claim_tasks(conn, "w1", 2)
    stale = await _expire(conn, expiring)

    released = await release_expired_leases(conn, batch=10)

    assert released == 1
    row = await _row(conn, expired_id)
    assert (row.status, row.claimed_by, row.lease_until, row.attempts, row.last_error) == (
        "queued",
        None,
        None,
        1,
        "lease expired",
    )
    assert timedelta(0) <= row.next_attempt_at - await _now(conn) <= timedelta(seconds=30)
    assert (await _row(conn, live_id)).status == "claimed"
    with pytest.raises(LeaseLost):
        await complete_task(conn, stale, "extracted")
    await complete_task(conn, live, "extracted")
    assert await _job_state(conn, job_id) == ("running", [])


async def test_claim_alternates_between_jobs_and_orders_by_score_only_within_a_job(conn: AsyncConnection) -> None:
    # A scored crawl does not lease ahead of another job's tasks at the same priority.
    scored, plain = await _job(conn), await _job(conn)
    low, high = await _tasks(conn, scored, 2)
    await conn.execute(update(UrlTask).where(UrlTask.id == low).values(score=0.2))
    await conn.execute(update(UrlTask).where(UrlTask.id == high).values(score=0.9))
    [other] = await _tasks(conn, plain, 1, url="o", canonical_url="o")

    claimed = await claim_tasks(conn, "w1", 3)

    assert {task.id for task in claimed[:2]} == {high, other}
    assert [task.id for task in claimed if task.job_id == scored] == [high, low]
    assert claimed[2].id == low
