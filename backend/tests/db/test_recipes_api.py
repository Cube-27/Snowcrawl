"""The custom recipe builder over HTTP: `POST /v1/recipes/propose` drafts a recipe from engineer captures whose pages
called the site's API, which reproduces each sample's call; `POST /v1/recipes/test` serves URLs of its patterns with
it and with the normal extraction, and stores no capture, record, config or site memory. The browser is a stand-in
whose page made one call to the site's API."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.acquire.rungs import Rungs
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.config_ir.api_recipe.model import ApiRecipe
from snowcrawl.config_ir.api_recipe.replay import request_for
from snowcrawl.db.models import Capture, Extraction, ExtractionConfig, Job, Record, Site, SiteContract, UrlTask
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.fetching import Fetching
from snowcrawl.storage.capture_bundle import NetworkBody
from tests.db.support import Web as SharedWeb
from tests.db.support import api
from tests.support.captures import call, fetched

pytestmark = pytest.mark.anyio


def _page(name: str) -> bytes:
    offers = {"@type": "Offer", "price": "19.90", "priceCurrency": "EUR"}
    node = {"@context": "https://schema.org", "@type": "Product", "name": name, "offers": offers}
    return (
        f'<!doctype html><html lang="en"><head><title>{name}</title>'
        f'<script type="application/ld+json">{json.dumps(node)}</script></head>'
        f"<body><main><h1>{name}</h1><p>{name} boils water quickly and quietly.</p></main></body></html>"
    ).encode()


def _endpoint(origin: str, slug: str) -> str:
    return f"{origin}/api/product?slug={slug}&v=2"


def _answer(slug: str) -> dict[str, Any]:
    return {"product": {"title": f"{slug.title()} kettle", "id": slug}}


@dataclass
class Web(SharedWeb):
    """`SharedWeb` with the site's API answering each endpoint of `endpoints` as JSON, any other with a 404."""

    endpoints: dict[str, object] = field(default_factory=dict)

    async def json(self, request: JsonRequest, policy: FetchPolicy) -> FetchResult:
        if request.url not in self.endpoints:
            return self.fetch_result(request.page_url, 404, b"{}", block_class="not_found", method="api")
        return self.fetch_result(request.page_url, 200, json.dumps(self.endpoints[request.url]).encode(), method="api")

    def rungs(self) -> Rungs:
        return replace(super().rungs(), json=self.json)


@pytest.fixture
def fetch_result() -> Callable[..., FetchResult]:
    """A rung's result; a browser fetch keeping every call (`calls` `all`) also holds the call the page made to the
    site's API for its product (its slug, the page's last path segment), with the headers the browser sent: its own,
    one the API reads, and a trace id that differs on every call."""
    sent: list[str] = []

    def result(url: str, status: int, body: bytes, *args: Any, **kwargs: Any) -> FetchResult:
        policy: FetchPolicy | None = kwargs.get("policy")
        network: tuple[tuple[NetworkBody, bytes], ...] = ()
        if kwargs["method"] == "browser" and policy is not None and policy.calls == "all":
            slug = url.rsplit("/", 1)[1]
            sent.append(slug)
            headers = [
                ["User-Agent", "Mozilla/5.0"],
                ["Accept", "application/json"],
                ["X-Api-Version", "2"],
                ["X-Trace", f"t-{len(sent)}"],
            ]
            answer = json.dumps(_answer(slug)).encode()
            origin = url.rsplit("/", 2)[0]
            entry: dict[str, Any] = {"method": "GET", "same_site": True, "request_headers": headers}
            network = (call(0, _endpoint(origin, slug), answer, **entry),)
        return fetched(url, status, body, *args, network=network, **kwargs)

    return result


@pytest.fixture
def web(fetch_result: Callable[..., FetchResult], own_origin: str) -> Web:
    return Web.site(fetch_result, own_origin)


@pytest.fixture
async def client(
    app_env: pytest.MonkeyPatch, engine: AsyncEngine, fetching: Fetching, engine_data: EngineData
) -> AsyncIterator[httpx.AsyncClient]:
    async with api(engine) as (app, client):
        app.state.runtime = replace(app.state.runtime, fetching=fetching, engine_data=engine_data)
        yield client


async def _captures(client: httpx.AsyncClient, web: Web, origin: str, schema: SchemaDef) -> list[str]:
    ids = []
    for slug in ("blue", "red"):
        url = f"{origin}/p/{slug}"
        web.serve(url, _page(f"{slug.title()} kettle"))
        body = {"url": url, "schema_id": str(schema.id), "page_kind": "detail"}
        response = await client.post("/v1/captures", json=body)
        assert response.status_code == 200, response.text
        ids.append(response.json()["capture_id"])
    return ids


async def _proposal(client: httpx.AsyncClient, ids: list[str], schema: SchemaDef) -> dict[str, Any]:
    body = {"capture_ids": ids, "main": {"capture_id": ids[0], "index": 0}, "schema_id": str(schema.id)}
    response = await client.post("/v1/recipes/propose", json=body)
    assert response.status_code == 200, response.text
    proposal: dict[str, Any] = response.json()
    return proposal


async def test_a_proposal_from_the_samples_reproduces_each_sample_s_call(
    client: httpx.AsyncClient, web: Web, own_origin: str, kettle_schema: SchemaDef
) -> None:
    ids = await _captures(client, web, own_origin, kettle_schema)

    proposal = await _proposal(client, ids, kettle_schema)

    recipe = proposal["recipe"]
    host = own_origin.removeprefix("https://")
    assert (recipe["schema_id"], recipe["page_kind"], recipe["patterns"]) == (
        str(kettle_schema.id),
        "detail",
        [f"{host}/p/{{p1}}"],
    )
    main = ApiRecipe.model_validate(recipe["main"])
    assert (main.url, main.params_from_url) == (_endpoint(own_origin, "{p1}"), {"p1": "path:1"})
    # The browser's own headers are the HTTP rung's; a value that differs on every call is never a recipe's.
    assert [(h.name, getattr(h, "value", None)) for h in main.headers] == [
        ("Accept", "application/json"),
        ("X-Api-Version", "2"),
    ]
    assert main.map == {"title": "$['product']['title']"}
    assert (proposal["unfilled"], proposal["unbound"], proposal["withheld_headers"]) == (["price"], [], ["X-Trace"])
    for slug in ("blue", "red"):
        request = request_for(main, f"{own_origin}/p/{slug}", {})
        assert request is not None
        assert (request.method, request.url) == ("GET", _endpoint(own_origin, slug))
    endpoint = {
        "method": "POST",
        "origin": own_origin,
        "has_path": True,
        "segments": 2,
        "suffix": "",
        "query": [],
        "body_shape": None,
    }
    body = {"capture_ids": ids, "main": endpoint, "schema_id": str(kettle_schema.id)}
    assert (await client.post("/v1/recipes/propose", json=body)).status_code == 422


async def _rows(engine: AsyncEngine) -> dict[str, int]:
    tables = {"capture": Capture, "extraction": Extraction, "record": Record, "config": ExtractionConfig}
    async with engine.connect() as conn:
        counts = {
            name: (await conn.execute(select(func.count()).select_from(t))).scalar_one() for name, t in tables.items()
        }
        counts["contract"] = (await conn.execute(select(func.count()).select_from(SiteContract))).scalar_one()
    return counts


async def test_a_test_serves_the_recipe_and_the_normal_extraction_and_stores_nothing(
    client: httpx.AsyncClient, web: Web, own_origin: str, kettle_schema: SchemaDef, engine: AsyncEngine
) -> None:
    ids = await _captures(client, web, own_origin, kettle_schema)
    recipe = (await _proposal(client, ids, kettle_schema))["recipe"]
    url = f"{own_origin}/p/green"
    web.serve(url, _page("Green kettle page"))
    web.endpoints[_endpoint(own_origin, "green")] = _answer("green")
    before = await _rows(engine)
    async with engine.connect() as conn:
        declined = (await conn.execute(select(Site.adapter_declined).where(Site.host == own_origin[8:]))).scalar_one()

    response = await client.post("/v1/recipes/test", json={"recipe": recipe, "urls": [url]})

    assert response.status_code == 200, response.text
    tested = response.json()
    [one] = tested["urls"]
    assert one["url"] == url
    assert one["recipe"]["fetch"]["method"] == "api"
    assert [record["fields"]["title"]["value"] for record in one["recipe"]["records"]] == ["Green kettle"]
    assert one["normal"]["fetch"]["method"] == "http"
    assert [record["fields"]["title"]["value"] for record in one["normal"]["records"]] == ["Green kettle page"]
    assert tested["usage"]["http_requests"] >= 2
    assert await _rows(engine) == before
    async with engine.connect() as conn:
        job = (await conn.execute(select(Job.status, Job.kind).where(Job.id == tested["job_id"]))).one()
        task = (await conn.execute(select(UrlTask.capture_id).where(UrlTask.job_id == tested["job_id"]))).one()
        site = (await conn.execute(select(Site.adapter_declined).where(Site.host == own_origin[8:]))).scalar_one()
    assert (tuple(job), task.capture_id, site) == (("completed", "scrape"), None, declined)
    assert (await client.get(f"/v1/jobs/{tested['job_id']}/request")).status_code == 200
    elsewhere = await client.post("/v1/recipes/test", json={"recipe": recipe, "urls": [f"{own_origin}/c/kettles/x"]})
    assert elsewhere.status_code == 422
