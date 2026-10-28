"""Optional `commerce_detail` fields replayed through `extract_page` on synthetic captures: each publishes what the
page's schema.org Product states, by its own `schema_org` path, and nothing the page does not state. A JSON-LD block
with raw control characters inside its strings is read, not lost (RFC 8259 §9). Pages are synthetic (`example.test`,
C3, C9)."""

import asyncio
import json
from pathlib import Path
from typing import Any

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
IMAGES = ["https://shop.example.test/img/1.jpg", "https://shop.example.test/img/2.jpg"]
_PRESET = next(p for p in load_presets() if p.name == "commerce_detail")
SCHEMA = SchemaDef(**_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")

PRODUCT: dict[str, Any] = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Synthetic Widget",
    "url": PAGE_URL,
    "sku": "W-1",
    "mpn": "MPN-77",
    "productID": "isbn-less-42",
    "description": "First line.\nSecond line.",
    "color": "Teal",
    "material": "Cotton",
    "image": IMAGES,
    "itemCondition": "https://schema.org/RefurbishedCondition",
    "audience": {"@type": "PeopleAudience", "suggestedGender": "unisex"},
    "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5", "reviewCount": 128},
    "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR", "availability": "InStock"},
}


def _extract(block: str) -> ExtractionResult:
    raw = f'<html><head><script type="application/ld+json">{block}</script></head></html>'.encode()
    return asyncio.run(extract_page(capture(raw), SCHEMA, None, None, data=DATA))


def _published(result: ExtractionResult) -> dict[str, JsonValue]:
    assert result.entity is not None
    return {name: outcome.value for name, outcome in result.entity.items() if outcome.status == "published"}


def test_each_optional_product_property_the_page_states_publishes_by_its_path() -> None:
    published = _published(_extract(json.dumps(PRODUCT)))

    assert {name: published.get(name) for name in ("description", "color", "material", "gender")} == {
        "description": "First line.\nSecond line.",
        "color": "Teal",
        "material": "Cotton",
        "gender": "unisex",
    }
    assert published.get("additional_images") == IMAGES[1:]  # the main image is image_url alone
    assert published.get("condition") == "RefurbishedCondition"
    assert published.get("product_id") == "isbn-less-42"
    assert published.get("mpn") == "MPN-77"
    assert (published.get("rating"), published.get("review_count")) == ("4.5", 128)


def test_a_property_the_page_does_not_state_is_missing_never_derived() -> None:
    bare = {k: v for k, v in PRODUCT.items() if k not in ("aggregateRating", "color", "description", "image")}
    result = _extract(json.dumps(bare))

    assert result.entity is not None
    for name in ("rating", "review_count", "color", "description", "additional_images"):
        assert (result.entity[name].status, result.entity[name].reason) == ("missing", "no_candidates")
