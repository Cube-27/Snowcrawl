"""Data calls: a platform's documented product JSON joins the page only when it states
the requested product, and is then read as structured evidence with its documented minor unit; another product's
answer is refused, as a detail API recipe's is. The fetch path is grounded at runtime; this covers the identity check,
the unit and the platform's declared currency on any product key and amount, and the variant rows."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.acquire.identity import NOT_THE_PRODUCT, identified, joined
from snowcrawl.acquire.types import FetchResult
from snowcrawl.collect.base import Entity
from snowcrawl.collect.currencies import with_page_declarations
from snowcrawl.collect.jsonld import JsonLdCollector
from snowcrawl.collect.platform_json import PlatformJsonCollector
from snowcrawl.collect.run import page_urls
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.capture import CaptureMethod
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.resolve.intent import primary_types, select_target
from snowcrawl.resolve.ownership import EntityGraph
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import SchemaDef
from tests.support.captures import calls, fetched

HOST = "https://shop.example.test"
KEYS = st.from_regex(r"[a-z][a-z0-9]{2,8}-[a-z0-9]{2,8}", fullmatch=True)
CENTS = st.integers(min_value=1, max_value=10**9)
VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
SCHEMA = next(
    SchemaDef(id=SchemaId(uuid7()), origin="preset", status="active", **d.model_dump())
    for d in load_presets()
    if d.name == "commerce_detail"
)


def _capture(url: str, method: CaptureMethod, body: bytes, answers: tuple[tuple[str, bytes], ...] = ()) -> FetchResult:
    headers = (("content-type", "application/json"),)
    return fetched(url, 200, body, headers, method=method, network=calls(answers, method="GET"))


def _product(key: str, cents: int = 2900) -> bytes:
    # Shopify Ajax API product (its documentation, "Product"): one variant, its price in cents for every currency.
    variant = {"id": 1, "title": "Default", "sku": f"{key}-1", "barcode": "4006381333931", "price": cents}
    product = {"handle": key, "title": "Item", "url": f"/products/{key}", "variants": [{**variant, "available": False}]}
    images = [f"//cdn.example.test/files/{key}-{n}.jpg" for n in (1, 2)]
    return json.dumps({**product, "description": "<p>A <b>mug</b>.</p>", "images": images}).encode()


# An Online Store page: the platform's features script and its presentment currency (`content_for_header`).
_PAGE = b'<html><script id="shopify-features"></script><script>Shopify.currency = {"active":"CAD"};</script></html>'


@given(KEYS, KEYS, CENTS)
def test_a_documented_answer_joins_only_for_its_product_and_reads_in_major_units(
    key: str, other: str, cents: int
) -> None:
    page = f"{HOST}/products/{key}"
    http = _capture(page, "http", _PAGE)

    served, attempt = joined(http, _capture(f"{page}.js", "api", _product(key, cents)), key, "shopify")

    assert served is not None
    assert attempt.attempts[-1].error is None
    assert served.bundle.manifest.network_bodies[-1].data_call == "shopify"
    parsed = ParsedPage.of(served.bundle)
    ctx = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC), html_lang="en-US", page_url=page, base_url=page)
    read = PlatformJsonCollector(VOCABULARY).collect(parsed, SCHEMA, with_page_declarations(ctx, parsed))
    prices = [c.normalized for c in read.candidates if c.field == "price"]
    assert len(prices) == 1
    assert isinstance(prices[0], Ok)
    # The amount in major units, in the currency the page's `Shopify.currency.active` declares.
    assert prices[0].value == {"amount": format(Decimal(cents).scaleb(-2), "f"), "currency": "CAD"}
    # The variant's own identifiers fill a `variants` row.
    row = {c.field: c.normalized for c in read.candidates if c.field.startswith("variants.")}
    assert row["variants.sku"] == Ok(f"{key}-1")
    assert row["variants.gtin"] == Ok("4006381333931")
    # `available: false` is OutOfStock; `true` names no one ItemAvailability (pre-order and back order can be bought).
    assert row["variants.availability"] == Ok("OutOfStock")
    # The product's every image, the featured one first, and its HTML body read as text.
    files: list[JsonValue] = [f"https://cdn.example.test/files/{key}-{n}.jpg" for n in (1, 2)]
    # The answer writes them scheme-relative; they publish resolved, their canonical form.
    assert [c.normalized for c in read.candidates if c.field == "additional_images"] == [Ok(files)]
    assert [c.normalized for c in read.candidates if c.field == "description"] == [Ok("A mug.")]
    if other != key:
        # Another product's answer (an endpoint that ignores its key) never joins, and says why.
        refused, attempt = joined(http, _capture(f"{page}.js", "api", _product(other)), key, "shopify")
        assert refused is None
        assert (attempt.block_class, attempt.attempts[-1].error) == ("error", NOT_THE_PRODUCT)
        # A detail API recipe's answer is checked the same way.
        recipe = identified(_capture(f"{HOST}/api/p", "api", _product(other)), page, (key,))
        assert recipe.attempts[-1].error == NOT_THE_PRODUCT


# How an HTML answer states its item: in a text node or an attribute value (HTML Standard §13.2), or by naming the
# item's page in a relative link (HTML Standard §4.6), its canonical link (RFC 6596) or its og:url (Open Graph), each
# resolved against the answer's URL (RFC 3986 §5).
_STATED = {
    "text": "<p>{key}</p>",
    "attribute": "<div data-item='{key}'></div>",
    "link": "<a href='../item/{key}'>More</a>",
    "canonical": "<link rel='canonical' href='/item/{key}/'>",
    "og": "<meta property='og:url' content='../item/{key}'>",
}


@given(KEYS, KEYS, st.sampled_from(sorted(_STATED)))
def test_an_html_answer_is_kept_only_when_it_states_the_requested_item(key: str, other: str, how: str) -> None:
    body = f"<html><body><h1>Item</h1>{_STATED[how].format(key=key)}</body></html>".encode()
    answer = f"{HOST}/fragments/item"

    kept = identified(fetched(answer, 200, body, method="api"), f"{HOST}/item/{key}", (key,), html=True)
    assert (kept.block_class, kept.attempts[-1].error) == ("ok", None)
    if other.casefold() != key.casefold():
        refused = identified(fetched(answer, 200, body, method="api"), f"{HOST}/item/{other}", (other,), html=True)
        assert refused.attempts[-1].error == NOT_THE_PRODUCT


def test_a_documented_answer_is_the_page_products_own_entity() -> None:
    # The answer's Product names its sku and barcode on its variants only, and no IRI: it passed the identity check,
    # so it is the page's one JSON-LD Product, and the page has one target holding both sources' values.
    page = f"{HOST}/products/blue-mug"
    markup = {"@context": "https://schema.org", "@type": "Product", "name": "Blue Mug", "description": "A blue mug."}
    html = f'<html><script type="application/ld+json">{json.dumps(markup)}</script></html>'.encode()
    served, _ = joined(
        _capture(page, "http", html), _capture(f"{page}.js", "api", _product("blue-mug")), "blue-mug", "shopify"
    )
    assert served is not None
    parsed = ParsedPage.of(served.bundle)
    ctx = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC), html_lang="en-US", page_url=page, base_url=page)
    reads = [c.collect(parsed, SCHEMA, ctx) for c in (JsonLdCollector(VOCABULARY), PlatformJsonCollector(VOCABULARY))]
    graph = EntityGraph.of((e for r in reads for e in r.entities), VOCABULARY, (page,))

    target = select_target(page, page, None, graph, primary_types(SCHEMA, VOCABULARY))

    assert isinstance(target, Entity)
    owned = {c.field for c in graph.adopt(c for r in reads for c in r.candidates) if graph.owned_by(c, target)}
    assert {"description", "price", "variants.sku"} <= owned
    # A sole root Product whose URL is another page's is another item: the answer stays apart from it.
    elsewhere = Entity(id="jsonld:0:$", types=("Product",), urls=(), parent=None, item_urls=(f"{HOST}/products/other",))
    assert not EntityGraph.of((elsewhere, *reads[1].entities), VOCABULARY, (page,)).aliases
    # Unless the page declares that URL as its version in another language (HTML `link rel=alternate hreflang`).
    regional = f"{HOST}/en-nz/products/blue-mug"
    # A feed in that language is another resource, not a version of the page.
    feed = f'<link rel="alternate" hreflang="en" type="application/rss+xml" href="{HOST}/feed">'
    linked = f'<html><head><link rel="alternate" hreflang="en-NZ" href="{regional}">{feed}</head></html>'.encode()
    declared = page_urls(ParsedPage.of(_capture(page, "http", linked).bundle), served.bundle.manifest)
    assert declared == (page, regional)
    sold_there = elsewhere.model_copy(update={"item_urls": (regional,)})
    assert EntityGraph.of((sold_there, *reads[1].entities), VOCABULARY, declared).aliases
    # A comparison table's microdata Products (one shape, repeated) are other items, not more roots of the page's own.
    table = (
        Entity(id=f"microdata:/html[1]/body[1]/table[1]/tr[1]/th[{n}]", types=("Product",), urls=(), parent=None)
        for n in (1, 2, 3)
    )
    beside = EntityGraph.of((*reads[0].entities, *table, *reads[1].entities), VOCABULARY, (page,))
    assert set(beside.aliases.values()) == {reads[0].entities[0].id}


def test_a_variant_s_page_reads_its_own_images_and_the_product_s() -> None:
    # Each variant features its own image (Shopify Ajax API `featured_image`); the image no variant features is the
    # product's. The page whose `?variant=` names the second variant shows its image first, then the product's.
    page = f"{HOST}/products/mug"
    variants = [
        {"id": n, "sku": f"mug-{n}", "available": True, "featured_image": {"src": f"https://cdn.example.test/{n}.jpg"}}
        for n in (1, 2)
    ]
    images = ["//cdn.example.test/both.jpg", "//cdn.example.test/1.jpg", "//cdn.example.test/2.jpg"]
    answer = json.dumps({"handle": "mug", "url": "/products/mug", "variants": variants, "images": images}).encode()
    served, _ = joined(_capture(page, "http", _PAGE), _capture(f"{page}.js", "api", answer), "mug", "shopify")
    assert served is not None
    parsed = ParsedPage.of(served.bundle)
    shown = f"{page}?variant=2"
    ctx = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC), html_lang="en-US", page_url=shown, base_url=shown)

    read = PlatformJsonCollector(VOCABULARY).collect(parsed, SCHEMA, ctx)

    lists = [c.normalized for c in read.candidates if c.field == "additional_images"]
    kept: list[JsonValue] = ["https://cdn.example.test/2.jpg", "https://cdn.example.test/both.jpg"]
    assert lists == [Ok(kept)]
