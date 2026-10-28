"""What the services that fetch while their caller waits (`scrape`, `map`, `extract`) and the worker's handlers fetch
with: the rungs and guard of this process, the SSRF guard's refusals, and the page fetch they send through
the one polite fetch (`services.polite`): the page through `acquire`, its adapter's and active
config's API recipe's requests sent as politely as a page's, as a learning run's recipe replays
are, and the plain HTTP fetches of a site's sitemaps. A custom recipe's requests also keep its own least interval
(`RecipePaces`).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from urllib.parse import urlsplit

from sqlalchemy import cast, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.api import JsonFetcher, JsonRequest
from snowcrawl.acquire.browser_tab import Tab
from snowcrawl.acquire.cookies import shared
from snowcrawl.acquire.politeness import Admit, HostLimiter
from snowcrawl.acquire.rungs import NO_PLAN, FetchPlan, Offered, Rung, Rungs, acquire
from snowcrawl.acquire.ssrf import SSRF_REJECTED, SafeConnector, SsrfRejected
from snowcrawl.acquire.types import (
    ActionsReport,
    AttemptRecord,
    BrowserActions,
    FetchPolicy,
    FetchResult,
    PagedFetch,
)
from snowcrawl.config_ir.api_recipe.execute import MAX_PAGES, Custom, RecipeRung, unserved
from snowcrawl.config_ir.api_recipe.model import ApiRecipe
from snowcrawl.config_ir.api_recipe.replay import Replay, Replays
from snowcrawl.core.egress import Egresses
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.core.errors import Refusal
from snowcrawl.crawl.paging import collect
from snowcrawl.crawl.sitemaps import MAX_SITEMAP_BYTES
from snowcrawl.db.models import JobId, Site
from snowcrawl.services.page_store import SiteRow, record_fetch_usage
from snowcrawl.services.polite import (
    IN_PLACE,
    SLOW_DOWN,
    HostRules,
    Later,
    PageFetch,
    ReadRules,
    Refused,
    Turns,
    Waiting,
    page_first,
    polite_fetch,
    rules_of,
)
from snowcrawl.storage.base import ObjectStore


@dataclass
class RecipePaces:
    """The turns of this process's custom recipes at their own least interval (`Custom.rate_ms`), beside the host's
    rate, which each request still waits for (`polite_fetch`): a recipe's requests are never closer than either."""

    clock: Callable[[], float] = time.monotonic
    _next: dict[str, float] = field(default_factory=dict)

    async def wait(self, recipe: str, rate_ms: int, bound: timedelta) -> bool:
        """Wait for the recipe's next turn and take it; False, taking none, when it is further off than `bound`."""
        now = self.clock()
        at = max(now, self._next.get(recipe, now))
        if at - now > bound.total_seconds():
            return False
        self._next[recipe] = at + rate_ms / 1000  # taken before the wait: a concurrent request waits for the next
        await asyncio.sleep(at - now)
        return True


# A custom recipe's turn at its own interval (`RecipePaces.wait`, bound to the recipe): False when it is too far off.
Pace = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class Fetching:
    """What a service fetches with: the rungs, the SSRF guard they connect through, the object store captures go to,
    the per-host limiter of this process and its custom recipes' turns."""

    rungs: Rungs
    connector: SafeConnector
    store: ObjectStore
    limiter: HostLimiter
    # The configured proxy pools (`SNOWCRAWL_PROXY_POOLS`), which a request's `fetch.proxy_pool` is checked against,
    # and the countries fetches go out from (`core.egress`).
    egresses: Egresses = field(default_factory=Egresses)
    paces: RecipePaces = field(default_factory=RecipePaces)


class TargetRejected(Refusal):
    """The SSRF guard refused the target, or a redirect from it. `reason` is the guard's, without the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TargetsRejected(Refusal):
    """The SSRF guard refused a target a request names: `member` names it (`urls[3]`, `webhook_url`), `reason`
    is the guard's, without the URL."""

    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"{member}: {reason}")
        self.member = member
        self.reason = reason


async def check_targets(
    connector: SafeConnector,
    targets: list[tuple[str, str]],
    *,
    webhook_url: str | None = None,
    queued: Collection[str] = (),
) -> None:
    """Check each of `targets`, (member, URL) pairs, and the request's `webhook_url` with the guard, before anything
    is fetched or stored. Raises `TargetsRejected` for the first refused one in order. The members in `queued`, URLs
    a task of their own fetches, are checked by their form alone (`SafeConnector.check_form`): a refusal only
    resolving the host tells (`dns`, a `private_address` it resolves to) fails that task when it fetches the URL."""
    if webhook_url is not None:
        targets = [*targets, ("webhook_url", webhook_url)]
    checks = (_checked(connector, url, form_only=member in queued) for member, url in targets)
    checked = await asyncio.gather(*checks, return_exceptions=True)
    for (member, _), result in zip(targets, checked, strict=True):
        if isinstance(result, SsrfRejected):
            raise TargetsRejected(member, str(result))
        if isinstance(result, BaseException):
            raise result


async def _checked(connector: SafeConnector, url: str, *, form_only: bool) -> None:
    if form_only:
        connector.check_form(url)
    else:
        await connector.check(url)


async def check_target(connector: SafeConnector, url: str) -> None:
    """Raise `TargetRejected` when the guard refuses `url`, before anything is fetched."""
    try:
        await connector.check(url)
    except SsrfRejected as e:
        raise TargetRejected(str(e)) from e


def refusal(result: FetchResult) -> str | None:
    """The guard's refusal of a request of the fetch, which a rung records as the attempt's error rather than raising;
    `None` when it refused none."""
    return next((a.error for a in result.attempts if a.error is not None and a.error.startswith(SSRF_REJECTED)), None)


def site_rungs(fetching: Fetching, host: str, site: SiteRow) -> Rungs:
    """This process's rungs with an adapter registry knowing the declines stored on the site of `host`."""
    return replace(fetching.rungs, adapters=fetching.rungs.adapters.for_host(host, site.adapter_declined))


async def store_declines(engine: AsyncEngine, site: SiteRow, host: str, rungs: Rungs) -> None:
    """Store on the site of `host`, in a transaction of its own, the adapter declines `rungs` made for it
    (`site_rungs`): those that differ from what its row held (the fallback to the page rungs).
    They are merged into the stored ones in one statement (a JSONB concatenation), so fetches that read the row before
    either stored keep each other's declines. One that ran out stays stored, and holds back nothing (`Registry.for_host`
    reads its time): the row holds at most one per adapter."""
    made = {
        name: until
        for name, until in rungs.adapters.declined_for(host).items()
        if site.adapter_declined.get(name) != until
    }
    if made:
        merged = Site.adapter_declined.op("||")(cast(made, JSONB))
        async with engine.begin() as conn:
            await conn.execute(update(Site).where(Site.id == site.id).values(adapter_declined=merged))


@dataclass
class _Turn:
    """The host whose turn at its rate the caller was admitted for, until a request takes it."""

    host: str | None

    def take(self, host: str) -> bool:
        taken = host == self.host
        if taken:
            self.host = None
        return taken


def recipe_rung(
    fetching: Fetching,
    recipe: ApiRecipe,
    page_host: str,
    rules: ReadRules,
    waiting: Waiting = IN_PLACE,
    custom: Custom | None = None,
) -> RecipeRung | None:
    """`recipe` bound to this process's rungs, for a page on `page_host` whose robots.txt the caller
        has checked and whose turn at the host's rate it has taken. That turn goes to the first request to the host: the
        HTML page the recipe fetches to bind, or else its endpoint. An endpoint is requested only where its host's
        robots.txt (`rules`) allows it, at that host's rate, waiting in place for a turn of its own when it needs one; a
        request robots.txt keeps from being sent leaves the page unserved, and `acquire` fetches it the configured way.
        A custom recipe's (`custom`) calls also wait for its own interval (`RecipePaces`); one sent by the browser
        (`sent_by`) sends them from its site's warm page (the rungs' `in_page`), at the same rates, and fetches the
        HTML page over HTTP.
    None without the rung that sends its calls."""
    json = _calls(fetching.rungs, custom)
    if json is None:
        return None
    turn = _Turn(page_host)
    http = fetching.rungs.http
    rules = _unshared(rules)

    async def fetch_page(request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        return await http(request.url, policy)

    pace = None
    if custom is not None and custom.rate_ms is not None:
        pace = partial(fetching.paces.wait, custom.recipe_id, custom.rate_ms, waiting.bound)
    # The page goes on the caller's turn unless a warm-up request to its host took it; then at the host's rate.
    via_page = _polite_send(fetching, fetch_page, rules, turn, waiting, pace)
    send = _polite_send(fetching, json, rules, turn, waiting, pace)

    async def page(url: str, policy: FetchPolicy) -> FetchResult:
        return await via_page(JsonRequest(url, url), policy)

    return RecipeRung(recipe, send, page, ENGINE_VERSION, custom)


def _calls(rungs: Rungs, custom: Custom | None) -> JsonFetcher | None:
    """What sends a recipe's calls: the JSON rung, or the warm page of the site's profile for a custom recipe sent by
    the browser, opened at its warm URL (`Custom.warm_url`)."""
    if custom is None or custom.sent_by == "http":
        return rungs.json
    in_page = rungs.in_page
    if in_page is None:
        return None
    warm_url = custom.warm_url

    async def send(request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        return await in_page(request, policy, warm_url(request.page_url))

    return send


def _unshared(rules: ReadRules) -> ReadRules:
    """`rules` reading robots.txt with a jar of its own: a recipe's shared jar (`shared`) holds its own calls' cookies
    only, and robots.txt is no call of the recipe (RFC 9309 §2.3 asks it as a plain resource)."""

    async def read(url: str, policy: FetchPolicy) -> HostRules | None:
        with shared(None):
            return await rules(url, policy)

    return read


def adapter_send(
    fetching: Fetching, page_host: str, rules: ReadRules, waiting: Waiting = IN_PLACE
) -> JsonFetcher | None:
    """The JSON fetch an adapter requests its API with, for a page on `page_host` whose robots.txt the
    caller has checked and whose turn at the host's rate it has taken: an endpoint is requested only where its host's
    robots.txt (`rules`) allows it, at that host's rate; the first request to the page's host takes the caller's turn,
    as a recipe's does. A request robots.txt keeps from being sent leaves the page to the page rungs. None without the
    JSON rung."""
    json = fetching.rungs.json
    if json is None:
        return None
    return _polite_send(fetching, json, rules, _Turn(page_host), waiting)


def _polite_send(
    fetching: Fetching,
    json: JsonFetcher,
    rules: ReadRules,
    turn: _Turn,
    waiting: Waiting = IN_PLACE,
    pace: Pace | None = None,
) -> JsonFetcher:
    """Each request sent through the polite fetch to its endpoint's host (`polite_fetch`), the first request to the
    page's host on the caller's turn (`turn`), and then on a custom recipe's turn at its own interval (`pace`), taken
    once the host admitted it, so requests go in the order their turns were taken; one robots.txt keeps from being
    sent, or whose turn is further off than the caller waits, answers `blocked` with the reason and sends nothing."""

    async def send(request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        async def fetch(_url: str, _admit: Admit, _known: HostRules) -> FetchResult:
            if pace is not None and not await pace():
                return unserved(request.page_url, policy, ENGINE_VERSION, "blocked", SLOW_DOWN)
            return await json(request, policy)

        host = urlsplit(request.url).hostname or ""
        result = await polite_fetch(
            request.url,
            policy,
            rules,
            fetching.limiter,
            replace(waiting, postpone=False),
            fetch,
            turn_taken=turn.take(host),
            endpoint=True,
        )
        if isinstance(result, FetchResult):
            return result
        # A request not sent is the recipe's or adapter's unserved page, as a page robots.txt keeps out is `blocked`.
        reason = result.reason if isinstance(result, Refused) else SLOW_DOWN
        return unserved(request.page_url, policy, ENGINE_VERSION, "blocked", reason)

    return send


def learning_replay(engine: AsyncEngine, fetching: Fetching) -> Replays | None:
    """A learning run's recipe replays over this process's JSON rung, as politely as a served
    recipe's request: at the endpoint host's rate. A `learn` job names no fetch policy, so replays go with
    the default one, which does not read robots.txt and goes direct; their requests count as the
    job's usage. None without the JSON rung."""
    json = fetching.rungs.json
    if json is None:
        return None

    def replays(job_id: JobId) -> Replay:
        policy = FetchPolicy()

        async def replay(request: JsonRequest) -> FetchResult:
            attempts: list[AttemptRecord] = []
            rules = rules_of(engine, fetching.rungs.http, attempts, Turns(fetching.limiter, IN_PLACE))
            result = await _polite_send(fetching, json, rules, _Turn(None))(request, policy)
            async with engine.begin() as conn:
                await record_fetch_usage(conn, job_id, [*attempts, *result.attempts], None)
            return result

        return replay

    return replays


@dataclass
class RecipePages:
    """A listing's pages as its active config's listing recipe (one with pagination, Appendix A #59) serves them, up
    to `limit` requests and until `deadline`; the pages after the first are kept in `further` and their attempts added
    to `attempts`. A further page's request the SSRF guard refused fails the fetch (`page_task`): a refused page is
    never stored, and the listing is not cut short silently."""

    limit: int = MAX_PAGES
    attempts: list[AttemptRecord] = field(default_factory=list)
    further: list[FetchResult] = field(default_factory=list)
    deadline: float | None = None  # on `time.monotonic`: no page request starts after it

    async def rung(self, recipe: RecipeRung, url: str, policy: FetchPolicy) -> Rung | None:
        """The recipe rung `acquire` fetches the page through: one that answers with the first page the listing recipe
                served, its further pages kept, so `acquire` keeps or falls back from that one answer as from any
                recipe's;
        None when it served no page. Each page request waits for its host's rate on its own
                (`recipe_rung`)."""
        pages = [
            page async for page in recipe.pages(url, policy, records=None, limit=self.limit, deadline=self.deadline)
        ]
        if not pages:
            return None
        first, further = pages[0], pages[1:]
        self.attempts.extend(attempt for page in further for attempt in page.attempts)
        self.further[:] = further if first.block_class == "ok" else []

        async def served(_url: str, _policy: FetchPolicy) -> FetchResult:
            return first

        return served


async def grown(
    fetching: Fetching,
    url: str,
    policy: FetchPolicy,
    actions: BrowserActions,
    deadline: float,
    admit_next: Admit,
    known: HostRules,
) -> PagedFetch:
    """The listing at `url` from the listing rung with the paging driver (`crawl.paging.collect`): each
    action waits for a turn at the site's rate (`admit_next`), and acts only on the listing's own host, where its
    robots.txt (`known`) allows it: a next page it navigates to, or the document the tab shows for a click or a scroll
    (the caller's steps may have navigated it elsewhere). That host's robots.txt and rate say nothing of another's
    (RFC 9309 §2.3: the rules are per origin)."""
    rung = fetching.rungs.listing
    if rung is None:
        raise RuntimeError("this process has no listing rung")
    host = urlsplit(url).hostname or ""
    drive = partial(_on_host, request=actions, deadline=deadline, host=host, known=known, admit_next=admit_next)
    return await rung(url, policy, drive, deadline)


async def _on_host(
    tab: Tab, *, request: BrowserActions, deadline: float, host: str, known: HostRules, admit_next: Admit
) -> ActionsReport:
    """`collect` on `tab`, each action admitted only on `host` (`grown`)."""

    # The listing's own navigation may have been redirected to another host (RFC 9110 §15.4); the fetch already
    # followed it under the listing's robots.txt, so that host is the listing's too. A host the caller's steps moved
    # the tab to is not.
    own = {host, urlsplit(tab.landed).hostname or ""}

    async def gate(next_url: str | None) -> bool:
        acted_on = tab.url if next_url is None else next_url
        if urlsplit(acted_on).hostname not in own or not known.allows(acted_on):
            return False
        return await admit_next()

    return await collect(tab, request, gate, deadline)


@dataclass
class Growth:
    """A single-URL listing scrape's browser actions where its page is rendered: the browser rung of the fetch
    grows the list toward `actions.records` until `deadline`, and keeps the document states it made in `paged`."""

    actions: BrowserActions
    deadline: float
    paged: list[PagedFetch] = field(default_factory=list)

    def rung(self, fetching: Fetching, admit_next: Admit, known: HostRules) -> Rung:
        async def browse(url: str, policy: FetchPolicy) -> FetchResult:
            paged = await grown(fetching, url, policy, self.actions, self.deadline, admit_next, known)
            self.paged.append(paged)
            return paged.pages[0]

        return browse

    def of(self, kept: FetchResult) -> PagedFetch | None:
        """The listing's states, when the fetch kept the first one."""
        first = self.paged[-1].pages[0] if self.paged else None
        same = first is not None and first.bundle.manifest.capture_id == kept.bundle.manifest.capture_id
        return self.paged[-1] if same else None


@dataclass(frozen=True)
class PagePlan:
    """How a page is fetched: the site's rungs (`site_rungs`), its row, the policy, the attempts every request of the
    fetch is added to, the site's active API `recipe` (with `pages` when it serves a listing in pages); `growth`, a
    single-URL listing scrape's browser actions in `auto` (`Growth`)."""

    rungs: Rungs
    site: SiteRow
    policy: FetchPolicy
    attempts: list[AttemptRecord]
    recipe: ApiRecipe | None = None
    pages: RecipePages | None = None
    growth: Growth | None = None
    # On `time.monotonic`: when the task's budget runs out; the browser is not started too close to it (`acquire`).
    deadline: float | None = None
    # What the config's field sources ask of the fetch: the browser, companion calls (`config_ir.active.fetch_plan`).
    fetch: FetchPlan = NO_PLAN
    # What the config's `recipe` serves beyond its main call when it is a custom recipe (`config_ir.active.custom_of`).
    custom: Custom | None = None
    # A recipe test's fetch (`services.recipe_trial`): the site keeps nothing of it, no adapter decline, no memory.
    trial: bool = False
    # Whether a detail page's capture states the item's offer as its records read it (`Records.offered`); without it no
    # page is rendered for stating none (`acquire.rungs`).
    offered: Offered | None = None


def page_fetch(
    fetching: Fetching, page: PagePlan, rules: ReadRules, waiting: Waiting, *, by_adapter: bool = False
) -> PageFetch:
    """The page through `acquire`, as the polite fetch calls it: the adapter that detects the URL and the active
    config's API recipe ask their endpoints politely (`adapter_send`, `recipe_rung`; endpoints on the page's host by the
    rules the polite fetch read, others by `rules`), and every attempt is added to the request's attempts. A listing
    recipe's pages are requested only for a page `acquire` fetches through the recipe: not one the policy captures as a
    file, nor one an adapter detects (`by_adapter`), unless the recipe is a custom one, which `acquire` tries before
    the adapter. A custom recipe serves only a page its robots.txt allows: it may fetch the page itself."""
    custom = page.custom
    uses_recipe = not page.policy.captures_page and (custom is not None or not by_adapter)

    async def fetch(url: str, admit_next: Admit, known: HostRules) -> FetchResult:
        host = urlsplit(url).hostname or ""
        here = page_first(host, known, rules)
        send = adapter_send(fetching, host, here, waiting)
        rung = await _recipe_served(fetching, page, url, here, known, waiting, uses_recipe=uses_recipe)

        rungs = page.rungs
        if page.growth is not None:
            # The growing render serves every browser attempt, the one after a refusal too: that one opens no site
            # session, whose cookies serve later HTTP fetches only.
            rungs = replace(rungs, browser=page.growth.rung(fetching, admit_next, known), exporting=None)

        result = await acquire(
            url,
            page.policy,
            page.site.remembered,
            rungs,
            custom=rung if custom is not None else None,
            recipe=rung if custom is None else None,
            admit_next=admit_next,
            send=send,
            deadline=page.deadline,
            plan=page.fetch,
            offered=page.offered,
        )
        page.attempts.extend(result.attempts)
        return result

    return fetch


async def _recipe_served(
    fetching: Fetching,
    page: PagePlan,
    url: str,
    here: ReadRules,
    known: HostRules,
    waiting: Waiting,
    *,
    uses_recipe: bool,
) -> Rung | None:
    """The rung of the page's recipe (`page_fetch`), a listing's first page fetched when it `uses_recipe`
    (`RecipePages.rung`); None without one, or for a custom recipe's page robots.txt disallows."""
    host = urlsplit(url).hostname or ""
    custom = page.custom
    recipe = None if page.recipe is None else recipe_rung(fetching, page.recipe, host, here, waiting, custom)
    if recipe is None or (custom is not None and not known.allows(url)):
        return None
    return recipe if page.pages is None or not uses_recipe else await page.pages.rung(recipe, url, page.policy)


async def fetch_planned(
    engine: AsyncEngine, fetching: Fetching, url: str, page: PagePlan, waiting: Waiting, *, tries: int = 1
) -> FetchResult | Refused | Later:
    """The page at `url` fetched by `page` through the polite fetch (`polite_fetch`), waiting for turns as `waiting`
    says, or why nothing was sent; the adapter declines it made are stored on the site, but for a `trial`'s. A request
    the guard refused is an attempt's error (`refusal`)."""
    rules = rules_of(engine, fetching.rungs.http, page.attempts, Turns(fetching.limiter, waiting))
    by_adapter = page.rungs.adapters.would_serve(url)
    fetch = page_fetch(fetching, page, rules, waiting, by_adapter=by_adapter)
    result = await polite_fetch(
        url, page.policy, rules, fetching.limiter, waiting, fetch, by_adapter=by_adapter, tries=tries
    )
    if not page.trial:
        await store_declines(engine, page.site, urlsplit(url).hostname or "", page.rungs)
    return result


def in_place(result: FetchResult | Refused | Later) -> FetchResult | Refused:
    """A fetch the caller waits for (`IN_PLACE`): nothing is postponed in place."""
    if isinstance(result, Later):
        raise TypeError("a fetch the caller waits for was postponed")
    return result


@dataclass(frozen=True)
class SiteHttp:
    """Plain HTTP fetches from one site through the polite fetch (robots.txt by its known `rules`, the site's rate,
    back-off), each with its attempts added to `attempts`: a site's sitemaps."""

    fetching: Fetching
    rules: HostRules
    policy: FetchPolicy
    attempts: list[AttemptRecord]
    waiting: Waiting = IN_PLACE

    async def fetch(self, url: str, policy: FetchPolicy) -> FetchResult | None:
        """`url` as fetched, or None when nothing was sent (robots.txt, or the host's turn too far off). Raises
        `TargetRejected` when the guard refused a request of the fetch."""
        http = self.fetching.rungs.http

        async def known(_url: str, _policy: FetchPolicy) -> HostRules:
            return self.rules

        async def once(target: str, _admit: Admit, _known: HostRules) -> FetchResult:
            result = await http(target, policy)
            self.attempts.extend(result.attempts)
            return result

        result = await polite_fetch(url, policy, known, self.fetching.limiter, self.waiting, once, endpoint=True)
        if not isinstance(result, FetchResult):
            return None
        if (refused := refusal(result)) is not None:
            raise TargetRejected(refused)
        return result

    async def sitemap_body(self, url: str) -> bytes | None:
        """The body of the sitemap at `url`, or `None` when there is none to read: not sent, not a 2xx, or failed.
        Raises `TargetRejected` as `fetch` does."""
        # sitemaps.org: a sitemap may be up to 50 MB uncompressed, larger than a page may be.
        policy = self.policy.model_copy(
            update={"max_body_bytes": MAX_SITEMAP_BYTES, "max_decompressed_bytes": MAX_SITEMAP_BYTES}
        )
        result = await self.fetch(url, policy)
        if result is None or result.attempts[-1].error is not None:
            return None
        return result.bundle.body if 200 <= result.bundle.manifest.status_code < 300 else None
