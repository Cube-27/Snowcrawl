"""The number normalizer: a decimal number as the page states it, such as a rating value, from a
JSON number or the one number a text holds, read by the CLDR number symbols of the page's locales. The value is the
decimal in fixed-point notation with its digits as written. A text with no number, two numbers or a sign that may be
a dash declines (C7)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.normalize import number, numbers
from snowcrawl.normalize.base import Ambiguous, Context, Invalid, Ok
from snowcrawl.normalize.dispatch import normalize
from snowcrawl.schemas.types import FieldSpec, FieldType

CTX = Context(captured_at=datetime(2026, 1, 1, tzinfo=UTC))
EN = CTX.model_copy(update={"html_lang": "en-US"})
DE = CTX.model_copy(update={"html_lang": "de-DE"})
NUMBER = FieldSpec(type=FieldType.NUMBER)

# The CLDR number conventions read across every locale are built once here, not inside the first example's deadline.
numbers.warm()

_decimals = st.decimals(min_value=0, max_value=10**6, places=2, allow_nan=False, allow_infinity=False)


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_a_json_integer_is_its_decimal(n: int) -> None:
    assert number.normalize(n, NUMBER, CTX) == Ok(str(n))
    assert normalize(n, NUMBER, CTX) == Ok(str(n))


@pytest.mark.parametrize(("raw", "value"), [(4.5, "4.5"), (-2.25, "-2.25"), (1e-7, "0.0000001")])
def test_a_json_float_is_the_decimal_it_writes(raw: float, value: str) -> None:
    assert number.normalize(raw, NUMBER, CTX) == Ok(value)


@given(_decimals)
def test_the_one_number_in_an_english_text_is_its_value_and_normalizes_to_itself(value: Decimal) -> None:
    first = number.normalize(f"Rated {value:f} stars", NUMBER, EN)

    assert first == Ok(f"{value:f}")
    assert number.normalize(f"{value:f}", NUMBER, EN) == first


def test_the_page_locale_reads_the_decimal_symbol() -> None:
    assert number.normalize("4,5", NUMBER, DE) == Ok("4.5")
    assert number.normalize("4.50", NUMBER, EN) == Ok("4.50")
    assert number.normalize("1,250", NUMBER, CTX) == Ambiguous("decimal_separator", ("1.250", "1250"))


@pytest.mark.parametrize(
    ("declared", "written", "value"),
    [
        ({"html_lang": "de"}, "1.250", "1250"),
        ({"og_locale": "en_GB"}, "1,250", "1250"),
        ({"content_language": ("de-AT",)}, "1,250", "1.250"),
        ({"in_language": ("en",)}, "1.250", "1.250"),
    ],
)
def test_every_declared_locale_settles_an_ambiguous_number(
    declared: dict[str, object], written: str, value: str
) -> None:
    assert number.normalize(written, NUMBER, CTX.model_copy(update=declared)) == Ok(value)


def test_a_number_only_one_convention_reads_ignores_the_declared_locale() -> None:
    assert number.normalize("1.234,5", NUMBER, EN) == Ok("1234.5")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("no rating", "no_number"),
        ("4.5 out of 5", "several_numbers"),
        ("- 4.5", "sign"),
        (True, "not_a_number"),
        ({"value": 4}, "not_a_number"),
        ("\ud800 4", "surrogate"),
    ],
)
def test_what_states_no_single_number_declines(raw: JsonValue, reason: str) -> None:
    assert number.normalize(raw, NUMBER, EN) == Invalid(reason)


@given(st.integers(min_value=1, max_value=100), st.integers(min_value=0, max_value=100))
def test_a_rating_on_a_stated_scale_maps_linearly_onto_five_points(best_above_worst: int, at: int) -> None:
    worst = 0
    best = 5 + best_above_worst  # schema.org Rating: a scale whose best is 5 is the five-point one already
    value = Decimal(min(at, best))
    raw: dict[str, JsonValue] = {"ratingValue": str(value), "bestRating": best, "worstRating": worst}

    result = number.normalize(raw, NUMBER, CTX)

    assert isinstance(result, Ok)
    assert abs(Decimal(str(result.value)) - value * 5 / best) <= Decimal("0.005")


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ({"ratingValue": "8.6"}, "8.6"),  # no stated scale: as stated, never rescaled
        ({"ratingValue": "4.5", "bestRating": "5", "worstRating": "1"}, "4.5"),
        ({"ratingValue": "8", "bestRating": "10"}, "3.89"),  # worstRating defaults to 1
        ({"ratingValue": "12", "bestRating": "10", "worstRating": "0"}, "12"),  # off its stated scale
        ({"ratingValue": "90", "bestRating": "100", "worstRating": "0", "reviewCount": 3}, "4.5"),
    ],
)
def test_a_rating_is_rescaled_only_by_the_scale_the_page_states(raw: dict[str, JsonValue], value: str) -> None:
    assert number.normalize(raw, NUMBER, CTX) == Ok(value)
