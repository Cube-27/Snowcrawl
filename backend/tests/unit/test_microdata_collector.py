"""The microdata collector: HTML Standard microdata items and RDFa Lite resources as entities, nested
scopes as nested entities, and a candidate for each `schema_org` path."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowcrawl.collect.base import Candidate, CollectResult, Entity
from snowcrawl.collect.microdata import MicrodataCollector
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
PAGE_URL = "https://shop.example.test/p/1"
CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url=PAGE_URL, page_url=PAGE_URL)
BODY = "/html[1]/body[1]"


def _text(path: str) -> FieldSpec:
    return FieldSpec(type=FieldType.TEXT, description="d", schema_org=path)


SCHEMA = SchemaDef(
    id=SchemaId(uuid7()),
    origin="user",
    status="active",
    name="synthetic",
    page_kind="detail",
    fields={
        "title": _text("Product.name"),
        "brand": _text("Product.brand.name"),
        "price": FieldSpec(type=FieldType.MONEY, description="d", schema_org="Offer.price+priceCurrency"),
        "images": FieldSpec(
            type=FieldType.LIST, description="d", item=FieldSpec(type=FieldType.IMAGE_URL), schema_org="Product.image"
        ),
        "note": FieldSpec(type=FieldType.TEXT, description="d"),  # no schema_org path: no candidates
    },
)


def _collect(body: str, schema: SchemaDef = SCHEMA) -> CollectResult:
    page = ParsedPage(f"<!doctype html><html><head></head><body>{body}</body></html>")
    return MicrodataCollector(VOCABULARY).collect(page, schema, CTX)


def _candidate(field: str, entity_id: str, raw: Any, normalized: Any, locator: str) -> Candidate:
    return Candidate(
        field=field,
        entity_id=entity_id,
        source_kind="microdata",
        raw=raw,
        normalized=normalized,
        locator=locator,
        independent=True,
    )


def test_a_product_with_nested_items() -> None:
    result = _collect(
        '<div itemscope itemtype="https://schema.org/Product">'
        '<h1 itemprop="name">Widget 9</h1>'
        '<link itemprop="url" href="/p/1">'
        '<div itemprop="brand" itemscope itemtype="https://schema.org/Brand"><span itemprop="name">Acme 7</span></div>'
        '<img itemprop="image" src="/a.png"><img itemprop="image" src="https://shop.example.test/b.png">'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<span itemprop="price">10.50</span><meta itemprop="priceCurrency" content="EUR">'
        "</div></div>"
    )
    product, brand, offer = (
        f"microdata:{BODY}/div[1]",
        f"microdata:{BODY}/div[1]/div[1]",
        f"microdata:{BODY}/div[1]/div[2]",
    )
    assert result.entities == (
        Entity(
            id=product, types=("Product",), urls=(PAGE_URL,), parent=None, item_urls=(PAGE_URL,), names=("Widget 9",)
        ),
        Entity(id=brand, types=("Brand",), urls=(), parent=product, via="brand", names=("Acme 7",)),
        Entity(id=offer, types=("Offer",), urls=(), parent=product, via="offers"),
    )
    assert result.candidates == (
        _candidate("title", product, "Widget 9", Ok("Widget 9"), f"{product}/h1[1]"),
        _candidate("brand", product, "Acme 7", Ok("Acme 7"), f"{brand}/span[1]"),
        _candidate(
            "price",
            offer,
            {"price": "10.50", "priceCurrency": "EUR"},
            Ok({"amount": "10.50", "currency": "EUR"}),
            f"{offer}/span[1]",
        ),
        _candidate(
            "images",
            product,
            ["/a.png", "https://shop.example.test/b.png"],
            Ok(["https://shop.example.test/a.png", "https://shop.example.test/b.png"]),
            f"{product}/img[1]",
        ),
    )
    assert result.diagnostics == ()
