"""The admission rule for embedded JSON state and network bodies, for any document,
page text and set of other candidates: a value is admitted exactly when it equals another source kind's candidate of
the field (then owned by that candidate's entity, not independent) or, for a field with no such candidate whose type
has a checkable syntax, when it is visible (then owned by the page, independent). The field here is text, which has
none, so its visible values are never admitted on their own."""

import json
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from snowcrawl.collect.base import Candidate
from snowcrawl.collect.js_state import JsStateCollector
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import checkable, equal, normalize
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

CTX = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), base_url="https://example.test/", page_url=None)
NAME = FieldSpec(type=FieldType.TEXT, description="d", schema_org="Product.name")
SCHEMA = SchemaDef(
    id=SchemaId(uuid7()),
    origin="user",
    status="active",
    name="synthetic",
    page_kind="detail",
    fields={"name": NAME},
)

# Few words, so that values, page text and other candidates often coincide.
_WORDS = st.sampled_from(["alpha", "Alpha", "beta", "gamma delta", "  beta "])
_DOCUMENT = st.recursive(
    _WORDS | st.integers(-3, 3) | st.booleans() | st.none(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(["a", "b", "c"]), inner, max_size=3),
    max_leaves=8,
)
_LOCATORS = {
    "jsonld": "jsonld:0:$",
    "og": "meta:og:title",
    "js_state": "js_state:/html[1]/script[1]:$",
    "xhr": "xhr:0:$",
}
_OTHER = st.tuples(
    st.sampled_from(["jsonld", "og", "js_state", "xhr"]), st.sampled_from(["page", "jsonld:0:$"]), _WORDS
)


def _other(source_kind: str, entity_id: str, raw: str) -> Candidate:
    return Candidate.model_validate(
        {
            "field": "name",
            "entity_id": entity_id,
            "source_kind": source_kind,
            "raw": raw,
            "normalized": normalize(raw, NAME, CTX),
            "locator": _LOCATORS[source_kind],
            "independent": True,
        }
    )


def _check(admitted: tuple[Candidate, ...], kind: str, others: list[Candidate], page: ParsedPage) -> None:
    anchors = [o for o in others if o.source_kind != kind]
    for candidate in admitted:
        assert isinstance(candidate.raw, str)
        assert isinstance(candidate.normalized, Ok)
        if candidate.independent:
            assert (candidate.entity_id, anchors) == ("page", [])
            assert checkable(candidate.raw, NAME)
            assert page.visible_values.visible(candidate.raw)
        else:
            assert any(
                o.entity_id == candidate.entity_id
                and isinstance(o.normalized, Ok)
                and equal(candidate.normalized.value, o.normalized.value, NAME)
                for o in anchors
            )


def _expected(document: object, kind: str, others: list[Candidate], page: ParsedPage) -> set[tuple[str, str, bool]]:
    anchors = [o for o in others if o.source_kind != kind]
    admitted: set[tuple[str, str, bool]] = set()
    for raw in _strings(document):
        if not isinstance(value := normalize(raw, NAME, CTX), Ok):
            continue
        if anchors:
            admitted |= {
                (json.dumps(value.value), o.entity_id, False)
                for o in anchors
                if isinstance(o.normalized, Ok) and equal(value.value, o.normalized.value, NAME)
            }
        elif checkable(raw, NAME) and page.visible_values.visible(raw):
            admitted.add((json.dumps(value.value), "page", True))
    return admitted


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for child in value.values() for s in _strings(child)]
    if isinstance(value, list):
        return [s for child in value for s in _strings(child)]
    return []


@given(_DOCUMENT, st.lists(_WORDS, max_size=3), st.lists(_OTHER, max_size=3))
def test_js_state_admits_exactly_by_equality_or_visibility(
    document: object, shown: list[str], others: list[tuple[str, str, str]]
) -> None:
    candidates = [_other(*o) for o in others]
    script = f"var s = {json.dumps([document])};"  # wrapped in an array: state is an object or an array
    page = ParsedPage(f"<html><head><script>{script}</script></head><body><p>{' | '.join(shown)}</p></body></html>")

    admitted = JsStateCollector().collect(page, SCHEMA, CTX, candidates).candidates

    _check(admitted, "js_state", candidates, page)
    assert len(admitted) == len(_expected(document, "js_state", candidates, page))


GENDER = FieldSpec(type=FieldType.ENUM, description="g", vocab="type:gender")
_WITH_GENDER = SCHEMA.model_copy(update={"fields": {"name": NAME, "gender": GENDER}})


@given(
    st.sampled_from(["Alpha Shoe", "Beta", "alpha  shoe"]), st.booleans(), st.sampled_from(["Men", "Women's", "Kids"])
)
def test_a_visible_enum_word_is_admitted_only_from_an_object_naming_the_item(
    held: str, listed: bool, word: str
) -> None:
    # A message catalog writes the same words as an item's data; only the object naming the item states its value.
    item = Candidate.model_validate(
        {
            "field": "name",
            "entity_id": "page",
            "source_kind": "jsonld",
            "raw": "Alpha Shoe",
            "normalized": normalize("Alpha Shoe", NAME, CTX),
            "locator": _LOCATORS["jsonld"],
            "independent": True,
        }
    )
    document = {"title": held, "tags": [word, "sale"] if listed else word}
    script = f"var s = {json.dumps(document)};"
    page = ParsedPage(f"<html><head><script>{script}</script></head><body><p>{word}</p></body></html>")

    admitted = JsStateCollector().collect(page, _WITH_GENDER, CTX, [item]).candidates

    names_item = held.split() == ["Alpha", "Shoe"]
    assert [c.raw for c in admitted if c.field == "gender"] == ([word] if names_item else [])
