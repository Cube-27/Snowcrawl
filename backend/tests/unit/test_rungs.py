"""Rung selection: which rungs a fetch tries, which outcome it keeps, and what it records.

The rungs are stubs answering synthetic outcomes; no network.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import get_args

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.acquire.memory import Remembered
from snowcrawl.acquire.rungs import BROWSER_MIN_S, FetchPlan, Rungs, acquire
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.adapters.base import AdapterMatch, PageRefs, Shape
from snowcrawl.adapters.registry import Registry
from snowcrawl.core.capture import BlockClass, CaptureMethod
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import FetchMethod, JobId, PageKind, UrlTaskId
from snowcrawl.db.queue import ClaimedTask, Finished
from snowcrawl.services.page_task import queued_end
from tests.support.captures import ScriptedRung

URL = "https://shop.example.test/item/1"
SHELL = b"<html><body><div id='app'></div></body></html>"
PAGE = b"<html><body><h1>Item</h1></body></html>"
BLOCK_CLASSES: tuple[BlockClass, ...] = ("ok", "blocked", "shell", "not_found", "error")
POLICY = FetchPolicy()


def _rung(method: CaptureMethod, block_class: BlockClass = "ok", status: int | None = None) -> ScriptedRung:
    """A rung answering `PAGE` for an `ok` outcome and `SHELL` for any other."""
    return ScriptedRung(method, block_class, status, ok_body=PAGE, other_body=SHELL)


@dataclass
class Web:
    http: ScriptedRung
    browser: ScriptedRung

    recipe: ScriptedRung | None = None

    def acquire(self, policy: FetchPolicy | None = None, remembered: FetchMethod = "http") -> FetchResult:
        rungs = Rungs(http=self.http, browser=self.browser)
        memory = Remembered(remembered)
        return asyncio.run(acquire(URL, policy or FetchPolicy(), memory, rungs, recipe=self.recipe))


def web(
    http: BlockClass,
    browser: BlockClass = "ok",
    *,
    http_status: int | None = None,
    recipe: BlockClass | None = None,
    recipe_status: int | None = None,
) -> Web:
    api = None if recipe is None else _rung("api", recipe, recipe_status)
    return Web(_rung("http", http, http_status), _rung("browser", browser), api)


def _attempts(result: FetchResult) -> list[tuple[CaptureMethod, BlockClass]]:
    return [(attempt.method, attempt.block_class) for attempt in result.attempts]


def test_a_shell_or_block_over_http_is_retried_in_the_browser_and_its_ok_kept() -> None:
    http: BlockClass = "shell"
    stubs = web(http, "ok")

    result = stubs.acquire(POLICY)

    assert (result.method, result.block_class, result.bundle.body) == ("browser", "ok", PAGE)
    assert _attempts(result) == [("http", http), ("browser", "ok")]
    assert stubs.browser.calls == [(URL, POLICY)]
    assert result.duration_ms == 2  # both rungs' time


def test_an_http_timeout_is_retried_in_the_browser_not_over_http_again() -> None:
    stubs = web("error", "ok")
    stubs.http.error = "timeout"

    result = stubs.acquire(POLICY)

    assert _attempts(result) == [("http", "error"), ("browser", "ok")]
    assert len(stubs.http.calls) == 1


def test_the_bundle_records_every_attempt_beside_its_policy() -> None:
    result = web("shell", "ok").acquire()

    assert result.bundle.manifest.fetch_policy == {
        **FetchPolicy().model_dump(mode="json"),
        "attempts": [
            {
                "method": "http",
                "status": 200,
                "block_class": "shell",
                "duration_ms": 1,
                "error": None,
                "challenge": None,
                "steps": [],
            },
            {
                "method": "browser",
                "status": 200,
                "block_class": "ok",
                "duration_ms": 1,
                "error": None,
                "challenge": None,
                "steps": [],
            },
        ],
    }


# --- the API recipe rung ------------------------------------------------------------------------


# --- never for an HTTP `ok` page ----------------------------

# Whether an `auto` fetch retries each HTTP outcome in the browser (the a.3 table): a block, a shell (a challenge's or
# a script-only document's), an unanswered request; never an `ok` page, a 4xx class, a redirect class or a non-HTML
# answer.
RETRIED: dict[tuple[BlockClass, str | None], bool] = {
    ("ok", None): False,
    ("blocked", None): True,
    ("shell", None): True,
    ("error", "network_error: curl 92"): True,
    ("error", "timeout"): True,
    ("error", "http_error"): False,
    ("not_found", None): False,
    ("auth_wall", None): False,
    ("soft_404", None): False,
    ("geo_redirect", None): False,
    ("redirected_to_listing", None): False,
    ("unsupported_media", None): False,
}


def test_the_browser_is_tried_exactly_for_the_block_classes_of_the_table() -> None:
    assert {block_class for block_class, _ in RETRIED} == set(get_args(BlockClass))
    for (block_class, error), retried in RETRIED.items():
        for kind in ("detail", "listing"):
            stubs = web(block_class, "ok")
            stubs.http.error = error
            # The `ok` page states no JSON-LD, Open Graph tag or price (`PAGE`): still no render.
            stubs.acquire(FetchPolicy(page_kind=kind))
            assert bool(stubs.browser.calls) is retried, (block_class, error, kind)


def test_no_browser_retry_starts_too_close_to_the_deadline() -> None:
    stubs = web("shell", "ok")
    rungs = Rungs(http=stubs.http, browser=stubs.browser)
    deadline = time.monotonic() + BROWSER_MIN_S - 1

    result = asyncio.run(acquire(URL, FetchPolicy(), Remembered(), rungs, deadline=deadline))

    assert (result.method, stubs.browser.calls) == ("http", [])


def test_a_remembered_browser_start_that_fails_asks_http_once_and_keeps_only_its_ok() -> None:
    for browser in ("blocked", "error"):
        for http, kept in (("ok", ("http", "ok")), ("shell", ("browser", browser))):
            stubs = web(http, browser)
            result = stubs.acquire(remembered="browser")
            assert (result.method, result.block_class) == kept
            assert _attempts(result) == [("browser", browser), ("http", http)]
    # A browser `ok`, a job's own browser and a field source's render ask nothing over HTTP.
    passed, own, planned = web("ok", "ok"), web("ok", "blocked"), web("ok", "blocked")
    passed.acquire(remembered="browser")
    own.acquire(FetchPolicy(method_hint="browser"))
    rungs = Rungs(http=planned.http, browser=planned.browser)
    asyncio.run(acquire(URL, POLICY, Remembered(), rungs, plan=FetchPlan(browser=True)))
    assert passed.http.calls == own.http.calls == planned.http.calls == []


# --- an HTTP listing shell kept for want of a browser outcome is the fetch's last page ---


def test_the_last_rungs_shell_no_challenge_names_is_the_page() -> None:
    # Owner decision 2026-10-23: the browser's short page with no challenge is `ok`; one a challenge names is not, and
    # then the HTTP shell is kept as it is, not the last rung's page.
    shown = web("shell", "shell").acquire()
    challenged = web("shell", "shell")
    challenged.browser.challenge = "vendor"
    refused = challenged.acquire()
    # HTTP is the last rung when no browser attempt is made.
    http_only = web("shell", "ok").acquire(FetchPolicy(method_hint="http"))

    assert (shown.method, shown.block_class) == ("browser", "ok")
    assert _attempts(shown) == [("http", "shell"), ("browser", "shell")]
    assert (refused.method, refused.block_class) == ("http", "shell")
    assert (http_only.method, http_only.block_class) == ("http", "ok")


# --- a 503 interstitial gets one browser attempt after the site's pause ---


# --- an HTTP 429 gets one browser attempt after the pause it asks for ---


def test_a_429_is_answered_once_in_the_browser_after_its_pause() -> None:
    stubs = web("blocked", "ok", http_status=429)
    stubs.http.headers = (("Retry-After", "7"),)
    pauses: list[timedelta] = []

    async def admit(*, after: timedelta = timedelta(0)) -> bool:
        pauses.append(after)
        return after <= timedelta(minutes=1)  # a caller waits in place this long at most

    rungs = Rungs(http=stubs.http, browser=stubs.browser)
    result = asyncio.run(acquire(URL, POLICY, Remembered(), rungs, admit_next=admit))

    assert _attempts(result) == [("http", "blocked"), ("browser", "ok")]
    assert pauses == [timedelta(seconds=7)]
    # A pause longer than the caller waits sends nothing more: the 429 is kept.
    stubs = web("blocked", "ok", http_status=429)
    stubs.http.headers = (("Retry-After", "300"),)
    rungs = Rungs(http=stubs.http, browser=stubs.browser)
    result = asyncio.run(acquire(URL, POLICY, Remembered(), rungs, admit_next=admit))
    assert (_attempts(result), stubs.browser.calls) == ([("http", "blocked")], [])


def test_a_429_the_browser_answered_ends_the_task_whatever_the_browser_answered() -> None:
    # The browser's own 429 is its one attempt's outcome: a retry would send HTTP and the browser again.
    stubs = Web(_rung("http", "blocked", 429), _rung("browser", "blocked", 429))
    task = ClaimedTask(
        UrlTaskId(uuid7()), JobId(uuid7()), URL, URL, "detail", 1, 0, 0, None, None, "w", datetime.now(UTC)
    )

    result = stubs.acquire(POLICY)

    assert [(a.method, a.status) for a in result.attempts] == [("http", 429), ("browser", 429)]
    assert queued_end(result, task) == Finished("blocked", reason="http 429")


# --- a shell asks its referral before the render ---------------

PLATFORM = "https://jobs.example.test/board"


@dataclass
class Referrer:
    """A referring adapter of the platform at `PLATFORM`, which a page refers to by linking to it."""

    answer: ScriptedRung = field(default_factory=lambda: _rung("api", "ok"))
    name: str = "referrer"

    def detect(self, url: str) -> AdapterMatch | None:
        return AdapterMatch(tenant="board", page_kind="listing") if url == PLATFORM else None

    def serves(self, match: AdapterMatch, endpoint: str) -> bool:
        return True

    async def fetch(self, url: str, policy: FetchPolicy, send: object) -> FetchResult:
        return await self.answer(url, policy)

    def shapes(self, match: AdapterMatch, answer: object, /) -> tuple[Shape, ...]:
        return ()

    def referred(self, refs: PageRefs, kind: PageKind | None, types: tuple[str, ...], /) -> str | None:
        return PLATFORM if PLATFORM in refs.links else None


async def _unsent(request: JsonRequest, policy: FetchPolicy) -> FetchResult:
    raise AssertionError("the adapter stub sends nothing")


def test_a_script_only_shell_asks_its_referral_before_the_render() -> None:
    for challenge, rendered in ((None, False), ("structural", True)):
        stubs = web("shell", "ok")
        stubs.http.body = f"<html><body><div id='app'></div><a href='{PLATFORM}'>Jobs</a></body></html>".encode()
        stubs.http.challenge = challenge
        referrer = Referrer()
        rungs = Rungs(http=stubs.http, browser=stubs.browser, adapters=Registry([referrer]))

        result = asyncio.run(acquire(URL, POLICY, Remembered(), rungs, send=_unsent))

        assert bool(stubs.browser.calls) is rendered, challenge
        if not rendered:  # the referral serves the page: no render is paid for
            assert _attempts(result) == [("http", "shell"), ("api", "ok")]
            assert referrer.answer.calls == [(PLATFORM, POLICY)]
