"""The structural DOM collector (admitted): label-value pairs whose
label is one of a field's labels (its name, schema.org property, description or drafted synonyms), the main region's
one typed value outside other items, and the main content's first image. Every
candidate is the page's, independent, with a `dom:` locator naming the element its value came from. Pages are
synthetic (`example.test`, C3, C9)."""

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from snowcrawl.collect.base import PAGE, Candidate
from snowcrawl.collect.dom import DomCollector
from snowcrawl.collect.dom_images import stated_images
from snowcrawl.collect.locator import parse_locator
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import PageKind, SchemaId
from snowcrawl.normalize.base import Context, Invalid, Ok
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

PAGE_URL = "https://shop.example.test/p/1"
CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url=PAGE_URL, page_url=PAGE_URL, html_lang="en")


def _field(type_: FieldType, path: str | None, **params: object) -> FieldSpec:
    return FieldSpec.model_validate({"type": type_, "description": "d", "schema_org": path, **params})


def _schema(page_kind: PageKind, **fields: FieldSpec) -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind=page_kind,
        fields=fields,
    )


FIELDS = {
    "title": _field(FieldType.TEXT, "Product.name"),
    "brand": _field(FieldType.TEXT, "Product.brand.name"),
    "sku": _field(FieldType.IDENTIFIER, "Product.sku", scheme="sku"),
    "availability": _field(FieldType.ENUM, "Offer.availability", vocab="schema:ItemAvailability"),
    "review_count": _field(FieldType.COUNT, "Product.aggregateRating.reviewCount"),
    "gender": _field(FieldType.TEXT, "Product.audience.suggestedGender"),
    "image_url": _field(FieldType.IMAGE_URL, "Product.image"),
    "images": _field(FieldType.LIST, "Product.image", item={"type": "image_url"}),
    "note": _field(FieldType.TEXT, None),
}
SCHEMA = _schema("detail", **FIELDS)


def _collect(body: str, schema: SchemaDef = SCHEMA) -> dict[str, list[tuple[object, str]]]:
    candidates = DomCollector().collect(ParsedPage(f"<html><body>{body}</body></html>"), schema, CTX).candidates
    found: dict[str, list[tuple[object, str]]] = {}
    for c in candidates:
        assert (c.entity_id, c.source_kind, c.independent) == (PAGE, "dom", True)
        value = c.normalized.value if isinstance(c.normalized, Ok) else c.normalized
        found.setdefault(c.field, []).append((value, c.locator))
    return found


def test_a_two_cell_table_row_whose_header_names_a_field_gives_its_value() -> None:
    found = _collect(
        "<table><tr><th>Brand:</th><td>Example Brand</td></tr>"
        "<tr><th>SKU</th><td> W-100 </td></tr>"
        "<tr><td>Availability</td><td>In stock (5 available)</td></tr></table>"
    )

    assert found == {
        "brand": [("Example Brand", "dom:/html[1]/body[1]/table[1]/tbody[1]/tr[1]/td[1]")],
        "sku": [("W-100", "dom:/html[1]/body[1]/table[1]/tbody[1]/tr[2]/td[1]")],
        "availability": [("InStock", "dom:/html[1]/body[1]/table[1]/tbody[1]/tr[3]/td[2]")],
    }


def test_a_description_list_gives_each_term_its_descriptions() -> None:
    found = _collect(
        "<dl><dt>Review count</dt><dd>12</dd><div><dt>Gender</dt><dt>Suggested gender</dt><dd>Unisex</dd></div></dl>"
    )

    assert found == {
        "review_count": [(12, "dom:/html[1]/body[1]/dl[1]/dd[1]")],
        # Two terms of one group naming the same field give its description once.
        "gender": [("Unisex", "dom:/html[1]/body[1]/dl[1]/div[1]/dd[1]")],
    }


@pytest.mark.parametrize(
    "body",
    [
        # A row of three cells is a data table's row, not a label and its value.
        "<table><tr><th>Brand</th><td>Example Brand</td><td>x</td></tr></table>",
        # A label that is neither a field name nor the property a field's path names.
        "<table><tr><th>Maker</th><td>Example Brand</td></tr></table>",
        # The property a path goes through is not the one it names (`Product.aggregateRating.reviewCount`).
        "<table><tr><th>Aggregate rating</th><td>12</td></tr></table>",
        # A value the page does not show.
        '<table><tr><th>Brand</th><td style="display:none">Example Brand</td></tr></table>',
        # A field without a schema.org path is matched by its name only, and an empty value is no value.
        "<table><tr><th>Name</th><td> </td></tr></table>",
    ],
)
def test_what_is_not_a_shown_label_value_pair_of_a_field_gives_nothing(body: str) -> None:
    assert _collect(body) == {}


def test_a_value_that_does_not_normalize_is_still_a_candidate() -> None:
    found = _collect("<table><tr><th>Availability</th><td>Ask us</td></tr></table>")

    assert found == {"availability": [(Invalid("not_a_member"), "dom:/html[1]/body[1]/table[1]/tbody[1]/tr[1]/td[1]")]}


def test_a_listing_schema_gets_no_page_level_pairs_or_image() -> None:
    body = '<main><img src="/a.jpg"></main><table><tr><th>Brand</th><td>Example Brand</td></tr></table>'
    assert _collect(body, _schema("listing", **FIELDS)) == {}


def test_the_main_image_is_the_first_image_of_the_main_element_outside_its_navigation() -> None:
    found = _collect(
        '<img src="/logo.png"><main><nav><img src="/menu.png"></nav>'
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><img src="/p/1.jpg"><img src="/p/2.jpg"></main>'
    )

    assert found["image_url"] == [("https://shop.example.test/p/1.jpg", "dom:/html[1]/body[1]/main[1]/img[2]")]


def test_the_main_images_slides_give_the_image_list_each_at_its_widest_source() -> None:
    # A lazy loader's placeholder `src` is left out of a source set with width descriptors (HTML Standard §4.8.4.3);
    # a slide without an image (a video) gives none.
    slide = '<div><span><img src="/lazy.gif" srcset="/p/{0}.jpg?w=300 300w, /p/{0}.jpg?w=900 900w" alt=P></span></div>'
    found = _collect(f"<main><div>{slide.format('a')}<div><video></video></div>{slide.format('b')}</div></main>")

    assert found["images"] == [
        (
            ["https://shop.example.test/p/a.jpg?w=900", "https://shop.example.test/p/b.jpg?w=900"],
            "dom:/html[1]/body[1]/main[1]/div[1]",
        )
    ]
    assert [value for value, _ in found["image_url"]] == ["https://shop.example.test/p/a.jpg?w=900"]


def test_a_width_too_long_to_be_one_is_no_width_descriptor() -> None:
    found = _collect(f'<main><img src="/p/1.jpg" srcset="/p/2.jpg {"9" * 5000}w" alt="P"></main>')

    assert [value for value, _ in found["image_url"]] == ["https://shop.example.test/p/1.jpg"]


@pytest.mark.parametrize(
    "others",
    [
        '<div><img src="/p/2.jpg" alt="Q"><a href="/c/b">Other product</a></div>',  # cards of other documents
        '<div><img src="/p/1.jpg" alt="Q"></div>',  # one source in every block: the template's, not a view
        "<div><p>Specifications</p></div>",  # no other block shows an image
    ],
)
def test_blocks_that_link_elsewhere_or_repeat_one_source_are_no_gallery(others: str) -> None:
    found = _collect(f'<main><section><div><img src="/p/1.jpg" alt="P"></div>{others}</section></main>')

    assert "images" not in found


@pytest.mark.parametrize(
    ("body", "image"),
    [
        ('<div role="main"><img srcset="/p/s.jpg 1x, /p/l.jpg 2x"></div>', "https://shop.example.test/p/s.jpg"),
        (
            '<article><header><img src="/h.png"></header><img src="/p/a.jpg"></article>',
            "https://shop.example.test/p/a.jpg",
        ),
    ],
)
def test_the_main_region_is_a_main_landmark_or_the_one_article(body: str, image: str) -> None:
    assert [value for value, _ in _collect(body).get("image_url", [])] == [image]


@pytest.mark.parametrize(
    "body",
    [
        '<div><img src="/p/1.jpg"></div>',  # no main region
        '<article><img src="/a.jpg"></article><article><img src="/b.jpg"></article>',  # two articles, neither main
        '<main><aside><img src="/ad.jpg"></aside></main>',  # only tangential content
    ],
)
def test_without_one_main_region_image_there_is_no_main_image(body: str) -> None:
    assert _collect(body) == {}


@pytest.mark.parametrize(
    "wrapper",
    [
        '<div role="dialog" aria-modal="true">{}</div>',  # WAI-ARIA 1.2 dialog: a window apart from the page
        '<div role="alertdialog">{}</div>',
        "<dialog open>{}</dialog>",  # HTML Standard §4.11.4
        '<div aria-hidden="true"><div>{}</div></div>',  # hidden from every user, as in the cleaned text
        "<div hidden>{}</div>",
        '<div style="display: none">{}</div>',
        '<div style="visibility: hidden"><span>{}</span></div>',  # CSS 2 §11.2: inherited
    ],
)
def test_an_image_in_a_dialog_or_a_hidden_subtree_is_not_the_main_image(wrapper: str) -> None:
    # A drawer or modal inside `main` holds images of its own (a badge, a size chart), not the entity's.
    found = _collect(f'<main>{wrapper.format("<img src=/badge.png>")}<img src="/p/1.jpg"></main>')

    assert [value for value, _ in found["image_url"]] == ["https://shop.example.test/p/1.jpg"]


def test_the_shown_image_of_the_stated_main_file_anchors_the_main_image_and_its_gallery() -> None:
    # No main landmark (a client-rendered storefront): the image structured data states, found by its file, is the main
    # image, and its slides the gallery; the header's logo is tangential content.
    slides = "".join(f'<div><img src="/p/{n}.jpg?w=200" alt="{n}"></div>' for n in "abc")
    body = f'<header><img src="/logo.png"></header><div><img src="/badge.png"></div><div>{slides}</div>'
    primary = ("https://shop.example.test/p/b.jpg",)

    def collect(markup: str, stated: tuple[str, ...]) -> dict[str, object]:
        page = ParsedPage(f"<html><body>{markup}</body></html>")
        found = DomCollector(primary=stated).collect(page, SCHEMA, CTX).candidates
        return {c.field: c.normalized.value for c in found if isinstance(c.normalized, Ok)}

    found = collect(body, primary)
    assert found["image_url"] == "https://shop.example.test/p/b.jpg?w=200"
    assert found["images"] == [f"https://shop.example.test/p/{n}.jpg?w=200" for n in "abc"]
    # In a main region the anchor goes before the region's first image.
    assert collect(f"<main>{body}</main>", primary)["image_url"] == "https://shop.example.test/p/b.jpg?w=200"
    # Without a stated file the page has no main region image.
    assert collect(body, ()) == {}
    # A rendition the image server writes into the path is the stated file too (`images.same_file`).
    served = body.replace("/p/b.jpg", "/cdn/v2/p/b.jpg/renditions/600.jpg")
    assert collect(served, primary)["image_url"] == "https://shop.example.test/cdn/v2/p/b.jpg/renditions/600.jpg?w=200"


def test_the_stated_file_in_tangential_content_anchors_its_gallery() -> None:
    # The page keeps the product's slides in an `aside` of `main`, each image beside a caption whose link addresses the
    # image itself (its query names the file, RFC 3986 §3.4); a swatch row comes first in the region.
    swatches = "".join(f'<li><button><img src="/s/{n}.jpg" alt="{n}"></button></li>' for n in "xy")
    slides = "".join(
        f'<div><div><img src="/p/{n}.jpg?w=600" alt="P"></div><div><a href="/look?image=/p/{n}.jpg">L</a></div></div>'
        for n in "abc"
    )
    body = f"<main><ul>{swatches}</ul><aside><div>{slides}</div></aside></main>"
    page = ParsedPage(f"<html><body>{body}</body></html>")
    found = DomCollector(primary=("https://shop.example.test/p/a.jpg",)).collect(page, SCHEMA, CTX).candidates
    values = {c.field: c.normalized.value for c in found if isinstance(c.normalized, Ok)}

    assert values["image_url"] == "https://shop.example.test/p/a.jpg?w=600"
    assert values["images"] == [f"https://shop.example.test/p/{n}.jpg?w=600" for n in "abc"]


def test_slides_named_after_the_main_file_are_its_gallery_wherever_the_block_holds_them() -> None:
    # The stated file is only in a lazy slide's `noscript` fallback; every slide's image has an empty `alt`; the first
    # slides show their image outside the main image's tag path. A swatch named after the file is no slide.
    def slide(name: str, *, lazy: bool) -> str:
        image = f'<img src="/i/{name}?h=900" alt="">'
        return (
            f'<div><span><img src="" alt=""><noscript>{image}</noscript></span></div>'
            if lazy
            else f"<div><span>{image}</span></div>"
        )

    slides = slide("P1_m", lazy=False) + slide("P1_d1", lazy=False) + slide("P1_d2", lazy=True) + slide("P1", lazy=True)
    body = f'<main><div>{slides}</div><div><img src="/i/P1_sw" alt="Red"></div></main>'
    page = ParsedPage(f"<html><body>{body}</body></html>")
    found = DomCollector(primary=("https://shop.example.test/i/P1?crop=1",)).collect(page, SCHEMA, CTX).candidates
    values = {c.field: c.normalized.value for c in found if isinstance(c.normalized, Ok)}

    assert values["image_url"] == "https://shop.example.test/i/P1?h=900"
    assert values["images"] == [f"https://shop.example.test/i/{n}?h=900" for n in ("P1_m", "P1_d1", "P1_d2", "P1")]


def test_a_lazy_or_hidden_slide_gives_its_source_to_the_gallery() -> None:
    # HTML Standard §4.8.4.3: a srcset candidate without a descriptor is 1x, which leaves the placeholder `src` out; a
    # lazy loader keeps the sources in `data-srcset`/`data-src`; a carousel hides the slides but the current one.
    slides = (
        '<div><img src="/ph.svg" srcset="/p/a.jpg, /p/a2.jpg 2x" alt="P"></div>'
        '<div style="display:none"><img src="/ph.svg" data-srcset="/p/b.jpg 300w,/p/b-big.jpg 900w" alt="P"></div>'
        '<div hidden><img src="/ph.svg" data-src="/p/c.jpg" alt="P"></div>'
    )
    found = _values(f"<main><div>{slides}</div></main>")

    assert found["image_url"] == ["https://shop.example.test/p/a.jpg"]
    assert found["images"] == [[f"https://shop.example.test/p/{n}.jpg" for n in ("a", "b-big", "c")]]


def test_the_stated_main_image_is_each_sources_first_image() -> None:
    # An image field holds a candidate per value a source states; the source's first names the main image.
    def stated(field: str, value: JsonValue, kind: str = "jsonld") -> Candidate:
        return Candidate.model_validate(
            {
                "field": field,
                "entity_id": "e",
                "source_kind": kind,
                "raw": value,
                "normalized": Ok(value),
                "locator": "jsonld:0:$['x']",
                "independent": True,
            }
        )

    files: list[JsonValue] = [f"https://cdn.example.test/p/{n}.jpg" for n in "abcd"]
    found = stated_images(
        SCHEMA,
        [stated("image_url", files[0]), stated("image_url", files[1]), stated("images", files[2:], "og")],
    )

    assert found == (files[0], files[2])


def test_a_hidden_main_region_has_no_main_image() -> None:
    assert _collect('<div aria-hidden="true"><main><img src="/p/1.jpg"></main></div>') == {}


def test_locators_parse_as_dom_xpaths() -> None:
    candidates = (
        DomCollector()
        .collect(
            ParsedPage('<html><body><main><img src="/p.jpg"><dl><dt>SKU</dt><dd>W-1</dd></dl></main></body></html>'),
            SCHEMA,
            CTX,
        )
        .candidates
    )
    assert [parse_locator(c.locator).scheme for c in candidates] == ["dom", "dom"]


@pytest.mark.parametrize(
    "other",
    [
        '<img src="/deco.jpg" alt="">',  # HTML Standard §4.8.4.4: an empty `alt` marks an image as decorative
        '<img src="/deco.jpg" alt=" " role="none">',  # WAI-ARIA 1.2: roles `none`, `presentation`
        '<img src="/deco.jpg" alt="x" role="presentation">',
        # HTML Standard §4.8.9, §4.8.7: a media element's children are fallback content, not shown where it is
        '<video poster="/poster.jpg"><img src="/poster.jpg"></video>',
        '<object data="/x.pdf"><img src="/x.png"></object>',
        # A link card: an image whose block holds one hyperlink to another document is that document's teaser.
        '<ul><li><div><img src="/c/a.jpg" alt="A"><p>Type A</p><a href="/c/a">Shop now</a></div></li>'
        '<li><div><img src="/c/b.jpg" alt="B"><p>Type B</p><a href="/c/b">Shop now</a></div></li></ul>',
        '<a href="https://promo.example.test/sale"><img src="/banner.jpg" alt="Sale"></a>',
        '<div><a href="/c/a"><img src="/c/a.jpg" alt="A"></a><a href="/c/a">Type A</a></div>',
    ],
)
def test_a_decorative_fallback_or_link_card_image_is_not_the_main_image(other: str) -> None:
    found = _collect(f'<main>{other}<img src="/p/1.jpg" alt="Product"></main>')

    assert [value for value, _ in found["image_url"]] == ["https://shop.example.test/p/1.jpg"]


@pytest.mark.parametrize(
    "wrapper",
    [
        '<a href="/p/1-large.jpg">{}</a>',  # a link to an image resource (RFC 6838 `image/*`) is the image itself
        '<a href="#zoom">{}</a>',  # a fragment link stays on this page (RFC 3986 §4.4)
        '<a href="/p/1">{}</a><a href="/p/1#reviews">Reviews</a>',  # links to this page itself
        '<div><a href="/c/a">Category A</a> <a href="/c/b">Category B</a>{}</div>',  # links to several documents
        # The nearest block with a link holds other images too: a gallery beside a link, not one card.
        '<div>{}<img src="/p/2.jpg" alt="Back"><a href="/c/a">Category A</a></div>',
    ],
)
def test_an_image_linked_to_itself_this_page_or_several_documents_stays_the_main_image(wrapper: str) -> None:
    found = _collect(f"<main>{wrapper.format(chr(60) + 'img src=/p/1.jpg alt=Product>')}</main>")

    assert [value for value, _ in found["image_url"]] == ["https://shop.example.test/p/1.jpg"]


def _values(body: str, schema: SchemaDef = SCHEMA) -> dict[str, list[object]]:
    return {name: [value for value, _ in found] for name, found in _collect(body, schema).items()}


@pytest.mark.parametrize(
    "body",
    [
        "<div>Brand: <span>Example Brand</span></div>",  # a label then an element holding the value
        "<p><b>Brand:</b> Example Brand</p>",  # a label element then the text that follows it
        "<div><span>Brand\uff1a</span><div>Example Brand</div></div>",  # a fullwidth colon
        "<ul><li><div>Brand</div><div>Example Brand</div></li><li><div>Other</div><div>x</div></li></ul>",  # siblings
        "<section><div>Brand</div><div>Example Brand</div></section>"
        "<section><div>Gender</div><div>Unisex</div></section>",
        "<label>Brand <span>— Example Brand</span></label>",  # a dash and a space separate label and value
        "<section><div>Brand</div><div>- Example Brand</div></section>"
        "<section><div>Gender</div><div>Unisex</div></section>",
    ],
)
def test_two_column_structures_beyond_rows_and_description_lists_give_a_label_its_value(body: str) -> None:
    assert _values(body)["brand"] == ["Example Brand"]


def test_a_label_and_value_pair_that_is_not_repeated_is_no_structure() -> None:
    assert _collect("<div><span>Brand</span><span>Example Brand</span></div>") == {}


def test_a_colon_label_gives_its_value_the_element_that_holds_it() -> None:
    found = _collect("<div>Brand: <span>Example Brand</span></div>")

    assert found == {"brand": [("Example Brand", "dom:/html[1]/body[1]/div[1]/span[1]")]}


def test_a_label_in_the_middle_of_a_line_is_no_label() -> None:
    assert _collect("<p>Our brand: Example Brand</p><p>Shipping: free</p>") == {}


def test_lines_giving_a_field_several_values_are_the_listed_items_not_the_pages() -> None:
    body = "<p>SKU: A-1</p><p>SKU: A-2</p><dl><dt>Brand</dt><dd>Example Brand</dd></dl><p>Brand: B</p><p>Brand: C</p>"

    assert _values(body) == {"brand": ["Example Brand"]}


def test_a_colon_line_giving_a_field_one_value_twice_gives_it_once() -> None:
    assert _values("<div><p>SKU: A-1</p></div><p>SKU: A-1</p>") == {"sku": ["A-1"]}
    # In three sibling blocks of one template, a line repeats for as many items (each review's own count).
    assert _values("<div>" + "<div><p>SKU: A-1</p></div>" * 3 + "</div>") == {}
    # One value shown in other places of the layout (responsive copies) is the page's own.
    assert _values("<div><p>SKU: A-1</p></div><section><p>SKU: A-1</p></section><p>SKU: A-1</p>") == {"sku": ["A-1"]}


def test_pairs_are_read_from_the_main_region_outside_its_tangential_content() -> None:
    body = (
        "<dl><dt>Brand</dt><dd>Outside</dd></dl>"
        "<main><footer><p>Brand: Footer</p></footer><p>Brand: Example Brand</p></main>"
    )

    assert _values(body) == {"brand": ["Example Brand"]}


MONEY_SCHEMA = _schema(
    "detail",
    price=_field(FieldType.MONEY, "Offer.price+priceCurrency"),
    gtin=_field(FieldType.IDENTIFIER, "Product.gtin", scheme="gtin"),
    sku=_field(FieldType.IDENTIFIER, "Product.sku", scheme="sku"),
)


def test_the_main_region_s_one_price_and_one_checked_identifier_are_spotted() -> None:
    body = "<main><h1>Widget</h1><p>Now <span>€19.99</span></p><p>EAN <b>4006381333931</b></p></main>"

    assert _values(body, MONEY_SCHEMA) == {
        "price": [{"amount": "19.99", "currency": "EUR"}],
        "gtin": ["4006381333931"],
    }


@pytest.mark.parametrize(
    "body",
    [
        "<main><p>€19.99</p><ul><li>€24.99</li></ul></main>",  # several prices: ambiguous
        "<main><s>€29.99</s></main>",  # a former price
        "<main><nav>€19.99</nav><aside>€19.99</aside></main>",  # tangential content
        "<p>€19.99</p>",  # no main region
        "<main><p>EAN 4006381333932</p></main>",  # a wrong check digit
        '<main><p style="display:none">€19.99</p></main>',  # not shown
        "<main><p>Only <span>19.99</span> left</p></main>",  # a number without a currency sign: no checked syntax
    ],
)
def test_a_typed_value_is_not_spotted_when_it_is_ambiguous_struck_apart_or_not_shown(body: str) -> None:
    assert _collect(body, MONEY_SCHEMA) == {}


def test_a_typed_value_one_element_holds_inside_another_is_read_once() -> None:
    found = _collect("<main><div><span>€19.99</span></div></main>", MONEY_SCHEMA)

    assert found["price"][0][1] == "dom:/html[1]/body[1]/main[1]/div[1]/span[1]"


@pytest.mark.parametrize(
    "body",
    [
        "<p>\n  <b>Brand:</b> Example Brand</p>",  # indented markup: a blank text node comes first
        "<p>Brand: Example Brand <span>SKU: A-1</span></p>",  # a second label on the line ends the first value
    ],
)
def test_a_label_line_gives_its_own_value_on_indented_or_shared_lines(body: str) -> None:
    assert _values(body)["brand"] == ["Example Brand"]


def test_two_labels_in_a_line_with_one_value_each_are_both_read() -> None:
    body = "<p><span>Brand: Example Brand</span> <span>Gender: Unisex</span></p>"

    assert _values(body) == {"brand": ["Example Brand"], "gender": ["Unisex"]}


@pytest.mark.parametrize(
    "body",
    [
        # Rows and groups are the page's own: several values and a conflicting line do not drop them.
        "<table><tr><th>Brand</th><td>A</td></tr><tr><th>Brand</th><td>B</td></tr></table>",
        "<dl><div><dt>Brand</dt><dd>A</dd></div><div><dt>Brand</dt><dd>B</dd></div></dl>",
        "<table><tr><th>Brand:</th><td>A</td></tr><tr><th>Brand:</th><td>B</td></tr></table>",
    ],
)
def test_table_rows_and_description_groups_are_not_dropped_by_the_line_and_sibling_rule(body: str) -> None:
    assert sorted(map(str, _values(body)["brand"])) == ["A", "B"]


def test_a_conflicting_line_does_not_drop_a_table_row() -> None:
    body = "<main><table><tr><th>Brand:</th><td>A</td></tr></table><p>Brand: B</p></main>"

    assert sorted(map(str, _values(body)["brand"])) == ["A", "B"]


def test_a_datetime_field_is_not_spotted_without_a_label() -> None:
    schema = _schema("detail", posted=_field(FieldType.DATETIME, "Article.dateModified"))

    assert _collect("<main><p>2026-10-01T10:00:00Z</p></main>", schema) == {}


def test_a_json_body_is_no_markup_to_read_pairs_from() -> None:
    page = ParsedPage('{"brand": "Example Brand"}', json_body=True)

    assert DomCollector().collect(page, SCHEMA, CTX).candidates == ()


def test_a_row_value_that_lines_also_state_among_others_is_kept() -> None:
    body = "<main><table><tr><th>Brand</th><td>A</td></tr></table><p>Brand: A</p><p>Brand: B</p></main>"

    assert _values(body) == {"brand": ["A"]}


def test_a_price_the_main_region_holds_itself_is_spotted() -> None:
    found = _collect("<main>€19.99</main>", MONEY_SCHEMA)

    assert found["price"][0][1] == "dom:/html[1]/body[1]/main[1]"


def test_a_label_names_a_field_through_the_schemas_drafted_vocabulary() -> None:
    body = (
        "<main><table><tr><th>Art.-Nr.</th><td>W-100</td></tr><tr><th>Item no.</th><td>W-100</td></tr></table></main>"
    )
    drafted = MONEY_SCHEMA.model_copy(update={"labels": {"sku": ["Art.-Nr.", "Item no."]}})

    assert _values(body, MONEY_SCHEMA) == {}
    assert _values(body, drafted) == {"sku": ["W-100"]}


def test_a_label_without_a_colon_gives_its_value_to_its_field_and_not_to_a_check_digit_identifier() -> None:
    body = "<main><div>Item no. <span>4006381333931</span></div><h4>Item no. <b>Specs</b></h4></main>"
    drafted = MONEY_SCHEMA.model_copy(update={"labels": {"sku": ["Item no."]}})

    assert _values(body, MONEY_SCHEMA) == {"gtin": ["4006381333931"]}
    assert _values(body, drafted) == {"sku": ["4006381333931"]}
    nested = "<main><table><tr><th>Item no.</th><td><span>4006381333931</span></td></tr></table></main>"
    assert _values(nested, drafted) == {"sku": ["4006381333931"]}


def test_a_checked_identifier_is_read_from_an_unlabelled_pair_but_not_from_other_items() -> None:
    cards = '<ul><li><a href="/p/2">Other</a> 5901234123457</li><li><a href="/p/3">Another</a> 9780306406157</li></ul>'
    related = '<div itemprop="isRelatedTo">EAN: 4012345678901</div>'

    assert _values(f"<main><p>EAN: 4006381333931</p>{cards}{related}</main>", MONEY_SCHEMA) == {
        "gtin": ["4006381333931"]
    }
    assert _values(f"<main>{related}</main>", MONEY_SCHEMA) == {}
    assert _values('<main><a href="/p/2">4006381333931</a></main>', MONEY_SCHEMA) == {}
    assert _values('<main><dl><dt>EAN</dt><dd><a href="/p/2">4006381333931</a></dd></dl></main>', MONEY_SCHEMA) == {}


def test_an_identifier_two_pairs_give_different_values_is_read_from_neither() -> None:
    body = "<main><table><tr><th>SKU</th><td>W-100</td></tr></table><dl><dt>SKU</dt><dd>W-200</dd></dl></main>"

    assert _values(body, MONEY_SCHEMA) == {}


def test_a_pair_naming_a_list_field_gives_its_value_list_items_or_its_one_value() -> None:
    schema = _schema(
        "detail",
        tags=_field(FieldType.LIST, None, item={"type": "text"}),
        places=_field(FieldType.LIST, "JobPosting.jobLocation", item={"type": "location"}),
    )

    found = _collect(
        "<dl><dt>Tags</dt><dd><ul><li>Kettles</li><li>Tea</li></ul></dd></dl><p>Places: Lakeside</p>", schema
    )

    assert {name: [value for value, _ in values] for name, values in found.items()} == {
        "tags": [["Kettles", "Tea"]],
        "places": [[{"text": "Lakeside", "locality": None, "region": None, "country": None, "remote_flag": None}]],
    }


PRICE_ROLES = _schema(
    "detail",
    price=next(p for p in load_presets() if p.name == "commerce_detail").fields["price"],
    list_price=_field(FieldType.MONEY, "Offer.priceSpecification{priceType=StrikethroughPrice}.price+priceCurrency"),
    unit_price=_field(FieldType.MONEY, "Offer.priceSpecification{referenceQuantity}.price+priceCurrency"),
)
_EUR = "EUR"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<main><p><s>€40</s> €25</p></main>", {"price": ["25"], "list_price": ["40"]}),
        ("<main><p><strike>€40</strike> €25</p></main>", {"price": ["25"], "list_price": ["40"]}),
        (
            '<main><p><span style="color: red; text-decoration: line-through">€40</span> <b>€25</b></p></main>',
            {"price": ["25"], "list_price": ["40"]},
        ),
        ("<main><p>€25/kg</p></main>", {"unit_price": ["25"]}),  # a unit's price, not the item's
        ("<main><p>$25/mo</p></main>", {}),  # an instalment or a subscription
    ],
)
def test_money_fields_read_the_main_region_by_their_price_role(body: str, expected: dict[str, list[str]]) -> None:
    # Extraction spec, "Structural DOM values": struck text is the list price; an amount per unit or period is no price.
    found = _values(body, PRICE_ROLES)

    assert found == {name: [{"amount": a, "currency": _EUR} for a in amounts] for name, amounts in expected.items()}
