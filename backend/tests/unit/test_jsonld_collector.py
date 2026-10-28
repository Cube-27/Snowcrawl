"""The JSON-LD collector: every `application/ld+json` block, `@graph` expanded, `@id` references
resolved within the page, schema.org nodes as entities, and a candidate for each `schema_org` path."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from snowcrawl.collect.base import Candidate, CollectResult, Entity
from snowcrawl.collect.jsonld import JsonLdCollector
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Invalid, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

VOCABULARY = Vocabulary.read(Path(__file__).parents[1] / "fixtures" / "synthetic" / "schemaorg_vocabulary.jsonld")
PAGE_URL = "https://shop.example.test/p/1"
CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url=PAGE_URL, page_url=PAGE_URL)
SCHEMA_ORG = "https://schema.org"


def _schema(**fields: FieldSpec) -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind="detail",
        fields=fields,
    )


def _text(path: str) -> FieldSpec:
    return FieldSpec(type=FieldType.TEXT, description="d", schema_org=path)


SCHEMA = _schema(
    title=_text("Product.name"),
    brand=_text("Product.brand.name"),
    price=FieldSpec(type=FieldType.MONEY, description="d", schema_org="Offer.price+priceCurrency"),
    images=FieldSpec(
        type=FieldType.LIST, description="d", item=FieldSpec(type=FieldType.IMAGE_URL), schema_org="Product.image"
    ),
    note=FieldSpec(type=FieldType.TEXT, description="d"),  # no schema_org path: no JSON-LD candidates
)


def _page(*blocks: Any) -> ParsedPage:
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>' for b in blocks
    )
    return ParsedPage(f"<html><head>{scripts}</head><body></body></html>")


def _collect(*blocks: Any, schema: SchemaDef = SCHEMA) -> CollectResult:
    return JsonLdCollector(VOCABULARY).collect(_page(*blocks), schema, CTX)


def _candidate(field: str, entity_id: str, raw: Any, normalized: Any, locator: str) -> Candidate:
    return Candidate(
        field=field,
        entity_id=entity_id,
        source_kind="jsonld",
        raw=raw,
        normalized=normalized,
        locator=locator,
        independent=True,
    )


def test_a_product_with_a_nested_offer() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@type": "Product",
            "name": "Widget 9",
            "url": PAGE_URL,
            "brand": {"@type": "Brand", "name": "Acme 7"},
            "image": ["https://shop.example.test/a.png", "/b.png"],
            "offers": {"@type": "Offer", "price": "10.50", "priceCurrency": "EUR"},
        }
    )
    product = Entity(
        id="jsonld:0:$", types=("Product",), urls=(PAGE_URL,), parent=None, item_urls=(PAGE_URL,), names=("Widget 9",)
    )
    assert result.entities == (
        product,
        Entity(id="jsonld:0:$['brand']", types=("Brand",), urls=(), parent=product.id, via="brand", names=("Acme 7",)),
        Entity(id="jsonld:0:$['offers']", types=("Offer",), urls=(), parent=product.id, via="offers"),
    )
    assert result.candidates == (
        _candidate("title", product.id, "Widget 9", Ok("Widget 9"), "jsonld:0:$['name']"),
        _candidate("brand", product.id, "Acme 7", Ok("Acme 7"), "jsonld:0:$['brand']['name']"),
        _candidate(
            "price",
            "jsonld:0:$['offers']",
            {"price": "10.50", "priceCurrency": "EUR"},
            Ok({"amount": "10.50", "currency": "EUR"}),
            "jsonld:0:$['offers']['price']",
        ),
        _candidate(
            "images",
            product.id,
            ["https://shop.example.test/a.png", "/b.png"],
            Ok(["https://shop.example.test/a.png", "https://shop.example.test/b.png"]),
            "jsonld:0:$['image'][0]",
        ),
    )
    assert result.diagnostics == ()


def test_an_untyped_node_under_a_products_offers_is_an_offer() -> None:
    offer = {"price": "10.50", "priceCurrency": "EUR"}
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9", "offers": [offer], "brand": {"name": "x"}}
    )

    assert [(e.id, e.types, e.via) for e in result.entities] == [
        ("jsonld:0:$", ("Product",), None),
        ("jsonld:0:$['offers'][0]", ("Offer",), "offers"),
    ]
    assert [c.field for c in result.candidates] == ["title", "brand", "price"]


def test_graph_nodes_and_id_references() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@graph": [
                {"@type": "Product", "@id": "#product", "name": "Widget 9", "offers": {"@id": "#offer"}},
                {"@type": "Offer", "@id": "#offer", "price": 5, "priceCurrency": "USD"},
            ],
        }
    )
    product_id, offer_id = "jsonld:0:$['@graph'][0]", "jsonld:0:$['@graph'][1]"
    assert [(e.id, e.types[0], e.parent, e.via) for e in result.entities] == [
        (product_id, "Product", None, None),
        (offer_id, "Offer", product_id, "offers"),
    ]
    assert [(c.field, c.entity_id, c.locator) for c in result.candidates] == [
        ("title", product_id, f"{product_id}['name']"),
        ("price", offer_id, f"{offer_id}['price']"),
    ]


def test_references_resolve_across_blocks() -> None:
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "@id": "#product", "offers": {"@id": "#offer"}},
        "not json",
        {"@context": SCHEMA_ORG, "@type": "Offer", "@id": "#offer", "price": "5", "priceCurrency": "USD"},
    )
    assert [(e.id, e.parent, e.via) for e in result.entities] == [
        ("jsonld:0:$", None, None),
        ("jsonld:2:$", "jsonld:0:$", "offers"),
    ]
    assert [(c.field, c.entity_id, c.locator) for c in result.candidates] == [
        ("price", "jsonld:2:$", "jsonld:2:$['price']")
    ]


@pytest.mark.parametrize("block", ['{"price": NaN}', "[" * 100_000, ""], ids=["nan", "deep", "empty"])
def test_text_that_is_not_json_is_a_diagnostic(block: str) -> None:
    result = _collect(block)
    assert (result.entities, result.candidates) == ((), ())
    assert [d.split(" (")[0] for d in result.diagnostics] == ["jsonld:0: invalid JSON"]


def test_deep_nesting_is_walked_without_the_call_stack() -> None:
    depth = 400
    block = "[" * depth + json.dumps({"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9"}) + "]" * depth
    result = _collect(block)
    assert [(c.raw, c.locator) for c in result.candidates] == [("Widget 9", "jsonld:0:$" + "[0]" * depth + "['name']")]


def test_a_value_the_candidate_model_refuses_is_skipped_with_a_diagnostic() -> None:
    deep: Any = "x"
    for _ in range(300):
        deep = {"a": deep}
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "name": deep},
        {"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9"},
    )
    assert [(c.raw, c.locator) for c in result.candidates] == [("Widget 9", "jsonld:1:$['name']")]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].startswith("value at jsonld:0:$['name'] skipped")


def test_an_invalid_block_is_skipped_with_a_diagnostic() -> None:
    result = _collect(
        '{"@type": "Product", "name": "x" "sku": "1"}', {"@context": SCHEMA_ORG, "@type": "Product", "name": "y"}
    )
    assert [(c.locator, c.raw) for c in result.candidates] == [("jsonld:1:$['name']", "y")]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].startswith("jsonld:0: invalid JSON")


@pytest.mark.parametrize(
    "block",
    [
        {"@context": "http://schema.org", "@type": "Product", "name": "Widget 9"},
        {"@context": "https://schema.org/", "@type": "Product", "name": "Widget 9"},
        {"@context": {"@vocab": "http://schema.org/"}, "@type": "Product", "name": "Widget 9"},
        {"@context": [{"@language": "en"}, SCHEMA_ORG], "@type": "Product", "name": "Widget 9"},
        {"@type": "http://schema.org/Product", "http://schema.org/name": "Widget 9"},
        {"@context": SCHEMA_ORG, "@type": "Product", "name": {"@value": "Widget 9", "@language": "en"}},
        [{"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9"}],
    ],
)
def test_schema_org_spellings(block: Any) -> None:
    result = _collect(block)
    assert [e.types[0] for e in result.entities] == ["Product"]
    assert [(c.field, c.raw) for c in result.candidates] == [("title", "Widget 9")]


@pytest.mark.parametrize(
    "block",
    [
        {"@type": "Product", "name": "Widget 9"},  # no context: `Product` is not a schema.org IRI
        {"@context": SCHEMA_ORG, "@type": "Widget", "name": "Widget 9"},  # not a schema.org type
    ],
)
def test_nodes_outside_schema_org_are_not_entities(block: Any) -> None:
    result = _collect(block, {"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 10"})
    assert [e.id for e in result.entities] == ["jsonld:1:$"]
    assert [(c.raw, c.locator) for c in result.candidates] == [("Widget 10", "jsonld:1:$['name']")]


def test_a_null_context_ends_the_schema_org_scope() -> None:
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "isRelatedTo": {"@context": None, "@type": "Product"}}
    )
    assert [e.id for e in result.entities] == ["jsonld:0:$"]


def test_a_subtype_matches_its_supertype_s_paths() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@type": ["Thing", "IndividualProduct"],
            "name": "Widget 9",
            "offers": [
                {"@type": "AggregateOffer", "price": "5", "priceCurrency": "USD"},
                {"@type": "Offer", "price": "6", "priceCurrency": "USD"},
            ],
        }
    )
    assert [e.types[0] for e in result.entities] == ["IndividualProduct", "AggregateOffer", "Offer"]
    assert [(c.field, c.entity_id) for c in result.candidates] == [
        ("title", "jsonld:0:$"),
        ("price", "jsonld:0:$['offers'][0]"),
        ("price", "jsonld:0:$['offers'][1]"),
    ]


def test_a_node_is_an_entity_of_each_of_its_types() -> None:
    # JSON-LD 1.1 §3.5: a node with several `@type` values is an instance of each; none is dropped for another.
    result = _collect({"@context": SCHEMA_ORG, "@type": ["CreativeWork", "Product"], "name": "Widget 9"})
    assert [e.types for e in result.entities] == [("CreativeWork", "Product")]


def test_an_unknown_type_between_entities_is_skipped_for_the_parent() -> None:
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "x": {"@type": "Widget", "offers": {"@type": "Offer"}}}
    )
    # The parent's property that holds the Offer is `x`, the one on the Product.
    assert [(e.id, e.parent, e.via) for e in result.entities] == [
        ("jsonld:0:$", None, None),
        ("jsonld:0:$['x']['offers']", "jsonld:0:$", "x"),
    ]


def test_via_names_the_schema_org_property_holding_the_entity() -> None:
    product = SCHEMA_ORG + "/Product"
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "isSimilarTo": {"@type": "Product"}},
        {"@type": product, SCHEMA_ORG + "/isRelatedTo": [{"@type": product}], "related": {"@type": product}},
    )
    # Without a schema.org context, only a full IRI names a schema.org property (JSON-LD 1.1 §4.1.2).
    assert [(e.id, e.via) for e in result.entities] == [
        ("jsonld:0:$", None),
        ("jsonld:0:$['isSimilarTo']", "isSimilarTo"),
        ("jsonld:1:$", None),
        ("jsonld:1:$['https://schema.org/isRelatedTo'][0]", "isRelatedTo"),
        ("jsonld:1:$['related']", None),
    ]


def test_entity_urls_are_canonical_and_name_pages() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@type": "Product",
            "@id": "HTTPS://Shop.Example.test/p/2",
            "url": "/p/1?x=1#reviews",
            "mainEntityOfPage": {"@type": "WebPage", "@id": "https://shop.example.test/p/3"},
            "offers": [{"@type": "Offer", "url": "https://shop.example.test/p/4"}, {"@id": "#offer"}],
            "sameAs": "https://shop.example.test/p/5",
        },
        {"@context": SCHEMA_ORG, "@type": "Offer", "@id": "#offer", "url": "https://shop.example.test/p/6"},
        {"@context": SCHEMA_ORG, "@type": "Product", "@id": "#second", "mainEntityOfPage": PAGE_URL},
    )
    by_id = {e.id: e.urls for e in result.entities}
    assert by_id["jsonld:0:$"] == (
        "https://shop.example.test/p/1?x=1",
        "https://shop.example.test/p/2",
        "https://shop.example.test/p/3",
        "https://shop.example.test/p/4",
        "https://shop.example.test/p/6",
    )
    # An `@id` with a fragment names a part of the document, not a page (RFC 3986 §3.5).
    assert by_id["jsonld:2:$"] == (PAGE_URL,)


def test_an_unresolved_reference_gives_no_value() -> None:
    result = _collect({"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9", "brand": {"@id": "#nowhere"}})
    assert [(c.field, c.raw) for c in result.candidates] == [("title", "Widget 9")]


def test_reference_cycles_end() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@graph": [
                {"@type": "Product", "@id": "#a", "isRelatedTo": {"@id": "#b"}, "name": "A"},
                {"@type": "Product", "@id": "#b", "isRelatedTo": {"@id": "#a"}, "name": "B"},
            ],
        }
    )
    assert [(e.id, e.parent, e.via) for e in result.entities] == [
        ("jsonld:0:$['@graph'][0]", "jsonld:0:$['@graph'][1]", "isRelatedTo"),
        ("jsonld:0:$['@graph'][1]", None, None),
    ]


def test_a_node_referenced_from_two_entities_has_no_parent() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@graph": [
                {"@type": "Product", "offers": {"@id": "#o"}},
                {"@type": "Product", "offers": {"@id": "#o"}},
                {"@type": "Offer", "@id": "#o"},
            ],
        }
    )
    assert [(e.parent, e.via) for e in result.entities] == [(None, None)] * 3


def test_null_and_nested_arrays_are_no_values() -> None:
    result = _collect({"@context": SCHEMA_ORG, "@type": "Product", "name": [None, ["x"], {"@value": None}, "Widget 9"]})
    assert [(c.raw, c.locator) for c in result.candidates] == [("Widget 9", "jsonld:0:$['name'][3]")]


def test_values_that_do_not_normalize_are_still_candidates() -> None:
    result = _collect({"@context": SCHEMA_ORG, "@type": "Product", "name": {"@type": "Thing"}})
    assert [(c.field, c.normalized) for c in result.candidates] == [("title", Invalid("not_a_string"))]


def test_member_names_are_escaped_as_normalized_paths() -> None:
    # RFC 9535 §2.7: a normalized path writes member names in single quotes, escaping `'` and `\`.
    result = _collect({"a'b\\": {"@context": SCHEMA_ORG, "@type": "Product"}})
    assert [e.id for e in result.entities] == ["jsonld:0:$['a\\'b\\\\']"]


def test_a_node_referenced_under_two_properties_of_its_parent_names_neither() -> None:
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@graph": [
                {"@type": "Product", "isRelatedTo": {"@id": "#p"}, "isSimilarTo": {"@id": "#p"}},
                {"@type": "Product", "@id": "#p", "offers": {"@id": "#p"}},
            ],
        }
    )
    assert [(e.parent, e.via) for e in result.entities] == [(None, None), ("jsonld:0:$['@graph'][0]", None)]


def test_an_entity_carries_what_identifies_it() -> None:
    # Resolution merges the entities of one item (`resolve.identity`); the collector only states their identity.
    result = _collect(
        {
            "@context": SCHEMA_ORG,
            "@type": "Product",
            "@id": "/p/1#product",
            "sku": ["W-1", " "],
            "gtin13": 4006381333931,
            "inProductGroupWithID": "G-1",
            "url": "/p/1",
            "offers": {"@type": "Offer", "url": "/p/1?v=2"},
        },
        {"@context": SCHEMA_ORG, "@type": "Product", "@id": "_:b0"},
    )

    product, blank = result.entities[0], result.entities[2]
    assert set(product.identity) == {("@id", f"{PAGE_URL}#product"), ("sku", "W-1"), ("gtin13", "4006381333931")}
    assert (product.item_urls, product.group_ids) == ((PAGE_URL,), ("G-1",))
    assert blank.identity == ()  # JSON-LD 1.1 §4.5.1: a blank node is scoped to its block


def test_a_block_with_raw_control_characters_inside_strings_is_collected() -> None:
    # RFC 8259 §7 requires them escaped; the collector accepts them raw inside strings, so the block is not lost.
    block = '{"@context": "https://schema.org", "@type": "Product", "name": "Widget\tNine", "brand": {"name": "A\nB"}}'
    result = _collect(block)
    assert [(c.field, c.raw) for c in result.candidates] == [("title", "Widget\tNine"), ("brand", "A\nB")]
    assert result.diagnostics == ()


DESCRIBED = _schema(
    title=_text("Product.name"),
    description=FieldSpec(type=FieldType.LONG_TEXT, description="d", schema_org="Product.description"),
)


@pytest.mark.parametrize(
    ("raw", "title", "description"),
    [
        pytest.param(
            "<p>Soft <b>cotton</b>.</p><ul><li>Machine wash</li><li>Tumble dry</li></ul>",
            "Soft cotton. Machine wash Tumble dry",
            "Soft cotton.\nMachine wash\nTumble dry",
            id="block-markup",
        ),
        # A reference escaped twice is text escaped twice: `&amp;amp;` is `&`, in a fragment as in plain text.
        pytest.param("<b>Fish</b> &amp;amp; chips", "Fish & chips", "Fish & chips", id="fragment-reference"),
        pytest.param("Fish &amp;amp; chips", "Fish & chips", "Fish & chips", id="text-reference"),
        pytest.param("Sizes 1 < 2 & more", "Sizes 1 < 2 & more", "Sizes 1 < 2 & more", id="no-element"),
        # Markup written escaped is the fragment; an escaped `<` that makes no element stays text.
        pytest.param(
            "&lt;p&gt;Soft &lt;b&gt;cotton&lt;/b&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Machine wash&lt;/li&gt;&lt;/ul&gt;",
            "Soft cotton. Machine wash",
            "Soft cotton.\nMachine wash",
            id="escaped-markup",
        ),
        pytest.param("Sizes 1 &lt; 2", "Sizes 1 < 2", "Sizes 1 < 2", id="escaped-no-element"),
    ],
)
def test_a_text_value_holding_element_markup_is_read_as_its_rendered_text(
    raw: str, title: str, description: str
) -> None:
    result = _collect({"@context": SCHEMA_ORG, "@type": "Product", "name": raw, "description": raw}, schema=DESCRIBED)

    assert [(c.field, c.raw, c.normalized) for c in result.candidates] == [
        ("title", raw, Ok(title)),
        ("description", raw, Ok(description)),
    ]


def test_a_text_value_naming_an_enumeration_member_by_its_iri_reads_as_the_member_name() -> None:
    raw = "http://schema.org/InStock"
    result = _collect(
        {"@context": SCHEMA_ORG, "@type": "Product", "name": raw, "brand": {"name": "https://example.test/x"}}
    )

    assert [(c.field, c.normalized) for c in result.candidates] == [
        ("title", Ok("InStock")),
        ("brand", Ok("https://example.test/x")),  # an IRI that names no member stays the text it is
    ]


def test_a_property_value_a_field_label_names_gives_its_value_to_the_entity_holding_it() -> None:
    def pair(value: str) -> dict[str, Any]:
        return {"@type": "PropertyValue", "name": "Model number", "value": value}

    related = {"@type": "Product", "name": "Other", "additionalProperty": [pair("M-8")]}
    block = {"@context": SCHEMA_ORG, "@type": "Product", "additionalProperty": pair("M-7"), "isRelatedTo": related}
    mpn = FieldSpec(type=FieldType.IDENTIFIER, scheme="mpn", description="Manufacturer part number")
    schema = _schema(mpn=mpn).model_copy(update={"labels": {"mpn": ["Model number"]}})

    found = [(c.entity_id, c.raw) for c in _collect(block, schema=schema).candidates]

    assert found == [("jsonld:0:$", "M-7"), ("jsonld:0:$['isRelatedTo']", "M-8")]
    assert _collect(block, schema=_schema(mpn=mpn)).candidates == ()


def test_an_image_object_is_the_url_it_states() -> None:
    # schema.org `image`: ImageObject or URL; a MediaObject's `contentUrl` is its file, else its `url`.
    images = [
        {"@type": "ImageObject", "contentUrl": "/a.png", "url": "/a-page"},
        {"@type": "ImageObject", "url": "https://shop.example.test/b.png"},
        "/c.png",
    ]

    result = _collect({"@context": SCHEMA_ORG, "@type": "Product", "name": "Widget 9", "image": images})

    found = next(c for c in result.candidates if c.field == "images")
    expected: list[JsonValue] = [f"https://shop.example.test/{name}.png" for name in "abc"]
    assert found.normalized == Ok(expected)


def test_a_listed_product_named_only_by_its_iri_has_that_item_url() -> None:
    # JSON-LD 1.1 §4.1.2: `@id` is the node's IRI, read last for a row's item URL: absolute, on the page's site, with no
    # fragment, and not the page's own URL.
    listing = _schema(
        title=_text("Product.name"), url=FieldSpec(type=FieldType.URL, description="d", schema_org="Product.url")
    ).model_copy(update={"page_kind": "listing"})
    ids = [
        "https://shop.example.test/p/7",
        "https://shop.example.test/p/8#product",
        "https://elsewhere.example/p/9",
        PAGE_URL,
        "https://shop.example.test/p/10",
    ]
    items = [{"@type": "Product", "@id": iri, "name": f"Widget {n}"} for n, iri in enumerate(ids)]
    items[4]["url"] = "https://shop.example.test/p/11"
    elements = [{"@type": "ListItem", "position": n + 1, "item": item} for n, item in enumerate(items)]

    result = _collect({"@context": SCHEMA_ORG, "@type": "ItemList", "itemListElement": elements}, schema=listing)

    stated, named = (Ok(f"https://shop.example.test/p/{n}") for n in (11, 7))
    assert [c.normalized for c in result.candidates if c.field == "url"] == [stated, named]
