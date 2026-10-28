"""Number normalizer: a decimal number as the page states it, such as a rating value (schema.org
`Rating.ratingValue`, whose range is Number or Text).

Accepts:
- a JSON number;
- a string, from structured data, the DOM or an LLM: the one number in it ("Rated 4.5 stars" is 4.5), read by the
  Unicode CLDR number symbols of the locales the page and the request state (`normalize.numbers`, as money and count
  read numbers);
- a JSON object from structured data: schema.org `ratingValue` with its AggregateRating's `reviewCount`
  (`ratingValue+reviewCount`), declined `no_ratings` when the count is 0, and with its `bestRating` and
  `worstRating`, which put it on the five-point scale.

The value is the decimal in fixed-point notation as a JSON string, with its digits as written (`4.50` stays `4.50`),
as money writes an amount; equality compares decimals. A record serves it as a JSON number rounded to two decimal
places (`served`), and nothing compares that form. Nothing is defaulted (C7), and a rating is rescaled only by the
scale the page states (a rating of 8 with no `bestRating` stays 8): a text with no number or
two numbers ("4.5 out of 5") is `Invalid`, and so is a minus sign set apart from the number, which may be a dash; a
number the page's locales read as different values is `Ambiguous`.
"""

from collections.abc import Hashable, Mapping
from contextlib import suppress
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from pydantic import JsonValue

from snowcrawl.normalize import numbers
from snowcrawl.normalize.base import Ambiguous, Context, Invalid, NormalizeResult, Ok, Raw, raw_key
from snowcrawl.schemas.types import FieldSpec, FieldType

# schema.org AggregateRating: `ratingValue` is the rating, `reviewCount` the count of reviews it is based on.
_RATING_VALUE, _REVIEW_COUNT = "ratingValue", "reviewCount"
# schema.org Rating: `bestRating` (default 5) and `worstRating` (default 1) state the scale of `ratingValue`.
_BEST, _WORST = "bestRating", "worstRating"
_DEFAULT_BEST, _DEFAULT_WORST = 5, 1
_RATING_KEYS = frozenset({_RATING_VALUE, _REVIEW_COUNT, _BEST, _WORST})
_FIVE = Decimal(5)
# A served number keeps at most two decimal places, rounded half to even (ISO
# 80000-1 Annex B, IEEE 754's default): a rating's float noise, `4.439477977161501`, serves as 4.44.
_SERVED_PLACES = Decimal("0.01")
_EXACT_DIGITS = 15  # IEEE 754 binary64 holds 15 significant decimal digits exactly


def normalize(raw: Raw, spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """Normalize a number field."""
    if spec.type is not FieldType.NUMBER:
        raise TypeError(f"the number normalizer does not handle type {spec.type}")
    if isinstance(raw, dict):
        return _rating(raw, spec, ctx)
    if isinstance(raw, str):
        return _text(raw, ctx)
    value = numbers.json_decimal(raw)
    if value is None or not value.is_finite():
        return Invalid("not_a_number")
    return Ok(_written(value))


def _rating(raw: Mapping[str, JsonValue], spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """A schema.org `ratingValue` joined to its AggregateRating's `reviewCount` (the `+reviewCount` of a path): the
    count of reviews the rating is based on. A count of 0 rates nothing, so the value is no rating (C7: a placeholder
    is not a value); a count that is not stated or not a number leaves the value as stated. A stated scale
    (`+bestRating+worstRating`) puts the value on the five-point scale (`_on_five_points`)."""
    if not raw.keys() <= _RATING_KEYS or _RATING_VALUE not in raw:
        return Invalid("not_a_number")
    if _count(raw.get(_REVIEW_COUNT)) == 0:
        return Invalid("no_ratings")
    result = normalize(raw[_RATING_VALUE], spec, ctx)
    if not isinstance(result, Ok) or not raw.keys() & {_BEST, _WORST}:
        return result
    best = _bound(raw.get(_BEST, _DEFAULT_BEST), spec, ctx)
    return _on_five_points(result, best, _bound(raw.get(_WORST, _DEFAULT_WORST), spec, ctx))


def _bound(raw: JsonValue, spec: FieldSpec, ctx: Context) -> Decimal | None:
    """A scale bound read as the rating value is (the page's number symbols), `None` when it is no one finite number."""
    result = normalize(raw, spec, ctx)
    return Decimal(str(result.value)) if isinstance(result, Ok) else None


def _on_five_points(rating: Ok, best: Decimal | None, worst: Decimal | None) -> Ok:
    """A rating on the scale the page states (schema.org `Rating`: `bestRating`, default 5, and `worstRating`, default
    1), mapped linearly from [worst, best] onto [0, 5]. A scale whose best is 5 is the five-point scale already, so its
    values stay as stated; so does a value off its stated scale, or a scale that is no range, which states nothing to
    map by."""
    value = Decimal(str(rating.value))
    if best is None or worst is None or best == _FIVE or not worst <= value <= best or worst >= best:
        return rating
    scaled = (value - worst) * _FIVE / (best - worst)
    return Ok(_written(scaled.quantize(_SERVED_PLACES, rounding=ROUND_HALF_EVEN).normalize()))


def _count(raw: JsonValue) -> Decimal | None:
    if isinstance(raw, str):
        with suppress(InvalidOperation):
            return Decimal(raw.strip())
        return None
    return numbers.json_decimal(raw)


def _text(raw: str, ctx: Context) -> NormalizeResult:
    found = numbers.stated(raw)
    if isinstance(found, str):
        return Invalid(found)
    # CLDR number patterns write the minus sign against the digits; one set apart from them may be a dash.
    signed = found.before.endswith(numbers.MINUS)  # each minus sign is one character
    if numbers.negative(found.before[:-1] if signed else found.before):
        return Invalid("sign")
    values = [
        -v if signed else v for v in numbers.readings(found.written, numbers.locale_set(ctx), ctx.page_convention)
    ]
    if not values:
        return Invalid("not_a_number")  # a run no CLDR convention parses strictly ("1,2,3")
    if len(values) == 1:
        return Ok(_written(values[0]))
    return Ambiguous("decimal_separator", tuple(_written(v) for v in sorted(values)))


def _written(value: Decimal) -> str:
    return format(value, "f")


def key(value: JsonValue, _spec: FieldSpec) -> Hashable:
    """Equality as a key: equal as decimals, so `4.5` equals `4.50`."""
    if isinstance(value, str):
        with suppress(InvalidOperation):
            return Decimal(value)
    return raw_key(value)


def served(value: JsonValue) -> JsonValue:
    """A published number as a record serves it: a JSON number of at most two
    decimal places (`numbers.json_number`); any other value as it is."""
    if isinstance(value, str):
        with suppress(InvalidOperation):
            number = Decimal(value)
            if number.is_finite():
                # Past binary64's 15 significant digits the fraction is lost in the JSON number anyway, and rounding
                # such a value would exceed the decimal context's precision.
                if number.adjusted() < _EXACT_DIGITS:
                    number = number.quantize(_SERVED_PLACES, rounding=ROUND_HALF_EVEN)
                return numbers.json_number(number)
    return value
