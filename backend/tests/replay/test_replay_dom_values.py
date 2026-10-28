"""Structural DOM values replayed through `extract_page` on synthetic captures (admitted 2026-10-16): a page without
structured data publishes the brand, sku, availability and main image it shows
in a labelled table and its main region, in the `dom` evidence bucket; where structured data states the same value
the two agree, and where it states another the field resolves as any disagreement does. Pages are synthetic
(`example.test`, C3, C9)."""

import asyncio
import json
from pathlib import Path

from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId, SourceKind
from snowcrawl.resolve.pipeline import EngineData, extract_page, states_offer
from snowcrawl.resolve.preference import PRECEDENCE_TYPES
from snowcrawl.resolve.types import FieldOutcome
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import capture

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
ORDER: tuple[SourceKind, ...] = ("adapter", "api", "jsonld", "microdata", "og", "dom", "config", "llm")
DATA = EngineData(vocabulary=VOCABULARY, orders=dict.fromkeys(PRECEDENCE_TYPES, ORDER), calibration=None)
_PRESET = next(p for p in load_presets() if p.name == "commerce_detail")
SCHEMA = SchemaDef(**_PRESET.model_dump(), id=SchemaId(uuid7()), origin="preset", status="active")

BODY = (
    '<main><h1>Synthetic Widget</h1><img src="/img/widget.jpg" alt="Synthetic Widget">'
    "<table><tr><th>Brand</th><td>Example Brand</td></tr>"
    "<tr><th>SKU</th><td>W-1</td></tr>"
    "<tr><th>Availability</th><td>In stock (3 available)</td></tr></table></main>"
)


def _fields(head: str = "", body: str = BODY) -> dict[str, FieldOutcome]:
    raw = f"<html><head>{head}</head><body>{body}</body></html>".encode()
    result = asyncio.run(extract_page(capture(raw), SCHEMA, None, None, data=DATA))
    assert result.entity is not None
    return dict(result.entity)


def _jsonld(**product: object) -> str:
    block = {"@context": "https://schema.org", "@type": "Product", "name": "Synthetic Widget", **product}
    return f'<script type="application/ld+json">{json.dumps(block)}</script>'


def test_a_page_without_structured_data_publishes_what_its_label_table_and_main_image_show() -> None:
    fields = _fields()

    published = {n: (o.value, o.source_kind, o.evidence) for n, o in fields.items() if o.status == "published"}
    assert {n: published.get(n) for n in ("brand", "sku", "availability", "image_url")} == {
        "brand": ("Example Brand", "dom", "dom"),
        "sku": ("W-1", "dom", "dom"),
        "availability": ("InStock", "dom", "dom"),
        "image_url": ("https://shop.example.test/img/widget.jpg", "dom", "dom"),
    }
    assert fields["brand"].locator == "dom:/html[1]/body[1]/main[1]/table[1]/tbody[1]/tr[1]/td[1]"


def test_a_structured_amount_the_page_shows_struck_is_the_was_price_and_the_shown_price_publishes() -> None:
    # HTML Standard §4.5.4 `s`, §4.7.2 `del`: the struck amount is no longer accurate; structured data stating it is
    # stale, and the one current amount the page shows is the price.
    for tag in ("s", "del"):
        body = f"<main><h1>Synthetic Widget</h1><p><{tag}>$70.00</{tag}> $54.97</p></main>"

        price = _fields(_jsonld(offers={"@type": "Offer", "price": "70.00", "priceCurrency": "USD"}), body)["price"]

        assert (price.value, price.source_kind) == ({"amount": "54.97", "currency": "USD"}, "dom")


def test_a_page_states_an_offer_when_its_extraction_publishes_a_price_or_an_availability() -> None:
    # What the ladder reads before rendering a detail page for its offer (`acquire.rungs._offerless`): the price or the
    # availability the page shows (schema.org Offer `price`, `availability`), the DOM's as well as structured data's.
    og = '<meta property="og:title" content="Synthetic Widget">'
    pages = {
        "<main><h1>Synthetic Widget</h1><p>$54.97</p></main>": True,
        BODY: True,  # "In stock", no price
        "<main><h1>Synthetic Widget</h1><p>A widget.</p></main>": False,
    }
    for body, offered in pages.items():
        raw = f"<html><head>{og}</head><body>{body}</body></html>".encode()

        assert states_offer(capture(raw), SCHEMA, None, data=DATA) is offered, body


def test_a_page_whose_variants_alone_state_their_prices_states_an_offer() -> None:
    variant = {
        "@type": "Product",
        "sku": "W-{0}",
        "offers": {"@type": "Offer", "price": "{0}.00", "priceCurrency": "USD"},
    }
    group = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Synthetic Widget",
        "hasVariant": [json.loads(json.dumps(variant).replace("{0}", n)) for n in ("10", "12")],
    }
    raw = f'<html><body><script type="application/ld+json">{json.dumps(group)}</script></body></html>'.encode()
    fields = asyncio.run(extract_page(capture(raw), SCHEMA, None, None, data=DATA)).entity
    assert fields is not None

    # The rows state the prices the item as a whole does not (schema.org ProductGroup `hasVariant`).
    assert (fields["price"].status != "published", fields["variants"].status) == (True, "published")
    assert states_offer(capture(raw), SCHEMA, None, data=DATA) is True
