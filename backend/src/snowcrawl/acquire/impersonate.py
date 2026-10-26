"""Which Chrome the HTTP and JSON rungs impersonate: the browser rung's own Google Chrome, so
a site that sees both rungs, and the cookies one hands the other, sees one browser on one platform.

At startup the browser rung's Chrome is started once, loading no site, to read the headers that name it on a navigation
(`browser_session.browser_identity`, in the browser rung's mode: its user agent as its profiles send it, its client
hints, `Accept` and `Accept-Language`). The rungs impersonate curl_cffi's Chrome profile nearest its major for the TLS
and HTTP/2 handshake. When that profile is the browser's major, they send the browser's headers in place of the
profile's own. When none is, with a warning, every header names the profile's major, as its handshake does: the
profile's own client hints, `Accept` and order, with the browser's platform (its user agent with the profile's major in
the product token, `Sec-CH-UA-Platform`) and its `Accept-Language`. A profile more majors behind than
`SNOWCRAWL_IMPERSONATE_LAG` warns at startup to upgrade curl_cffi. A request that carries cookies the browser rung
earned (`acquire.session`) sends the browser's own headers in either case: a clearance cookie may be bound to the user
agent that earned it. A target the operator sets (`SNOWCRAWL_IMPERSONATE`) is kept in place of the nearest profile: in a
process that runs the browser rung, with the browser's headers as above when it is a desktop Chrome profile (the
browser's own on earned cookies alone when it names another client), so the HTTP rung names the platform its renders
do; in a process without it, or when that Chrome cannot be read, with its own headers, starting no browser. Without an
operator's target, a process that runs the browser rung does not start when that Chrome cannot be read
(`BrowserUnread`): its HTTP rung would name another browser than its renders. A process without it (`snowcrawl fetch`
over HTTP) keeps the default target's own headers, with a warning.
"""

import logging
import re
from dataclasses import dataclass, replace
from typing import Final, cast, get_args

from curl_cffi.requests import BrowserTypeLiteral
from patchright.async_api import Error as PlaywrightError

from snowcrawl.acquire.browser_session import browser_identity, open_driver
from snowcrawl.core.settings import AcquireSettings
from snowcrawl.storage.capture_bundle import HeaderList

__all__ = ["BrowserUnread", "Impersonation", "lags", "nearest_target", "rung_impersonation"]

log = logging.getLogger(__name__)

# curl_cffi's desktop Chrome profiles are named `chrome<major>`; lettered and Android variants are other clients.
_CHROME_PROFILE: Final = re.compile(r"chrome(\d+)")
# The product token of Chrome's user agent (Chromium's reduced user agent: `Chrome/<major>.0.0.0`).
_CHROME_PRODUCT: Final = re.compile(r"\bChrome/(\d+)(?:\.\d+)*")
_USER_AGENT = "user-agent"
# The browser's headers that name its platform (the user agent's platform token, User-Agent Client Hints
# `Sec-CH-UA-Platform`) and its user's language (RFC 9110 §12.5.4), not its version.
_OWN: Final = frozenset({_USER_AGENT, "sec-ch-ua-platform", "accept-language"})
_IMPERSONATE = "impersonate"


class BrowserUnread(RuntimeError):
    """The browser rung's Chrome could not be read in a process that runs the browser rung (module docstring)."""


@dataclass(frozen=True)
class Impersonation:
    """The curl_cffi target whose TLS, HTTP/2 and header order the rungs send, the headers that replace the
    target's own of the same name in any case (each spelled as the browser sends it; none: the target's own), and
    those that replace them on a request that carries cookies the browser rung earned (none: `headers`)."""

    target: BrowserTypeLiteral
    headers: HeaderList = ()
    earned: HeaderList = ()

    def replaying(self, earned: bool) -> "Impersonation":
        """The impersonation of a request that carries cookies the browser rung earned, when `earned`."""
        return replace(self, headers=self.earned) if earned and self.earned else self


async def rung_impersonation(settings: AcquireSettings, *, browser_rung: bool) -> Impersonation:
    """The impersonation of the rungs: the browser rung's Chrome (module docstring). `browser_rung`: the process also
    renders in that browser. Raises `BrowserUnread` for such a process when its Chrome cannot be read."""
    configured = settings.impersonate
    explicit = _IMPERSONATE in settings.model_fields_set
    if explicit and not browser_rung:
        return Impersonation(configured)
    # An operator's target is the escape when the browser cannot be read: it starts with its own headers.
    required = browser_rung and not explicit
    try:
        headers = await _read_headers(settings.browser_headless)
    except (PlaywrightError, OSError) as e:  # the driver or the browser could not be started
        return _unread("did not start", configured, required, e)
    agent = next((value for name, value in headers if name.lower() == _USER_AGENT), "")
    major = _CHROME_PRODUCT.search(agent)
    if major is None:
        return _unread("names no Chrome in its user agent", configured, required)
    browser = int(major.group(1))
    return _following(configured if explicit else nearest_target(browser), headers, browser, settings.impersonate_lag)


def _following(target: BrowserTypeLiteral, headers: HeaderList, browser: int, lag: int) -> Impersonation:
    """`target` with the headers of the browser of major `browser` (module docstring); an operator's target that is no
    desktop Chrome profile names another client, and sends the browser's only with the cookies it earned."""
    profile = _major(target)
    if profile is None:
        return Impersonation(target, (), headers)
    if lags(browser, profile, lag):
        log.warning(
            "curl_cffi's Chrome profile %s is %d majors behind Chrome %d: upgrade curl_cffi",
            target,
            browser - profile,
            browser,
        )
    if profile == browser:
        return Impersonation(target, headers)
    log.warning("impersonating %s for Chrome %s: the headers name the profile's major", target, browser)
    return Impersonation(target, _naming(headers, profile), headers)


def lags(browser: int, profile: int, bound: int) -> bool:
    """Whether the profile of Chrome major `profile` trails the browser's major `browser` by more than `bound`: its
    handshake then names a Chrome older than every header a request replaying earned cookies sends (`replaying`)."""
    return browser - profile > bound


def _naming(headers: HeaderList, major: int) -> HeaderList:
    """The browser's headers that name its platform and its user, not its version (`_OWN`), the user agent's product
    token naming `major` as Chromium's reduced user agent does (`Chrome/<major>.0.0.0`)."""
    return tuple(
        (name, _CHROME_PRODUCT.sub(f"Chrome/{major}.0.0.0", value, count=1) if name.lower() == _USER_AGENT else value)
        for name, value in headers
        if name.lower() in _OWN
    )


def _unread(
    why: str, configured: BrowserTypeLiteral, browser_rung: bool, cause: Exception | None = None
) -> Impersonation:
    if browser_rung:
        raise BrowserUnread(
            f"the browser rung's Chrome {why}: install it (`patchright install chrome`) or set SNOWCRAWL_IMPERSONATE"
        ) from cause
    log.warning("the browser rung's Chrome %s: impersonating %s with its own headers", why, configured)
    return Impersonation(configured)


async def _read_headers(headless: bool) -> HeaderList:
    async with open_driver(headless) as playwright:
        return (await browser_identity(playwright, headless=headless)).headers


def nearest_target(major: int) -> BrowserTypeLiteral:
    """curl_cffi's desktop Chrome profile whose major is nearest `major`, the newer on a tie."""
    profiles = {major_: name for name in get_args(BrowserTypeLiteral) if (major_ := _major(name)) is not None}
    nearest = min(profiles, key=lambda candidate: (abs(candidate - major), -candidate))
    return cast(BrowserTypeLiteral, profiles[nearest])


def _major(target: str) -> int | None:
    match = _CHROME_PROFILE.fullmatch(target)
    return None if match is None else int(match.group(1))
