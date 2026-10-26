"""The browser rung's session: one Playwright driver per worker process
(`open_browser`) and one browser per open site profile (`_Profiles`, `acquire.browser_profiles`: a persistent context of
the site's own, not an incognito one). Every connection a profile's browser opens goes through its `SocksGuard`, which
applies the SSRF guard to every redirect hop, frame and worker. The caller's cookies (`RequestContext`) are added
to the context before the navigation, as host-only cookies of the URL's host; the caller's headers are never set in the
browser.

The browser runs in one of two modes (`SNOWCRAWL_BROWSER_HEADLESS`), which decide its launch options
(`_mode_options`) and whether its identity is set over its own:
- headless (the default): every page, cross-site frame and popup presents the browser's own identity (`present`), and
  so does every service and shared worker (`present_workers`), on the screen of its site profile
  (`acquire.desktop_screen`);
- headful: the Patchright README's best practice exactly, a browser window that presents itself, with no user agent,
  header, screen or override of ours (`present` and `present_workers` set nothing). On Linux with no display it opens on
  a virtual one (`acquire.display`).
"""

import asyncio
import contextlib
import json
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Required, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

from patchright._impl._api_structures import ProxySettings, SetCookieParam
from patchright.async_api import BrowserContext, CDPSession, Frame, Page, Playwright, async_playwright
from patchright.async_api import Error as PlaywrightError

from snowcrawl.acquire.browser_profiles import Profile, discard_profile, open_profile, profile_name
from snowcrawl.acquire.desktop_screen import desktop_screen
from snowcrawl.acquire.display import ensure_display
from snowcrawl.acquire.locality import Locality, locality
from snowcrawl.acquire.outcome import Exchange, Traffic
from snowcrawl.acquire.public_suffix import site_of
from snowcrawl.acquire.session import SessionCookie, SessionScope, browser_cookies
from snowcrawl.acquire.shared_browser import BROWSER_CONTEXTS
from snowcrawl.acquire.socks_guard import SocksGuard
from snowcrawl.acquire.ssrf import SafeConnector, Target
from snowcrawl.acquire.types import FetchPolicy, RequestContext
from snowcrawl.core.proxy import UNDECLARED, ExitPlace, ProxyEndpoint, ProxyPool
from snowcrawl.storage.capture_bundle import HeaderList

__all__ = [
    "BrowserSession",
    "ContextKey",
    "Failed",
    "HeadlessIdentity",
    "Identity",
    "OpenProfile",
    "browser_identity",
    "context_cookies",
    "context_key",
    "context_locality",
    "open_browser",
    "open_context",
    "open_driver",
    "present",
    "present_workers",
]

# Patchright's documented setup: Google Chrome stable (`chrome`, installed with
# `patchright install chrome`), the build the Patchright README recommends, with its own client hints and no custom
# headers, in a persistent profile, with the README's `no_viewport`. Headful, that is all (the README's "Best
# Practice"). Headless, two kinds of launch argument: its user agent (`_USER_AGENT_SWITCH`), and the window and screen
# of the profile (`acquire.desktop_screen`). WebRTC's proxy policy is a profile preference (`acquire.browser_profiles`).
_CHANNEL = "chrome"
# Headless, the one change to the browser's identity: its own user
# agent, read from the browser, with the token headless mode adds (`HeadlessChrome/`) named as a headed browser names
# it, and its own User-Agent Client Hints. The user agent is Chrome's `--user-agent` switch, its default for every
# request and every worker it starts: a service or shared worker's script request goes from the browser process, and
# its `navigator.userAgent` is fixed when it starts, both before any CDP target exists. With the switch, Chrome
# reports no high-entropy client hints of its own, so every page, frame and worker takes them, with the user agent,
# through Chrome's user agent override (`present`, `present_workers`). Nothing the browser presents may still name
# headless mode (`_HEADLESS`, case-insensitive): the process refuses to start rather than send it.
_HEADLESS_PRODUCT: Final = "HeadlessChrome/"
_HEADED_PRODUCT: Final = "Chrome/"
_HEADLESS: Final = "headless"
_USER_AGENT: Final = "user-agent"
_USER_AGENT_SWITCH: Final = "--user-agent="
_PLATFORM: Final = "sec-ch-ua-platform"
_OVERRIDE: Final = "Emulation.setUserAgentOverride"
# The targets that run a page's scripts outside its tabs and frames (HTML Standard §10.2.6 shared workers, Service
# Workers §3.1), in CDP's target types.
_WORKERS: Final = ({"type": "service_worker"}, {"type": "shared_worker"})
# The browser started only to read the identity it presents (`browser_identity`) loads no page of any site; anything it
# requests in the background goes to this closed loopback port and fails, rather than around the guard.
_NOWHERE: Final = "socks5://127.0.0.1:9"
# The page that browser navigates to, the one address its proxy bypasses: a listener of this process on the loopback
# address (RFC 6890 §2.2.2), which reads the request as the browser's network stack sends it (Playwright's request
# routing sees it before that stack adds `Accept-Language`) and answers an empty page. A loopback origin is potentially
# trustworthy (W3C Secure Contexts §3.2): client hints go to it, and its page reads them.
_LOOPBACK: Final = "127.0.0.1"
_EMPTY_PAGE: Final = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
# The headers of a navigation that name the browser (RFC 9110 §10.1.5 `User-Agent`, RFC 8942 client hints, the
# User-Agent Client Hints low-entropy set) and what it asks for (RFC 9110 §12.5.1 `Accept`, §12.5.4 `Accept-Language`
# as a context without a locale sends it, Upgrade Insecure Requests §3.2.1). The HTTP and JSON rungs send them in place
# of their impersonation's (`acquire.impersonate`).
_NAMING_HEADERS: Final = frozenset(
    {
        _USER_AGENT,
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        _PLATFORM,
        "accept",
        "accept-language",
        "upgrade-insecure-requests",
    }
)
# The browser's User-Agent Client Hints as it reports them itself (User-Agent Client Hints §3.3
# `getHighEntropyValues`), every hint CDP's `Emulation.UserAgentMetadata` carries; `uaFullVersion` is its
# `fullVersion`.
_HINTS: Final = (
    "architecture",
    "bitness",
    "formFactors",
    "fullVersionList",
    "model",
    "platformVersion",
    "uaFullVersion",
    "wow64",
)
_HINTS_JS: Final = f"() => navigator.userAgentData.getHighEntropyValues({list(_HINTS)!r})"
_HINT_FIELDS: Final = {"uaFullVersion": "fullVersion"}
# Chromium's implicit proxy-bypass rules (`ProxyBypassRules`, net/proxy_resolution) send `localhost`, 127.0.0.0/8,
# [::1], 169.254.0.0/16 and fe80::/10 around any proxy; 169.254.169.254 is the cloud metadata address. The bypass rule
# `<-loopback>` removes those implicit rules, so they too go through the guard. Playwright adds it itself
# unless told not to; it is named here so the guard does not rest on a library default.
_PROXY_ALL_LOOPBACK = "<-loopback>"
# Site profiles a process keeps open (`_Profiles`), rendering or idle: as many as it renders at once, each a browser
# process of its own. An idle one closes after `_PROFILE_IDLE_S`, an unmeasured starting point: the next page of a site
# in a job usually comes within seconds.
_OPEN_PROFILES: Final = BROWSER_CONTEXTS
_PROFILE_IDLE_S = 60.0
# The browser's identity (`browser_identity`), read once per process and mode: the installed Chrome does not change
# while the process runs.
_IDENTITY: dict[tuple[str, bool], "Identity"] = {}


class HeadlessIdentity(RuntimeError):
    """What the browser presents still names headless mode: the process does not start."""


@dataclass(frozen=True)
class Identity:
    """The browser rung's Chrome as its profiles present it: the headers that name it on a navigation
    (`_NAMING_HEADERS`, its user agent's headless token renamed), which the HTTP and JSON rungs send too, and its
    User-Agent Client Hints as the browser reports them (CDP's `Emulation.UserAgentMetadata`), which `present` sets
    with that user agent."""

    headers: HeaderList
    metadata: Mapping[str, object]

    @property
    def agent(self) -> str:
        return next((value for name, value in self.headers if name.lower() == _USER_AGENT), "")

    @property
    def platform(self) -> str:
        """The platform `Sec-CH-UA-Platform` names, its value an sf-string (RFC 8941 §3.3.3) unquoted."""
        value = next((value for name, value in self.headers if name.lower() == _PLATFORM), "")
        return value.strip('"')


class Failed(Exception):
    """A browser fetch that ends as an `error` outcome: the short reason, and the exchange when a response arrived."""

    def __init__(self, error: str, exchange: Exchange | None) -> None:
        super().__init__(error)
        self.error = error
        self.exchange = exchange


@dataclass
class OpenProfile:
    """A site profile's browser (a persistent context), the guard every connection it opens goes through and the
    connector that guard checks with, the bytes the guard has relayed, the locale it was opened with, and what closes
    them and releases the profile. `gone` once its browser closed unasked (it crashed or was killed). `warm` is the tab
    a recipe's calls are sent from (`acquire.browser_send`), opened on its first call and kept while the profile is
    open: it closes with the browser."""

    context: BrowserContext
    guard: SocksGuard
    connector: SafeConnector
    traffic: Traffic
    stack: AsyncExitStack
    locale: Locality
    profile: Profile
    closing: bool = False
    gone: bool = False
    warm: Page | None = None

    async def close(self) -> None:
        self.closing = True
        with contextlib.suppress(PlaywrightError):
            await self.stack.aclose()


# What a profile is (`context_key`): the site, the proxy pool and endpoint.
type ContextKey = tuple[str, str | None, ProxyEndpoint | None]


def _exit_bound(key: ContextKey) -> bool:
    """A profile of a sticky exit (`core.proxy`), which lives in this process alone: it lasts while it is open, in a
    temporary directory, and is never kept on disk for a later process, whose exits are new."""
    return key[2] is not None and key[2].gateway is not None


@dataclass
class _Profiles:
    """The site profiles open in this process: a site's next page, in any job, opens
    in the profile its earlier pages left, so its cookies, storage and passed challenges carry on, as in one user's
    browser. A profile is taken while a page renders in it, so no two pages share one at once (a second page of the
    site gets a temporary profile, `acquire.browser_profiles`); at most `_OPEN_PROFILES` are open, the least recently
    used idle one closed first, an idle one closes after `_PROFILE_IDLE_S`, and all close with the session. A profile
    whose browser closed unasked is dropped alone (`forget`): the other profiles' renders go on. A sticky exit's
    profile is a temporary one kept idle like the rest (`_exit_bound`)."""

    root: Path | None = None
    idle: OrderedDict[ContextKey, OpenProfile] = field(default_factory=OrderedDict)
    busy: int = 0
    _timers: dict[ContextKey, asyncio.TimerHandle] = field(default_factory=dict)
    _closing: set[asyncio.Task[None]] = field(default_factory=set)

    async def take(self, key: ContextKey, locale: Locality, connector: SafeConnector) -> OpenProfile | None:
        """The idle profile of `key`, now rendering; None when there is none, or it has another locale or guard
        connector (then it is closed)."""
        self._stop_timer(key)
        held = self.idle.pop(key, None)
        if held is not None and (held.locale != locale or held.connector is not connector):
            await held.close()
            held = None
        if held is not None:
            self.busy += 1
        return held

    async def room(self) -> None:
        """Make room for a profile about to open: idle ones are closed, least recently used first, down to the cap."""
        while self.idle and len(self.idle) + self.busy >= _OPEN_PROFILES:
            key, oldest = self.idle.popitem(last=False)
            self._stop_timer(key)
            await oldest.close()
        self.busy += 1

    async def keep(self, key: ContextKey, context: OpenProfile) -> None:
        """`context` stops rendering and is kept idle for the site's next page; a temporary profile opened because the
        site's own was held, or one whose browser is gone, is closed instead, so it never stands in for the site's
        profile."""
        if context.gone or (self.root is not None and context.profile.lock is None and not _exit_bound(key)):
            await self.release(context)
            return
        self.busy -= 1
        previous = self.idle.pop(key, None)
        if previous is not None:  # two pages of a site rendered at once: the one kept first is closed, never dropped
            self._stop_timer(key)
            await previous.close()
        self.idle[key] = context
        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(_PROFILE_IDLE_S, self._expire, key, context)

    async def discard(self, key: ContextKey) -> None:
        """Delete the profile of `key` (`acquire.browser_profiles.discard_profile`), closing it first when it is idle
        here; one rendering a page stays."""
        self._stop_timer(key)
        held = self.idle.pop(key, None)
        if held is None:
            if not _exit_bound(key):
                discard_profile(self.root, profile_name(*key))
            return
        held.profile.discarded = True
        await held.close()

    async def release(self, context: OpenProfile) -> None:
        """`context` stops rendering and closes."""
        self.busy -= 1
        await context.close()

    async def aclose(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        while self.idle:
            _, context = self.idle.popitem()
            await context.close()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def _expire(self, key: ContextKey, context: OpenProfile) -> None:
        self._timers.pop(key, None)
        if self.idle.get(key) is context:
            del self.idle[key]
            self._close_soon(context)

    def forget(self, context: OpenProfile) -> None:
        """`context`'s browser closed unasked: no page may take it again. An idle one is closed now, so its guard
        stops and its profile is released; a rendering one is released when its page ends (`keep`)."""
        context.gone = True
        for key, idle in tuple(self.idle.items()):
            if idle is context:
                del self.idle[key]
                self._stop_timer(key)
                self._close_soon(context)

    def _close_soon(self, context: OpenProfile) -> None:
        """Close `context` from a callback (no await there); `aclose` waits for it."""
        task = asyncio.get_running_loop().create_task(context.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _stop_timer(self, key: ContextKey) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()


@dataclass(frozen=True)
class BrowserSession:
    """A Playwright driver, shared by the fetches of one worker process, the identity its profiles present (`Identity`),
    the site profiles it keeps open, the place this machine's direct fetches go out from
    (`SNOWCRAWL_EGRESS_*`, the settings' `direct`), and the browser's mode (`SNOWCRAWL_BROWSER_HEADLESS`)."""

    playwright: Playwright
    identity: Identity
    profiles: _Profiles = field(default_factory=_Profiles)
    direct: ExitPlace = UNDECLARED
    headless: bool = True

    @property
    def alive(self) -> bool:
        """Whether the Playwright driver is still connected (`SharedBrowser` restarts it if not). A profile's browser
        that crashed is dropped on its own (`_Profiles.forget`) and does not end the session."""
        return _driver_connected(self.playwright)


def _driver_connected(playwright: Playwright) -> bool:
    """Playwright has no public driver liveness: its pipe transport sets `on_error_future` once the driver process's
    pipe closes, which fails every call after it."""
    transport = getattr(getattr(playwright, "_impl_obj", None), "_connection", None)
    error = getattr(getattr(transport, "_transport", None), "on_error_future", None)
    return not isinstance(error, asyncio.Future) or not error.done()


@asynccontextmanager
async def open_browser(
    profiles: Path | None = None, direct: ExitPlace = UNDECLARED, *, headless: bool = True
) -> AsyncIterator[BrowserSession]:
    """The driver runs until the block ends; leaving it closes its profiles' browsers and the driver process. Site
    profiles are kept under `profiles` (`SNOWCRAWL_BROWSER_PROFILES`); without it, each lasts while it is open.
    `direct` is the place the machine's direct fetches go out from, whose locality a direct render's context takes.
    `headless` is the browser's mode (module docstring)."""
    async with open_driver(headless) as playwright:
        identity = await browser_identity(playwright, headless=headless)
        session = BrowserSession(playwright, identity, _Profiles(profiles), direct, headless)
        try:
            yield session
        finally:
            await session.profiles.aclose()


@asynccontextmanager
async def open_driver(headless: bool) -> AsyncIterator[Playwright]:
    """A Playwright driver for browsers of the mode `headless`; a headful one's windows need a display, which the driver
    and its browsers find in the environment when the driver starts (`acquire.display`)."""
    if not headless:
        await asyncio.to_thread(ensure_display)
    async with async_playwright() as playwright:
        yield playwright


async def browser_identity(playwright: Playwright, *, headless: bool = True) -> Identity:
    """The browser's identity in the mode `headless`: the headers that name it (`_NAMING_HEADERS`) as its navigation
    sends them, its user agent's headless token renamed, and the client hints its page reports (`_HINTS`, read in
    Patchright's isolated world); read once per process and mode (`_IDENTITY`) from a browser of that mode started for
    it alone, whose one navigation goes to a listener of this process (`_LOOPBACK`), so nothing is sent beyond the
    machine. Raises `HeadlessIdentity` when any of it still names headless mode."""
    if (known := _IDENTITY.get((_CHANNEL, headless))) is not None:
        return known
    sent: dict[str, str] = {}
    async with _listening(sent) as url:
        proxy = ProxySettings(server=_NOWHERE, bypass=_LOOPBACK)
        browser = await playwright.chromium.launch(channel=_CHANNEL, headless=headless, proxy=proxy)
        try:
            page = await browser.new_page()
            await page.goto(url)
            hints = cast(dict[str, object], await page.evaluate(_HINTS_JS))
        finally:
            await browser.close()
    headers = tuple(
        (name, value.replace(_HEADLESS_PRODUCT, _HEADED_PRODUCT, 1) if name.lower() == _USER_AGENT else value)
        for name, value in sent.items()
        if name.lower() in _NAMING_HEADERS
    )
    identity = Identity(headers, {_HINT_FIELDS.get(name, name): value for name, value in hints.items()})
    if _HEADLESS in json.dumps([identity.headers, identity.metadata]).lower():
        raise HeadlessIdentity("the browser rung's Chrome still names headless mode in its headers or client hints")
    _IDENTITY[_CHANNEL, headless] = identity
    return identity


@asynccontextmanager
async def _listening(sent: dict[str, str]) -> AsyncIterator[str]:
    """A listener on the loopback address that records the header fields of the first request it reads into `sent`
    (RFC 9112 §5, each name spelled as the browser sent it, which the HTTP rung sends it as over HTTP/1.1; names compare
    case-insensitively, RFC 9110 §5.1) and answers each request with an empty page; the URL to navigate to."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            head = await reader.readuntil(b"\r\n\r\n")
            if not sent:
                for line in head.decode("latin-1").split("\r\n")[1:]:
                    name, colon, value = line.partition(":")
                    if colon:
                        sent[name.strip()] = value.strip()
            writer.write(_EMPTY_PAGE)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, _LOOPBACK, 0)
    async with server:
        # Plain HTTP to the loopback address: the listener is this process, and a loopback origin is potentially
        # trustworthy (W3C Secure Contexts §3.2) without TLS.
        yield urlunsplit(("http", f"{_LOOPBACK}:{server.sockets[0].getsockname()[1]}", "/", "", ""))


class _ModeOptions(TypedDict, total=False):
    headless: Required[bool]
    args: list[str]


def _mode_options(session: BrowserSession, site: ContextKey) -> _ModeOptions:
    """The launch options of the session's mode (module docstring): headless, the browser's user agent
    (`_USER_AGENT_SWITCH`) and the screen of the site's profile (`acquire.desktop_screen`); headful, none of ours."""
    if not session.headless:
        return {"headless": False}
    screen = desktop_screen(profile_name(*site), session.identity.platform)
    return {"headless": True, "args": [_USER_AGENT_SWITCH + session.identity.agent, *screen.launch_args()]}


async def present(page: Page, session: BrowserSession) -> None:
    """Headless, set the session's identity on `page` before it navigates, then on each cross-site frame and popup it
    opens: Chrome's user agent override (the CDP Emulation domain's) with the browser's own client hints, which Chrome
    does not report itself under the `--user-agent` switch (`_USER_AGENT_SWITCH`). A dedicated worker takes its
    page's. A cross-site frame, which renders in a process of its own, and a popup take it once Playwright reports
    them, so their first requests carry the browser's default: the same user agent and brands, without the
    high-entropy client hints (a known gap). A service or shared worker takes it from `present_workers`. Headful, the
    browser presents its own and nothing is set."""
    if session.headless:
        await _present_tab(page, session.identity)


async def _present_tab(page: Page, identity: Identity) -> None:
    await _override(page, identity)
    page.on("framenavigated", lambda frame: _present_frame(frame, identity))
    page.on("popup", lambda popup: _present_popup(popup, identity))


async def present_workers(context: BrowserContext, session: BrowserSession) -> None:
    """Headless, set the session's identity on each service and shared worker `context`'s browser starts (`_WORKERS`),
    as `present` sets it on a tab: Chrome's user agent override, sent once Chrome reports the worker
    (`Target.targetCreated`). Chrome does not hold a worker for this session (Patchright's own session resumes it at
    once), so what a worker reads in its first milliseconds is the browser's default: the same user agent
    (`_USER_AGENT_SWITCH`) and brands, without the high-entropy client hints (a known gap). The override stays across
    the worker's restarts. Headful, nothing is set."""
    if not session.headless:
        return
    identity = session.identity
    browser = context.browser
    if browser is None:
        raise PlaywrightError("the context has no browser")
    cdp = await browser.new_browser_cdp_session()
    cdp.on("Target.targetCreated", lambda event: _present_worker(cdp, event, identity))
    await cdp.send("Target.setDiscoverTargets", {"discover": True, "filter": list(_WORKERS)})


async def _present_worker(session: CDPSession, event: Mapping[str, Mapping[str, str]], identity: Identity) -> None:
    # Patchright's Python CDP session sends only to its own target, never to a flattened child session; a target
    # attached without `flatten` is reached through `Target.sendMessageToTarget` (Chrome DevTools Protocol, Target).
    with contextlib.suppress(PlaywrightError):  # the worker or its browser is gone
        target = {"targetId": event["targetInfo"]["targetId"], "flatten": False}
        attached = await session.send("Target.attachToTarget", target)
        message = json.dumps({"id": 1, "method": _OVERRIDE, "params": _override_params(identity)})
        await session.send("Target.sendMessageToTarget", {"sessionId": attached["sessionId"], "message": message})


def _override_params(identity: Identity) -> dict[str, object]:
    return {"userAgent": identity.agent, "userAgentMetadata": dict(identity.metadata)}


async def _override(target: Page | Frame, identity: Identity) -> None:
    owner = target if isinstance(target, Page) else target.page
    session = await owner.context.new_cdp_session(target)
    await session.send(_OVERRIDE, _override_params(identity))


async def _present_frame(frame: Frame, identity: Identity) -> None:
    # The page's own frames share its session: Playwright refuses a session for one; a frame may also be gone.
    if frame.parent_frame is not None:
        with contextlib.suppress(PlaywrightError):
            await _override(frame, identity)


async def _present_popup(popup: Page, identity: Identity) -> None:
    with contextlib.suppress(PlaywrightError):  # the popup closed
        await _present_tab(popup, identity)


def context_locality(policy: FetchPolicy, session: BrowserSession, pool: ProxyPool | None) -> Locality:
    """The context's locality (`acquire.locality`) for the request's locale through the place the render goes out
    from: the pool's declared place through a pool, else the machine's; a pool
    that declares none is not the machine's."""
    return locality(policy.locale_hint, session.direct if pool is None else pool.place)


async def open_context(
    url: str,
    policy: FetchPolicy,
    session: BrowserSession,
    connector: SafeConnector,
    first: Target,
    proxy: ProxyEndpoint | None,
    scope: SessionScope | None,
    key: ContextKey | None,
    locale: Locality,
) -> OpenProfile:
    """The site profile of `key` (a temporary one without a key, `acquire.browser_profiles`), launched as a persistent
    context behind a guard of its own, in the session's mode (`_mode_options`: headless, on the screen of the page's
    site, pool and endpoint even in a temporary profile), holding the caller's cookies, or, in a temporary profile of
    the site, the cookies `scope` gives (`SiteSessions.for_browser`; a kept profile holds its own), with the `locale`
    and `timezone_id` of `locale` (`context_locality`). Nothing intercepts its requests: Playwright's request routing
    turns the HTTP cache off, so every request carries `Cache-Control: no-cache` and `Pragma: no-cache`, which no user's
    browser sends on a first load."""
    stack = AsyncExitStack()
    traffic = Traffic()
    site = key or _site_key(url, policy, proxy)
    try:
        profile = open_profile(session.profiles.root, None if key is None or _exit_bound(key) else profile_name(*key))
        stack.callback(profile.close)  # after the browser has closed, so its last writes are in the profile
        guard = await stack.enter_async_context(
            SocksGuard(connector, checked=(first,), upstream=proxy, traffic=traffic)
        )
        context = await session.playwright.chromium.launch_persistent_context(
            profile.directory,
            channel=_CHANNEL,
            no_viewport=True,
            proxy={"server": guard.url, "bypass": _PROXY_ALL_LOOPBACK},
            accept_downloads=False,
            **_mode_options(session, site),
            **locale,
        )
        held = OpenProfile(context, guard, connector, traffic, stack, locale, profile)
        stack.push_async_callback(context.close)
        context.on("close", lambda _: _closed(session, held))
        await present_workers(context, session)
        temporary = profile.lock is None and key is not None
        if temporary and scope is not None and (seeded := await scope.for_browser(url)):
            await context.add_cookies(seeded_cookies(seeded))
        if policy.context is not None and policy.context.cookies:
            await context.add_cookies(caller_cookies(url, policy.context))
    except BaseException as e:
        await stack.aclose()
        # A profile the browser could not open is this fetch's error, not a crashed browser.
        if isinstance(e, PlaywrightError | OSError):
            raise Failed("render_error", None) from e
        raise
    return held


def _closed(session: BrowserSession, held: OpenProfile) -> None:
    """A profile's browser closed: unasked, it crashed or was killed, and only that profile is dropped."""
    if not held.closing:
        session.profiles.forget(held)


def context_key(url: str, scope: SessionScope | None, proxy: ProxyEndpoint | None) -> ContextKey | None:
    """The profile a page renders in (`acquire.browser_profiles`): the page's site (HTML Standard §7.1.1) and the
    proxy pool and endpoint its guard goes through, a sticky exit's session id included (`core.proxy`). None for a
    fetch outside any site `scope` (`SiteSessions.scope_for`): it gets a temporary profile, never kept."""
    if scope is None or urlsplit(url).hostname is None:
        return None
    return (site_of(url), scope.pool, proxy)


def _site_key(url: str, policy: FetchPolicy, proxy: ProxyEndpoint | None) -> ContextKey:
    """The site, proxy pool and endpoint of a page: its profile's key, and the screen of a temporary profile."""
    return (site_of(url), policy.proxy_pool, proxy)


def caller_cookies(url: str, context: RequestContext) -> list[SetCookieParam]:
    """The caller's cookies as `BrowserContext.add_cookies` takes them: given with `url`, each is a host-only cookie
    of its host for every path (RFC 6265 §5.3 step 6, path `/` from the URL's root)."""
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}/"
    return [SetCookieParam(name=name, value=value.get_secret_value(), url=root) for name, value in context.cookies]


def seeded_cookies(cookies: Sequence[SessionCookie]) -> list[SetCookieParam]:
    """Session cookies as `BrowserContext.add_cookies` takes them, each with its own path: a host-only cookie with its
    host as the domain (RFC 6265 §5.3 step 6; as Playwright sets a cookie given with a URL), a `Domain` cookie with its
    domain led by a dot; a session cookie with no expiry."""
    params: list[SetCookieParam] = []
    for cookie in cookies:
        domain = cookie.domain if cookie.host_only else f".{cookie.domain}"
        param = SetCookieParam(
            name=cookie.name, value=cookie.value, domain=domain, path=cookie.path, secure=cookie.secure
        )
        if cookie.expires is not None:
            param["expires"] = cookie.expires
        params.append(param)
    return params


async def context_cookies(context: BrowserContext) -> tuple[SessionCookie, ...]:
    """Every cookie the context holds, read without changing any (nothing is set in the browser); none when
    the context is gone."""
    try:
        return browser_cookies(cast(list[Mapping[str, object]], await context.cookies()))
    except PlaywrightError:
        return ()
