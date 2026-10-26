"""A body's readings indexed by value (`api_recipe.discovery.body_readings`) over generated bodies and published
values: a published value's map paths are exactly those, in document order, whose reading is `equal` to it, as
comparing it with every reading finds them, money's joined member pairs included."""

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.collect import json_doc
from snowcrawl.config_ir.api_recipe.discovery import Reading, body_readings, member_pairs, normalized
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context
from snowcrawl.normalize.dispatch import equal
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

SCHEMA = SchemaDef(
    id=SchemaId(uuid7()),
    origin="user",
    status="active",
    name="synthetic",
    page_kind="detail",
    fields={
        "title": FieldSpec(type=FieldType.TEXT, description="d", required=True),
        "price": FieldSpec(type=FieldType.MONEY, description="d"),
        "count": FieldSpec(type=FieldType.NUMBER, description="d"),
        "place": FieldSpec(type=FieldType.LOCATION, description="d"),
    },
)
_CTX = Context(captured_at=datetime(2026, 10, 26, tzinfo=UTC), base_url="https://shop.example.test/")
_Leaf = st.sampled_from(["Blue mug", "blue  mug", "12.50", "USD", "EUR", "Paris", "Paris, France", "7", 7, 12.5, "€3"])
_Body = st.recursive(
    _Leaf, lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.sampled_from("abcd"), inner, max_size=4)
)
_Published = st.fixed_dictionaries(
    {},
    optional={
        "title": st.sampled_from(["Blue mug", "Paris"]),
        "price": st.sampled_from([{"amount": "12.50", "currency": "USD"}, {"amount": "3", "currency": "EUR"}]),
        "count": st.sampled_from([7, 12.5]),
        "place": st.sampled_from(["Paris", "Paris, France"]),
    },
)


@settings(max_examples=200, deadline=None)
@given(_Body, _Published)
def test_indexed_readings_find_the_paths_comparing_every_reading_finds(
    document: JsonValue, published: dict[str, JsonValue]
) -> None:
    leaves = [Reading(path, raw) for path, raw in json_doc.scalars(document)]
    pairs = list(member_pairs(document))
    expected: dict[str, dict[str, None]] = {}
    for name, value in published.items():
        spec = SCHEMA.fields[name]
        readings = normalized(leaves + (pairs if spec.type is FieldType.MONEY else []), spec, _CTX)
        if paths := dict.fromkeys(reading.path for reading, read in readings if equal(read, value, spec)):
            expected[name] = paths

    found = body_readings(document, _CTX, SCHEMA).hits(published)

    assert {name: list(paths) for name, paths in found.items()} == {
        name: list(paths) for name, paths in expected.items()
    }
