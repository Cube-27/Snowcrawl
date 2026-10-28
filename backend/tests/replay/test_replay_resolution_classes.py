"""Failure classes of extraction replayed through `extract_page` on synthetic captures: each
test builds the class's page shape on `example.test` names (C3, C9) and checks what the commerce preset publishes."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId, SourceKind
from snowcrawl.resolve.pipeline import EngineData, extract_page
from snowcrawl.resolve.preference import PRECEDENCE_TYPES
from snowcrawl.resolve.types import ExtractionResult
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import PAGE_URL, capture

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "config", "llm")
DATA = EngineData(vocabulary=VOCABULARY, orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER), calibration=None)
_PRESET = next(p for p in load_presets() if p.name == "commerce_detail")
SCHEMA = SchemaDef(**_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")


def _extract(
    head: str, body: str = "", schema: SchemaDef = SCHEMA, lang: str = "", url: str = PAGE_URL
) -> ExtractionResult:
    lang_attribute = f' lang="{lang}"' if lang else ""
    raw = f"<html{lang_attribute}><head>{head}</head><body>{body}</body></html>".encode()
    return asyncio.run(extract_page(capture(raw, url=url), schema, None, None, data=DATA))


def _jsonld(*nodes: Mapping[str, Any]) -> str:
    return "".join(f'<script type="application/ld+json">{json.dumps(node)}</script>' for node in nodes)


def _outcomes(result: ExtractionResult, *names: str) -> dict[str, tuple[str, str | None, JsonValue]]:
    assert result.entity is not None
    return {name: (result.entity[name].status, result.entity[name].reason, result.entity[name].value) for name in names}


def _og(*metas: tuple[str, str]) -> str:
    return "".join(f'<meta property="{p}" content="{c}">' for p, c in metas)


def test_site_wide_og_values_on_a_client_rendered_page_do_not_publish() -> None:
    # The page names its product only in rendered text; its og block is the site's default.
    head = _og(
        ("og:site_name", "Synthetic Tools - Workshop"),
        ("og:title", "Synthetic Tools"),
        ("og:image", "https://shop.example.test/banner.png"),
        ("og:url", PAGE_URL),
    )

    result = _extract(head, "<h1>Widget One</h1>")

    assert _outcomes(result, "title", "image_url", "url") == {
        "title": ("missing", "no_candidates", None),
        "image_url": ("missing", "no_candidates", None),
        "url": ("published", None, PAGE_URL),
    }


def _product(**properties: Any) -> dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "Product", **properties}


def _offer(price: str, availability: str, **properties: Any) -> dict[str, Any]:
    return {"@type": "Offer", "price": price, "priceCurrency": "EUR", "availability": availability, **properties}


_LISTING_PRESET = next(p for p in load_presets() if p.name == "commerce_listing")
LISTING = SchemaDef(**_LISTING_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")


_MICRODATA_PRODUCT = (
    '<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Widget One</h1>'
    '<meta itemprop="sku" content="W-1"><div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
    '<meta itemprop="price" content="14.00"><meta itemprop="priceCurrency" content="EUR">'
    '<link itemprop="availability" href="https://schema.org/InStock"></div></div>'
)


def test_standalone_variants_naming_their_group_s_id_are_its_variants() -> None:
    # schema.org `inProductGroupWithID`: each Product is a variant of the ProductGroup with that `productGroupID`, so
    # the group is the page's product, its variants are rows, and the properties it states for all of them publish.
    group = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "productGroupID": "G-1",
        "name": "Widget",
        "url": PAGE_URL,
        "color": "Blue",
        "audience": {"@type": "PeopleAudience", "suggestedGender": "unisex"},
        "variesBy": "https://schema.org/size",
    }
    variants = [
        _product(
            name=f"Widget {size}",
            sku=f"W-{size}",
            size=size,
            inProductGroupWithID="G-1",
            offers=_offer("10.00", "https://schema.org/InStock"),
        )
        for size in ("S", "M")
    ]

    result = _extract(_jsonld(group, *variants))

    assert _outcomes(result, "title", "color", "gender", "price") == {
        "title": ("published", None, "Widget"),
        "color": ("published", None, "Blue"),
        "gender": ("published", None, "unisex"),
        "price": ("published", None, {"amount": "10.00", "currency": "EUR"}),
    }
    assert result.entity is not None
    published = result.entity["variants"]
    assert published.status == "published"
    assert [item.fields["sku"].value for item in published.items or ()] == ["W-S", "W-M"]


def test_a_visible_bound_of_a_stated_price_range_is_no_price() -> None:
    # Resolution spec, "Candidate score": `€80` beside an AggregateOffer of 80-120 is its bound; the range publishes.
    price_range = {"type": "range", "description": "d", "schema_org": "AggregateOffer.lowPrice+highPrice+priceCurrency"}
    fields = {**_PRESET.model_dump()["fields"], "price_range": price_range}
    preset = {**_PRESET.model_dump(), "fields": fields}
    schema = SchemaDef(**preset, id=SchemaId(uuid7()), origin="user", status="active")
    offers = {"@type": "AggregateOffer", "lowPrice": 80, "highPrice": 120, "priceCurrency": "EUR"}
    head = _jsonld(_product(name="Widget One", sku="W-1", offers=offers))

    result = _extract(head, "<main><h1>Widget One</h1><p>From <span>€80</span></p></main>", schema)

    assert _outcomes(result, "price", "price_range") == {
        "price": ("missing", "range_bound", None),
        "price_range": (
            "published",
            None,
            {"min": "80", "max": "120", "currency": "EUR", "unit": None, "period": None},
        ),
    }


def test_an_amount_equal_to_a_bound_of_the_items_price_range_yields_to_the_shown_variants_own() -> None:
    # Resolution spec, "Candidate score": `og:price:amount` 10 is the low bound of the AggregateOffer's 10-30, not the
    # shown variant W-2's 20; the bound stands where the shown variant is the cheapest (W-1).
    def price(shown: str) -> tuple[str, str | None, JsonValue]:
        offers = [
            {"@type": "AggregateOffer", "lowPrice": 10, "highPrice": 30, "priceCurrency": "EUR"},
            _offer("10.00", "https://schema.org/InStock", sku="W-1"),
            _offer("20.00", "https://schema.org/InStock", sku="W-2"),
        ]
        head = _jsonld(_product(name="Widget", sku=shown, offers=offers)) + _og(
            ("og:price:amount", "10.00"), ("og:price:currency", "EUR")
        )
        return _outcomes(_extract(head), "price")["price"]

    assert price("W-2") == ("published", None, {"amount": "20.00", "currency": "EUR"})
    assert price("W-1") == ("published", "offers_disagree", {"amount": "10.00", "currency": "EUR"})


def _cards(path: str, n: int, price: str = "") -> str:
    card = '<li class="card"><img src="/i/{i}.jpg" alt=""><a href="' + path + '">Item {i} name</a>' + price + "</li>"
    return "<ul>" + "".join(card.format(i=i) for i in range(n)) + "</ul>"


def test_a_section_of_another_url_template_is_not_merged_into_the_listing() -> None:
    # The chosen group's item URLs all start with one word; a strip of company pages beside them is another kind.
    company = '<div class="co"><a href="/items-at-company{i}/"><img src="/c/{i}.png" alt="">Company {i}</a></div>'
    body = (
        _cards("/item-{i}0-blue-kettle/", 8, "<span>{i}9.50 EUR</span>")
        + "<section>"
        + "".join(company.format(i=i) for i in range(6))
        + "</section>"
    )

    result = _extract("", body, LISTING)

    assert len(result.rows) == 8


def test_a_listing_price_takes_the_currency_a_page_wide_data_attribute_declares() -> None:
    # HTML Standard §3.2.6.6: `data-currency` is the element's `dataset.currency`, a page-wide currency member.
    body = '<div class="page" data-currency="GBP">' + _cards("/shop/kettle-{i}", 6, "<span>£1{i}.50</span>") + "</div>"

    result = _extract("", body, LISTING)

    assert {json.dumps(row["price"].value) for row in result.rows} >= {
        json.dumps({"amount": "10.50", "currency": "GBP"})
    }


def test_the_main_entity_of_a_page_naming_another_document_is_not_the_page_s_product() -> None:
    # A WebPage naming another document states that page's main entity (schema.org `mainEntity`), not this page's.
    stub = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "url": "https://shop.example.test/other",
        "mainEntity": {"@type": "Product", "name": "Widget Old", "brand": {"@type": "Brand", "name": "Acme"}},
    }

    result = _extract(_jsonld(stub) + _og(("og:title", "Widget New")), "<h1>Widget New</h1>")

    assert _outcomes(result, "title", "brand") == {
        "title": ("published", None, "Widget New"),
        "brand": ("missing", "not_owned", None),
    }


def _variant(n: int, availability: str, **values: Any) -> dict[str, Any]:
    offer = _offer("50.00", f"https://schema.org/{availability}")
    return {"@type": "Product", "sku": f"W-{n}", "url": f"{PAGE_URL}?v={n}", "offers": offer, **values}


def test_a_top_level_value_no_variant_shares_is_a_conflict_and_a_shared_one_fills() -> None:
    # Every variant is out of stock and red; the group's own Offer says in stock and names no colour.
    group = _product(
        **{"@type": "ProductGroup"},
        name="Widget One",
        url=PAGE_URL,
        offers=_offer("50.00", "https://schema.org/InStock"),
        hasVariant=[_variant(1, "OutOfStock", color="Red"), _variant(2, "OutOfStock", color="Red")],
    )

    result = _extract(_jsonld(group), "<h1>Widget One</h1>")

    assert _outcomes(result, "availability", "color", "price") == {
        "availability": ("conflict", "variants_disagree", None),
        "color": ("published", None, "Red"),
        "price": ("published", None, {"amount": "50.00", "currency": "EUR"}),
    }


@pytest.mark.parametrize(
    ("query", "selected"),
    [
        ("?colour=blue", ("W-2", "Blue", "45.00", "OutOfStock")),  # an option value one row states
        ("?item=101", ("W-1", "Red", "40.00", "InStock")),  # an id one row states
        ("?id=W", None),  # a part of every sku: no row is named
        ("?colour=blue&item=101", None),  # two rows named: none selected
        ("?v=2&utm_source=x", ("W-2", "Blue", "45.00", "OutOfStock")),  # a row's own URL, with a pair it lacks added
        ("?w=2&utm_source=x", None),  # pairs no row's URL has all of
        ("?v=1&v=2", None),  # two rows' URLs, each with all its pairs among the request's
    ],
)
def test_the_variant_the_request_url_selects_states_the_top_level_values(
    query: str, selected: tuple[str, str, str, str] | None
) -> None:
    # The group's own Offer states another price; the URL names a variant by one value of its row.
    group = _product(
        **{"@type": "ProductGroup"},
        name="Widget One",
        url=PAGE_URL,
        offers=_offer("50.00", "https://schema.org/InStock"),
        hasVariant=[
            {
                **_variant(1, "InStock", color="Red", productID="101"),
                "offers": _offer("40.00", "https://schema.org/InStock"),
            },
            {
                **_variant(2, "OutOfStock", color="Blue", productID="102"),
                "offers": _offer("45.00", "https://schema.org/OutOfStock"),
            },
        ],
    )

    outcomes = _outcomes(
        _extract(_jsonld(group), "<h1>Widget One</h1>", url=PAGE_URL + query), "sku", "color", "price", "availability"
    )

    values = (outcomes["sku"][2], outcomes["color"][2], outcomes["price"][2], outcomes["availability"][2])
    if selected is None:
        assert values[:2] == (None, None)
    else:
        sku, color, amount, availability = selected
        assert values == (sku, color, {"amount": amount, "currency": "EUR"}, availability)
