"""The enum normalizer and the vocabulary framework: schema.org members by IRI or
name, language-tagged type vocabularies by token sequence (never substring order), and declines on no match, two
members, and empty input."""

import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from snowcrawl.normalize import dispatch, enum, vocab
from snowcrawl.normalize.base import Ambiguous, Context, Invalid, Ok
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import FieldSpec, FieldType

CTX = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC))
SCHEMA = FieldSpec(type=FieldType.ENUM, vocab="schema:ItemAvailability")
TYPE = FieldSpec(type=FieldType.ENUM, vocab="type:availability")
GENDER = FieldSpec(type=FieldType.ENUM, vocab="type:gender")
CONDITION = FieldSpec(type=FieldType.ENUM, vocab="schema:OfferItemCondition")
MEMBERS = ("BackOrder", "InStock", "LimitedAvailability", "OutOfStock", "PreOrder", "SoldOut")

_members = st.sampled_from(MEMBERS)
_whitespace = st.text(alphabet=st.characters(categories=["Zs", "Cc"]).filter(str.isspace), max_size=8)
_rest = st.sampled_from(["", ".", " (5 left)", "\nShips today", ": 3", "!"])


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
    ("raw", "spec", "stated"),
    [
        ("https://schema.org/Product", SCHEMA, "https://schema.org/Product"),
        ("https://example.test/InStock", SCHEMA, "https://example.test/InStock"),
        ({"@id": "https://example.test/InStock"}, SCHEMA, "https://example.test/InStock"),
        ("  Not in   stock ", SCHEMA, "Not in stock"),
        ("5 in stock", SCHEMA, "5 in stock"),
        ("Sold to a happy customer", SCHEMA, "Sold to a happy customer"),
        ("Like New", CONDITION, "Like New"),
        ("Women's shoes", GENDER, "Women's shoes"),
        ("https://schema.org/Male/Other", GENDER, "https://schema.org/Male/Other"),
    ],
)
def test_a_value_no_vocabulary_maps_publishes_as_stated_unchecked(raw: object, spec: FieldSpec, stated: str) -> None:
    # The tables may lack a page's word; the value stays as the page states it, but is no evidence of the field.
    assert enum.normalize(raw, spec, CTX) == Ok(stated, checked=False)  # type: ignore[arg-type]


@given(_members, _whitespace, _rest)
def test_text_opening_with_a_member_phrase_matches_through_the_type_vocabulary(
    member: str, pad: str, rest: str
) -> None:
    # The phrase is the text's whole first clause; what follows punctuation, a digit or a line break is not read.
    assert enum.normalize(f"{pad}{_phrase(member).title()}{rest}", SCHEMA, CTX) == Ok(member)
    if member in ("InStock", "OutOfStock"):  # the codes `type:availability` lists
        assert enum.normalize(f"{_phrase(member).upper()}{rest}", TYPE, CTX) == Ok(member)


_IN_STOCK = ["available", "available now", "ready to ship", "in stock now"]
_OUT_OF_STOCK = ["sold", "unavailable", "not available", "currently unavailable", "no longer available", "oos"]
_spacing = st.sampled_from([" ", "  ", "\u00a0", "\u2009"])


@given(
    st.sampled_from(
        [*((t, "InStock") for t in _IN_STOCK), *((t, "OutOfStock") for t in _OUT_OF_STOCK), ("pending", "BackOrder")]
    ),
    st.sampled_from([str.upper, str.lower, str.title]),
    _spacing,
    _rest,
)
def test_a_listed_stock_term_states_its_member_whatever_its_case_or_spacing(
    term: tuple[str, str], case: Callable[[str], str], space: str, rest: str
) -> None:
    written = case(term[0]).replace(" ", space)

    # A code the owner's table maps is as checked as one a member's name states.
    assert enum.normalize(written + rest, SCHEMA, CTX) == Ok(term[1])
    assert enum.normalize(written, TYPE, Context(captured_at=CTX.captured_at, html_lang="en")) == Ok(term[1])


@pytest.mark.parametrize(
    ("raw", "member"),
    [
        ("out-of-stock", "OutOfStock"),
        ("IN-STOCK", "InStock"),
        ("Instock", "InStock"),
        ("Sold out", "SoldOut"),
        ("Pre-order", "PreOrder"),
    ],
)
def test_a_member_name_in_words_is_checked_beside_the_stock_terms(raw: str, member: str) -> None:
    # A member's own words state it as evidence; "sold" inside "sold out" yields to the longer phrase.
    assert enum.normalize(raw, SCHEMA, CTX) == Ok(member)


def test_a_rival_stock_term_later_in_the_text_is_ambiguous() -> None:
    result = enum.normalize("Out of stock. Available in store", SCHEMA, CTX)

    assert result == Ambiguous("several_members", ("InStock", "OutOfStock"))


@given(
    st.sampled_from(
        [
            ("New", "NewCondition"),
            ("Brand new", "NewCondition"),
            ("PRE-OWNED", "UsedCondition"),
            ("second hand", "UsedCondition"),
            ("Vintage", "UsedCondition"),
            ("Certified refurbished", "RefurbishedCondition"),
            ("renewed", "RefurbishedCondition"),
            ("Damaged", "DamagedCondition"),
        ]
    ),
    _rest,
)
def test_a_listed_condition_term_states_its_member(term: tuple[str, str], rest: str) -> None:
    assert enum.normalize(term[0] + rest, CONDITION, CTX) == Ok(term[1])


@pytest.mark.parametrize(("raw", "member"), [("Used condition", "UsedCondition"), ("newcondition", "NewCondition")])
def test_a_condition_member_name_in_words_is_checked(raw: str, member: str) -> None:
    assert enum.normalize(raw, CONDITION, CTX) == Ok(member)


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
    ("raw", "code"),
    [(f"Men{chr(0x2019)}s", "male"), ("MENS", "male"), ("womens", "female"), ("Ladies", "female"), ("Girl's", "kids")],
)
def test_a_listed_page_term_states_its_code_whatever_its_case_or_apostrophe(raw: str, code: str) -> None:
    assert enum.normalize(raw, GENDER, Context(captured_at=CTX.captured_at, html_lang="en")) == Ok(code)


def test_a_mapped_value_is_evidence_whichever_vocabulary_maps_it() -> None:
    # Which text is the field's value is told by where the page states it, not by which table maps it.
    assert dispatch.checkable("InStock", SCHEMA)
    assert dispatch.checkable("kids", GENDER)
    assert enum.normalize("Sold", SCHEMA, CTX) == Ok("OutOfStock")


_DETAIL = next(p for p in load_presets() if p.name == "commerce_detail").fields


@pytest.mark.parametrize(
    ("stated", "member"),
    [("Sold", "OutOfStock"), ("Sold out", "SoldOut"), ("In stock", "InStock"), ("Ask us", "Ask us")],
)
def test_a_variant_rows_availability_reads_as_the_top_level_field_does(stated: str, member: str) -> None:
    rows = dispatch.normalize([{"availability": stated}], _DETAIL["variants"], CTX)

    assert isinstance(rows, Ok)
    assert rows.value == [{"availability": member}]
    assert rows.value[0] == {"availability": dispatch.normalize(stated, _DETAIL["availability"], CTX).value}  # type: ignore[union-attr]
