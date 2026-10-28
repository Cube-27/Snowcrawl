"""A site whose active config has an API recipe is served through it ("Adapter / API" rung): in a scrape, and in the
worker's `batch_scrape` and `crawl` tasks. A URL a custom recipe's pattern matches is served by that recipe, any other
by the learned config. The recipe's request goes
through robots.txt and the site's rate like the page's would; its values are `api` candidates in the
`adapter` bucket; a page the recipe serves joins no drift window, which measures the config's page steps. A custom
recipe serves with its warm-up, its extra calls (bound from the main answer too), its HTML answers read by XPath and
its page fields; it is tried before an adapter, and a page it does not serve goes to the page rungs, its attempts
first, each naming the recipe."""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.api import JsonFetcher, JsonRequest
from snowcrawl.acquire.rungs import Rungs
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.adapters.base import AdapterMatch, Shape
from snowcrawl.adapters.registry import Registry
from snowcrawl.config_ir.activation import store_recipe
from snowcrawl.config_ir.custom_recipe import CustomRecipe
from snowcrawl.config_ir.progressive import StateKey
from snowcrawl.db.models import (
    ExtractionConfig,
    ExtractionConfigId,
    SiteId,
)
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.schemas.types import SchemaDef, document_digest
from snowcrawl.services.fetching import Fetching
from snowcrawl.services.scrape import ScrapeRequest, scrape
from tests.db.support import Web as SharedWeb

pytestmark = pytest.mark.anyio

# The page's heading is not the product's name, so a value read from the page and one from the API differ.
# It shows its price, so the ladder renders it for no missing offer (`acquire.rungs._offerless`).
PAGE = b"""<!doctype html><html lang="en"><head><title>Lamps</title></head>
<body><main><h1>Lamp page</h1><p>A lamp that lights a desk.</p><p>$19.50</p><a href="/p/other-lamp">Another lamp</a></main></body>
</html>"""
# The API's answer states the product it was asked for (its slug), as the identity check requires.
PRODUCT = {"data": {"slug": "desk-lamp", "name": "Desk Lamp", "price": 19.5, "currency": "EUR"}}


@dataclass
class Web(SharedWeb):
    """`SharedWeb` with each endpoint of `endpoints` answering as JSON, and of `html` as HTML; any other is a 404."""

    endpoints: dict[str, object] = field(default_factory=dict)
    html: dict[str, bytes] = field(default_factory=dict)
    requested: list[JsonRequest] = field(default_factory=list)
    adapters: Registry = field(default_factory=lambda: Registry(()))
    warm_urls: list[str] = field(default_factory=list)  # each call sent from a warm page: the URL it opens at

    async def json(self, request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        self.requested.append(request)
        if request.url in self.html:
            html = (("Content-Type", "text/html; charset=utf-8"),)
            return self.fetch_result(request.page_url, 200, self.html[request.url], html, method="api")
        if request.url not in self.endpoints:
            return self.fetch_result(request.page_url, 404, b"{}", block_class="not_found", method="api")
        body = json.dumps(self.endpoints[request.url]).encode()
        return self.fetch_result(request.page_url, 200, body, method="api")

    async def in_page(self, request: JsonRequest, policy: FetchPolicy, warm_url: str) -> FetchResult:
        self.warm_urls.append(warm_url)
        return await self.json(request, policy)

    def rungs(self) -> Rungs:
        return replace(super().rungs(), json=self.json, in_page=self.in_page, adapters=self.adapters)


@pytest.fixture
def web(fetch_result: Callable[..., FetchResult], own_origin: str) -> Web:
    return Web.site(fetch_result, own_origin)


def _ir(origin: str, schema: SchemaDef) -> dict[str, Any]:
    """A config that reads the title from the page's heading, with a recipe that reads both fields from the API."""
    return {
        "ir_version": "1",
        "site": origin.removeprefix("https://"),
        "schema": {"name": schema.name, "digest": document_digest(schema)},
        "page_kind": "detail",
        "fetch": {"method": "http"},
        "fields": {"title": {"steps": [{"xpath": "normalize-space(//h1)"}], "status": "corroborated"}},
        "api_recipe": {
            "method": "GET",
            "url": f"{origin}/api/products/{{p1}}",
            "params_from_url": {"p1": "path:1"},
            "map": {"title": "$['data']['name']", "price": "$['data']['price']+$['data']['currency']"},
            "proof": {"validated_on": [str(uuid.UUID(int=1))], "headers_tried": 1, "bound_by": "deterministic"},
        },
        "provenance": {
            "origin": "induced",
            "validated_on": [],
            "learned_at": "2026-10-01T00:00:00Z",
            "engine_version": "t",
        },
    }


@pytest.fixture
async def recipe_config(
    engine: AsyncEngine, own_origin: str, site: SiteId, kettle_schema: SchemaDef
) -> ExtractionConfigId:
    """The site's active config for the schema, with its API recipe."""
    values = {
        "site_id": site,
        "schema_id": kettle_schema.id,
        "page_kind": "detail",
        "version": 1,
        "status": "active",
        "ir": _ir(own_origin, kettle_schema),
        "origin": "induced",
        "field_status": {"title": "corroborated"},
    }
    async with engine.begin() as conn:
        config_id = (
            await conn.execute(insert(ExtractionConfig).values(values).returning(ExtractionConfig.id))
        ).scalar_one()
    return config_id


async def _drift_window(engine: AsyncEngine, config_id: uuid.UUID) -> object:
    async with engine.connect() as conn:
        query = select(ExtractionConfig.drift_window).where(ExtractionConfig.id == config_id)
        return (await conn.execute(query)).scalar_one()


# The scrape service.


def _scrape(url: str, schema: SchemaDef, *formats: str, **fields: Any) -> ScrapeRequest:
    return ScrapeRequest.model_validate(
        {
            "url": url,
            "formats": ["records", *formats],
            "schema": {"id": str(schema.id)},
            "page_kind": "detail",
            **fields,
        }
    )


async def test_a_scrape_of_a_site_with_a_recipe_is_served_over_its_api(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    kettle_schema: SchemaDef,
    recipe_config: uuid.UUID,
    engine_data: EngineData,
) -> None:
    url = f"{own_origin}/p/desk-lamp"
    web.serve(url, PAGE)
    web.endpoints[f"{own_origin}/api/products/desk-lamp"] = PRODUCT

    result = await scrape(engine, fetching, _scrape(url, kettle_schema), data=engine_data)

    assert result.fetch is not None
    assert (result.outcome, result.fetch.method) == ("extracted", "api")
    assert web.page_fetches() == []  # no HTML page, no browser
    assert [(r.url, r.page_url) for r in web.requested] == [(f"{own_origin}/api/products/desk-lamp", url)]
    assert result.records is not None
    (record,) = result.records
    title, price = record.fields["title"], record.fields["price"]
    assert (record.mode, title.value, title.source_kind, title.evidence) == ("config", "Desk Lamp", "api", "adapter")
    assert title.locator == f"api:{recipe_config}:$['data']['name']"
    assert price.value == {"amount": 19.5, "currency": "EUR"}
    # The page steps did not run on the JSON, so their window shows nothing of them.
    assert await _drift_window(engine, recipe_config) == {}


async def test_an_endpoint_robots_txt_disallows_is_not_requested_and_the_page_is_fetched(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    kettle_schema: SchemaDef,
    recipe_config: uuid.UUID,
    engine_data: EngineData,
) -> None:
    url = f"{own_origin}/p/desk-lamp"
    web.serve(url, PAGE)
    web.robots(own_origin, b"User-agent: *\nDisallow: /api/\n")
    web.endpoints[f"{own_origin}/api/products/desk-lamp"] = PRODUCT
    obeying = _scrape(url, kettle_schema, fetch={"respect_robots": True})

    result = await scrape(engine, fetching, obeying, data=engine_data)

    assert result.fetch is not None
    assert (result.fetch.method, web.page_fetches(), web.requested) == ("http", [("http", url)], [])
    assert [(a.method, a.error) for a in result.fetch.attempts if a.method == "api"] == [("api", "robots")]
    assert result.records is not None
    assert result.records[0].fields["title"].value == "Lamp page"


# The worker's tasks.


# Selection by URL.


async def _custom(
    engine: AsyncEngine, own_origin: str, site: SiteId, schema: SchemaDef, **recipe: Any
) -> ExtractionConfigId:
    """A custom recipe for the URLs `/item/{slug}` of the site, stored active."""
    host = own_origin.removeprefix("https://")
    document = {"schema_id": schema.id, "page_kind": "detail", "patterns": [f"{host}/item/{{slug}}"], **recipe}
    ir = CustomRecipe.model_validate(document).to_ir(schema, at=datetime(2026, 10, 1, tzinfo=UTC), engine_version="t")
    async with engine.begin() as conn:
        return await store_recipe(conn, StateKey(site, schema.id, "detail"), ir, replaces=None)


def _items(own_origin: str) -> dict[str, Any]:
    """The main call of `/item/{slug}`: the item's JSON by its slug."""
    return {
        "method": "GET",
        "url": f"{own_origin}/api/items/{{p1}}",
        "params_from_url": {"p1": "path:1"},
        "map": {"title": "$['data']['name']", "price": "$['data']['price']+$['data']['currency']"},
    }


@pytest.fixture
async def custom_recipe(
    engine: AsyncEngine, own_origin: str, site: SiteId, kettle_schema: SchemaDef, recipe_config: ExtractionConfigId
) -> ExtractionConfigId:
    """Beside the learned `recipe_config`, a custom recipe for the URLs `/item/{slug}`, served by another endpoint."""
    return await _custom(engine, own_origin, site, kettle_schema, main=_items(own_origin))


async def test_a_url_a_recipes_pattern_matches_is_served_by_it_and_any_other_by_the_learned_config(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    kettle_schema: SchemaDef,
    recipe_config: uuid.UUID,
    custom_recipe: uuid.UUID,
    engine_data: EngineData,
) -> None:
    item, about = f"{own_origin}/item/desk-lamp", f"{own_origin}/about"
    web.serve(item, PAGE)
    web.serve(about, PAGE)
    web.endpoints[f"{own_origin}/api/items/desk-lamp"] = PRODUCT

    served = await scrape(engine, fetching, _scrape(item, kettle_schema), data=engine_data)
    learned = await scrape(engine, fetching, _scrape(about, kettle_schema), data=engine_data)

    assert served.fetch is not None
    assert served.records is not None
    assert served.fetch.method == "api"
    assert served.records[0].fields["title"].locator == f"api:{custom_recipe}:$['data']['name']"
    # The learned config's recipe binds nothing on `/about`, so its page steps read the page.
    assert learned.fetch is not None
    assert learned.records is not None
    assert (learned.fetch.method, web.page_fetches()) == ("http", [("http", about)])
    title = learned.records[0].fields["title"]
    # The recipe has no page steps: the learned config's read the heading.
    assert (learned.records[0].mode, title.value, title.locator) == (
        "config",
        "Lamp page",
        "config:title:0:normalize-space(//h1)",
    )
    assert [r.url for r in web.requested] == [f"{own_origin}/api/items/desk-lamp"]


# Serving a custom recipe.


async def test_an_extra_call_bound_from_the_main_answer_fills_its_fields_after_a_form_post(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    site: SiteId,
    kettle_schema: SchemaDef,
    engine_data: EngineData,
) -> None:
    main = {
        "method": "POST",
        "url": f"{own_origin}/api/search",
        "params_from_url": {"q": "pattern:slug"},
        "body": {"template": "q={q}&n=1", "encoding": "form"},
        "map": {"title": "$['data']['name']"},
    }
    stock = {
        "method": "GET",
        "url": f"{own_origin}/api/stock/{{id}}",
        "params_from_url": {"id": "main:$['data']['id']"},
        "map": {"price": "$['price']+$['currency']"},
    }
    config = await _custom(engine, own_origin, site, kettle_schema, main=main, calls={"stock": stock})
    url = f"{own_origin}/item/desk%20lamp"
    web.endpoints[f"{own_origin}/api/search"] = {"data": {"id": "7701", "slug": "desk lamp", "name": "Desk Lamp"}}
    web.endpoints[f"{own_origin}/api/stock/7701"] = {"item": "7701", "price": 19.5, "currency": "EUR"}

    result = await scrape(engine, fetching, _scrape(url, kettle_schema), data=engine_data)

    assert [(r.method, r.url, r.body) for r in web.requested] == [
        ("POST", f"{own_origin}/api/search", "q=desk+lamp&n=1"),
        ("GET", f"{own_origin}/api/stock/7701", None),
    ]
    assert ("Content-Type", "application/x-www-form-urlencoded") in web.requested[0].headers
    assert result.fetch is not None
    assert result.records is not None
    assert (result.fetch.method, web.page_fetches()) == ("api", [])
    assert [(a.method, a.recipe) for a in result.fetch.attempts] == [("api", str(config))] * 2
    title, price = result.records[0].fields["title"], result.records[0].fields["price"]
    assert (title.value, title.locator) == ("Desk Lamp", f"api:{config}:$['data']['name']")
    assert (price.value, price.locator) == (
        {"amount": 19.5, "currency": "EUR"},
        f"api:{config}.stock:$['price']+$['currency']",
    )


async def test_an_html_answer_is_read_by_xpath_with_a_parameter_the_warm_up_answered(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    site: SiteId,
    kettle_schema: SchemaDef,
    engine_data: EngineData,
) -> None:
    main = {
        "method": "GET",
        "url": f"{own_origin}/fragments/{{p1}}?t={{t}}",
        "params_from_url": {"p1": "pattern:slug", "t": "warm_up:$['token']"},
        "answer": "html",
        "map": {"title": "normalize-space(//h2)", "price": "string(//span[@id='price'])"},
    }
    warm_up = {"url": f"{own_origin}/session"}
    config = await _custom(engine, own_origin, site, kettle_schema, main=main, warm_up=warm_up)
    url = f"{own_origin}/item/desk-lamp"
    web.endpoints[f"{own_origin}/session"] = {"token": "tok-1234"}
    fragment = f"{own_origin}/fragments/desk-lamp?t=tok-1234"
    web.html[fragment] = b"<div data-item='desk-lamp'><h2> Desk  Lamp </h2><span id='price'>EUR 19.50</span></div>"

    result = await scrape(engine, fetching, _scrape(url, kettle_schema), data=engine_data)

    assert [r.url for r in web.requested] == [f"{own_origin}/session", fragment]
    assert web.requested[1].raw
    assert result.fetch is not None
    assert result.records is not None
    assert result.fetch.method == "api"
    title, price = result.records[0].fields["title"], result.records[0].fields["price"]
    assert (title.value, title.source_kind, title.locator) == (
        "Desk Lamp",
        "api",
        f"api:{config}:normalize-space(//h2)",
    )
    assert price.value == {"amount": 19.5, "currency": "EUR"}


async def test_a_recipe_sent_by_the_browser_sends_every_call_from_the_warm_page_of_its_warm_up(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    site: SiteId,
    kettle_schema: SchemaDef,
    engine_data: EngineData,
) -> None:
    warm_up = {"url": f"{own_origin}/session/{{slug}}"}
    await _custom(engine, own_origin, site, kettle_schema, main=_items(own_origin), warm_up=warm_up, sent_by="browser")
    web.endpoints[f"{own_origin}/session/desk-lamp"] = {}
    web.endpoints[f"{own_origin}/api/items/desk-lamp"] = PRODUCT

    result = await scrape(engine, fetching, _scrape(f"{own_origin}/item/desk-lamp", kettle_schema), data=engine_data)

    assert result.fetch is not None
    assert result.fetch.method == "api"
    assert [r.url for r in web.requested] == [f"{own_origin}/session/desk-lamp", f"{own_origin}/api/items/desk-lamp"]
    assert web.warm_urls == [f"{own_origin}/session/desk-lamp"] * 2  # both from the page the warm-up opens


async def test_a_page_field_is_read_from_the_page_the_recipe_fetched(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    site: SiteId,
    kettle_schema: SchemaDef,
    engine_data: EngineData,
) -> None:
    main = {**_items(own_origin), "map": {"price": "$['data']['price']+$['data']['currency']"}}
    fields = {"title": {"steps": [{"xpath": "normalize-space(//h1)"}]}}
    config = await _custom(engine, own_origin, site, kettle_schema, main=main, fields=fields)
    url = f"{own_origin}/item/desk-lamp"
    web.serve(url, PAGE)
    web.endpoints[f"{own_origin}/api/items/desk-lamp"] = PRODUCT

    result = await scrape(engine, fetching, _scrape(url, kettle_schema), data=engine_data)

    assert result.fetch is not None
    assert result.records is not None
    assert (result.fetch.method, web.page_fetches()) == ("api", [("http", url)])
    assert [(a.method, a.recipe) for a in result.fetch.attempts] == [("http", str(config)), ("api", str(config))]
    title, price = result.records[0].fields["title"], result.records[0].fields["price"]
    assert (title.value, title.locator) == ("Lamp page", "config:title:0:normalize-space(//h1)")
    assert price.locator == f"api:{config}:$['data']['price']+$['data']['currency']"


async def test_a_page_the_recipe_does_not_serve_goes_to_the_page_rungs_with_the_recipes_attempts_first(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    kettle_schema: SchemaDef,
    custom_recipe: uuid.UUID,
    engine_data: EngineData,
) -> None:
    url = f"{own_origin}/item/desk-lamp"
    web.serve(url, PAGE)  # the endpoint answers 404

    result = await scrape(engine, fetching, _scrape(url, kettle_schema), data=engine_data)

    assert result.fetch is not None
    assert (result.fetch.method, web.page_fetches()) == ("http", [("http", url)])
    assert [(a.method, a.recipe) for a in result.fetch.attempts] == [("api", str(custom_recipe)), ("http", None)]


@dataclass
class _Adapter:
    """An adapter that detects every page of `host` and is never to be asked."""

    host: str
    asked: list[str] = field(default_factory=list)
    name: str = "stub"

    def detect(self, url: str) -> AdapterMatch | None:
        return AdapterMatch(tenant="t", page_kind="detail") if urlsplit(url).hostname == self.host else None

    def serves(self, match: AdapterMatch, endpoint: str) -> bool:
        return False

    async def fetch(self, url: str, policy: FetchPolicy, send: JsonFetcher) -> FetchResult:
        self.asked.append(url)
        raise AssertionError("a custom recipe serves the page before the adapter")

    def shapes(self, match: AdapterMatch, answer: object, /) -> tuple[Shape, ...]:
        return ()


async def test_a_custom_recipe_serves_an_adapter_sites_page_before_the_adapter(
    engine: AsyncEngine,
    fetching: Fetching,
    web: Web,
    own_origin: str,
    kettle_schema: SchemaDef,
    custom_recipe: uuid.UUID,
    engine_data: EngineData,
) -> None:
    adapter = _Adapter(own_origin.removeprefix("https://"))
    web.adapters = Registry((adapter,))
    url = f"{own_origin}/item/desk-lamp"
    web.endpoints[f"{own_origin}/api/items/desk-lamp"] = PRODUCT

    result = await scrape(engine, replace(fetching, rungs=web.rungs()), _scrape(url, kettle_schema), data=engine_data)

    assert result.fetch is not None
    assert (result.fetch.method, adapter.asked) == ("api", [])
    assert [r.url for r in web.requested] == [f"{own_origin}/api/items/desk-lamp"]
