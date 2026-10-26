"""What the services run with in one process: the database, the fetching rungs, the LLM client and the engine's data
files. The API app, the MCP server's stdio process, the worker and the scheduler open one each from the settings."""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from snowcrawl.acquire.api import JsonRequest, fetch_json
from snowcrawl.acquire.browser import fetch_browser
from snowcrawl.acquire.browser_send import send_in_page
from snowcrawl.acquire.browser_session import BrowserSession, open_browser
from snowcrawl.acquire.browser_tab import Drive, fetch_listing
from snowcrawl.acquire.cookie_store import CookieStore
from snowcrawl.acquire.host_turns import HostTurns
from snowcrawl.acquire.http import fetch_http
from snowcrawl.acquire.impersonate import rung_impersonation
from snowcrawl.acquire.locality import check_places
from snowcrawl.acquire.politeness import HostLimiter
from snowcrawl.acquire.rungs import Rungs, adapter_registry
from snowcrawl.acquire.session import CookieExport, SiteSessions
from snowcrawl.acquire.shared_browser import SharedBrowser
from snowcrawl.acquire.ssrf import SafeConnector
from snowcrawl.acquire.types import FetchPolicy, FetchResult, PagedFetch
from snowcrawl.collect.run import Adapters
from snowcrawl.collect.vocabulary import VocabularyMissing
from snowcrawl.config_ir.drift import MARGINS_FILE, DriftMargins, load_margins
from snowcrawl.content.fragments import eager_frames
from snowcrawl.core.context_digest import configure as configure_context_digest
from snowcrawl.core.egress import Egresses
from snowcrawl.core.engine_version import ENGINE_VERSION, engine_data_dir
from snowcrawl.core.event_loop import quiet_peer_resets
from snowcrawl.core.proxy import ExitPlace, ProxyPool
from snowcrawl.core.settings import AcquireSettings, Settings
from snowcrawl.db.models import ApiKeyId
from snowcrawl.llm.client import LLMClient
from snowcrawl.llm.draft_cap import KeyCapped
from snowcrawl.llm.providers.from_settings import llm_from_settings
from snowcrawl.llm.setup import LLMSetup
from snowcrawl.normalize.dispatch import warm
from snowcrawl.normalize.locales import iso_3166_alpha2
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.search.providers import SearchProvider, configured
from snowcrawl.services.fetching import Fetching
from snowcrawl.storage import open_store

log = logging.getLogger(__name__)


def load_engine_data(adapters: Adapters) -> EngineData | None:
    """The engine's data files from the engine directory's `data/`, with the process's active `adapters` to read their
    captures, for `records` and replays; None where they are not there (no engine directory, or a tree without
    `snowcrawl data fetch`): `records` and replays are then unavailable, and the other formats work."""
    data = engine_data_dir()
    if data is None:
        log.warning("no engine directory: records and replays are unavailable")
        return None
    try:
        return EngineData.load(data, adapters)
    except VocabularyMissing as e:
        log.warning("records and replays are unavailable: %s", e)
        return None


def load_drift_margins() -> DriftMargins | None:
    """The dev-fitted drift margins; None when none are fitted, and then no field is
    suspended."""
    data = engine_data_dir()
    return None if data is None else load_margins(data / MARGINS_FILE)


@dataclass(frozen=True)
class Runtime:
    database: AsyncEngine
    fetching: Fetching
    llm: LLMClient
    engine_data: EngineData | None  # None without the data files: no `records`
    drift_margins: DriftMargins | None
    llm_setup: LLMSetup  # checked in the background from when `open_runtime` opens (`snowcrawl.llm.setup`)
    search: SearchProvider | None = None  # None without a configured provider: search answers 503


@dataclass(frozen=True)
class Caller:
    """One call of an API key, over REST or MCP: the process's runtime, its LLM calls that no job pays for counted
    against the key's draft cap (`KeyCapped`)."""

    runtime: Runtime
    api_key_id: ApiKeyId

    @classmethod
    def of(cls, runtime: Runtime, api_key_id: ApiKeyId) -> "Caller":
        capped = replace(runtime, llm=KeyCapped(runtime.llm, runtime.database, api_key_id))
        return cls(capped, api_key_id)


@asynccontextmanager
async def open_fetching(
    settings: Settings, connector: SafeConnector | None = None, database: AsyncEngine | None = None
) -> AsyncIterator[Fetching]:
    """What a process fetches pages with: the rungs (`open_rungs`), the object store captures go to, the per-host
    limiter and the egresses. With a `database`, hosts' turns and holds are shared with every process through it
    (`acquire/host_turns.py`), and with a cookie key too, the site sessions keep the stored cookie state of temporary
    browser profiles in it. The cookie key also keys the digest of request contexts (`core.context_digest`)."""
    connector = connector or SafeConnector()
    key = settings.cookie_key
    if key is not None:
        configure_context_digest(key.get_secret_value())
    store = None if database is None or key is None else CookieStore(database, key.get_secret_value())
    async with open_rungs(settings, connector, SiteSessions(store=store)) as rungs:
        limiter = HostLimiter(shared=None if database is None else HostTurns(database))
        egresses = Egresses(settings.proxy_pools, settings.egress_country)
        yield Fetching(rungs, connector, open_store(settings.object_store), limiter, egresses)


@asynccontextmanager
async def open_rungs(
    settings: AcquireSettings, connector: SafeConnector, sessions: SiteSessions, *, browser_rung: bool = True
) -> AsyncIterator[Rungs]:
    """The rungs of a process: HTTP, the process's one shared browser (launched when a render first needs it; the page
    rung's and the listing rung's alike), the adapters and the process's site `sessions`, all through one SSRF guard;
    the browser closes with the block. `browser_rung`: the process renders in that browser, so its Chrome must be
    readable (`rung_impersonation`). The declared egress places are checked first (`check_exits`)."""
    pools = settings.proxy_pools
    check_exits(pools, settings.direct)

    async def fetch_in(
        session: BrowserSession, url: str, policy: FetchPolicy, export: CookieExport | None
    ) -> FetchResult:
        return await fetch_browser(
            url,
            policy,
            session=session,
            engine_version=ENGINE_VERSION,
            connector=connector,
            pools=pools,
            export=export,
            scope=sessions.scope_for(policy),
        )

    impersonate = await rung_impersonation(settings, browser_rung=browser_rung)

    async def http(url: str, policy: FetchPolicy) -> FetchResult:
        return await fetch_http(
            url,
            policy,
            engine_version=ENGINE_VERSION,
            impersonate=impersonate,
            connector=connector,
            pools=pools,
            scope=sessions.scope_for(policy),
            direct=settings.direct,
        )

    async def json(request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        return await fetch_json(
            request,
            policy,
            engine_version=ENGINE_VERSION,
            impersonate=impersonate,
            connector=connector,
            pools=pools,
            scope=sessions.scope_for(policy),
            direct=settings.direct,
        )

    async with aclosing(
        SharedBrowser(
            partial(open_browser, settings.browser_profiles, settings.direct, headless=settings.browser_headless),
            fetch_in,
            lambda session: session.alive,
        )
    ) as browser:

        async def listing(url: str, policy: FetchPolicy, drive: Drive, deadline: float) -> PagedFetch:
            # A listing with browser actions renders in the shared session too, holding one of its slots.
            return await browser.render(
                lambda session: fetch_listing(
                    url,
                    policy,
                    drive,
                    deadline,
                    session=session,
                    engine_version=ENGINE_VERSION,
                    connector=connector,
                    pools=pools,
                    scope=sessions.scope_for(policy),
                )
            )

        async def in_page(request: JsonRequest, policy: FetchPolicy, warm_url: str) -> FetchResult:
            # A recipe's call sent from its site's warm page holds one of the shared session's slots, as a render does.
            return await browser.render(
                lambda session: send_in_page(
                    request,
                    policy,
                    warm_url,
                    session=session,
                    engine_version=ENGINE_VERSION,
                    connector=connector,
                    pools=pools,
                    scope=sessions.scope_for(policy),
                )
            )

        yield Rungs(
            http=http,
            browser=browser.fetch,
            adapters=adapter_registry(settings.disabled_adapters),
            json=json,
            in_page=in_page,
            sessions=sessions,
            exporting=browser.fetch,
            listing=listing,
            frames=eager_frames,
        )


async def fetch_once(
    settings: AcquireSettings,
    url: str,
    policy: FetchPolicy,
    *,
    in_browser: bool,
    connector: SafeConnector | None = None,
) -> FetchResult:
    """One fetch of `url`, outside any job: in the browser rung when `in_browser`, else over HTTP, whose
    impersonation then does without the browser when its Chrome cannot be read."""
    async with open_rungs(settings, connector or SafeConnector(), SiteSessions(), browser_rung=in_browser) as rungs:
        rung = rungs.browser if in_browser else rungs.http
        return await rung(url, policy)


def check_exits(pools: Mapping[str, ProxyPool], direct: ExitPlace) -> None:
    """Refuse, at startup, a declared place that names no real country, zone or locale, of the `direct` egress or a
    pool (`acquire.locality.check_places`). A declared country must be a real one: a typo would be recorded as a known
    egress, and would win over the locale's region for the browser's zone."""
    declared = [pool.place.country for pool in pools.values()] + [direct.country]
    if not all(country is None or country in iso_3166_alpha2() for country in declared):
        raise ValueError("an egress country is an ISO 3166-1 alpha-2 country code")
    check_places(pools, direct)


def _search(settings: Settings) -> SearchProvider | None:
    return configured(
        settings.search_searxng_url, settings.search_url_template, settings.search_timeout_s, settings.search_fields
    )


@asynccontextmanager
async def open_runtime(settings: Settings) -> AsyncIterator[Runtime]:
    quiet_peer_resets(asyncio.get_running_loop())
    engine = create_async_engine(settings.database_url.get_secret_value())
    llm, setup = llm_from_settings(settings, engine)
    check = asyncio.create_task(setup.verify())
    # The normalizers' all-locale tables are built in the background, not by the first request that extracts.
    warming = asyncio.create_task(asyncio.to_thread(warm))
    try:
        async with open_fetching(settings, database=engine) as fetching:
            yield Runtime(
                database=engine,
                fetching=fetching,
                llm=llm,
                engine_data=load_engine_data(fetching.rungs.adapters),
                drift_margins=load_drift_margins(),
                llm_setup=setup,
                search=_search(settings),
            )
    finally:
        check.cancel()
        warming.cancel()
        await engine.dispose()
