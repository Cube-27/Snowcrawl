"""What a fetch teaches the acquisition contract (`acquire.memory.lesson`), over every combination of the HTTP evidence,
the challenge it named and whether the browser's cookies
went on to HTTP; and what the contract holds (`site_contract`). The rows themselves are grounded at runtime.
"""

from itertools import product
from typing import Literal

from snowcrawl.acquire.memory import Lesson, lesson, refused
from snowcrawl.acquire.types import AttemptRecord, FetchPolicy, FetchResult
from snowcrawl.core.capture import BlockClass
from snowcrawl.db.models import SiteContract
from tests.support.captures import fetched, with_attempts

URL = "https://shop.example.test/item/1"
BODY = b"<html><body><h1>Item</h1></body></html>"
HTTP_OUTCOMES: tuple[tuple[BlockClass, int], ...] = (
    ("ok", 200),
    ("shell", 200),
    ("blocked", 403),
    ("blocked", 429),
    ("blocked", 503),
    ("error", 500),
)
HINTS: tuple[Literal["auto", "http", "browser"], ...] = ("auto", "http", "browser")


def _fetched(policy: FetchPolicy, kept: AttemptRecord, attempts: tuple[AttemptRecord, ...]) -> FetchResult:
    result = fetched(URL, kept.status or 0, BODY, method=kept.method, block_class=kept.block_class, policy=policy)
    return with_attempts(result, attempts)


def test_a_contract_is_taught_only_by_an_auto_fetch_that_tried_http_and_a_handoff_only_by_a_passed_block() -> None:
    for (http_class, status), challenge, carried, hint in product(
        HTTP_OUTCOMES, (None, "vendor"), (False, True), HINTS
    ):
        http = AttemptRecord(method="http", status=status, block_class=http_class, duration_ms=1, challenge=challenge)
        browser = AttemptRecord(method="browser", status=200, block_class="ok", duration_ms=1, site_session=carried)
        learned = lesson(_fetched(FetchPolicy(method_hint=hint), browser, (http, browser)))
        if hint != "auto":
            assert learned is None
            continue
        passed_block = refused(http)
        assert passed_block == (
            (http_class == "blocked" and status not in (429, 503)) or (http_class == "shell" and challenge is not None)
        )
        # A kept HTTP `ok` teaches the default; a fetch that never tried HTTP teaches nothing.
        assert lesson(_fetched(FetchPolicy(), http, (http,))) == (None if http_class != "ok" else Lesson("http"))
        assert lesson(_fetched(FetchPolicy(), browser, (browser,))) is None
        # Only HTTP evidence about the method (a shell, or a refusal) teaches anything from a browser `ok`: a 429, a
        # 503 or a server error is transient (an `ok` HTTP attempt before a browser `ok` is no fetch `rungs` makes).
        if not (http_class == "shell" or passed_block):
            assert learned is None
            continue
        assert learned is not None
        # A browser `ok` after a block whose cookies went on to HTTP teaches `http` with the handoff.
        assert learned.handoff == (passed_block and carried)
        assert learned.method == ("http" if learned.handoff else "browser")


def test_a_transient_http_failure_before_a_browser_ok_teaches_nothing() -> None:
    browser = AttemptRecord(method="browser", status=200, block_class="ok", duration_ms=1)
    for status, error in ((None, "timeout"), (None, "network_error"), (500, None)):
        http = AttemptRecord(method="http", status=status, block_class="error", duration_ms=1, error=error)
        assert lesson(_fetched(FetchPolicy(), browser, (http, browser))) is None


def test_the_contract_is_method_engine_handoff_and_coverage() -> None:
    # Its key and bookkeeping aside; the platform is the site's, `site.platform`.
    bookkeeping = {"site_id", "page_kind", "failures", "checked_at"}
    assert set(SiteContract.__table__.columns.keys()) - bookkeeping == {"method", "engine", "handoff", "coverage"}
