"""The extraction pipeline: a capture and a schema in, every field's outcome out.

Pages are synthetic (`example.test`); the schema.org vocabulary is the synthetic fixture (C3, C9).
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from snowcrawl.acquire.api import JsonFetcher
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.adapters.base import AdapterMatch, Shape
from snowcrawl.adapters.registry import Registry
from snowcrawl.collect.base import Candidate
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.core.capture import BlockClass, CaptureMethod
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import PageKind, SchemaId, SiteId, SourceKind
from snowcrawl.normalize.base import Ok
from snowcrawl.resolve.evidence import Bucket, Calibration
from snowcrawl.resolve.pipeline import (
    EngineData,
    ListingGapReply,
    RoleC,
    extract_page,
)
from snowcrawl.resolve.preference import PRECEDENCE_TYPES
from snowcrawl.resolve.score import PRECEDENCE_FILE, load_precedence
from snowcrawl.resolve.task import page_records
from snowcrawl.resolve.types import ExtractionResult
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef
from snowcrawl.storage.capture_bundle import (
    CaptureBundle,
)
from tests.support.captures import calls, capture
from tests.support.role_c import CALL, LISTING_CALL, PRICE, FakeRoleC

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
PAGE_URL = "https://shop.example.test/p/1"
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "config", "llm")
DATA = EngineData(vocabulary=VOCABULARY, orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER), calibration=None)
# The order committed under `backend/data/`, as the engine loads it when no fit has replaced it.
COMMITTED_ORDERS = load_precedence(Path(__file__).parents[2] / "data" / PRECEDENCE_FILE)
COMMITTED = EngineData(vocabulary=VOCABULARY, orders=COMMITTED_ORDERS, calibration=None)


def _schema(page_kind: PageKind = "detail", **fields: FieldSpec) -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind=page_kind,
        fields=fields,
    )


PRODUCT = _schema(
    title=FieldSpec(type=FieldType.TEXT, description="d", required=True, schema_org="Product.name"),
    price=FieldSpec(type=FieldType.MONEY, description="d", required=True, schema_org="Offer.price+priceCurrency"),
    sku=FieldSpec(type=FieldType.IDENTIFIER, scheme="sku", description="d", schema_org="Product.sku"),
)


def _product(name: str = "Widget One", url: str = PAGE_URL, *, price: str | None = "19.90") -> dict[str, Any]:
    node: dict[str, Any] = {"@type": "Product", "name": name, "url": url, "sku": "W-1"}
    if price is not None:
        node["offers"] = {"@type": "Offer", "price": price, "priceCurrency": "EUR"}
    return node


def _html(*nodes: Mapping[str, Any], head: str = "", body: str = "") -> str:
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", **n})}</script>'
        for n in nodes
    )
    return f'<html lang="en"><head>{blocks}{head}</head><body><h1>Widget One</h1>{body}</body></html>'


def _bundle(
    body: str,
    *,
    block_class: BlockClass = "ok",
    method: CaptureMethod = "http",
    url: str = PAGE_URL,
    final_url: str | None = None,
) -> CaptureBundle:
    return capture(body.encode(), url=url, final_url=final_url, method=method, block_class=block_class)


def _extract(
    bundle: CaptureBundle,
    schema: SchemaDef = PRODUCT,
    *,
    role_c: RoleC | None = None,
    data: EngineData = DATA,
) -> ExtractionResult:
    return asyncio.run(extract_page(bundle, schema, None, role_c, data=data))


def _statuses(result: ExtractionResult) -> dict[str, tuple[str, str | None]]:
    assert result.entity is not None
    return {name: (outcome.status, outcome.reason) for name, outcome in result.entity.items()}


# --- a served page ---


def test_a_product_page_publishes_its_structured_values() -> None:
    result = _extract(_bundle(_html(_product())))
    assert result.outcome == "extracted"
    assert result.entity is not None
    published = {name: (o.value, o.evidence, o.source_kind) for name, o in result.entity.items()}
    assert published == {
        "title": ("Widget One", "structured", "jsonld"),
        "price": (PRICE, "structured", "jsonld"),
        "sku": ("W-1", "structured", "jsonld"),
    }
    # No calibration was fitted, so every field is uncalibrated and the record has no confidence.
    assert not any(o.calibrated for o in result.entity.values())
    assert result.record_confidence is None


def test_a_page_missing_a_required_field_is_partial() -> None:
    result = _extract(_bundle(_html(_product(price=None))))
    assert result.outcome == "partial"
    assert _statuses(result)["price"] == ("missing", "no_candidates")


def test_a_page_whose_content_the_engine_cannot_read_is_partial_never_no_match() -> None:
    """A custom element with no light DOM (a closed shadow root's host) beside a document title: no needed field has a
    candidate, and the caller-supplied page keeps its record, `partial`, its fields `missing` with `no_candidates` (the
    render hint's evidence); a crawl-discovered one still keeps none."""
    body = '<html lang="en"><head><title>Widget One</title></head><body><product-view></product-view></body></html>'
    result = _extract(_bundle(body))
    assert result.entity is not None
    assert "signal:closed_shadow_root" in result.diagnostics
    assert _statuses(result)["title"] == ("missing", "no_candidates")
    site = SiteId(uuid7())

    def records(caller_supplied: bool) -> tuple[str, int]:
        assert result.entity is not None
        outcome, drafts = page_records(
            PRODUCT,
            result.entity,
            result.rows,
            site_id=site,
            page_url=PAGE_URL,
            caller_supplied=caller_supplied,
            diagnostics=result.diagnostics,
        )
        return outcome, len(drafts)

    assert records(caller_supplied=True) == ("partial", 1)
    assert records(caller_supplied=False) == ("no_match", 0)


def _group(*prices: str) -> dict[str, Any]:
    # A ProductGroup whose offers sit only under its variants, each variant repeating the group's URL.
    variants = [
        {
            "@type": "Product",
            "url": PAGE_URL,
            "sku": f"W-1-{i}",
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR"},
        }
        for i, price in enumerate(prices)
    ]
    return {"@type": "ProductGroup", "name": "Widget One", "url": PAGE_URL, "hasVariant": variants}


def test_a_product_group_whose_variant_offers_agree_publishes_their_price() -> None:
    result = _extract(_bundle(_html(_group("19.90", "19.90", "19.90"))))
    assert result.entity is not None
    assert (result.entity["title"].value, result.entity["price"].value) == ("Widget One", PRICE)
    # Each variant's sku is the variant's, not the group's: the group states none.
    assert _statuses(result)["sku"] == ("missing", "not_owned")


# --- pages that were not served ---


# --- one parse per parser ---


# --- failures ---


# --- the LLM hook ---


def test_required_fields_left_missing_go_to_role_c_once() -> None:
    role_c = FakeRoleC()
    result = _extract(_bundle(_html(_product(price=None))), role_c=role_c)
    assert [r.missing for r in role_c.requests] == [("price",)]
    assert role_c.requests[0].target.types == ("Product",)
    assert result.entity is not None
    assert (result.entity["price"].value, result.entity["price"].evidence) == (PRICE, "llm_grounded")
    assert result.llm_calls == (CALL,)
    assert result.outcome == "extracted"


def test_role_c_is_not_called_for_conflicts() -> None:
    # §4.13: a conflict is never sent to the LLM; with no single target every field is one.
    role_c = FakeRoleC()
    page = _html(
        _product("Widget Two", "https://shop.example.test/p/2"),
        _product("Widget Three", "https://shop.example.test/p/3"),
    )
    result = _extract(_bundle(page), role_c=role_c)
    assert _statuses(result) == dict.fromkeys(("title", "price", "sku"), ("conflict", "intent_ambiguous"))
    assert (result.outcome, role_c.requests, result.llm_calls) == ("partial", [], ())


# --- api captures ---


class FakeAdapter:
    """A platform whose page URLs are under `/jobs/` of the synthetic host, whose API is under `/api/`, and whose
    answer is a JobPosting with a `title` member."""

    name = "synthetic"

    def detect(self, url: str) -> AdapterMatch | None:
        return AdapterMatch(tenant="t", page_kind="detail") if "/jobs/" in url else None

    def serves(self, match: AdapterMatch, endpoint: str) -> bool:
        return "/api/" in endpoint

    async def fetch(self, url: str, policy: FetchPolicy, send: JsonFetcher) -> FetchResult:
        raise AssertionError("extraction fetches nothing")

    def shapes(self, match: AdapterMatch, _answer: JsonValue, /) -> tuple[Shape, ...]:
        return (Shape(type="JobPosting", properties={"title": ("title",)}),)


JOB = _schema(title=FieldSpec(type=FieldType.TEXT, description="d", required=True, schema_org="JobPosting.title"))
JOB_URL = "https://jobs.example.test/jobs/1"
JOB_API = "https://jobs.example.test/api/jobs/1"


def test_an_api_capture_is_read_by_the_adapter_that_detects_its_page_url_and_serves_its_endpoint() -> None:
    data = EngineData(VOCABULARY, DATA.orders, None, adapters=Registry([FakeAdapter()]))
    bundle = _bundle('{"title": "Engineer"}', method="api", url=JOB_URL, final_url=JOB_API)
    schema = JOB.model_copy(
        update={
            "fields": {**JOB.fields, "url": FieldSpec(type=FieldType.URL, description="d", schema_org="JobPosting.url")}
        }
    )
    result = _extract(bundle, schema, data=data)
    assert result.entity is not None
    title = result.entity["title"]
    assert (title.value, title.source_kind, title.evidence, title.locator) == (
        "Engineer",
        "adapter",
        "adapter",
        "adapter:synthetic:$['title']",
    )
    # The capture stands for the page it was requested for: that page, not the endpoint, is the record's URL.
    assert (result.entity["url"].value, result.entity["url"].derived_by) == (JOB_URL, "final_url")


# --- cold listings (§4.7 "Listings") ---

LISTING_URL = "https://shop.example.test/c/widgets?page=2"
LISTING = _schema(
    "listing",
    title=PRODUCT.fields["title"],
    url=FieldSpec(type=FieldType.URL, description="d", required=True, schema_org="Product.url"),
    price=FieldSpec(type=FieldType.MONEY, description="d", schema_org="Offer.price+priceCurrency"),
    # Og:title fills text fields whose path ends in `.name` (the OGP table); on the page, only the page owns it.
    page_title=FieldSpec(type=FieldType.TEXT, description="d", scope="page", schema_org="Product.name"),
    next_page_url=FieldSpec(type=FieldType.URL, description="d", scope="page"),
)


def _row(n: int, **extra: Any) -> dict[str, Any]:
    return {**_product(f"Widget {n}", f"https://shop.example.test/p/{n}", price=f"{n}.50"), **extra}


def _item_list(*items: Mapping[str, Any]) -> dict[str, Any]:
    elements = [{"@type": "ListItem", "position": i + 1, "item": item} for i, item in enumerate(items)]
    return {"@type": "ItemList", "itemListElement": elements}


def _listing(page: str, schema: SchemaDef = LISTING, **kwargs: Any) -> ExtractionResult:
    return _extract(_bundle(page, url=LISTING_URL), schema, **kwargs)


def _row_values(result: ExtractionResult) -> list[dict[str, JsonValue]]:
    return [{name: o.value for name, o in row.items() if o.status == "published"} for row in result.rows]


def test_the_items_of_an_item_list_are_the_rows() -> None:
    result = _listing(_html(_item_list(_row(1), _row(2))))
    assert _row_values(result) == [
        {"title": "Widget 1", "url": "https://shop.example.test/p/1", "price": {"amount": "1.50", "currency": "EUR"}},
        {"title": "Widget 2", "url": "https://shop.example.test/p/2", "price": {"amount": "2.50", "currency": "EUR"}},
    ]
    # Each row holds its row-scoped fields; the page-scoped ones resolve once, against the page.
    assert {frozenset(row) for row in result.rows} == {frozenset({"title", "url", "price"})}
    assert result.entity is not None
    assert set(result.entity) == {"page_title", "next_page_url"}
    assert result.outcome == "extracted"


def _branded(n: int) -> str:
    return f'<li class="card"><h3><a href="/p/{n}">Widget {n}</a></h3><span>{n}.50 EUR</span><p>Acme {n}</p></li>'


BRANDED = _schema(
    "listing",
    title=LISTING.fields["title"],
    url=LISTING.fields["url"],
    brand=FieldSpec(type=FieldType.TEXT, description="d", schema_org="Product.brand.name"),
)


def test_role_c_fills_the_block_rows_when_a_row_field_publishes_on_no_row() -> None:
    # Listings spec, "Role C rows": no block publishes `brand`, so each block's row is asked for it, and only for it.
    role_c = FakeRoleC(fill_blocks=True)
    page = _html(body=f"<ul>{''.join(_branded(n) for n in (1, 2, 3))}</ul>")

    result = _listing(page, BRANDED, role_c=role_c, data=COMMITTED)

    (request,) = role_c.listing_requests
    assert [(block.index, block.url, block.fields) for block in request.blocks] == [
        (n - 1, f"https://shop.example.test/p/{n}", ("brand",)) for n in (1, 2, 3)
    ]
    assert [block.text for block in request.blocks] == [f"Widget {n}\n{n}.50 EUR\nAcme {n}" for n in (1, 2, 3)]
    assert [block.row for block in request.blocks] == [f"item_block:0:{i}" for i in range(3)]
    # The fake fills `brand` with the first line: the claim fills the block's row, the block's own values stand.
    assert [(row["title"].value, row["brand"].value, row["brand"].evidence) for row in result.rows] == [
        (f"Widget {n}", f"Widget {n}", "llm_grounded") for n in (1, 2, 3)
    ]


PLACED = _schema(
    "listing",
    url=LISTING.fields["url"],
    price=LISTING.fields["price"],
    location=FieldSpec(type=FieldType.LOCATION, description="d", schema_org="Accommodation.address"),
)


def _building(b: int) -> str:
    units = "".join(
        f'<li class="unit"><a href="/u/{b}{u}">Unit {u}</a> <span>{u}.50 EUR</span></li>' for u in (1, 2, 3)
    )
    return f'<li class="building"><h3>Building {b}</h3><p>{b} Main Street</p><ul class="units">{units}</ul></li>'


def test_role_c_reads_a_nested_row_with_what_its_enclosing_item_states() -> None:
    # Listings spec, "Role C rows": each building states its name and address once, before its units; the units are
    # the rows, and role C reads each with its building's text, so a field only the building shows can be claimed.
    role_c = FakeRoleC()
    page = _html(body=f'<ul class="buildings">{"".join(_building(b) for b in (1, 2, 3))}</ul>')

    _listing(page, PLACED, role_c=role_c, data=COMMITTED)

    (request,) = role_c.listing_requests
    assert [(block.context, block.text) for block in request.blocks] == [
        (f"Building {b}\n{b} Main Street", f"Unit {u} {u}.50 EUR") for b in (1, 2, 3) for u in (1, 2, 3)
    ]


def test_a_grid_in_one_panel_has_no_enclosing_context() -> None:
    # One of three panels holds the grid: the nesting does not repeat, so the panel's heading is no row's context.
    role_c = FakeRoleC()
    grid = f"<h2>Featured</h2><ul>{''.join(_branded(n) for n in (1, 2, 3))}</ul>"
    others = "".join(f'<div class="panel"><a href="/help/{n}">Help {n}</a></div>' for n in (1, 2))
    page = _html(body=f'<div class="panels"><div class="panel">{grid}</div>{others}</div>')

    _listing(page, BRANDED, role_c=role_c, data=COMMITTED)

    (request,) = role_c.listing_requests
    assert [block.context for block in request.blocks] == ["", "", ""]


def _llm(field: str, owner: str, raw: str, start: int) -> Candidate:
    return Candidate(
        field=field,
        entity_id=owner,
        source_kind="llm",
        raw=raw,
        normalized=Ok(raw),
        locator=f"text:{start}-{start + len(raw)}",
        independent=True,
    )


# A listing schema whose page title is required: role C is asked for the page fields when it is missing.
TITLED = _schema(
    "listing",
    **{**LISTING.fields, "page_title": LISTING.fields["page_title"].model_copy(update={"required": True})},
)


def test_the_page_fields_are_asked_when_a_required_one_is_missing() -> None:
    role_c = FakeRoleC(listing_reply=ListingGapReply((_llm("page_title", "page", "All widgets", 0),), (LISTING_CALL,)))

    result = _listing(_html(body="<h1>All widgets</h1>"), TITLED, role_c=role_c)

    (request,) = role_c.listing_requests
    assert (request.blocks, request.page_missing) == ((), ("page_title", "next_page_url"))
    assert result.entity is not None
    assert (result.entity["page_title"].value, result.entity["page_title"].evidence) == ("All widgets", "llm_grounded")
    assert result.llm_calls == (LISTING_CALL,)


def test_a_listing_role_c_error_propagates() -> None:
    html, role_c = _html(), FakeRoleC(error=LookupError("no recorded response"))
    with pytest.raises(LookupError, match="no recorded response"):
        _listing(html, TITLED, role_c=role_c)


def _card(n: int, *links: str) -> str:
    anchors = "".join(f'<a href="{href}">Widget {n}</a>' for href in links)
    return f'<li class="card"><h3>{anchors or f"Widget {n}"}</h3><span>{n}.50 EUR</span></li>'


def test_a_grid_of_cards_gives_its_rows_before_role_c_is_asked() -> None:
    # Listings spec, "Rows from blocks": each card's heading link and price; its `#reviews` link is the same document.
    cards = "".join(_card(n, f"/p/{n}", f"/p/{n}#reviews") for n in (1, 2, 3))
    role_c = FakeRoleC()

    result = _listing(_html(body=f"<ul>{cards}</ul>"), role_c=role_c, data=COMMITTED)

    assert _row_values(result) == [
        {
            "title": f"Widget {n}",
            "url": f"https://shop.example.test/p/{n}",
            "price": {"amount": f"{n}.50", "currency": "EUR"},
        }
        for n in (1, 2, 3)
    ]
    assert [(row["url"].source_kind, row["url"].locator) for row in result.rows] == [
        ("dom", f"dom:/html[1]/body[1]/ul[1]/li[{n}]/h3[1]/a[1]") for n in (1, 2, 3)
    ]
    assert (role_c.listing_requests, result.outcome) == ([], "extracted")


# --- listing rows from captured calls (Appendix A #58) ---


def _network_listing(*items: Mapping[str, Any], shown: Sequence[Mapping[str, Any]] | None = None) -> CaptureBundle:
    """A rendered listing showing each item's link and price (`shown`'s, else the call's), and the call it loaded
    `items` with."""
    cards = "".join(f'<li><a href="{i["href"]}">{i["href"]}</a> <span>{i["price"]}</span></li>' for i in shown or items)
    raw = f'<html lang="en"><body><h1>All widgets</h1><ul>{cards}</ul></body></html>'.encode()
    data = json.dumps({"results": list(items)}).encode()
    network = calls([("https://api.example.test/widgets?page=1", data)], method="GET")
    return capture(raw, method="browser", url=LISTING_URL, network=network)


NETWORK_ITEMS = [
    {"name": f"Widget {n}", "href": f"https://shop.example.test/p/{n}", "price": f"€{n}.50"} for n in (1, 2, 3)
]


def test_a_listing_without_structured_rows_takes_the_rows_of_its_captured_call() -> None:
    result = _extract(_network_listing(*NETWORK_ITEMS), LISTING, data=COMMITTED)

    assert _row_values(result) == [
        {"url": f"https://shop.example.test/p/{n}", "price": {"amount": f"{n}.50", "currency": "EUR"}}
        for n in (1, 2, 3)
    ]
    # The cards show each item's link and price: the price agrees with the call's, the link is the page's spelling.
    assert {(row["price"].evidence, row["price"].source_kind) for row in result.rows} == {("agreed", "xhr")}
    assert [row["url"].locator for row in result.rows] == [
        f"dom:/html[1]/body[1]/ul[1]/li[{n}]/a[1]" for n in (1, 2, 3)
    ]
    # The titles are text, admitted by equality only: every row lacks its required title.
    assert result.outcome == "partial"


def test_complete_rows_of_a_call_do_not_call_role_c() -> None:
    role_c = FakeRoleC()
    schema = _schema("listing", url=LISTING.fields["url"], price=LISTING.fields["price"])
    result = _extract(_network_listing(*NETWORK_ITEMS), schema, role_c=role_c, data=COMMITTED)

    assert (role_c.listing_requests, result.outcome, len(result.rows)) == ([], "extracted", 3)


# --- the shown variant's availability ---

VARIANT_FIELDS: dict[str, Any] = {
    "type": "object",
    "fields": {
        "sku": {"type": "identifier", "scheme": "sku", "description": "d", "schema_org": "Offer.sku"},
        "price": {"type": "money", "description": "d", "schema_org": "Offer.price+priceCurrency"},
        "availability": {
            "type": "enum",
            "vocab": "schema:ItemAvailability",
            "description": "d",
            "schema_org": "Offer.availability",
        },
        "url": {"type": "url", "description": "d", "schema_org": "Offer.url"},
    },
}
WITH_VARIANTS = _schema(
    title=FieldSpec(type=FieldType.TEXT, description="d", required=True, schema_org="Product.name"),
    price=FieldSpec(type=FieldType.MONEY, description="d", schema_org="Offer.price+priceCurrency"),
    availability=FieldSpec(
        type=FieldType.ENUM, vocab="schema:ItemAvailability", description="d", schema_org="Offer.availability"
    ),
    sku=FieldSpec(type=FieldType.IDENTIFIER, scheme="sku", description="d", schema_org="Product.sku"),
    variants=FieldSpec.model_validate(
        {
            "type": "list",
            "description": "d",
            "schema_org": "Product.offers",
            "key": ["sku", "url"],
            "item": VARIANT_FIELDS,
        }
    ),
)


def _offered(**product: Any) -> dict[str, Any]:
    offers = [
        {
            "@type": "Offer",
            "sku": sku,
            "price": "19.90",
            "priceCurrency": "EUR",
            "availability": f"https://schema.org/{availability}",
            "url": f"{PAGE_URL}?variant={sku}",
        }
        for sku, availability in (("W-1", "OutOfStock"), ("W-2", "InStock"))
    ]
    return {"@type": "Product", "name": "Widget One", "url": PAGE_URL, "offers": offers} | product


def test_availability_is_the_shown_variant_s_and_no_default_otherwise() -> None:
    shown = _extract(_bundle(_html(_offered(sku="W-2"))), WITH_VARIANTS)
    unshown = _extract(_bundle(_html(_offered())), WITH_VARIANTS)

    assert shown.entity is not None
    assert (shown.entity["availability"].value, shown.entity["availability"].reason) == ("InStock", "offers_disagree")
    assert _statuses(unshown)["availability"] == ("conflict", "offers_disagree")


def test_a_price_that_differs_between_variants_is_the_shown_variant_s_and_no_default_otherwise() -> None:
    def priced(**product: Any) -> dict[str, Any]:
        node = _offered(**product)
        node["offers"][1]["price"] = "24.90"
        return node

    shown = _extract(_bundle(_html(priced(sku="W-2"))), WITH_VARIANTS)
    unshown = _extract(_bundle(_html(priced())), WITH_VARIANTS)

    assert shown.entity is not None
    assert shown.entity["price"].value == {"amount": "24.90", "currency": "EUR"}
    assert _statuses(unshown)["price"] == ("conflict", "offers_disagree")


def test_only_a_sku_the_target_publishes_selects_the_shown_variant() -> None:
    # A sku that publishes below the request's min_confidence, or only the request URL carries, is no sku
    # the target shows.
    below = Calibration(git_sha="0" * 40, buckets={"identifier|structured": Bucket(n=40, precision=0.5)})
    doubted = replace(DATA, calibration=below, min_confidence=0.9)
    unconfident = _extract(_bundle(_html(_offered(sku="W-2"))), WITH_VARIANTS, data=doubted)
    from_url = _extract(_bundle(_html(_offered(sku="W-2")), url=f"{PAGE_URL}?sku=W-2"), WITH_VARIANTS)

    assert _statuses(unconfident)["availability"] == ("conflict", "offers_disagree")
    assert _statuses(from_url)["availability"] == ("conflict", "offers_disagree")
