"""Which Chrome the HTTP and JSON rungs impersonate: the browser rung's Chrome, read once per
process, its headers sent in place of the nearest curl_cffi profile's own; an operator's explicit target is kept with
its own headers; a process that renders does not start without them. The headers go out on the wire through the real
curl transport to a local server."""

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import get_args

import pytest
from curl_cffi.requests import BrowserTypeLiteral
from patchright.async_api import Error as PlaywrightError

from snowcrawl.acquire import impersonate
from snowcrawl.acquire.curl_client import Hop, Limits, curl_transport
from snowcrawl.acquire.impersonate import BrowserUnread, Impersonation, nearest_target, rung_impersonation
from snowcrawl.acquire.outcome import Traffic
from snowcrawl.core.settings import AcquireSettings
from snowcrawl.storage.capture_bundle import HeaderList
from tests.local_server import LoopbackConnector, Reply, Server, serving

_PROFILES = sorted(
    int(name[6:]) for name in get_args(BrowserTypeLiteral) if name[6:].isdigit() and name[:6] == "chrome"
)
_NEWER = _PROFILES[-1] + 3
# Headers as a Chrome newer than every curl_cffi profile sends them on an HTTP/1.1 navigation, in its spelling
# (synthetic values).
_BROWSER = (
    ("User-Agent", f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_NEWER}.0.0.0"),
    ("sec-ch-ua", f'"Chromium";v="{_NEWER}", "Google Chrome";v="{_NEWER}", "Not.A/Brand";v="99"'),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", '"Linux"'),
    ("Accept", "text/html,*/*;q=0.8"),
)


def _of(major: int) -> HeaderList:
    return tuple((name, value.replace(str(_NEWER), str(major))) for name, value in _BROWSER)


@pytest.mark.parametrize("major", [_PROFILES[-1], _NEWER])
def test_unset_the_target_follows_the_browser_and_names_the_major_of_its_handshake(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, major: int
) -> None:
    async def read(_headless: bool) -> HeaderList:
        return _of(major)

    monkeypatch.setattr(impersonate, "_read_headers", read)

    with caplog.at_level(logging.WARNING):
        chosen = asyncio.run(rung_impersonation(AcquireSettings(impersonate_lag=2), browser_rung=True))

    assert chosen.target == nearest_target(major)
    # A profile more than the bound behind the browser warns to upgrade curl_cffi.
    assert ("upgrade curl_cffi" in caplog.text) == (major - _PROFILES[-1] > 2)
    # Cookies the browser earned go with its own headers.
    assert chosen.replaying(True).headers == _of(major)
    if major == _PROFILES[-1]:  # a profile of the browser's major: its own headers
        assert chosen.headers == _of(major)
        return
    # Else the profile's own, but for the browser's platform, under the profile's major.
    assert chosen.headers == (("User-Agent", _of(_PROFILES[-1])[0][1]), _BROWSER[3])
    assert f"Chrome {_NEWER}" in caplog.text


def test_an_explicit_target_is_kept_with_its_own_headers_and_starts_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unread(_headless: bool) -> None:
        raise AssertionError("no browser starts for an explicit target")

    monkeypatch.setattr(impersonate, "_read_headers", unread)
    monkeypatch.setenv("SNOWCRAWL_IMPERSONATE", f"chrome{_PROFILES[0]}")

    chosen = asyncio.run(rung_impersonation(AcquireSettings(), browser_rung=True))

    assert chosen == Impersonation(nearest_target(_PROFILES[0]))


async def _failing(_headless: bool) -> HeaderList:
    raise PlaywrightError("Chromium distribution 'chrome' is not found")


def test_a_browser_that_does_not_start_leaves_the_default_target_where_nothing_renders(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(impersonate, "_read_headers", _failing)

    with caplog.at_level(logging.WARNING):
        chosen = asyncio.run(rung_impersonation(AcquireSettings(), browser_rung=False))

    assert chosen == Impersonation(AcquireSettings().impersonate)
    assert "did not start" in caplog.text


def test_a_process_that_renders_does_not_start_without_its_browsers_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Its HTTP rung would otherwise name another browser than its renders.
    monkeypatch.setattr(impersonate, "_read_headers", _failing)
    reading = rung_impersonation(AcquireSettings(), browser_rung=True)

    with pytest.raises(BrowserUnread):
        asyncio.run(reading)


@pytest.fixture
def server() -> Iterator[Server]:
    yield from serving()


def test_the_browsers_headers_replace_the_profiles_and_a_requests_own_replace_them(server: Server) -> None:
    """Each field goes out once, in the browser's spelling whatever the request's (RFC 9110 §5.1), and a cleartext
    request offers no `h2c` upgrade, which no browser sends."""
    server.route("/p", Reply(body=b"ok"))
    url = server.url("/p")

    async def send() -> None:
        target = await LoopbackConnector(server.port, {}).check(url)
        limits = Limits(10_000, 10_000, time.monotonic() + 5)
        chosen = Impersonation(nearest_target(_PROFILES[-1]), _BROWSER)
        async with curl_transport(chosen, None, limits, Traffic()) as transport:
            await transport(target, Hop("GET", {"accept": "application/json", "Accept-Language": "de-DE"}))

    asyncio.run(send())

    (_, sent), *_ = server.requests
    expected = {**dict(_BROWSER), "Accept": "application/json", "Accept-Language": "de-DE"}
    named = [(name, value) for name, value in sent if name.lower() in {name.lower() for name in expected}]
    assert dict(named) == expected
    assert len(named) == len(expected)  # each field once
    assert not {name.lower() for name, _ in sent} & {"upgrade", "http2-settings"}
