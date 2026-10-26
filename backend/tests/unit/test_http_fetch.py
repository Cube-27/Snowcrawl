"""HTTP fetch: manual redirects through the SSRF guard, caps, timeouts and the manifest.

A local server on 127.0.0.1 serves synthetic pages under `.test` hosts; no live network. `LoopbackConnector`
admits only those hosts, at the server's port, and sends every other URL through the real guard with a stub
resolver, so redirect hops leaving the server are checked exactly as in production.
"""

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

import pytest

from snowcrawl import cli
from snowcrawl.acquire.http import fetch_http
from snowcrawl.acquire.ssrf import SafeConnector, Target
from snowcrawl.acquire.types import AttemptRecord, FetchPolicy, FetchResult
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.storage.capture_bundle import decode, encode
from tests.local_server import LoopbackConnector, Reply, Server, serving
from tests.stand_in import IMPERSONATE

_LINKS = "".join(f'<a href="/item/{i}">Item {i}</a>' for i in range(8))
_PAGE = f"<html><body><h1>Catalogue</h1>{_LINKS}</body></html>".encode()


@pytest.fixture
def server() -> Iterator[Server]:
    yield from serving()


_DNS = {"internal.example.com": "10.0.0.1", "public.example.com": "93.184.215.14"}


def fetch(
    server: Server, url: str, policy: FetchPolicy | None = None, connector: SafeConnector | None = None
) -> FetchResult:
    connector = connector or LoopbackConnector(server.port, _DNS)
    return asyncio.run(
        fetch_http(url, policy or FetchPolicy(), engine_version="test", impersonate=IMPERSONATE, connector=connector)
    )


def outcome(result: FetchResult) -> tuple[int, str, str | None]:
    (attempt,) = result.attempts
    return result.bundle.manifest.status_code, result.block_class, attempt.error


def html(body: bytes, status: int = 200, *extra: tuple[str, str]) -> Reply:
    return Reply(status, (("Content-Type", "text/html; charset=utf-8"), *extra), body)


def redirect(location: str, status: int = 302) -> Reply:
    return Reply(status, (("Location", location),))


# --- a plain fetch -----------------------------------------------------------------------------------------------


def test_a_page_is_fetched_into_a_bundle_through_the_pinned_address(server: Server) -> None:
    # `shop.example.test` resolves nowhere (RFC 2606): the request can only reach the server through the address the
    # connector checked.
    server.route("/p", html(_PAGE))
    result = fetch(server, server.url("/p"))

    manifest = result.bundle.manifest
    assert (manifest.method, manifest.status_code, manifest.block_class) == ("http", 200, "ok")
    assert (manifest.url, manifest.final_url) == (server.url("/p"), server.url("/p"))
    assert result.bundle.body == _PAGE
    attempt = result.attempts[0]
    assert result.attempts == (
        AttemptRecord(
            method="http",
            status=200,
            block_class="ok",
            duration_ms=attempt.duration_ms,
            transferred=attempt.transferred,
        ),
    )
    assert attempt.transferred > len(_PAGE)  # the body and both header blocks
    assert manifest.engine_version == "test"
    assert manifest.fetch_policy == FetchPolicy().model_dump(mode="json")
    assert decode(encode(result.bundle)) == result.bundle


def test_a_checked_address_that_refuses_is_passed_over_for_the_next(server: Server) -> None:
    # The server listens on 127.0.0.1 only: the first address, ::1, refuses the connection.
    class TwoAddresses(LoopbackConnector):
        async def check(self, url: str) -> Target:
            checked = await super().check(url)
            return replace(checked, ip=IPv6Address("::1"), others=(IPv4Address("127.0.0.1"),))

    server.route("/p", html(_PAGE))

    result = fetch(server, server.url("/p"), connector=TwoAddresses(server.port, _DNS))

    assert outcome(result) == (200, "ok", None)


def test_a_refusal_standing_in_for_the_page_is_labelled_a_challenge_and_stays_blocked(server: Server) -> None:
    # The generic structural signal (a 403 document that can navigate on and is no page of the site); the label
    # changes no class. An `ok` page carries none.
    server.route("/p", html(b"<html><body><script>location.reload()</script></body></html>", 403))
    server.route("/ok", html(_PAGE))

    refused, kept = fetch(server, server.url("/p")), fetch(server, server.url("/ok"))

    assert (refused.block_class, refused.attempts[0].challenge) == ("blocked", "structural")
    assert (kept.block_class, kept.attempts[0].challenge) == ("ok", None)


# --- redirects ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("http://internal.example.com/admin", "private_address"),
        ("file:///etc/passwd", "scheme"),
        ("http://public.example.com:6379/", "port"),
        ("http://unknown.example.com/", "dns"),
    ],
)
def test_a_redirect_hop_that_fails_the_guard_is_an_error(server: Server, location: str, reason: str) -> None:
    server.route("/p", redirect(location))
    result = fetch(server, server.url("/p"))
    assert outcome(result) == (302, "error", f"ssrf_rejected: {reason}")
    assert result.bundle.body == b""
    assert len(server.requests) == 1


# --- cookies -----------------------------------------------------------------------------------------------------


# --- cookies stay inside one fetch -----------------------------------------------------------------------


# --- untrusted bodies are bounded --------------------------------------------------------------------------


def test_inv30_a_body_over_the_cap_is_an_error_without_a_body(server: Server) -> None:
    server.route("/p", html(b"x" * 50_000))
    result = fetch(server, server.url("/p"), FetchPolicy(max_body_bytes=10_000))
    assert outcome(result) == (200, "error", "body_cap")
    assert result.bundle.body == b""
    # A 2xx answer no page reader reads is told by its media type, whatever its size: no body is kept either.
    server.route("/f.pdf", Reply(200, (("Content-Type", "application/pdf"),), b"%PDF" * 20_000))
    result = fetch(server, server.url("/f.pdf"), FetchPolicy(max_body_bytes=10_000))
    assert outcome(result) == (200, "unsupported_media", "body_cap")
    assert result.bundle.body == b""


# --- caps and failures -------------------------------------------------------------------------------------------


# --- the fetch command -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "status", "block_class"),
    [(html(b"gone", 404), 404, "not_found")],
)
def test_the_fetch_command_writes_a_bundle_that_decodes(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reply: Reply,
    status: int,
    block_class: str,
) -> None:
    # It needs no database or object store: the bundle goes to a file.
    for name in list(os.environ):
        if name.upper().startswith("SNOWCRAWL_"):
            monkeypatch.delenv(name)
    server.route("/p", reply)
    out = tmp_path / "bundle.zst"

    asyncio.run(cli.fetch_to_file(server.url("/p"), out, LoopbackConnector(server.port, _DNS)))

    bundle = decode(out.read_bytes())
    manifest = bundle.manifest
    assert (manifest.url, manifest.status_code, manifest.block_class) == (server.url("/p"), status, block_class)
    assert bundle.body == reply.body
    assert manifest.engine_version == ENGINE_VERSION
    assert json.loads(capsys.readouterr().out) == {
        "capture_id": str(manifest.capture_id),
        "final_url": server.url("/p"),
        "status_code": status,
        "block_class": block_class,
        "error": None,
    }
