"""The caller's request context and page actions as a request names them: the checks of `fetch`,
the step vocabulary's translation, the redaction of secret values, and the HTTP rung sending headers and cookies to the
fetched URL's host only.

The HTTP rung runs against a local server under `.test` names (RFC 2606), as `test_http_fetch.py` does.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from snowcrawl.acquire.api import JsonRequest, fetch_json
from snowcrawl.acquire.http import fetch_http
from snowcrawl.acquire.types import FetchPolicy, FetchResult, RequestContext
from snowcrawl.core.redaction import REDACTED, has_secrets
from snowcrawl.services.fetch_options import (
    KEEP_SECRETS,
    FetchOptions,
)
from tests.local_server import LoopbackConnector, Reply, Server, serving
from tests.stand_in import IMPERSONATE

_PAGE = b'<html><body><a href="/a">a</a><a href="/b">b</a><p>words of a page</p></body></html>'


@pytest.fixture
def server() -> Iterator[Server]:
    yield from serving()


def _options(**members: Any) -> FetchOptions:
    return FetchOptions.model_validate(members)


def _refused(**members: Any) -> str:
    with pytest.raises(ValidationError) as refused:
        FetchOptions.model_validate(members)
    return str(refused.value)


# --- the `fetch` member ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["User-Agent", "Cookie", "Host", "Sec-CH-UA", "sec-fetch-site", "Proxy-Authorization"])
def test_a_header_the_fetch_sets_itself_is_refused(name: str) -> None:
    assert "sets itself" in _refused(headers={name: "x"})


def test_secret_values_are_redacted_except_in_a_stored_job_request() -> None:
    options = _options(headers={"Authorization": "Bearer t0ken"}, cookies={"sid": "s3cret"})

    shown = options.model_dump(mode="json")
    kept = options.model_dump(mode="json", context={KEEP_SECRETS: True})

    assert shown["headers"] == {"Authorization": REDACTED}
    assert shown["cookies"] == {"sid": REDACTED}
    assert kept["headers"] == {"Authorization": "Bearer t0ken"}
    assert kept["cookies"] == {"sid": "s3cret"}
    assert FetchOptions.model_validate(kept) == options  # the worker reads back what was submitted
    assert "t0ken" not in repr(options)
    assert "s3cret" not in repr(options)


# --- the HTTP rung -----------------------------------------------------------------------------------------------


def _fetch(server: Server, url: str, context: RequestContext) -> FetchResult:
    return asyncio.run(
        fetch_http(
            url,
            FetchPolicy(context=context),
            engine_version="test",
            impersonate=IMPERSONATE,
            connector=LoopbackConnector(server.port, {}),
        )
    )


def _context(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> RequestContext:
    return RequestContext(
        headers=tuple((k, SecretStr(v)) for k, v in (headers or {}).items()),
        cookies=tuple((k, SecretStr(v)) for k, v in (cookies or {}).items()),
    )


def _sent(server: Server, at: str) -> dict[str, str]:
    return next({name.lower(): value for name, value in headers} for path, headers in server.requests if path == at)


def test_the_callers_header_and_cookie_go_to_the_pages_host_and_stay_out_of_the_capture(server: Server) -> None:
    server.route("/p", Reply(200, (("Content-Type", "text/html"),), _PAGE))
    result = _fetch(server, server.url("/p"), _context({"Authorization": "Bearer t0ken"}, {"sid": "s3cret"}))

    sent = _sent(server, "shop.example.test/p")
    assert sent["authorization"] == "Bearer t0ken"
    assert sent["cookie"] == "sid=s3cret"
    stored = result.bundle.manifest.model_dump_json()
    assert "t0ken" not in stored
    assert "s3cret" not in stored


def _fetch_json(server: Server, endpoint: str, context: RequestContext, page_url: str | None = None) -> FetchResult:
    return asyncio.run(
        fetch_json(
            JsonRequest(url=endpoint, page_url=page_url or server.url("/board")),
            FetchPolicy(context=context),
            engine_version="test",
            impersonate=IMPERSONATE,
            connector=LoopbackConnector(server.port, {}),
        )
    )


def test_a_json_call_to_another_host_carries_neither_the_header_nor_the_cookie(server: Server) -> None:
    # An adapter's or a recipe's endpoint on another host than the page the caller named.
    server.route("/api/jobs", Reply(200, (("Content-Type", "application/json"),), b"[]"))
    # A header of the call's own (`Accept`) in another case is replaced, not sent twice (RFC 9110 §5.1).
    context = _context({"Authorization": "Bearer t0ken", "accept": "application/x-test"}, {"sid": "s3cret"})
    _fetch_json(server, server.url("/api/jobs", "api.example.test"), context)
    _fetch_json(server, server.url("/api/jobs"), context)

    elsewhere = _sent(server, "api.example.test/api/jobs")
    assert "authorization" not in elsewhere
    assert "cookie" not in elsewhere
    same_host = _sent(server, "shop.example.test/api/jobs")
    assert same_host["authorization"] == "Bearer t0ken"
    assert same_host["cookie"] == "sid=s3cret"
    raw = next(headers for path, headers in server.requests if path == "shop.example.test/api/jobs")
    assert [(name, value) for name, value in raw if name.lower() == "accept"] == [("Accept", "application/x-test")]


def test_a_call_from_an_https_page_over_cleartext_carries_neither_the_header_nor_the_cookie(server: Server) -> None:
    # The same host and port as the page, but its scheme downgraded: another origin (RFC 6454 §4).
    server.route("/api/jobs", Reply(200, (("Content-Type", "application/json"),), b"[]"))
    context = _context({"Authorization": "Bearer t0ken"}, {"sid": "s3cret"})
    _fetch_json(server, server.url("/api/jobs"), context, page_url=server.url("/board").replace("http:", "https:", 1))

    downgraded = _sent(server, "shop.example.test/api/jobs")
    assert "authorization" not in downgraded
    assert "cookie" not in downgraded


# --- the CLI -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fetch", "expected"),
    [
        ({"headers": {"X-Token": "s3cret"}}, True),
        ({"cookies": {"sid": "abc"}}, True),
        ({"headers": {"X-Token": REDACTED}, "cookies": {"sid": REDACTED}}, False),
        ({"headers": {}, "cookies": {}, "method_hint": "http"}, False),
        ({"headers": {"X-Token": REDACTED}, "cookies": {"sid": "abc"}}, True),
    ],
)
def test_a_request_holds_secrets_while_any_header_or_cookie_value_is_not_the_placeholder(
    fetch: dict[str, object], expected: bool
) -> None:
    assert has_secrets({"fetch": fetch}) is expected
