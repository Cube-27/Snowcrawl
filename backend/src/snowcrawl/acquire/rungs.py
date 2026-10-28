"""Rung selection: HTTP by default, the browser on a block, and what the site's memory says
(`acquire.memory`): the rung its pages of the kind start with; a browser start memory chose that is blocked or fails
asks HTTP once more. The fetch plan of the page's field sources
(`FetchPlan`) starts it in the browser when a located field source is the browser's
render (or the surface renders every page), and adds the companion calls of its `call` sources to an HTTP page. An
HTTP detail page of a platform that documents its product JSON is joined with that JSON, always (`_joined`).

A URL an active adapter detects goes to that adapter (`snowcrawl.adapters`) first, and to the page rungs when the
adapter does not serve it; a URL a custom recipe's pattern matches is served by that recipe first, before any adapter,
and goes on to the adapter when the recipe does not serve it. A fetched page a page-scoped adapter detects (a storefront
collection), at its final URL when a redirect kept it on its site, is joined with that adapter's answer for the rows the
page lists, a data call marked `ADAPTER_CALL`. A fetched page whose own canonical link is its URL without the query,
which an adapter detects, or which an adapter says stands for one of its platform's pages, is then served by that
adapter; an HTTP shell that would be rendered for lacking content asks it before the render. A site whose active config
has an API recipe is served by the recipe's rung, which the caller binds to the recipe: the config IR sits above
`acquire` (`.importlinter`). The browser rung is `snowcrawl.acquire.browser.fetch_browser`. Every rung is a function of
(url, policy), so `acquire` decides and records only. The one exception is the browser rung's exporting form, which also
hands the context's cookies to a site session (`acquire.session`).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Final, Protocol
from urllib.parse import urlsplit, urlunsplit

from snowcrawl.acquire.api import JsonFetcher, JsonRequest, site_relation
from snowcrawl.acquire.browser_tab import ListingRung
from snowcrawl.acquire.challenge import block_class_of
from snowcrawl.acquire.classify import carries_unrendered, interstitial, lacks_offer
from snowcrawl.acquire.fingerprint import platform_shown
from snowcrawl.acquire.identity import identified, joined, with_call
from snowcrawl.acquire.memory import Remembered
from snowcrawl.acquire.page_refs import canonical_link, page_refs
from snowcrawl.acquire.politeness import BACKOFF_MAX, SLOW_DOWN, Admit, backoff
from snowcrawl.acquire.session import CookieExport, SiteSessions
from snowcrawl.acquire.stored_fetch import policy_with_attempts
from snowcrawl.acquire.types import (
    AttemptRecord,
    BlockClass,
    CallEndpoint,
    FetchPolicy,
    FetchResult,
)
from snowcrawl.adapters.base import ADAPTER_CALL
from snowcrawl.adapters.platforms import ProductJson, product_json
from snowcrawl.adapters.registry import Registry, platform_adapters

Rung = Callable[[str, FetchPolicy], Awaitable[FetchResult]]
# A custom recipe's call sent from its site's warm page (`acquire.browser_send.send_in_page`), the page opened at the
# URL given third when the site's profile has none.
InPage = Callable[[JsonRequest, FetchPolicy, str], Awaitable[FetchResult]]
# The eager fragments a document at a URL declares, as (id, absolute URL) pairs (`content.fragments.eager_frames`,
# which sits above `acquire`, so the process hands it in).
FramePlan = Callable[[str, str], Sequence[tuple[str, str]]]

log = logging.getLogger(__name__)


# A fetch so far: the outcome kept, every attempt and their duration.
Fetched = tuple[FetchResult, tuple[AttemptRecord, ...], int]
# A companion call of a located `call` field source: the request its recipe makes for
# the kept HTTP page, bound from the page's URL and documents and the policy's request context; None when it binds
# nothing there (the field falls back to the generic path on that page). The config IR builds it: it sits above
# `acquire` (`.importlinter`).
CallPlan = Callable[[FetchResult, FetchPolicy], tuple[JsonRequest, tuple[str, ...]] | None]
# The `data_call` mark of a located field source's companion call.
LOCATED_CALL: Final = "located"


@dataclass(frozen=True)
class FetchPlan:
    """What the page's field sources need fetched, from the site's active config
    alone: the browser for a located `browser` field source (or a surface whose `render_mode` is `always`), the
    companion calls of its `call` sources, and their endpoints, which the browser's ready wait names."""

    browser: bool = False
    calls: tuple[CallPlan, ...] = ()
    endpoints: tuple[CallEndpoint, ...] = ()


# The plan of a page no config's field source asks anything of.
NO_PLAN: Final = FetchPlan()


class ExportingRung(Protocol):
    """The browser rung filling `export` with the context's cookies (`snowcrawl.acquire.browser.fetch_browser`)."""

    def __call__(self, url: str, policy: FetchPolicy, *, export: CookieExport) -> Awaitable[FetchResult]: ...


# A render takes seconds (`FetchPolicy.browser_timeout_s` at most), so none starts with less than this left of the
# task's budget; the HTTP outcome is kept instead.
BROWSER_MIN_S: Final = 20.0
_UNAVAILABLE = 503  # RFC 9110 §15.6.4
_TOO_MANY = 429  # RFC 6585 §4
# Why a browser retry the HTTP outcome called for was not sent (`_unsent`): the task's budget is spent, or the site's
# rate admits no further request within the caller's wait.
NO_BUDGET: Final = "no_budget"
NOT_ADMITTED: Final = "not_admitted"
# The HTTP outcomes the browser may pass where HTTP did not: a block, and a shell (a challenge's, or a script-only
# document's, rendered once per page).
_BROWSER_EVIDENCE: frozenset[BlockClass] = frozenset({"shell", "blocked"})
# The browser outcomes of a remembered browser start that HTTP is asked once more for (`_remembered_browser`).
_HTTP_AGAIN: frozenset[BlockClass] = frozenset({"blocked", "error"})
# The reason of a request a transport failure left unanswered (`curl_client.reason`), such as a reset
# HTTP/2 stream (curl 92) or connection. A filter may reset a client whose TLS or HTTP/2 fingerprint it does not trust;
# the browser's own network stack may pass. A timeout (`policy.timeout_s`) counts too: a filter may hold a
# client it does not trust unanswered, and the same transport again would wait as long.
_UNANSWERED: Final = ("network_error", "timeout")
# Turbo handbook (Frames): a frame's request asks for HTML and names the frame, so the server may answer it alone.
_FRAME_ACCEPT: Final = "text/html, application/xhtml+xml"
_FRAME_HEADER: Final = "Turbo-Frame"


@dataclass(frozen=True)
class Rungs:
    http: Rung
    browser: Rung
    adapters: Registry = field(default_factory=lambda: Registry(()))
    # JSON over HTTP (`acquire.api.fetch_json`), which an active config's API recipe sends its request with;
    # without it no recipe serves a page.
    json: JsonFetcher | None = None
    # The calls of a custom recipe sent by the browser (`sent_by = browser`); without it no such recipe serves a page.
    in_page: InPage | None = None
    # A listing with browser actions (`acquire.browser_tab.fetch_listing`); the API process sets it.
    listing: ListingRung | None = None
    # The site sessions of this process, which the HTTP and JSON rungs were bound to, and the browser rung's exporting
    # form that opens them; without both a browser retry opens none.
    sessions: SiteSessions | None = None
    exporting: ExportingRung | None = None
    # The eager fragments of an HTTP page, each fetched as a companion call (`_with_frames`); without it none is.
    frames: FramePlan | None = None


def adapter_registry(disabled: Collection[str]) -> Registry:
    """The registry of every platform adapter, all running but the `disabled` ones, for the processes
    that fetch; it raises `ValueError` for a name no adapter has. One per process: it remembers the hosts its adapters
    declined."""
    adapters = platform_adapters()
    names = {adapter.name for adapter in adapters}
    if unknown := sorted(set(disabled) - names):
        raise ValueError(f"no adapter named {unknown}")
    return Registry(adapter for adapter in adapters if adapter.name not in disabled)


async def acquire(
    url: str,
    policy: FetchPolicy,
    remembered: Remembered,
    rungs: Rungs,
    *,
    custom: Rung | None = None,
    recipe: Rung | None = None,
    admit_next: Admit | None = None,
    send: JsonFetcher | None = None,
    deadline: float | None = None,
    plan: FetchPlan = NO_PLAN,
) -> FetchResult:
    """Fetch `url` through `custom`, the custom recipe whose URL pattern it matches bound to its fetch, when there is
    one: its result is kept when it is `ok` or asks for fewer requests, or when `admit_next` admits no further
    request; otherwise the page is fetched as if there were none (`_ladder`), and the result lists the recipe's
    attempts first. A policy that captures the page uses no custom recipe."""
    first = None if custom is None or policy.captures_page else await custom(url, policy)
    if first is not None and (first.block_class == "ok" or slow_down(first) or not await _admitted(admit_next)):
        return first
    result = await _ladder(
        url, policy, remembered, rungs, recipe=recipe, admit_next=admit_next, send=send, deadline=deadline, plan=plan
    )
    if first is None:
        return result
    return with_attempts(result, first.attempts + result.attempts, first.duration_ms + result.duration_ms)


async def _ladder(
    url: str,
    policy: FetchPolicy,
    remembered: Remembered,
    rungs: Rungs,
    *,
    recipe: Rung | None,
    admit_next: Admit | None,
    send: JsonFetcher | None,
    deadline: float | None,
    plan: FetchPlan,
) -> FetchResult:
    """Fetch `url` through the active adapter that detects it, if any, requesting its API with `send` (no adapter runs
        without it); a page an adapter does not serve skips the recipe. Otherwise through `recipe`, the
        active config's API recipe bound to its fetch, when the site has one. The adapter's result is kept
        when it is `ok`, the recipe's when it is `ok` or asks for fewer requests; otherwise the page is fetched as if
        there
        were neither. Without either, fetch it with `policy.method_hint`, or else in the browser when the
        `plan` says so, or else the method the site's memory says (`remembered`). In `auto`, an HTTP block, shell or
        network
        error is fetched again in the browser, and the browser's outcome is kept only if it is `ok` (or a shell no
        challenge
        names: the last rung's page, `_as_last_page`); an HTTP `ok` page never is. Each request after the first (the
        page
        after an
        adapter or a recipe, the browser retry, the companion call) waits for `admit_next` to admit it at the site's
        rate,
    and is not sent when `admit_next` refuses it: the fetch keeps the outcome it has. The result
        lists every attempt, in its bundle's manifest too. A page no adapter detected whose canonical link is its URL
        without the query, which an adapter detects, is fetched again by that adapter on its own turn. An
        HTTP `ok` detail page kept is joined with its platform's documented product JSON (`_joined`, sent with `send`),
        and
        any kept HTTP `ok` page with the answers of its eager fragments on its own site (`_with_frames`), and with the
        answers of the `plan`'s companion calls (`_with_calls`, sent with `send`). A policy that captures the page
        (`screenshot`, `pdf`) uses neither an adapter, a recipe nor a companion call. No browser retry starts with less
        than
        `BROWSER_MIN_S` left before `deadline` (on `time.monotonic`), the task's budget: the HTTP outcome is kept."""
    if policy.captures_page:
        send = recipe = None  # a screenshot or PDF is of the page itself, which an adapter's or a recipe's JSON is not
    served = None if send is None else await rungs.adapters.serve(url, policy, send)
    # An adapter's endpoint is not the page: its slow-down holds back the page only by the pause it put on the host's
    # rate, which `admit_next` waits out. A recipe's may be the page's own (its HTML page request).
    by_recipe = served is None and recipe is not None
    if served is None and recipe is not None:
        served = await recipe(url, policy)
    if served is not None and (
        served.block_class == "ok" or (by_recipe and slow_down(served)) or not await _admitted(admit_next)
    ):
        return served
    ready, planned = _planned(policy, remembered, plan)
    # A page no adapter detected (none answered, or only the recipe did) may name a URL one does: asked before a render
    # for lacking content (`_page`), and of the kept page.
    refer = (
        None if send is None or not (served is None or by_recipe) else _Referral(url, policy, rungs, send, admit_next)
    )
    page = await _page(url, ready, planned, rungs, _Retry(admit_next, deadline, refer, remembered=not plan.browser))
    if refer is not None:
        page = await refer(page)
    page = await _joined(policy, page, admit_next, send)
    page = await _with_calls(policy, page, admit_next, send, plan.calls)
    kept, attempts, duration_ms = await _with_frames(policy, page, admit_next, send, rungs.frames)
    if served is not None:
        return with_attempts(kept, served.attempts + attempts, served.duration_ms + duration_ms)
    return with_attempts(kept, attempts, duration_ms)


@dataclass
class _Referral:
    """The adapter answers a fetched page asks for, each URL once per fetch (`asked`): the page asks
    before its render for lacking content (`_page`) and again once kept, and the second ask of one URL would take a
    turn at the site's rate for an answer already had."""

    url: str
    policy: FetchPolicy
    rungs: Rungs
    send: JsonFetcher
    admit_next: Admit | None
    asked: set[str] = field(default_factory=set)

    async def __call__(self, page: Fetched) -> Fetched:
        """The page joined with a page-scoped adapter's answer for the rows it lists (`_listed`); else the adapter's
        answer for the page's canonical URL (`_query_dropped`), or for the platform page the page on its own host
        stands for (`_referred`), when there is one not asked yet, it is `ok` and the site's rate admits the request;
        else the page. Either way with every attempt."""
        kept, attempts, duration_ms = page
        adapters = self.rungs.adapters
        listed = _listed(self.url, kept, adapters)
        canonical = listed or _query_dropped(self.url, kept, adapters) or _referred(kept, self.policy, adapters)
        if canonical is None or canonical in self.asked or not await _admitted(self.admit_next):
            return page
        self.asked.add(canonical)
        served = await adapters.serve(canonical, self.policy, self.send, page=lambda: page_refs(kept))
        if served is None:
            return page
        if served.block_class == "ok" and listed is not None:
            # The page stays the capture. The answer holds only products the page links to (the adapter's identity
            # check: an answer stating none is `not_served`), and joins the page as a data call.
            kept = with_call(kept, served, served.bundle.manifest.final_url, ADAPTER_CALL, method="POST")
        elif served.block_class == "ok":
            kept = served
        return kept, attempts + served.attempts, duration_ms + served.duration_ms


def _referable(page: FetchResult) -> bool:
    """Whether the page states what it stands for: an `ok` page, or a `shell` no challenge names (a script-only
    document): its redirect, canonical link and `og:url` are the server's own, though it lacks the
    content. A challenge's document and a refusal state nothing of the page."""
    return page.block_class == "ok" or (page.block_class == "shell" and page.attempts[-1].challenge is None)


def _listed(url: str, page: FetchResult, adapters: Registry) -> str | None:
    """The URL of the `ok` page a page-scoped adapter, which serves a fetched page, detects: its final URL, whose links
    the page's references resolve against, when a redirect kept it on the request's site (HTML Standard §7.1.1 "same
    site": an apex host and its `www` host); None for a page that left its site or that no such adapter detects."""
    if page.block_class != "ok":
        return None
    final = page.bundle.manifest.final_url
    if site_relation(url, final) == "cross-site":
        return None
    return final if adapters.would_serve(final, fetched=True) and not adapters.would_serve(final) else None


def _query_dropped(url: str, page: FetchResult, adapters: Registry) -> str | None:
    """`url` without its query, when an adapter detects that URL and the page names it as its canonical (RFC 6596; HTML
    Standard §4.6.7.3, `link rel=canonical`): the page says the parameters it drops do not identify it. No list of
    parameter names decides it (C4). None otherwise."""
    parts = urlsplit(url)
    if not parts.query or not _referable(page):
        return None
    bare = urlunsplit(parts._replace(query="", fragment=""))
    # A host its adapter declined is not asked again, so the request would take a turn at the site's rate for nothing.
    if not adapters.would_serve(bare) or (canonical := canonical_link(page)) is None:
        return None
    return bare if _same_resource(canonical, bare) else None


def _referred(page: FetchResult, policy: FetchPolicy, adapters: Registry) -> str | None:
    """The platform page an active adapter says the fetched page stands for, from what the page states (its redirect,
    canonical link, `og:url`, links): a site on a tenant's own host that the platform serves."""
    if not _referable(page):
        return None
    return adapters.referred(lambda: page_refs(page), policy.page_kind, policy.target_types)


def _same_resource(a: str, b: str) -> bool:
    """Equal scheme, host (both case-insensitive, RFC 3986 §6.2.2.1), port, path and query; the fragment names a part
    of the resource, not another one."""
    x, y = urlsplit(a), urlsplit(b)
    return (x.scheme.lower(), x.hostname, x.port, x.path, x.query) == (
        y.scheme.lower(),
        y.hostname,
        y.port,
        y.path,
        y.query,
    )


@dataclass(frozen=True)
class _Retry:
    """What decides the page rung's requests (`acquire`): the admission of a further request, the deadline no browser
    retry starts too close to, and the referral a page asks before its render (`refer`)."""

    admit_next: Admit | None
    deadline: float | None
    refer: _Referral | None = None
    # Whether a browser start is memory's (`_remembered_browser`), not a field source's (`FetchPlan.browser`).
    remembered: bool = False

    def in_budget(self) -> bool:
        return self.deadline is None or self.deadline - time.monotonic() >= BROWSER_MIN_S


async def _page(url: str, policy: FetchPolicy, remembered: Remembered, rungs: Rungs, retry: _Retry) -> Fetched:
    """The page over HTTP or in the browser (`acquire`): the outcome kept, every attempt and their duration. An HTTP
    page kept for want of a browser outcome, and the browser's page, are the fetch's last page (`_as_last_page`); an
    HTTP page kept after the browser's attempt failed is not the last rung's. An HTTP shell the browser
    would retry for lacking content (`_referable`) asks its referral first (`retry.refer`), and an `ok` answer is kept
    without the render."""
    method = remembered.method if policy.method_hint == "auto" else policy.method_hint
    if method == "browser" and policy.method_hint == "auto" and retry.remembered:
        return await _remembered_browser(url, policy, rungs, retry)
    if method == "http":
        result = await rungs.http(url, policy)
    else:
        result = await rungs.browser(url, policy)
    retried = policy.method_hint == "auto" and method == "http" and await _browser_may_help(result)
    attempts, duration_ms = result.attempts, result.duration_ms
    if retried and retry.refer is not None and result.block_class != "ok" and _referable(result):
        referred, attempts, duration_ms = await retry.refer((result, attempts, duration_ms))
        if referred is not result:
            return referred, attempts, duration_ms
    if not retried:
        return await _as_last_page(result, policy, last=True), attempts, duration_ms
    if (unsent := await _unsent(retry, _paused_by(result))) is not None:
        log.info("browser retry not sent: %s (http %s)", unsent, result.bundle.manifest.status_code)
        return await _as_last_page(result, policy, last=True), attempts, duration_ms
    rendered = await _browser_retry(url, policy, rungs, result)
    return await _kept(result, rendered, policy), attempts + rendered.attempts, duration_ms + rendered.duration_ms


async def _unsent(retry: _Retry, pause: timedelta) -> str | None:
    """Why the browser retry is not sent (`NO_BUDGET`, `NOT_ADMITTED`), or None once the site's rate admitted it after
    `pause`. The budget is checked again once admitted: the wait for the site's turn may have spent it."""
    if not retry.in_budget():
        return NO_BUDGET
    if not await _admitted(retry.admit_next, after=pause):
        return NOT_ADMITTED
    return None if retry.in_budget() else NO_BUDGET


async def _kept(result: FetchResult, rendered: FetchResult, policy: FetchPolicy) -> FetchResult:
    """The page kept after the browser's attempt `rendered` at the HTTP `result`: the browser's `ok` unless it is no
    better (`_no_better`), else the HTTP page when it is `ok` as a page kept with no browser outcome (`_as_last_page`,
    not the last rung's), else the browser's page when the last rung's rule makes it `ok`, else the HTTP page."""
    if rendered.block_class == "ok" and not await asyncio.to_thread(_no_better, result, rendered, policy):
        return rendered
    http = await _as_last_page(result, policy, last=False)
    if http.block_class == "ok":
        return http
    shown = await _as_last_page(rendered, policy, last=True)
    return shown if shown.block_class == "ok" else http


def _no_better(result: FetchResult, rendered: FetchResult, policy: FetchPolicy) -> bool:
    """Whether the render of an HTTP detail page that was a shell only for stating no offer (`classify.lacks_offer`)
    states none either: the HTTP page is kept, and the fetch teaches no browser start
    (`memory.lesson`). Parses both documents: CPU work."""
    if policy.page_kind != "detail" or result.block_class != "shell" or result.method != "http":
        return False
    http = result.bundle.text
    only_offer = lacks_offer(http, policy.target_types) and not carries_unrendered(http)
    return only_offer and lacks_offer(rendered.bundle.text, policy.target_types)


async def _joined(policy: FetchPolicy, page: Fetched, admit_next: Admit | None, send: JsonFetcher | None) -> Fetched:
    """The kept HTTP `ok` page fetched as a detail page, joined with the product JSON its platform documents
    (`adapters.platforms`), always, when the answer states the page's
    product (`acquire.identity`). The call waits for the site's rate (`admit_next`) and its attempt is added; without a
    call, or one not admitted, the page as it is."""
    result, attempts, duration_ms = page
    if send is None or policy.page_kind != "detail" or result.method != "http" or result.block_class != "ok":
        return page
    called = await asyncio.to_thread(_documented, result)  # scans the page's markup: CPU work
    if called is None or not await _admitted(admit_next):
        return page
    # The page that loaded the product, after its redirects, is the call's referrer.
    sent = await send(JsonRequest(url=called.url, page_url=result.bundle.manifest.final_url), policy)
    marked = sent.model_copy(
        update={"attempts": tuple(a.model_copy(update={"data_call": called.platform}) for a in sent.attempts)}
    )
    kept, call = joined(result, marked, called.key, called.platform)
    return (kept or result), attempts + call.attempts, duration_ms + call.duration_ms


def _planned(policy: FetchPolicy, remembered: Remembered, plan: FetchPlan) -> tuple[FetchPolicy, Remembered]:
    """The policy and memory the page is fetched with under `plan`: the browser first when a field source needs it, and
    a ready wait that names the endpoints of the plan's calls."""
    ready = policy.model_copy(update={"ready_calls": plan.endpoints}) if plan.endpoints else policy
    return ready, Remembered("browser") if plan.browser else remembered


async def _with_calls(
    policy: FetchPolicy, page: Fetched, admit_next: Admit | None, send: JsonFetcher | None, calls: Sequence[CallPlan]
) -> Fetched:
    """The kept HTTP `ok` page with the answers of its located companion calls (`FetchPlan.calls`), each sent with
    `send` (the SSRF-guarded JSON rung, polite to the endpoint's host) once the site's rate admits it (`admit_next`).
    Every attempt is added, marked `LOCATED_CALL`; an `ok` answer joins the capture as a network body at the call's
    URL, which the field's `xhr` step reads, once it states the page's product (`identified`, with the values the
    request was bound with). A call that binds nothing on the page is not sent."""
    result, attempts, duration_ms = page
    if not calls or send is None or result.method != "http" or result.block_class != "ok":
        return page
    for call in calls:
        bound = await asyncio.to_thread(call, result, policy)  # binding parses the page: CPU work
        if bound is None:
            continue
        if not await _admitted(admit_next):
            break
        request, keys = bound
        sent = identified(await send(request, policy), result.bundle.manifest.url, keys)
        marked = tuple(a.model_copy(update={"data_call": LOCATED_CALL}) for a in sent.attempts)
        attempts, duration_ms = attempts + marked, duration_ms + sent.duration_ms
        if sent.block_class == "ok":
            result = with_call(result, sent, request.url, method=request.method, request_body=request.body)
    return result, attempts, duration_ms


async def _with_frames(
    policy: FetchPolicy, page: Fetched, admit_next: Admit | None, send: JsonFetcher | None, plan: FramePlan | None
) -> Fetched:
    """The kept HTTP `ok` page with the answers of its eager Turbo frames (`content.fragments`), each a companion GET
    of the frame's `src` on the page's own site (Fetch Metadata §2.1: same-origin or same-site), sent as the frame's
    own request would be, through the SSRF-guarded JSON rung (`send`, raw), and admitted at the site's rate
    (`admit_next`) like any further request. Every attempt is added; an `ok` answer joins the capture as a network body
    at the frame's URL, which the page's parse includes (`content.page.prepassed`)."""
    result, attempts, duration_ms = page
    if plan is None or send is None or result.method != "http" or result.block_class != "ok":
        return page
    page_url = result.bundle.manifest.final_url
    frames = await asyncio.to_thread(plan, result.bundle.text, page_url)  # parses the page: CPU work
    for frame_id, src in frames:
        if site_relation(page_url, src) == "cross-site":
            continue
        if not await _admitted(admit_next):
            break
        headers = (("Accept", _FRAME_ACCEPT), (_FRAME_HEADER, frame_id))
        sent = await send(JsonRequest(url=src, page_url=page_url, headers=headers, raw=True), policy)
        attempts, duration_ms = attempts + sent.attempts, duration_ms + sent.duration_ms
        if sent.block_class == "ok":
            result = with_call(result, sent, src)
    return result, attempts, duration_ms


def _documented(result: FetchResult) -> ProductJson | None:
    """The documented product JSON request of the page's platform (`adapters.platforms.product_json`), or None."""
    return product_json(platform_shown(result), result.bundle.manifest.final_url, result.bundle.text)


async def _browser_retry(url: str, policy: FetchPolicy, rungs: Rungs, result: FetchResult) -> FetchResult:
    """The browser attempt after the HTTP `result`. When HTTP was refused (`shell` or `blocked`) and the process keeps
    site sessions and the fetch is in their scope (`SiteSessions.scope_for`), the browser's `ok` opens the site's
    session with the context's cookies, and its attempt says so (`site_session`)."""
    scope = None if rungs.sessions is None else rungs.sessions.scope_for(policy)
    exporting = rungs.exporting
    if scope is None or exporting is None or result.block_class not in _BROWSER_EVIDENCE:
        return await rungs.browser(url, policy)
    export = CookieExport()
    retry = await exporting(url, policy, export=export)
    if retry.block_class != "ok" or scope.open(url, export) is None:
        return retry
    opened = tuple(a.model_copy(update={"site_session": True}) for a in retry.attempts)
    return retry.model_copy(update={"attempts": opened})


async def _browser_may_help(result: FetchResult) -> bool:
    """Whether the HTTP outcome is one the browser retries: a block, a shell, an unanswered request, a 429, or a 503
    interstitial."""
    return _browser_may_pass(result) or _too_many_requests(result) or await _interstitial_unavailable(result)


def _too_many_requests(result: FetchResult) -> bool:
    """Owner decision 2026-10-23: an HTTP 429 (RFC 6585 §4) is answered once in the browser, after the pause the site
    asked for (`_paused_by`): a filter may rate a client by its TLS or HTTP/2 fingerprint, which the browser's own
    network stack does not share. The task does not retry it after that (`worker.handlers.steps`)."""
    manifest = result.bundle.manifest
    return manifest.method == "http" and manifest.status_code == _TOO_MANY


async def _interstitial_unavailable(result: FetchResult) -> bool:
    """A 503 (RFC 9110 §15.6.4) whose document stands in for the page until its script or refresh navigates
    on (`classify.interstitial`): the browser may pass it, once the site's pause is over (`_paused_by`)."""
    manifest = result.bundle.manifest
    if manifest.status_code != _UNAVAILABLE or manifest.method != "http":
        return False
    text = result.bundle.text
    return await asyncio.to_thread(
        interstitial, manifest.status_code, manifest.headers.response, text, manifest.final_url
    )


async def _remembered_browser(url: str, policy: FetchPolicy, rungs: Rungs, retry: _Retry) -> Fetched:
    """The page of a fetch memory starts in the browser (`acquire.memory`): a browser outcome `blocked` or `error`
    (`_HTTP_AGAIN`) is fetched once more over HTTP when the site's rate admits it, and an HTTP `ok` is kept. Memory is
    advisory, and a failed render is no evidence that HTTP still fails: a kept HTTP `ok` teaches HTTP again. A
    browser outcome asking for fewer requests (RFC 6585 §4, RFC 9110 §15.6.4) sends none. Otherwise the browser's
    page, the last rung's (`_as_last_page`)."""
    rendered = await rungs.browser(url, policy)
    again = rendered.block_class in _HTTP_AGAIN and not slow_down(rendered)
    if not again or not await _admitted(retry.admit_next):
        return await _as_last_page(rendered, policy, last=True), rendered.attempts, rendered.duration_ms
    fetched = await rungs.http(url, policy)
    attempts, duration_ms = rendered.attempts + fetched.attempts, rendered.duration_ms + fetched.duration_ms
    kept = fetched if fetched.block_class == "ok" else await _as_last_page(rendered, policy, last=True)
    return kept, attempts, duration_ms


def _paused_by(result: FetchResult) -> timedelta:
    """The pause a slow-down `result` asks for before the browser attempt (`politeness.backoff`); none for another
    result. A pause past the longest backoff is longer than any caller waits in place, so the attempt is not sent."""
    if not slow_down(result):
        return timedelta(0)
    pause = backoff(result.attempts[-1])
    return BACKOFF_MAX if pause is None else pause


async def _as_last_page(result: FetchResult, policy: FetchPolicy, *, last: bool) -> FetchResult:
    """A page kept for want of an `ok` outcome is extracted whatever its links, as the browser's is
    (`browser.classified`). When it is the `last` rung's page (the browser's, or HTTP's when no browser attempt is
    made), a `shell` no challenge names (`_referable`: a 2xx document with no vendor's signature on its attempt) is
    `ok`: the server gave the page and it shows what it shows (a page announcing a launch, a
    parked domain), so its text is published as it is. Any other HTTP shell kept is `ok` when it is a shell by the
    listing rule alone (a page with site structure but no repeated items, `classify`), by the content rule alone (a page
    whose text a script carries, read for content formats) or by the detail rule alone (a page whose structured data
    describes the item of a `target_types` type while a script carries the rest), so the rows role C reads from it, the
    text it shows, or the item its data states are kept; one showing a vendor's stand-in marker (`block_class_of`) or
    as little text as a shell stays as it is. Its attempt keeps the class its rung gave it."""
    if result.block_class != "shell":
        return result
    if last and _referable(result):
        return _as_ok(result)
    manifest = result.bundle.manifest
    if manifest.method != "http":
        return result
    text = result.bundle.text
    unlisted = await asyncio.to_thread(
        partial(block_class_of, types=policy.target_types), manifest.status_code, manifest.headers.response, text
    )
    return _as_ok(result) if unlisted == "ok" else result


def _as_ok(result: FetchResult) -> FetchResult:
    manifest = result.bundle.manifest
    bundle = result.bundle.model_copy(update={"manifest": manifest.model_copy(update={"block_class": "ok"})})
    return result.model_copy(update={"bundle": bundle})


async def _admitted(admit_next: Admit | None, *, after: timedelta = timedelta(0)) -> bool:
    """Whether a further request of the fetch may go: `admit_next` waits for its turn, after the pause `after` the site
    asked for, or refuses one too far off. Without `admit_next` nothing is waited for (a caller that keeps no rate)."""
    if admit_next is None:
        return True
    return await (admit_next(after=after) if after else admit_next())


def _browser_may_pass(result: FetchResult) -> bool:
    return (result.block_class in _BROWSER_EVIDENCE and not slow_down(result)) or _unanswered(result)


def _unanswered(result: FetchResult) -> bool:
    error = result.attempts[-1].error
    return result.block_class == "error" and error is not None and error.startswith(_UNANSWERED)


def slow_down(result: FetchResult) -> bool:
    # A 429 or 503 asks for fewer requests (RFC 6585 §4, RFC 9110 §15.6.4): another rung would be one more, inside
    # the backoff the politeness step is about to apply; only the browser's one attempt waits that pause out first
    # (`_too_many_requests`, `_interstitial_unavailable`).
    return result.bundle.manifest.status_code in SLOW_DOWN


def with_attempts(result: FetchResult, attempts: tuple[AttemptRecord, ...], duration_ms: int) -> FetchResult:
    manifest = result.bundle.manifest
    fetch_policy = policy_with_attempts(manifest.fetch_policy, attempts)
    bundle = result.bundle.model_copy(update={"manifest": manifest.model_copy(update={"fetch_policy": fetch_policy})})
    return result.model_copy(update={"bundle": bundle, "duration_ms": duration_ms, "attempts": attempts})
