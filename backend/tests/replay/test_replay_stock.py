"""Availability and stock quantity replayed through `extract_page` on synthetic captures: an
Offer that writes its availability in words publishes the schema.org member its first clause names, or declines;
`inventoryLevel` publishes as the count `stock_quantity` of preset `commerce_detail` version 4. Pages are synthetic
(`example.test`, C3, C9)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId, SourceKind
from snowcrawl.resolve.pipeline import EngineData, extract_page
from snowcrawl.resolve.preference import PRECEDENCE_TYPES
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import PAGE_URL, capture

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "config", "llm")
DATA = EngineData(vocabulary=VOCABULARY, orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER), calibration=None)
_PRESET = next(p for p in load_presets() if p.name == "commerce_detail")
SCHEMA = SchemaDef(**_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")


def _fields(offer: dict[str, Any]) -> dict[str, tuple[str, str | None, JsonValue]]:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Synthetic Widget",
        "url": PAGE_URL,
        "sku": "W-1",
        "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR"} | offer,
    }
    raw = f'<html><head><script type="application/ld+json">{json.dumps(product)}</script></head></html>'.encode()
    result = asyncio.run(extract_page(capture(raw), SCHEMA, None, None, data=DATA))
    assert result.entity is not None
    outcomes = {name: result.entity[name] for name in ("availability", "stock_quantity")}
    return {name: (o.status, o.reason, o.value) for name, o in outcomes.items()}


@pytest.mark.parametrize(
    ("offer", "availability", "stock"),
    [
        ({"availability": "In stock (5 available)", "inventoryLevel": "5"}, ("published", None, "InStock"), 5),
    ],
)
def test_an_offer_s_availability_and_inventory_level_publish_as_two_fields(
    offer: dict[str, Any], availability: tuple[str, None, JsonValue], stock: int | None
) -> None:
    fields = _fields(offer)

    assert fields["availability"] == availability
    published = ("published", None, stock)
    assert fields["stock_quantity"] == (published if stock is not None else ("missing", "no_candidates", None))


@pytest.mark.parametrize("written", ["Not in stock"])
def test_availability_text_that_qualifies_or_negates_a_member_publishes_as_stated_never_as_one(written: str) -> None:
    assert _fields({"availability": written})["availability"] == ("published", None, written)


def test_each_source_s_one_offer_is_the_item_s_offer() -> None:
    # Two sources state the product and one Offer each: the microdata Offer names more fields, the JSON-LD one alone
    # states the availability, which is that one offer's, not another variant's.
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Synthetic Widget",
        "url": PAGE_URL,
        "sku": "W-1",
        "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR", "availability": "PreOrder"},
    }
    microdata = (
        '<div itemscope itemtype="https://schema.org/Product"><span itemprop="sku">W-1</span>'
        f'<link itemprop="url" href="{PAGE_URL}"><div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<meta itemprop="sku" content="W-1"><meta itemprop="gtin" content="4006381333931">'
        '<meta itemprop="price" content="10.00"><meta itemprop="priceCurrency" content="EUR"></div></div>'
    )
    raw = f'<html><head><script type="application/ld+json">{json.dumps(product)}</script></head>'
    raw += f"<body>{microdata}</body></html>"

    result = asyncio.run(extract_page(capture(raw.encode()), SCHEMA, None, None, data=DATA))

    assert result.entity is not None
    assert (result.entity["availability"].status, result.entity["availability"].value) == ("published", "PreOrder")


def test_the_shown_variant_s_availability_publishes_over_other_variants_sold_out() -> None:
    # The JSON-LD Product states one Offer, the page's (its availability written as a network-path reference); the
    # microdata lists every variant's Offer, the shown one by the Product's sku and stating no availability. The other
    # variant's OutOfStock is its own, not the item's.
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Synthetic Widget",
        "url": PAGE_URL,
        "sku": "W-2",
        "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR", "availability": "//schema.org/InStock"},
    }
    microdata = "".join(
        f'<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="sku" content="{sku}">'
        f"{state}</div>"
        for sku, state in (("W-1", '<link itemprop="availability" href="https://schema.org/OutOfStock">'), ("W-2", ""))
    )
    raw = f'<html><head><script type="application/ld+json">{json.dumps(product)}</script></head><body>'
    raw += '<div itemscope itemtype="https://schema.org/Product"><span itemprop="sku">W-2</span>'
    raw += f'<link itemprop="url" href="{PAGE_URL}">'
    raw += f"{microdata}</div></body></html>"

    result = asyncio.run(extract_page(capture(raw.encode()), SCHEMA, None, None, data=DATA))

    assert result.entity is not None
    availability = result.entity["availability"]
    assert (availability.status, availability.reason, availability.value) == ("published", "offers_disagree", "InStock")


def test_a_lone_offer_naming_another_variant_is_not_the_shown_one() -> None:
    # The JSON-LD Product's one Offer states sku W-1 and OutOfStock; the microdata Offer of the Product's sku W-2 states
    # InStock: the lone Offer names another variant, so W-2's availability is the item's.
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Synthetic Widget",
        "url": PAGE_URL,
        "sku": "W-2",
        "offers": {"@type": "Offer", "sku": "W-1", "availability": "https://schema.org/OutOfStock"},
    }
    raw = f'<html><head><script type="application/ld+json">{json.dumps(product)}</script></head><body>'
    raw += '<div itemscope itemtype="https://schema.org/Product"><span itemprop="sku">W-2</span>'
    raw += (
        f'<link itemprop="url" href="{PAGE_URL}"><div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
    )
    raw += '<meta itemprop="sku" content="W-2"><link itemprop="availability" href="https://schema.org/InStock">'
    raw += "</div></div></body></html>"

    result = asyncio.run(extract_page(capture(raw.encode()), SCHEMA, None, None, data=DATA))

    assert result.entity is not None
    assert (result.entity["availability"].status, result.entity["availability"].value) == ("published", "InStock")
