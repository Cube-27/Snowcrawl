"""The OpenGraph collector: `og:*` and `product:*` meta properties mapped to schema fields through the
fixed OGP table, every candidate owned by the `page` entity."""

from datetime import UTC, datetime
from html import escape

import pytest
from pydantic import JsonValue

from snowcrawl.collect.base import Candidate, CollectResult, Entity
from snowcrawl.collect.og import OpenGraphCollector
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

PAGE_URL = "https://shop.example.test/p/1"
CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url=PAGE_URL, page_url=PAGE_URL)


def _schema(**fields: FieldSpec) -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind="detail",
        fields=fields,
    )


def _field(type_: FieldType, path: str | None, **params: object) -> FieldSpec:
    return FieldSpec.model_validate({"type": type_, "description": "d", "schema_org": path, **params})


SCHEMA = _schema(
    title=_field(FieldType.TEXT, "Product.name"),
    brand=_field(FieldType.TEXT, "Product.brand.name"),
    price=_field(FieldType.MONEY, "Offer.price+priceCurrency"),
    availability=_field(FieldType.ENUM, "Offer.availability", vocab="schema:ItemAvailability"),
    url=_field(FieldType.URL, "Product.url"),
    image_url=_field(FieldType.IMAGE_URL, "Product.image"),
    note=_field(FieldType.TEXT, None),
)


def _page(*metas: tuple[str, str]) -> ParsedPage:
    tags = "".join(f'<meta property="{escape(p)}" content="{escape(c)}">' for p, c in metas)
    return ParsedPage(f"<html><head>{tags}</head><body><p>body</p></body></html>")


def _collect(*metas: tuple[str, str], schema: SchemaDef = SCHEMA) -> CollectResult:
    return OpenGraphCollector().collect(_page(*metas), schema, CTX)


def _og(field: str, raw: object, normalized: object, locator: str) -> Candidate:
    return Candidate.model_validate(
        {
            "field": field,
            "entity_id": "page",
            "source_kind": "og",
            "raw": raw,
            "normalized": normalized,
            "locator": locator,
            "independent": True,
        }
    )


def test_each_ogp_property_maps_to_the_field_of_its_role() -> None:
    result = _collect(
        ("og:title", "Synthetic Widget"),
        ("og:url", "https://shop.example.test/p/1?ref=x"),
        ("og:image", "/img/widget.png"),
        ("product:price:amount", "19.99"),
        ("product:price:currency", "EUR"),
        ("product:availability", "InStock"),
    )

    assert result == CollectResult(
        entities=(Entity(id="page", types=("page",), urls=("https://shop.example.test/p/1?ref=x",), parent=None),),
        candidates=(
            _og("title", "Synthetic Widget", Ok("Synthetic Widget"), "meta:og:title"),
            _og(
                "price",
                {"amount": "19.99", "currency": "EUR"},
                Ok({"amount": "19.99", "currency": "EUR"}),
                "meta:product:price:amount",
            ),
            _og("availability", "InStock", Ok("InStock"), "meta:product:availability"),
            _og("url", "https://shop.example.test/p/1?ref=x", Ok("https://shop.example.test/p/1?ref=x"), "meta:og:url"),
            _og(
                "image_url",
                "/img/widget.png",
                Ok("https://shop.example.test/img/widget.png"),
                "meta:og:image",
            ),
        ),
    )


DESCRIBED = _schema(
    **SCHEMA.fields,
    description=_field(FieldType.LONG_TEXT, "Product.description"),
    summary=_field(FieldType.TEXT, "Product.description"),
)


def test_a_field_maps_only_when_its_type_matches_the_role() -> None:
    schema = _schema(
        title=_field(FieldType.TEXT, "Product.name"),
        name_as_url=_field(FieldType.URL, "Product.name"),
        image_as_text=_field(FieldType.TEXT, "Product.image"),
    )

    result = _collect(("og:title", "Synthetic Widget"), ("og:image", "https://shop.example.test/a.png"), schema=schema)

    assert result.candidates == (_og("title", "Synthetic Widget", Ok("Synthetic Widget"), "meta:og:title"),)


def test_one_stated_image_is_no_list_of_images() -> None:
    # OGP "Arrays": a list reads a repeated property; one image (written as `og:image` and `og:image:url`) is not one.
    schema = _schema(images=_field(FieldType.LIST, "Product.image", item={"type": "image_url"}))

    def images(*files: tuple[str, str]) -> list[JsonValue]:
        return [c.raw for c in _collect(*files, schema=schema).candidates]

    assert images(("og:image", "/a.jpg"), ("og:image:url", "/a.jpg")) == []
    assert images(("og:image", "/a.jpg"), ("og:image", "/b.jpg")) == [["/a.jpg", "/b.jpg"]]


def test_a_price_without_a_currency_publishes_its_amount_with_the_currency_unknown() -> None:
    # Normalization spec, money: a grounded amount publishes; no currency is filled in (C7).
    result = _collect(("product:price:amount", "19.99"))

    assert result.candidates == (
        _og(
            "price",
            {"amount": "19.99"},
            Ok({"amount": "19.99", "currency": None}, basis="unknown"),
            "meta:product:price:amount",
        ),
    )


def test_og_url_names_the_page_entity_even_without_a_url_field() -> None:
    schema = _schema(title=_field(FieldType.TEXT, "Product.name"))

    result = _collect(("og:url", "HTTPS://Shop.Example.TEST/p/1"), schema=schema)

    assert result == CollectResult(
        entities=(Entity(id="page", types=("page",), urls=("https://shop.example.test/p/1",), parent=None),),
        candidates=(),
    )


def test_an_og_title_naming_the_site_is_no_entity_value() -> None:
    # OGP: `og:site_name` is the name of the overall site. An `og:title` equal to it (or to a separator run of it)
    # describes the site, and so does the `og:image` beside it; `og:url` still names the page.
    for site_name in ("Synthetic Tools", "Synthetic Tools - Workshop", "SYNTHETIC tools"):
        result = _collect(
            ("og:site_name", site_name),
            ("og:title", "Synthetic Tools"),
            ("og:image", "https://shop.example.test/banner.png"),
            ("og:description", "Tools for every workshop."),
            ("og:url", PAGE_URL),
            schema=DESCRIBED,
        )

        assert [c.field for c in result.candidates] == ["url"]
        assert result.diagnostics == (
            "og: og:title names the site, so og:title, og:image and og:description are not read",
        )


def test_an_og_description_naming_the_site_is_the_sites_summary() -> None:
    # OGP: `og:description` describes the object and `og:site_name` names the overall site; a description naming the
    # site (its `og:site_name`, else the title's site segment) is the site's summary, unless the item's name holds it.
    def description(*metas: tuple[str, str]) -> list[str]:
        result = _collect(("og:title", "Steel Kettle 2L | Synthetic Tools"), *metas, schema=DESCRIBED)
        return [str(c.raw) for c in result.candidates if c.field == "description"]

    summary = ("og:description", "Shop kettles online at Synthetic Tools.")
    own = ("og:description", "A steel kettle that boils a litre in two minutes.")
    assert description(("og:site_name", "Synthetic Tools"), own) == [own[1]]
    assert description(("og:site_name", "Synthetic Tools"), summary) == []

    # Without `og:site_name`, the title's trailing segment spelling the host's label names the site.
    def titled_metas() -> tuple[tuple[str, str], ...]:
        return (
            ("og:title", "Steel Kettle 2L | Example Store"),
            ("og:description", "Shop kettles online at Example Store."),
        )

    titled = _collect(*titled_metas(), schema=DESCRIBED)
    assert [c.field for c in titled.candidates] == ["title"]
    # A blank `og:site_name` names nothing, so the title's site segment still does.
    assert [c.field for c in _collect(("og:site_name", " "), *titled_metas(), schema=DESCRIBED).candidates] == ["title"]
    # Each repeated description is judged on its own: the item's own one after a site summary is read.
    assert description(("og:site_name", "Synthetic Tools"), summary, own) == [own[1]]
    branded = _collect(
        ("og:site_name", "Acme"),
        ("og:title", "Acme Steel Kettle"),
        ("og:description", "The Acme kettle boils a litre fast."),
        schema=DESCRIBED,
    )
    assert [c.field for c in branded.candidates] == ["title", "description"]


def test_a_sale_price_is_the_price_and_the_price_beside_it_the_list_price() -> None:
    # The `product:` namespace: the sale price is the price paid, `product:price` the price it was reduced from.
    list_price = "Offer.priceSpecification{priceType=StrikethroughPrice}.price+priceCurrency"
    schema = _schema(price=SCHEMA.fields["price"], list_price=_field(FieldType.MONEY, list_price))
    sale: JsonValue = {"amount": "15.00", "currency": "EUR"}
    before: JsonValue = {"amount": "19.99", "currency": "EUR"}

    result = _collect(
        ("product:price:amount", "19.99"),
        ("product:price:currency", "EUR"),
        ("product:sale_price:amount", "15.00"),
        ("product:sale_price:currency", "EUR"),
        schema=schema,
    )

    assert result.candidates == (
        _og("price", sale, Ok(sale), "meta:product:sale_price:amount"),
        _og("list_price", before, Ok(before), "meta:product:price:amount"),
    )


@pytest.mark.parametrize(
    ("end", "on_sale"),
    [("2026-09-29", False), ("2026-09-30T23:00Z", False), ("2026-09-30", True), ("2026-10-02T00:00Z", True)],
)
def test_a_sale_that_ended_before_the_capture_is_no_price(end: str, on_sale: bool) -> None:
    # `product:sale_price_dates:end`: after it the sale price is gone and `product:price` is the price again.
    schema = _schema(price=SCHEMA.fields["price"])

    result = _collect(
        ("product:price:amount", "19.99"),
        ("product:sale_price:amount", "15.00"),
        ("product:sale_price_dates:end", end),
        schema=schema,
    )

    assert [c.raw for c in result.candidates] == [{"amount": "15.00" if on_sale else "19.99"}]


@pytest.mark.parametrize("path", ["Product.offers.seller.url", "Product.brand.image"])
def test_a_path_through_another_thing_reads_none_of_the_pages_basic_metadata(path: str) -> None:
    # og:url and og:image describe the page's object, not its seller or its brand.
    field_type = FieldType.URL if path.endswith("url") else FieldType.IMAGE_URL
    schema = _schema(other=_field(field_type, path))

    result = _collect(("og:url", PAGE_URL), ("og:image", "https://shop.example.test/a.jpg"), schema=schema)

    assert result.candidates == ()


def test_an_og_title_holding_the_page_urls_slug_reads_as_the_slugs_words() -> None:
    # RFC 3986 §3.3: a path segment spelling the title's words as a slug names the item; the rest is the site's wording.
    page = _page(("og:title", "Buy Crème Kettle 2L for Kitchens Online, Shop at Synthetic - 12345"))
    ctx = Context(captured_at=CTX.captured_at, page_url="https://shop.example.test/creme-kettle-2l/p/12345")

    (title,) = [c for c in OpenGraphCollector().collect(page, SCHEMA, ctx).candidates if c.field == "title"]
    assert title.normalized == Ok("Crème Kettle 2L")
    # A slug the title does not hold (a fixed segment naming the page's kind) says nothing; an earlier one names it.
    detail = Context(captured_at=CTX.captured_at, page_url="https://shop.example.test/creme-kettle-2l/12345/item-page")
    (title,) = [c for c in OpenGraphCollector().collect(page, SCHEMA, detail).candidates if c.field == "title"]
    assert title.normalized == Ok("Crème Kettle 2L")
    # Only the last slug segment the title holds names the item: a category segment earlier in the path is not its
    # name.
    category = Context(captured_at=CTX.captured_at, page_url="https://shop.example.test/kitchen-tools/acme-kettle")
    page = _page(("og:title", "Acme Kettle for Kitchen Tools"))
    (title,) = [c for c in OpenGraphCollector().collect(page, SCHEMA, category).candidates if c.field == "title"]
    assert title.normalized == Ok("Acme Kettle for Kitchen Tools")
    # A title that starts with the slug's words is read as written, a later repeat of them aside.
    page = _page(("og:title", "Acme Kettle Deluxe - Buy Acme Kettle Online"))
    (title,) = [c for c in OpenGraphCollector().collect(page, SCHEMA, category).candidates if c.field == "title"]
    assert title.normalized == Ok("Acme Kettle Deluxe - Buy Acme Kettle Online")


def test_an_og_title_holding_the_pages_top_level_heading_reads_as_the_heading() -> None:
    # HTML Standard §4.3.6: the one `h1` is the document's title as the page shows it; the words around it in og:title
    # are the site's wording.
    def title(heading: str) -> object:
        page = ParsedPage(
            '<html><head><meta property="og:title" content="Buy Acme Kettle Deluxe 2L Online at Synthetic Shop">'
            f"</head><body>{heading}</body></html>"
        )
        (found,) = [c for c in OpenGraphCollector().collect(page, SCHEMA, CTX).candidates if c.field == "title"]
        return found.normalized

    assert title("<h1>Acme Kettle Deluxe 2L</h1>") == Ok("Acme Kettle Deluxe 2L")
    # One word may begin a longer name; several `h1` name no one title.
    assert title("<h1>Acme</h1>") == Ok("Buy Acme Kettle Deluxe 2L Online at Synthetic Shop")
    assert title("<h1>Acme Kettle</h1><h1>Deluxe 2L</h1>") == Ok("Buy Acme Kettle Deluxe 2L Online at Synthetic Shop")
