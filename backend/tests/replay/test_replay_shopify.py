"""Storefront API answers replayed through `extract_page`: a collection page's answer publishes one
row per product, with a price only where its range is one price.

The answers are synthetic, written to the documented shapes (`example.test` hosts, invented products, C3, C9).
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.adapters.base import ADAPTER_CALL
from snowcrawl.adapters.registry import Registry
from snowcrawl.adapters.shopify import API_VERSION, Shopify
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId, SourceKind
from snowcrawl.resolve.pipeline import EngineData, extract_page
from snowcrawl.resolve.preference import PRECEDENCE_TYPES
from snowcrawl.resolve.types import ExtractionResult, FieldSet
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import call, capture

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "config", "llm")
DATA = EngineData(
    vocabulary=VOCABULARY,
    orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER),
    calibration=None,
    adapters=Registry([Shopify()]),
)
SHOP = "https://shop.example.test"
ENDPOINT = f"{SHOP}/api/{API_VERSION}/graphql.json"


def _preset(name: str) -> SchemaDef:
    preset = next(p for p in load_presets() if p.name == name)
    return SchemaDef(**preset.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")


LISTING = _preset("commerce_listing")


def _extract(page_url: str, answer: JsonValue, schema: SchemaDef) -> ExtractionResult:
    raw = json.dumps(answer).encode()
    bundle = capture(
        raw, url=page_url, final_url=ENDPOINT, method="api", headers=(("X-Shopify-API-Version", API_VERSION),)
    )
    return asyncio.run(extract_page(bundle, schema, None, None, data=DATA))


def _money(amount: str) -> dict[str, JsonValue]:
    return {"amount": amount, "currencyCode": "EUR"}


def _shop(*currencies: str) -> dict[str, JsonValue]:
    return {"paymentSettings": {"enabledPresentmentCurrencies": list(currencies)}}


def _published(fields: FieldSet) -> dict[str, JsonValue]:
    return {name: outcome.value for name, outcome in fields.items() if outcome.status == "published"}


def _collection(prices: list[tuple[str, str]], currencies: tuple[str, ...] = ("EUR",)) -> dict[str, JsonValue]:
    nodes: list[JsonValue] = [
        {
            "id": f"gid://shopify/Product/{n}",
            "title": f"Widget {n}",
            "onlineStoreUrl": f"{SHOP}/products/widget-{n}",
            "featuredImage": {"url": f"{SHOP}/img/{n}.png"},
            "priceRange": {"minVariantPrice": _money(low), "maxVariantPrice": _money(high)},
        }
        for n, (low, high) in enumerate(prices)
    ]
    return {"data": {"shop": _shop(*currencies), "collection": {"products": {"nodes": nodes}}}}


amounts = st.integers(min_value=1, max_value=99_999).map(lambda cents: f"{cents // 100}.{cents % 100:02d}")


@given(prices=st.lists(st.tuples(amounts, amounts), min_size=1, max_size=8))
def test_every_listed_product_is_a_row_priced_only_when_its_range_is_one_price(
    prices: list[tuple[str, str]],
) -> None:
    result = _extract(f"{SHOP}/collections/widgets", _collection(prices), LISTING)

    assert result.outcome == "extracted"
    expected = [
        {"title": f"Widget {n}", "url": f"{SHOP}/products/widget-{n}"}
        | ({"price": {"amount": low, "currency": "EUR"}} if low == high else {})
        for n, (low, high) in enumerate(prices)
    ]
    # A range's bounds are two Offers and the answer states no one price of the product: a range is no price.
    assert [_published(row) for row in result.rows] == expected
    assert [row["price"].reason for row in result.rows if row["price"].status == "conflict"] == [
        "offers_disagree" for low, high in prices if low != high
    ]


@pytest.mark.parametrize("currencies", [("EUR", "SEK"), ()])
def test_a_shop_that_may_show_another_currency_lists_its_products_without_the_answers_prices(
    currencies: tuple[str, ...],
) -> None:
    # The API prices in the shop's primary market; a shop presenting several currencies may show the page in another
    # market, with other amounts, so an answer's amount is not the page's price even with its currency unknown. Live,
    # such a shop is not served (`test_shopify_adapter`) and the page path publishes the price the page shows.
    answer = _collection([("1.00", "1.00"), ("2.00", "2.00")], currencies)

    result = _extract(f"{SHOP}/collections/widgets", answer, LISTING)

    assert [_published(row) for row in result.rows] == [
        {"title": f"Widget {n}", "url": f"{SHOP}/products/widget-{n}"} for n in (0, 1)
    ]


def test_a_card_price_a_gated_collection_page_shows_contests_the_answers_amount() -> None:
    # A content-gating app conceals the main landmark's content until its script runs; the theme shows tax-inclusive
    # amounts the API's catalogue amounts exclude. The page's cards link each product under the collection.
    cards = "".join(
        f'<div class="card"><a href="/collections/widgets/products/widget-{n}">Widget {n}</a><span>€12.10</span></div>'
        for n in range(3)
    )
    html = (
        f'<html><body><main><div style="display:none"><h2>Widgets</h2><div class="grid">{cards}</div></div>'
        '<div style="display:none">Locked</div></main></body></html>'
    )
    answer = json.dumps(_collection([("10.00", "10.00")] * 3)).encode()
    network = (
        call(0, ENDPOINT, answer, response_headers=(("X-Shopify-API-Version", API_VERSION),), data_call=ADAPTER_CALL),
    )
    page = f"{SHOP}/collections/widgets"
    bundle = capture(html.encode(), url=page, network=network)

    # The page's own values rank too (the dev-fitted order holds every source kind).
    data = replace(DATA, orders=dict.fromkeys(PRECEDENCE_TYPES, (*ORDER, "dom", "xhr")))
    result = asyncio.run(extract_page(bundle, LISTING, None, None, data=data))

    # The amount the card shows publishes, flagged; the row's URL is the page's own link to the product.
    assert [(row["price"].value, row["price"].reason) for row in result.rows] == [
        ({"amount": "12.10", "currency": "EUR"}, "disagree")
    ] * 3
    assert [row["url"].value for row in result.rows] == [
        f"{SHOP}/collections/widgets/products/widget-{n}" for n in range(3)
    ]


def _variant(n: int, cents: int, *, available: bool) -> dict[str, JsonValue]:
    return {"id": 100 + n, "title": f"Colour {n}", "sku": f"W-{n}", "price": cents, "available": available}


def _product_page(
    variants: list[dict[str, JsonValue]], *, query: str = "", markup: str = "", **product: JsonValue
) -> ExtractionResult:
    """A product page at `query` with `markup` in its body, and its Ajax product answer listing `variants`, with the
    `product` members."""
    answer = json.dumps({"title": "Widget", "url": "/products/widget", "variants": variants, **product}).encode()
    network = (call(0, f"{SHOP}/products/widget.js", answer, data_call="shopify"),)
    head = '<script id="shopify-features"></script><script>Shopify.currency = {"active":"EUR"};</script>'
    html = f"<html><head>{head}</head><body>{markup}</body></html>".encode()
    page = f"{SHOP}/products/widget{query}"
    bundle = capture(html, url=page, network=network)
    return asyncio.run(extract_page(bundle, _preset("commerce_detail"), None, None, data=DATA))


@pytest.mark.parametrize(
    ("query", "shown"),
    [
        ("?variant=102", 2),  # the variant the URL selects, available or not
        ("?variant=100", 0),
        ("?variant=999", 1),  # an id the product lacks: the first available variant
        ("", 1),  # none selected: the first available variant
    ],
)
def test_a_product_page_shows_the_variant_its_url_selects_else_the_first_available(query: str, shown: int) -> None:
    # Shopify Liquid `product.selected_or_first_available_variant`: the Ajax product answer lists every variant; the
    # page shows the one `?variant=` names, else the first available one.
    variants = [
        _variant(0, 1000, available=False),
        _variant(1, 1200, available=True),
        _variant(2, 1500, available=True),
    ]
    result = _product_page(variants, query=query)

    assert result.entity is not None
    cents = variants[shown]["price"]
    assert isinstance(cents, int)
    assert result.entity["price"].value == {"amount": f"{cents // 100}.00", "currency": "EUR"}
    assert result.entity["sku"].value == f"W-{shown}"
    # Only the shown variant's availability is the item's: `available: true` names no one value, so another
    # variant's OutOfStock never publishes for it.
    assert result.entity["availability"].value == ("OutOfStock" if shown == 0 else None)
    # Each variant's row states the page that shows it (Liquid `variant.url`): the product's URL with its `?variant=`.
    rows = result.entity["variants"].value
    assert isinstance(rows, list)
    assert [row.get("url") for row in rows if isinstance(row, dict)] == [
        f"{SHOP}/products/widget?variant={100 + n}" for n in range(3)
    ]


def _jsonld(node: Mapping[str, object]) -> str:
    return f'<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", **node})}</script>'


@pytest.mark.parametrize(
    ("query", "identifiers"),
    [
        ("", ("STYLE-1", "9780306406157")),  # the first available variant: the product's own identifiers publish
        ("?variant=101", ("W-1", "4006381333931")),  # the variant the URL selects, by its row's URL
    ],
)
def test_the_shown_variants_identifiers_yield_to_those_the_page_states_for_the_product_unless_the_url_selects_it(
    query: str, identifiers: tuple[str, str]
) -> None:
    # Variant fill: the answer states its sku and barcode on the variants only; the product's own
    # sku and gtin, as the page states them, publish, and the shown variant's are no rivals of them. They still name
    # the variant the page shows, whose price is the item's. A variant the request URL selects states the record's
    # values: its row's URL is the request URL.
    variants = [
        _variant(0, 1000, available=False),
        {**_variant(1, 1200, available=True), "barcode": "4006381333931"},
    ]
    product = {"@type": "Product", "name": "Widget", "sku": "STYLE-1", "gtin13": "9780306406157"}

    result = _product_page(variants, query=query, markup=_jsonld(product))

    assert result.entity is not None
    assert (result.entity["sku"].value, result.entity["gtin"].value) == identifiers
    assert result.entity["price"].value == {"amount": "12.00", "currency": "EUR"}


def test_a_product_answer_is_the_page_groups_item_not_the_shown_variant() -> None:
    # Identity: the answer states the product with every variant; sharing the shown variant's sku
    # with one `hasVariant` member does not make it that member, so the page's variants stay two rows.
    variants = [_variant(0, 1000, available=True), _variant(1, 1000, available=True)]
    offers = [{"@type": "Offer", "url": f"{SHOP}/products/widget?variant={100 + n}"} for n in range(2)]
    members = [{"@type": "Product", "sku": f"W-{n}", "offers": offer} for n, offer in enumerate(offers)]
    group = {"@type": "ProductGroup", "@id": "/products/widget#product", "name": "Widget", "hasVariant": members}
    product = {"@type": "Product", "@id": "/products/widget#product", "name": "Widget"}

    result = _product_page(variants, markup=_jsonld(group) + _jsonld(product))

    assert result.entity is not None
    rows = result.entity["variants"].value
    assert isinstance(rows, list)
    assert [row["sku"] for row in rows if isinstance(row, dict)] == ["W-0", "W-1"]


def test_the_page_s_offers_and_the_answer_s_variants_stating_one_url_are_that_variant() -> None:
    # The page states one Offer per variant by its URL only, the answer each variant by its `variant.url`: two sources'
    # rows stating one URL are one variant, keyed by it, not the group's URL (one source's rows repeat that).
    variants: list[dict[str, JsonValue]] = [
        {"id": 100 + n, "title": f"Colour {n}", "price": 1000, "available": True} for n in range(2)
    ]
    offers = [{"@type": "Offer", "url": f"{SHOP}/products/widget?variant={100 + n}"} for n in range(2)]
    product = {"@type": "Product", "name": "Widget", "url": f"{SHOP}/products/widget", "offers": offers}

    result = _product_page(variants, markup=_jsonld(product))

    assert result.entity is not None
    rows = result.entity["variants"].value
    assert isinstance(rows, list)
    assert [(row.get("url"), row.get("name")) for row in rows if isinstance(row, dict)] == [
        (f"{SHOP}/products/widget?variant={100 + n}", f"Colour {n}") for n in range(2)
    ]


def test_the_products_lowest_price_the_page_states_is_no_rival_of_the_shown_variants() -> None:
    # The answer's `price_min` and `price_max` are the range the variants span (an AggregateOffer's bounds): the
    # page's `og:price:amount` and an Offer stating the low bound for the shown variant are that bound, not its price.
    variants = [_variant(0, 1500, available=True), _variant(1, 1000, available=True), _variant(2, 2000, available=True)]
    offer = {
        "@type": "Offer",
        "price": "10.00",
        "priceCurrency": "EUR",
        "sku": "W-0",
        "url": f"{SHOP}/products/widget?variant=100",
    }
    product = {"@type": "Product", "name": "Widget", "sku": "W-0", "offers": offer}
    og = '<meta property="og:price:amount" content="10.00"><meta property="og:price:currency" content="EUR">'

    result = _product_page(
        variants, markup=og + _jsonld(product), price=1000, price_min=1000, price_max=2000, price_varies=True
    )

    assert result.entity is not None
    assert result.entity["price"].value == {"amount": "15.00", "currency": "EUR"}
    rows = result.entity["variants"].value
    assert isinstance(rows, list)
    assert [row.get("price") for row in rows if isinstance(row, dict)] == [
        {"amount": f"{amount}.00", "currency": "EUR"} for amount in (15, 10, 20)
    ]
