"""A page a vendor's challenge keeps out is an outcome (master plan 2026-10-18 §3.1): `blocked` naming the vendor, and a
worker task retried at most once after the back-off. Results are synthetic (RFC 2606 hosts); nothing goes to the
network."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from snowcrawl.acquire.challenge import LEGAL, STRUCTURAL, challenge_label, vendor_block
from snowcrawl.acquire.outcome import Content, Exchange, fetch_result
from snowcrawl.acquire.types import AttemptRecord, BlockClass, FetchPolicy, FetchResult
from snowcrawl.adapters.challenges import SIGNATURES
from snowcrawl.core.capture import CaptureMethod
from snowcrawl.core.ids import uuid7
from snowcrawl.core.retry import FIRST_BACKOFF
from snowcrawl.db.models import CaptureId, JobId, UrlTaskId
from snowcrawl.db.queue import ClaimedTask, Failed, Finished
from snowcrawl.services.page_run import unreached

URL = "https://shop.example.test/item/1"
# Vendor labels come from the adapter package's signature data (C3).
VENDOR, OTHER = SIGNATURES[0].vendor, SIGNATURES[1].vendor


def _result(block_class: BlockClass, *labels: str | None) -> FetchResult:
    status = 200 if block_class == "ok" else 403
    result = fetch_result(
        url=URL,
        method="http",
        policy=FetchPolicy(),
        engine_version="test",
        fetched_at=datetime.now(UTC),
        started=time.monotonic(),
        exchange=Exchange(url=URL, status=status, request_headers=(), response_headers=()),
        content=Content(body=b"<html></html>"),
        block_class=block_class,
        error=None,
    )
    if not labels:
        return result
    method: tuple[CaptureMethod, CaptureMethod] = ("http", "browser")
    attempts = tuple(
        AttemptRecord(method=method[i % 2], status=status, block_class=block_class, duration_ms=1, challenge=label)
        for i, label in enumerate(labels)
    )
    return result.model_copy(update={"attempts": attempts})


@pytest.mark.parametrize(
    ("result", "vendor"),
    [
        pytest.param(_result("blocked", VENDOR, VENDOR), VENDOR, id="vendor"),
        pytest.param(_result("blocked", OTHER, STRUCTURAL), OTHER, id="latest-vendor-label"),
        # Attempts alternate http, browser; the kept capture is the HTTP one: a browser attempt's label is not its.
        pytest.param(_result("blocked", STRUCTURAL, OTHER), None, id="rejected-attempt-of-another-method"),
        pytest.param(_result("blocked", VENDOR, OTHER), VENDOR, id="label-of-the-kept-method"),
        pytest.param(_result("blocked", STRUCTURAL), None, id="structural"),
        pytest.param(_result("blocked", None), None, id="unlabelled"),
        pytest.param(_result("ok", VENDOR), None, id="reached"),
    ],
)
def test_a_vendor_block_is_a_blocked_fetch_whose_attempt_names_a_vendor(
    result: FetchResult, vendor: str | None
) -> None:
    assert vendor_block(result) == vendor


def _task(attempts: int) -> ClaimedTask:
    return ClaimedTask(
        id=UrlTaskId(uuid7()),
        job_id=JobId(uuid7()),
        url=URL,
        canonical_url=URL,
        page_kind="detail",
        attempts=attempts,
        priority=0,
        depth=0,
        score=None,
        parent_task_id=None,
        claimed_by="w",
        lease_until=datetime.now(UTC),
    )


def _fetched(result: FetchResult) -> tuple[FetchResult, CaptureId]:
    return result, CaptureId(uuid7())


def _ended(page: tuple[FetchResult, CaptureId], task: ClaimedTask) -> Finished | Failed | None:
    return unreached(*page, task, retryable=True)


def test_a_vendor_blocked_task_is_retried_once_after_the_back_off_then_ends_blocked_naming_the_vendor() -> None:
    page = _fetched(_result("blocked", VENDOR))
    # The browser rung met the same vendor's challenge: a retry would send the same rungs to it.
    browser_met = _fetched(_result("blocked", VENDOR, VENDOR))

    first, second = _ended(page, _task(1)), _ended(page, _task(2))

    # The back-off is jittered (`core.retry.backoff_delay`): at most the first.
    assert isinstance(first, Failed)
    assert first.after is not None
    assert first.after <= FIRST_BACKOFF
    assert replace(first, after=None) == Failed(
        f"challenge: {VENDOR}", transient=True, exhausted="blocked", capture_id=page[1]
    )
    assert second == Finished("blocked", reason=f"challenge: {VENDOR}", capture_id=page[1])
    blocked = Finished("blocked", reason=f"challenge: {VENDOR}", capture_id=browser_met[1])
    assert _ended(browser_met, _task(1)) == blocked


def test_a_451_is_blocked_for_legal_reasons_and_ends_geo_blocked_without_a_retry() -> None:
    # RFC 7725 §3: a refusal for legal reasons, whatever its document shows; no vendor's challenge.
    assert challenge_label(451, (), "<html><script>void 0</script></html>", URL) == LEGAL
    page = _fetched(_result("blocked", LEGAL))

    assert vendor_block(page[0]) is None
    assert _ended(page, _task(1)) == Finished("geo_blocked", capture_id=page[1])
