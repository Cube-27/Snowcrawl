"""The enum normalizer and the vocabulary framework: schema.org members by IRI or
name, language-tagged type vocabularies by token sequence (never substring order), and declines on no match, two
members, and empty input."""

import re
from datetime import UTC, datetime

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from snowcrawl.normalize import dispatch, enum, vocab
from snowcrawl.normalize.base import Ambiguous, Context, Invalid, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType

CTX = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC))
SCHEMA = FieldSpec(type=FieldType.ENUM, vocab="schema:ItemAvailability")
TYPE = FieldSpec(type=FieldType.ENUM, vocab="type:availability")
MEMBERS = ("BackOrder", "InStock", "LimitedAvailability", "OutOfStock", "PreOrder", "SoldOut")

_members = st.sampled_from(MEMBERS)
_whitespace = st.text(alphabet=st.characters(categories=["Zs", "Cc"]).filter(str.isspace), max_size=8)
_rest = st.sampled_from(["", ".", " (5 available)", "\nShips today", ": 3", "!"])


def _phrase(member: str) -> str:
    return " ".join(re.findall(r"[A-Z][a-z]*", member)).lower()


_FORMS = ["https://schema.org/{}", "schema:{}", "{}", "//schema.org/{}", "HTTP://Schema.org/{}"]


@given(_members, st.sampled_from(_FORMS), _whitespace)
def test_a_schema_member_matches_by_iri_or_name_or_node_reference(member: str, form: str, pad: str) -> None:
    assert enum.normalize(pad + form.format(member) + pad, SCHEMA, CTX) == Ok(member)
    assert enum.normalize({"@id": f"https://schema.org/{member}"}, SCHEMA, CTX) == Ok(member)
    assert enum.normalize(member.upper(), SCHEMA, CTX) == Ok(member)
    assert enum.normalize(f"http://schema.org/{member.lower()}", SCHEMA, CTX) == Ok(member)


@pytest.mark.parametrize(
    "raw",
    [
        "https://schema.org/Product",
        "https://example.test/InStock",
        "https://schema.org/In\tStock",
        "red",
        "Not in stock",
        "5 in stock",
    ],
)
def test_a_name_or_phrase_that_is_no_member_is_invalid(raw: str) -> None:
    assert enum.normalize(raw, SCHEMA, CTX) == Invalid("not_a_member")


@given(_members, _whitespace, _rest)
def test_text_opening_with_a_member_phrase_matches_through_the_type_vocabulary(
    member: str, pad: str, rest: str
) -> None:
    # The phrase is the text's whole first clause; what follows punctuation, a digit or a line break is not read.
    assert enum.normalize(f"{pad}{_phrase(member).title()}{rest}", SCHEMA, CTX) == Ok(member)
    assert enum.normalize(f"{_phrase(member).upper()}{rest}", TYPE, CTX) == Ok(member)


@pytest.mark.parametrize(
    ("raw", "member"),
    [("New", "NewCondition"), ("used", "UsedCondition"), ("Refurbished condition", "RefurbishedCondition")],
)
def test_a_condition_phrase_names_its_offer_item_condition(raw: str, member: str) -> None:
    condition = FieldSpec(type=FieldType.ENUM, vocab="schema:OfferItemCondition")

    assert enum.normalize(raw, condition, CTX) == Ok(member)
    assert enum.normalize("Like new", condition, CTX) == Invalid("not_a_member")


@given(_members, _members)
def test_text_stating_two_members_is_ambiguous(a: str, b: str) -> None:
    assume(a != b)
    assume(not set(_phrase(a).split()) & set(_phrase(b).split()))

    result = enum.normalize(f"{_phrase(a)}. the item {_phrase(b)}", SCHEMA, CTX)

    assert result == Ambiguous("several_members", tuple(sorted((a, b))))


@pytest.mark.parametrize("raw", [3, None, ["InStock"], {"@id": 3}, {"@id": "InStock", "name": "x"}])
def test_a_schema_value_that_is_neither_a_string_nor_a_node_reference_is_invalid(raw: object) -> None:
    assert enum.normalize(raw, SCHEMA, CTX) == Invalid("not_a_string")  # type: ignore[arg-type]


@given(_whitespace)
def test_empty_input_is_invalid(raw: str) -> None:
    assert enum.normalize(raw, TYPE, CTX) == Invalid("empty")
    assert enum.normalize(raw, SCHEMA, CTX) == Invalid("empty")


@pytest.mark.parametrize("ref", ["type:nonexistent", "schema:NoSuchEnumeration"])
def test_an_unknown_vocabulary_declines(ref: str) -> None:
    assert enum.normalize("InStock", FieldSpec(type=FieldType.ENUM, vocab=ref), CTX) == Invalid("unknown_vocab")


@pytest.mark.parametrize(
    ("codes", "error"),
    [({"A": ["In-Stock"]}, "not in token form"), ({"A": ["in stock"], "B": ["in stock"]}, "listed under two codes")],
)
def test_a_malformed_vocabulary_is_refused(codes: dict[str, list[str]], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        vocab.Entries.of(codes)


GENDER = FieldSpec(type=FieldType.ENUM, vocab="type:gender")
_codes = st.sampled_from(["male", "female", "unisex", "kids"])
_languages = st.sampled_from([None, "en", "de", "ja"])


@given(_codes, _languages)
def test_a_gender_code_is_read_in_any_case_and_page_language(code: str, language: str | None) -> None:
    ctx = Context(captured_at=CTX.captured_at, html_lang=language)

    assert enum.normalize(code.upper(), GENDER, ctx) == Ok(code)
    assert enum.normalize(f"{code.title()}.", GENDER, ctx) == Ok(code)


@pytest.mark.parametrize(("raw", "code"), [("https://schema.org/Male", "male"), ({"@id": "schema:Female"}, "female")])
def test_a_schema_org_gender_iri_states_its_code(raw: object, code: str) -> None:
    assert enum.normalize(raw, GENDER, CTX) == Ok(code)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    ["Men's", "Women", "toddler", "https://example.test/male", "https://schema.org/Male/Other", "female and more"],
)
def test_a_word_the_gender_sources_do_not_list_states_no_code(raw: str) -> None:
    assert enum.normalize(raw, GENDER, CTX) == Invalid("no_match")


def test_only_a_schema_org_enumeration_is_evidence_by_its_syntax() -> None:
    # A type vocabulary's codes are common words (a navigation's "Kids"), admitted by equality only.
    assert dispatch.checkable("InStock", SCHEMA)
    assert not dispatch.checkable("kids", GENDER)
