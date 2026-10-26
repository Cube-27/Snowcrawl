"""The browser rung runs Patchright's documented setup:
Google Chrome stable in a persistent profile, with the WebRTC policy the guard needs in the profile's preferences, and
no client hints or headers but its own. Headless (the default), its own user agent with only its headless token renamed
and the window and screen of its site are its launch arguments, and its identity is set on its workers; headful, the
Patchright README's best practice, nothing of ours, on a display. The context's zone is the exit's
declared zone, else its country's, else the request locale's region's; its locale the request's, else the exit's. A
fake Playwright records what `open_browser` and `fetch_browser` ask for; no browser runs (what the real probe reads,
and what pages present on the wire: `test_browser_fetch.py`).
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from patchright.async_api import Playwright

import snowcrawl
from snowcrawl.acquire import browser_session
from snowcrawl.acquire.browser import fetch_browser
from snowcrawl.acquire.browser_profiles import profile_name
from snowcrawl.acquire.browser_session import BrowserSession, HeadlessIdentity, browser_identity, open_browser
from snowcrawl.acquire.desktop_screen import desktop_screen
from snowcrawl.acquire.session import SiteSessions
from snowcrawl.acquire.ssrf import SafeConnector, Target
from snowcrawl.acquire.types import FetchPolicy
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.core.proxy import UNDECLARED, ExitPlace

_SRC = Path(snowcrawl.__file__).parent


class _Recorder:
    def __init__(self) -> None:
        self.displays = 0
        self.overrides: list[str] = []
        self.launch: dict[str, Any] = {}
        self.probe: dict[str, Any] = {}
        self.probes = 0
        self.navigated: list[str] = []
        self.preferences: dict[str, Any] = {}


class _Stop(Exception):
    """Raised where the fake stops, at the first page event a fetch listens to: only the launch and context options and
    the identity set on the tab are under test."""


class _Session:
    def on(self, _event: str, _handler: object) -> None:
        return None

    async def send(self, _method: str, _params: object) -> dict[str, object]:
        return {}


class _ContextBrowser:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def new_browser_cdp_session(self) -> _Session:
        self._recorder.overrides.append("workers")
        return _Session()


class _Tab:
    def __init__(self, context: "_Context") -> None:
        self.context = context
        self.page = self  # `_override` takes a target that is no `Page` for a frame, and its page from it

    def on(self, _event: str, _handler: object) -> None:
        raise _Stop

    async def close(self) -> None:
        return None


class _Context:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.browser = _ContextBrowser(recorder)

    def on(self, _event: str, _handler: object) -> None:
        return None

    async def new_page(self) -> _Tab:
        return _Tab(self)

    async def new_cdp_session(self, _target: object) -> _Session:
        self._recorder.overrides.append("tab")
        return _Session()

    async def close(self) -> None:
        return None


_OWN_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36"
)
# What the browser's navigation sends, and its page's client hints (synthetic values); `priority` names nothing.
_HINTS = {"platform": "Linux", "platformVersion": "", "uaFullVersion": "156.0.0.1"}
_SENT = {
    "user-agent": _OWN_UA,
    "sec-ch-ua": '"Chromium";v="156", "Google Chrome";v="156", "Not.A/Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "accept": "text/html,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
    "priority": "u=0, i",
}


class _Page:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def goto(self, url: str) -> None:
        """Send `_SENT` to `url` as the browser's navigation would, over a real connection."""
        self._recorder.navigated.append(url)
        parts = urlsplit(url)
        reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
        fields = "".join(f"{name}: {value}\r\n" for name, value in _SENT.items())
        writer.write(f"GET / HTTP/1.1\r\nHost: {parts.netloc}\r\n{fields}\r\n".encode())
        await reader.read()
        writer.close()

    async def evaluate(self, _expression: str) -> dict[str, str]:
        return _HINTS


class _Browser:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def new_page(self) -> _Page:
        return _Page(self._recorder)

    async def close(self) -> None:
        return None


class _Chromium:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def launch(self, **options: Any) -> _Browser:
        self._recorder.probe = options
        self._recorder.probes += 1
        return _Browser(self._recorder)

    async def launch_persistent_context(self, user_data_dir: Path, **options: Any) -> _Context:
        self._recorder.launch = options
        self._recorder.preferences = json.loads((user_data_dir / "Default" / "Preferences").read_text())
        return _Context(self._recorder)


class _Playwright:
    def __init__(self, recorder: _Recorder) -> None:
        self.chromium = _Chromium(recorder)


class _Connector(SafeConnector):
    async def check(self, url: str) -> Target:
        return Target(url=url, host="shop.example.test", port=80, ip=IPv4Address("192.0.2.1"))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorded = _Recorder()

    @asynccontextmanager
    async def fake_playwright() -> Any:
        yield _Playwright(recorded)

    def display() -> None:
        recorded.displays += 1

    monkeypatch.setattr(browser_session, "async_playwright", fake_playwright)
    monkeypatch.setattr(browser_session, "ensure_display", display)
    monkeypatch.setattr(browser_session, "_IDENTITY", {})  # the identity is read once per process and mode
    return recorded


async def _fetch_once(locale: str | None = None, direct: ExitPlace = UNDECLARED, *, headless: bool = True) -> None:
    policy = FetchPolicy(browser_timeout_s=5, locale_hint=locale)
    async with open_browser(direct=direct, headless=headless) as session:
        assert isinstance(session, BrowserSession)
        await fetch_browser(
            "http://shop.example.test/item",
            policy,
            session=session,
            engine_version=ENGINE_VERSION,
            connector=_Connector(),
            scope=SiteSessions().scope_for(policy),
        )


# Headless: its user agent and the window and screen of the site's profile as launch arguments. Headful: the Patchright
# README's best practice, no argument of ours.
_MODES: dict[bool, dict[str, object]] = {
    True: {
        "headless": True,
        "args": [
            "--user-agent=" + _OWN_UA.replace("HeadlessChrome/", "Chrome/"),
            *desktop_screen(profile_name("example.test", None, None), "Linux").launch_args(),
        ],
    },
    False: {"headless": False},
}


@pytest.mark.parametrize("headless", [True, False], ids=["headless", "headful"])
def test_inv42_inv43_chrome_in_a_profile_with_the_guards_webrtc_policy_in_each_mode(
    recorder: _Recorder, headless: bool
) -> None:
    fetching = _fetch_once(headless=headless)
    with pytest.raises(_Stop):
        asyncio.run(fetching)

    # `chrome` is Google Chrome stable (`patchright install chrome`), Patchright's recommended build, in the mode's
    # launch options; WebRTC's proxy policy, which the SSRF guard needs, is a profile preference.
    recorder_launch = {k: v for k, v in recorder.launch.items() if k != "proxy"}
    launch_expected = {"channel": "chrome", "no_viewport": True, "accept_downloads": False, **_MODES[headless]}
    assert recorder_launch == launch_expected
    # Headless, the browser's identity is set on its workers and its tab; headful, nothing is set, on a display.
    set_on = (recorder.overrides, recorder.displays)
    set_expected = (["workers", "tab"], 0) if headless else ([], 1)
    assert set_on == set_expected
    # The browser started to read its headers is the same build in the same mode, loads no site and sends nothing
    # around the guard. It goes only to a listener of this process on 127.0.0.1, the one address its proxy bypasses.
    proxy = {"server": "socks5://127.0.0.1:9", "bypass": "127.0.0.1"}
    assert recorder.probe == {"channel": "chrome", "headless": headless, "proxy": proxy}
    assert [urlsplit(url).hostname for url in recorder.navigated] == ["127.0.0.1"]
    # Read once per process and mode: a session opened again (a restart) starts no second browser.
    fetch_again = _fetch_once(headless=headless)
    with pytest.raises(_Stop):
        asyncio.run(fetch_again)
    assert recorder.probes == 1
    assert recorder.preferences["webrtc"] == {
        "ip_handling_policy": "disable_non_proxied_udp",
        "multiple_routes_enabled": False,
        "nonproxied_udp_enabled": False,
    }


# Calls that change the browser's identity or run code in the page's main world (Patchright README: no custom
# user agent or headers; evaluate runs in an isolated world). Matched in source, so a new call site fails here; the ones
# allowed set the browser's own user agent with its headless token renamed and, through Chrome's
# override, its own client hints.
_IDENTITY_CALLS = re.compile(
    r"user_agent|user-agent=|extra_http_headers|setUserAgentOverride|add_init_script"
    r"|new_(browser_)?cdp_session|isolated_context=False|--enable-automation|--disable-blink-features"
)


def test_inv44_no_source_file_changes_the_browser_identity() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{number}"
        for path in sorted(_SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _IDENTITY_CALLS.search(line)
    ]

    # The browser's user agent switch, and Chrome's user agent override with the browser's own client hints on tabs,
    # frames and workers (`present`, `present_workers`); and a custom recipe's call sent by the browser, through the
    # warm page's own `fetch` in its main world, so the page's hooks on it apply (`acquire.browser_send`).
    allowed = ("user-agent=", '"Emulation.setUserAgentOverride"', "new_browser_cdp_session", "new_cdp_session(target)")
    session = sorted((f"acquire/browser_session.py:{_line_of(line)}" for line in allowed), key=_number)
    sent = f"acquire/browser_send.py:{_line_of('isolated_context=False', 'browser_send.py')}"
    assert offenders == [sent, *session]  # in path order


def _number(offender: str) -> int:
    return int(offender.rsplit(":", 1)[1])


def _line_of(text: str, module: str = "browser_session.py") -> int:
    """The one line of the acquire `module`'s source holding `text`."""
    lines = (_SRC / "acquire" / module).read_text(encoding="utf-8").splitlines()
    (number,) = (number for number, line in enumerate(lines, 1) if text in line)
    return number


@pytest.mark.parametrize(
    ("read", "value"),
    [(_SENT, '"HeadlessChrome";v="156", "Chromium";v="156"'), (_HINTS, "HeadlessChrome 156")],
    ids=["header", "client-hint"],
)
def test_a_browser_that_still_names_headless_mode_is_refused_at_startup(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, read: dict[str, str], value: str
) -> None:
    # Owner 2026-10-23: nothing the browser presents, and so nothing the HTTP rungs send, may name headless mode.
    monkeypatch.setitem(read, "sec-ch-ua" if read is _SENT else "model", value)
    probing = browser_identity(cast(Playwright, _Playwright(recorder)))

    with pytest.raises(HeadlessIdentity):
        asyncio.run(probing)


def test_open_shadow_roots_are_serialized_by_get_html_in_the_isolated_world_only() -> None:
    """Acquisition spec, "Rendered HTML": the render serializes open shadow roots with `getHTML`, called through
    `evaluate` in Patchright's isolated world, and nowhere else (no init script, user agent or extra headers)."""
    calls = [
        (path.relative_to(_SRC).as_posix(), line.strip())
        for path in sorted(_SRC.rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "getHTML" in line or "DOCUMENT_HTML_JS" in line
    ]
    evaluated = [line for _, line in calls if "evaluate(" in line]

    assert {path for path, _ in calls} == {"acquire/browser_capture.py"}
    assert evaluated == ["return cast(str, await page.evaluate(DOCUMENT_HTML_JS, isolated_context=True))"]


@pytest.mark.parametrize(
    ("hint", "direct", "zone"),
    [
        ("de-DE", UNDECLARED, "Europe/Berlin"),
        ("de-DE", ExitPlace("JP"), "Asia/Tokyo"),
        ("de-DE", ExitPlace("US"), None),
        ("de-DE", ExitPlace("US", "America/Chicago", "en-US"), "America/Chicago"),
        (None, ExitPlace("US", "America/Chicago", "de-DE"), "America/Chicago"),
    ],
    ids=["locale-region", "egress-country", "egress-country-of-many-zones", "declared-zone", "declared-locale"],
)
def test_inv44_a_profile_sets_only_the_proxy_downloads_screen_and_the_locality(
    recorder: _Recorder, hint: str | None, direct: ExitPlace, zone: str | None
) -> None:
    fetching = _fetch_once(hint, direct)
    with pytest.raises(_Stop):
        asyncio.run(fetching)

    # The guard's proxy, no downloads, the profile's screen, the request's locale (else the exit's declared one), and
    # the exit's declared zone, else the zone of its declared country, else of the locale's region (owner P8); a
    # country of several zones and no declared zone sets none (the machine's). The browser's own user agent, renamed,
    # as an argument; no client hints or headers.
    zoned = {} if zone is None else {"timezone_id": zone}
    assert {k: v for k, v in recorder.launch.items() if k in ("locale", "timezone_id")} == {"locale": "de-DE", **zoned}
    assert set(recorder.launch) == {
        "channel",
        "headless",
        "no_viewport",
        "args",
        "proxy",
        "accept_downloads",
        "locale",
        *zoned,
    }
