"""The DOM description collector (requirement "DOM description block"): the prose block of a detail
page that states a long_text field in full, found as the block that extends what another collector stated or as the
section a heading or an ARIA label names with the field's words, and, where only a summary is stated, the main text.
Pages are synthetic (`example.test`, C3, C9)."""

from datetime import UTC, datetime

import pytest

from snowcrawl.collect.base import PAGE, Candidate
from snowcrawl.collect.dom_text import DescriptionCollector
from snowcrawl.collect.locator import format_locator
from snowcrawl.collect.schema_org_map import candidate
from snowcrawl.content.page import ParsedPage, node_xpath
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import PageKind, SchemaId, SourceKind
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

PAGE_URL = "https://shop.example.test/p/1"
CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url=PAGE_URL, page_url=PAGE_URL, html_lang="en")


def _field(type_: FieldType, path: str | None) -> FieldSpec:
    return FieldSpec.model_validate({"type": type_, "description": "d", "schema_org": path})


def _schema(page_kind: PageKind = "detail") -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind=page_kind,
        fields={
            "title": _field(FieldType.TEXT, "Product.name"),
            "description": _field(FieldType.LONG_TEXT, "Product.description"),
            "care": _field(FieldType.LONG_TEXT, None),
        },
    )


def _stated(kind: SourceKind, value: str, field: str = "description") -> Candidate:
    made = candidate(field, _schema().fields[field], PAGE, kind, value, format_locator("dom", "/html"), CTX, [])
    assert made is not None
    return made


def _read(body: str, *stated: Candidate, schema: SchemaDef | None = None) -> dict[str, str]:
    page = ParsedPage(f"<html><body>{body}</body></html>")
    result = DescriptionCollector().collect(page, schema or _schema(), CTX, stated)
    found = {}
    for c in result.candidates:
        assert (c.entity_id, c.source_kind, c.independent) == (PAGE, "dom", True)
        assert isinstance(c.normalized, Ok)
        assert isinstance(c.normalized.value, str)
        found[c.field] = c.normalized.value
    return found


SUMMARY = "A sturdy kettle for everyday use."
FIRST = "A sturdy kettle for everyday use. It boils a litre in two minutes."
SECOND = "The lid locks and the handle stays cool."
TAGLINE = "Sturdy everyday kettle; boils a litre fast."  # a summary that is no prefix of the text


def test_block_extending_a_summary_is_the_description() -> None:
    body = f"<main><h1>Kettle</h1><div><p>{FIRST}</p><p>{SECOND}</p></div><button>Buy</button></main>"
    found = _read(body, _stated("og", SUMMARY))
    assert found == {"description": f"{FIRST}\n{SECOND}"}


def test_the_description_does_not_go_on_past_a_heading_after_the_stated_text() -> None:
    # The title, the text, then a lower-ranked section of its own, all in one block.
    body = f"<main><div><h1>Kettle</h1><p>{FIRST}</p><h2>Delivery</h2><p>Ships in a day.</p></div></main>"
    assert _read(body, _stated("og", SUMMARY)) == {"description": FIRST}


def test_a_heading_the_stated_text_holds_is_within_the_description() -> None:
    stated = f"{FIRST} Usage {SECOND}"
    body = f"<main><h1>Kettle</h1><div><p>{FIRST}</p><h3>Usage</h3><p>{SECOND}</p></div></main>"
    assert _read(body, _stated("jsonld", stated)) == {"description": f"{FIRST}\nUsage\n{SECOND}"}


def test_review_and_tangential_blocks_are_not_the_description() -> None:
    body = (
        "<main><h1>Kettle</h1>"
        f"<aside><p>{FIRST} Sold out.</p></aside>"
        f'<div itemprop="review"><p>{FIRST} Loved it.</p></div>'
        f"<dialog open><p>{FIRST} Close.</p></dialog>"
        f"<div><p>{FIRST}</p></div></main>"
    )
    assert _read(body, _stated("jsonld", SUMMARY)) == {"description": FIRST}


def test_main_text_stands_for_a_description_only_an_og_summary_states() -> None:
    body = (
        f"<main><h1>Kettle</h1><button>Buy</button><div><h2>Overview</h2><p>{FIRST}</p><p>{SECOND}</p></div>"
        "<p>Short note.</p></main>"
    )
    found = _read(body, _stated("og", "A sturdy kettle that boils fast."))
    assert found["description"] == f"{FIRST}\n{SECOND}"
    # A line that only names the item (fewer words of its own than of the title) summarizes no block in particular.
    named = body.replace("<h1>Kettle</h1>", "<h1>Acme Steel Kettle 2L</h1>")
    found = _read(named, _stated("og", "Buy Acme Steel Kettle 2L at Example."))
    assert found["description"] == f"{FIRST}\n{SECOND}"
    # A value of another kind restating the summary is that summary, not a description of its own.
    summary = "A sturdy kettle that boils fast."
    found = _read(body, _stated("og", summary), _stated("jsonld", summary))
    assert found["description"] == f"{FIRST}\n{SECOND}"


def test_a_block_of_notes_is_no_main_text() -> None:
    # Each line opens with a superscript marker (Unicode `<super>`): the page's notes, however long.
    notes = "".join(f"<p>{mark}Pay monthly at 0% APR over twelve months, subject to approval.</p>" for mark in "¹²³")
    body = f"<main><h1>Kettle</h1><div><p>{FIRST}</p></div><div>{notes}</div></main>"
    assert _read(body, _stated("og", "A sturdy kettle that boils fast.")) == {"description": FIRST}


@pytest.mark.parametrize(
    "extra",
    [
        '<div><a href="#details">More details</a></div>',  # a link line after the text
        "<dl><dt>Article no</dt><dd>K-100</dd></dl>",  # HTML Standard §4.4.9: name-value groups, not prose
    ],
)
def test_a_link_line_or_a_description_list_beside_the_text_is_not_the_description(extra: str) -> None:
    body = f"<main><h1>Kettle</h1><div><p>{FIRST}</p>{extra}</div><p>Note.</p></main>"
    assert _read(body, _stated("og", SUMMARY)) == {"description": FIRST}


def test_a_legend_is_no_main_text() -> None:
    # HTML Standard §4.10.16: a `legend` captions a fieldset's group of controls, however long; it is no prose.
    legend = "Choose the size and the colour of your kettle before adding it to the basket, then pick a delivery day."
    body = (
        f"<main><h1>Kettle</h1><fieldset><legend>{legend}</legend><input type=radio name=s></fieldset>"
        f"<div><p>{FIRST}</p></div></main>"
    )
    assert _read(body) == {"description": FIRST}


def test_a_one_sentence_line_is_no_main_text() -> None:
    # An image overlay's hint or a stock notice is the longest block of some buy regions: one line is no running text.
    assert (
        _read("<main><h1>Kettle</h1><p>Pick a size to see if it is in stock nearby.</p><p>Tap to enlarge</p></main>")
        == {}
    )


def test_a_section_panel_holding_a_control_gives_its_prose() -> None:
    # A disclosure panel's "more" button beside the prose: the section's text is the panel's prose.
    body = (
        "<main><h1>Kettle</h1><h2><button>Description</button></h2>"
        f"<div><div><p>{FIRST}</p><p>{SECOND}</p></div><button>Read more</button></div><h2>Care</h2><p>Dry.</p></main>"
    )
    assert _read(body, _stated("og", "Buy Kettle at Example."))["description"] == f"{FIRST}\n{SECOND}"


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        # The buy box: the title heading and a price beside a line of text.
        (f"<main><div><h1>Kettle</h1><p>{FIRST} Now on sale.</p></div><p>Note.</p></main>", TAGLINE),
        # A block no longer than the summary.
        ("<main><h1>Kettle</h1><p>Sturdy kettle, boils.</p></main>", TAGLINE),
        # A notice that restates none of the summary's words beyond the title's.
        ("<main><h1>Kettle</h1><p>Prices include tax; delivery fees are shown at checkout.</p></main>", TAGLINE),
    ],
)
def test_main_text_must_be_what_the_summary_summarizes(body: str, summary: str) -> None:
    assert _read(body, _stated("og", summary)) == {}


def test_a_main_text_block_holding_a_read_price_is_the_buy_box() -> None:
    body = f"<main><h2>Kettle</h2><div><p>{FIRST}</p><p>{SECOND}</p><span>$20</span></div><p>Note.</p></main>"
    page = ParsedPage(f"<html><body>{body}</body></html>")
    span = page.tree.css_first("span")
    assert span is not None
    schema = _schema()
    schema = schema.model_copy(update={"fields": {**schema.fields, "price": _field(FieldType.MONEY, None)}})
    price = candidate(
        "price", schema.fields["price"], PAGE, "dom", "$20", format_locator("dom", node_xpath(span)), CTX, []
    )
    assert price is not None

    found = DescriptionCollector().collect(page, schema, CTX, (_stated("og", TAGLINE), price)).candidates

    assert found == ()


def test_a_stated_value_equal_to_the_summary_is_extended_by_a_longer_stated_text() -> None:
    body = f"<main><h1>Kettle</h1><p>{SUMMARY}</p><div><p>{FIRST}</p><p>{SECOND}</p></div></main>"
    full = f"{FIRST} {SECOND}"
    found = _read(body, _stated("og", SUMMARY), _stated("jsonld", SUMMARY), _stated("jsonld", full))
    assert found == {"description": f"{FIRST}\n{SECOND}"}


def test_an_image_among_the_paragraphs_keeps_the_rich_text_one_block() -> None:
    body = f'<main><h1>Kettle</h1><div><p>{FIRST}</p><p><img src="/a.png" alt="Kettle"></p><p>{SECOND}</p></div></main>'
    assert _read(body, _stated("og", SUMMARY)) == {"description": f"{FIRST}\n{SECOND}"}


def test_an_element_valued_description_goes_on_over_its_sibling_sections_in_its_item() -> None:
    body = (
        '<main><div itemscope itemtype="https://schema.org/JobPosting"><h1 itemprop="title">Engineer</h1>'
        f'<section itemprop="description"><p>{FIRST}</p></section>'
        f'<section><h2>Duties</h2><p>{SECOND}</p><img src="/icon.png" alt=""></section>'
        '<span itemprop="datePosted">2026-10-01</span><p>Apply today.</p></div></main>'
    )
    page = ParsedPage(f"<html><body>{body}</body></html>")
    element = page.tree.css_first('[itemprop="description"]')
    assert element is not None
    locator = format_locator("microdata", node_xpath(element))
    stated = candidate("description", _schema().fields["description"], PAGE, "microdata", FIRST, locator, CTX, [])
    assert stated is not None

    found = DescriptionCollector().collect(page, _schema(), CTX, (stated,)).candidates

    assert [c.normalized for c in found] == [Ok(f"{FIRST}\nDuties\n{SECOND}")]
    # A sibling holding another property's element is not the description's text.
    nested = body.replace("<h2>Duties</h2>", '<h2 itemprop="occupationalCategory">Duties</h2>')
    nested_found = DescriptionCollector().collect(
        ParsedPage(f"<html><body>{nested}</body></html>"), _schema(), CTX, (stated,)
    )
    assert [c.normalized for c in nested_found.candidates] == [Ok(FIRST)]
    # Out of wrappers holding nothing after it, inside the item; its own heading names it, it is no text.
    wrapped = body.replace(
        f'<section itemprop="description"><p>{FIRST}</p></section>',
        f'<div><div><section itemprop="description"><h2>About</h2><p>{FIRST}</p></section></div></div>',
    )
    page = ParsedPage(f"<html><body>{wrapped}</body></html>")
    element = page.tree.css_first('[itemprop="description"]')
    assert element is not None
    locator = format_locator("microdata", node_xpath(element))
    stated = candidate("description", _schema().fields["description"], PAGE, "microdata", FIRST, locator, CTX, [])
    assert stated is not None
    wrapped_found = DescriptionCollector().collect(page, _schema(), CTX, (stated,)).candidates
    assert [c.normalized for c in wrapped_found] == [Ok(f"{FIRST}\nDuties\n{SECOND}")]


def test_schema_without_long_text_reads_nothing() -> None:
    schema = SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind="detail",
        fields={"title": _field(FieldType.TEXT, "Product.name")},
    )
    assert _read(f"<main><p>{FIRST}</p></main>", schema=schema) == {}


def test_a_heading_naming_a_list_field_gives_the_items_of_its_section_lists() -> None:
    schema = _schema().model_copy(
        update={"fields": {"requirements": FieldSpec.model_validate({"type": "list", "item": {"type": "text"}})}}
    )
    body = "<main><h2>Requirements</h2><ul><li>Kettles</li><li>Tea <b>leaves</b></li></ul><h2>Other</h2><p>x</p></main>"
    page = ParsedPage(f"<html><body>{body}</body></html>")

    found = DescriptionCollector().collect(page, schema, CTX, ()).candidates

    assert [(c.field, c.normalized) for c in found] == [("requirements", Ok(["Kettles", "Tea leaves"]))]
    # Two sections that list different items under the field's heading leave it unread.
    twice = body.replace("</main>", "<h3>Requirements</h3><ol><li>Cups</li></ol></main>")
    assert DescriptionCollector().collect(ParsedPage(twice), schema, CTX, ()).candidates == ()
    # A hidden item is no item, and a hidden heading names nothing.
    hidden = body.replace("<li>Kettles</li>", '<li>Kettles</li><li style="display:none">Spoons</li>')
    assert [c.normalized for c in DescriptionCollector().collect(ParsedPage(hidden), schema, CTX, ()).candidates] == [
        Ok(["Kettles", "Tea leaves"])
    ]
    unshown = body.replace("<h2>Requirements</h2>", "<h2 hidden>Requirements</h2>")
    assert DescriptionCollector().collect(ParsedPage(unshown), schema, CTX, ()).candidates == ()
