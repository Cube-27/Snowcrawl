"""A detail page's own variants and images replayed through `extract_page` on synthetic captures (own variants,
renditions): a `hasVariant` member that states only a URL, or whose URL
names another page, is no variant; one image file is one image, its largest rendition. Pages are synthetic
(`example.test`, C3, C9)."""

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
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import PAGE_URL, capture

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "config", "llm")
DATA = EngineData(vocabulary=VOCABULARY, orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER), calibration=None)
_PRESET = next(p for p in load_presets() if p.name == "commerce_detail")
SCHEMA = SchemaDef(**_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")
IMAGE = "https://cdn.example.test/files/widget.png?v=7&width="


def _variant(sku: str, size: str, url: str) -> dict[str, Any]:
    offer = {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"}
    return {"@type": "Product", "sku": sku, "size": size, "url": url, "offers": offer}


def _entity(group: dict[str, Any], head: str = "") -> dict[str, JsonValue]:
    node = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "Synthetic Widget"} | group
    raw = f'<html><head>{head}<script type="application/ld+json">{json.dumps(node)}</script></head></html>'.encode()
    result = asyncio.run(extract_page(capture(raw), SCHEMA, None, None, data=DATA))
    assert result.entity is not None
    return {name: outcome.value for name, outcome in result.entity.items()}


def test_only_members_that_state_more_than_a_url_of_this_page_are_variants() -> None:
    sibling = "https://shop.example.test/p/2"
    own = [_variant("W-1-S", "S", f"{PAGE_URL}?size=S"), _variant("W-1-M", "M", f"{PAGE_URL}?size=M")]
    # A member that states no URL of its own is the group's variant, wherever its Offer is offered.
    offered = {key: value for key, value in _variant("W-1-L", "L", "").items() if key != "url"}
    offered["offers"] |= {"url": f"{PAGE_URL}-L"}
    links = [{"@type": "Product", "url": f"{sibling}?size={size}"} for size in ("S", "M")]
    other = _variant("W-2-S", "S", f"{sibling}?size=S")

    entity = _entity({"url": PAGE_URL, "hasVariant": [*links, *own, offered, other, *links]})

    variants = entity["variants"]
    assert isinstance(variants, list)
    assert [(v["sku"], v["url"]) for v in variants if isinstance(v, dict)] == [
        ("W-1-S", f"{PAGE_URL}?size=S"),
        ("W-1-M", f"{PAGE_URL}?size=M"),
        ("W-1-L", f"{PAGE_URL}-L"),
    ]


def test_members_stating_only_a_platform_variant_url_of_this_page_are_variants() -> None:
    # A platform that addresses a variant by its page URL's query (adapters/platforms.py: Shopify's `variant`) makes
    # a member stating only such a URL a variant; on a page of no such platform it is a link and is dropped.
    members = [{"@type": "Product", "url": f"{PAGE_URL}?variant={n}"} for n in (1, 2)]
    shop = '<script id="shopify-features" type="application/json">{}</script>'  # the platform's own element

    on_platform = _entity({"url": PAGE_URL, "hasVariant": members}, shop)["variants"]
    elsewhere = _entity({"url": PAGE_URL, "hasVariant": members}).get("variants")

    assert isinstance(on_platform, list)
    assert [v["url"] for v in on_platform if isinstance(v, dict)] == [f"{PAGE_URL}?variant={n}" for n in (1, 2)]
    assert elsewhere is None


def test_an_image_stated_in_several_widths_is_one_image_its_widest() -> None:
    entity = _entity({"url": PAGE_URL, "sku": "W-1", "image": [f"{IMAGE}{w}" for w in (100, 900, 300)]})

    assert entity["image_url"] == f"{IMAGE}900"
    assert entity["additional_images"] is None  # its one file is the main image
