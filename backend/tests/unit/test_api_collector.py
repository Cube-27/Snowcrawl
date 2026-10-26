"""The API recipe collector: an `api` capture's JSON read through the recipe's map, each value an `api`
candidate located as `api:<recipe id>:<normalized path>`, owned by the page on a detail page and by the rows the
recipe's `rows` query selects on a listing.

The documents are synthetic.
"""

import json
from datetime import UTC, datetime

from pydantic import JsonValue

from snowcrawl.collect.api import ApiCollector
from snowcrawl.collect.base import PAGE
from snowcrawl.collect.config import Executed
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import PageKind, SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

CTX = Context(captured_at=datetime(2026, 10, 14, tzinfo=UTC), base_url="https://shop.example.test/p/1")
DETAIL_MAP = {"title": "$['data']['name']", "price": "$['data']['price']+$['data']['currency']"}


def _schema(page_kind: PageKind, **fields: FieldSpec) -> SchemaDef:
    return SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind=page_kind,
        fields=fields,
    )


DETAIL = _schema(
    "detail",
    title=FieldSpec(type=FieldType.TEXT, description="d", required=True),
    price=FieldSpec(type=FieldType.MONEY, description="d"),
)
LISTING = _schema(
    "listing",
    title=FieldSpec(type=FieldType.TEXT, description="d", required=True),
    category=FieldSpec(type=FieldType.TEXT, description="d", scope="page"),
)


def _collect(document: object, schema: SchemaDef, collector: ApiCollector) -> Executed:
    return collector.collect(ParsedPage(json.dumps(document)), schema, CTX)


def _values(executed: Executed) -> list[tuple[str, str, str, JsonValue]]:
    return [
        (c.field, c.entity_id, c.locator, c.normalized.value if isinstance(c.normalized, Ok) else None)
        for c in executed.candidates
    ]


def test_a_detail_page_values_are_api_candidates_of_the_page() -> None:
    document = {"data": {"name": "Desk Lamp", "price": 19.5, "currency": "EUR"}}

    executed = _collect(document, DETAIL, ApiCollector("cfg1", DETAIL_MAP))

    assert executed.rows == ()
    assert {c.source_kind for c in executed.candidates} == {"api"}
    assert all(c.independent for c in executed.candidates)
    assert _values(executed) == [
        ("title", PAGE, "api:cfg1:$['data']['name']", "Desk Lamp"),
        ("price", PAGE, "api:cfg1:$['data']['price']+$['data']['currency']", {"amount": "19.5", "currency": "EUR"}),
    ]
    assert executed.diagnostics == ()


def test_a_listing_reads_row_fields_relative_to_each_row_and_page_fields_at_the_root() -> None:
    document = {"category": "Lamps", "items": [{"name": "Desk Lamp"}, "not an object", {"name": "Floor Lamp"}]}
    collector = ApiCollector("cfg1", {"title": "$['name']", "category": "$['category']"}, rows="$['items'][*]")

    executed = _collect(document, LISTING, collector)

    assert [(row.id, row.types, row.parent) for row in executed.rows] == [
        ("api:cfg1:$['items'][0]", ("ListItem",), None),
        ("api:cfg1:$['items'][2]", ("ListItem",), None),
    ]
    assert _values(executed) == [
        ("category", PAGE, "api:cfg1:$['category']", "Lamps"),
        ("title", "api:cfg1:$['items'][0]", "api:cfg1:$['items'][0]['name']", "Desk Lamp"),
        ("title", "api:cfg1:$['items'][2]", "api:cfg1:$['items'][2]['name']", "Floor Lamp"),
    ]


RECIPE = _schema(
    "detail",
    title=FieldSpec(type=FieldType.TEXT, description="d", required=True),
    body=FieldSpec(type=FieldType.LONG_TEXT, description="d"),
    images=FieldSpec(type=FieldType.LIST, description="d", item=FieldSpec(type=FieldType.IMAGE_URL, description="d")),
)


def test_a_list_field_reads_every_match_and_another_field_needs_exactly_one_scalar() -> None:
    document = {
        "p": {
            "title": "Lamp",
            "pics": [{"src": "https://img.example.test/a.png"}, {"src": "https://img.example.test/b.png"}],
        }
    }
    collector = ApiCollector("cfg1", {"images": "$['p']['pics'][*]['src']", "title": "$['p']['pics'][*]['src']"})

    executed = _collect(document, RECIPE, collector)

    assert _values(executed) == [
        (
            "images",
            PAGE,
            "api:cfg1:$['p']['pics'][0]['src']",
            ["https://img.example.test/a.png", "https://img.example.test/b.png"],
        )
    ]


def test_a_list_field_with_no_match_is_no_candidate() -> None:
    executed = _collect({"p": {"pics": []}}, RECIPE, ApiCollector("cfg1", {"images": "$['p']['pics'][*]['src']"}))

    assert executed.candidates == ()


def test_a_text_value_holding_markup_reads_as_its_rendered_text() -> None:
    document = {"p": {"body": "<p>Warm <b>light</b></p><meta charset='utf-8'>"}}

    executed = _collect(document, RECIPE, ApiCollector("cfg1", {"body": "$['p']['body']"}))

    assert [value for *_, value in _values(executed)] == ["Warm light"]
