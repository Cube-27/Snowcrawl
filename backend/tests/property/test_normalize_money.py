"""The money normalizer: CLDR currency symbols and number symbols (Babel) for the
locales the context states, `price-parser` to locate the amount. A grounded amount always gives a value, its currency
from the best evidence or unknown; it declines on ambiguous decimal separators, ranges and amounts that are not
positive. The tests use ISO codes and CLDR symbols only."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import babel
import pytest
from babel import Locale
from babel.core import get_cldr_version
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.normalize import money
from snowcrawl.normalize.base import Ambiguous, Basis, Context, Invalid, Ok
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import FieldSpec, FieldType

AT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = Context(captured_at=AT)
MONEY = FieldSpec(type=FieldType.MONEY)
_CODES = ["EUR", "USD", "GBP", "CHF", "INR", "JPY"]
_amounts = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999999.99"), places=2)


def _ok(amount: str, currency: str | None, basis: Basis | None = None, *, checked: bool = True) -> Ok:
    return Ok({"amount": amount, "currency": currency}, basis=basis, checked=checked)


def _ctx(**fields: object) -> Context:
    return Context(captured_at=AT, **fields)  # type: ignore[arg-type]


def test_the_currency_comes_from_the_best_evidence_with_weaker_evidence_noted() -> None:
    # 1. Written with the price; 2. the item's structured data; 3. a page-wide declaration, consistent across the page;
    # 4. the page's locale and region narrowing a shared sign to one currency.
    assert money.normalize("$12 CAD", MONEY, CTX) == _ok("12", "CAD")
    assert money.normalize("$ 12", MONEY, _ctx(page_currencies=("CAD",), declared_currencies=("AUD",))) == _ok(
        "12", "CAD"
    )
    assert money.normalize("$ 12", MONEY, _ctx(declared_currencies=("AUD",))) == _ok("12", "AUD", "page")
    assert money.normalize("12", MONEY, _ctx(declared_currencies=("AUD",))) == _ok("12", "AUD", "page", checked=False)
    assert money.normalize("12", MONEY, _ctx(page_currencies=("AUD",), declared_currencies=("NZD",))) == _ok(
        "12", None, "unknown", checked=False
    )
    assert money.normalize("$12.99", MONEY, _ctx(html_lang="en-AU")) == _ok("12.99", "AUD", "locale")
    assert money.normalize("$12.99", MONEY, _ctx(html_lang="en", cctld_territory="CA")) == _ok("12.99", "CAD", "locale")
    # A language alone is every territory it is used in: `en` settles no shared sign, so the sign's primary currency
    # (CLDR's English locale) does, flagged `symbol`; so with no hint at all, and a `¥` is yen.
    assert money.normalize("$12.99", MONEY, _ctx(html_lang="en")) == _ok("12.99", "USD", "symbol")
    assert money.normalize("$5", MONEY, _ctx()) == _ok("5", "USD", "symbol")
    assert money.normalize("¥500", MONEY, _ctx()) == _ok("500", "JPY", "symbol")
    assert money.normalize("$5", MONEY, _ctx(page_currencies=("AUD",))) == _ok("5", "AUD")
    cctld = "ca"  # RFC 1591: a two-letter ISO 3166-1 code; the host is synthetic, under RFC 2606's `example` label
    assert money.normalize("$5", MONEY, _ctx(page_url=f"https://shop.example.{cctld}/p")) == _ok("5", "CAD", "locale")
    assert money.normalize("$5", MONEY, _ctx(content_language=("en-NZ",))) == _ok("5", "NZD", "locale")
    # The locale a request asked for is the caller's, not the page's; two territories that read `$` differently settle
    # nothing.
    assert money.normalize("$5", MONEY, _ctx(request_locale="en-AU")) == _ok("5", "USD", "symbol")
    assert money.normalize("$5", MONEY, _ctx(html_lang="en-US", og_locale="en-CA")) == _ok("5", "USD", "symbol")


def _cctld(code: str) -> str:
    """A synthetic URL under RFC 2606's `example` label with the ccTLD `code` (RFC 1591)."""
    return f"https://shop.example.{code}/p"


@pytest.mark.parametrize(
    ("url", "lang"),
    [
        ("https://shop.example.com/p/1", None),  # a generic TLD
        ("https://shop.example.com/en-gb/fr-ca/p", None),  # two locale segments
        ("https://ar.example.com/p", "ar"),  # a subdomain naming a territory the page's language is not used in
        (_cctld("co"), "en"),  # likewise a ccTLD
    ],
)
def test_a_url_naming_no_one_territory_of_the_page_names_no_currency(url: str, lang: str | None) -> None:
    assert money.normalize({"price": "12"}, MONEY, _ctx(page_url=url, html_lang=lang)) == _ok("12", None, "unknown")
    # The final URL decides: the requested URL never settles what it leaves unsettled.
    requested = "https://shop.example.com/en-gb/p"
    ctx = _ctx(page_url=url, request_url=requested, html_lang=lang)
    expected = _ok("12", "GBP", "url") if url == "https://shop.example.com/p/1" else _ok("12", None, "unknown")
    assert money.normalize({"price": "12"}, MONEY, ctx) == expected


def test_a_hreflang_fallback_marker_is_no_page_language() -> None:
    ctx = _ctx(page_url="https://shop.example.com/en-gb/p", hreflang=("x-default",))
    assert money.normalize({"price": "12"}, MONEY, ctx) == _ok("12", "GBP", "url")


def test_a_sign_written_with_an_abbreviation_full_stop_is_also_the_symbol_without_it() -> None:
    # CLDR writes the abbreviation `kr` both with and without its full stop, for different sets of currencies.
    assert money.normalize("kr. 12", MONEY, _ctx(page_currencies=("NOK",))) == _ok("12", "NOK")


def test_a_lone_separator_is_ambiguous_until_the_page_locale_or_convention_settles_it() -> None:
    # KWD has three decimals (ISO 4217), so both readings are amounts of it.
    readings: tuple[JsonValue, ...] = ({"amount": "1.234", "currency": "KWD"}, {"amount": "1234", "currency": "KWD"})

    assert money.normalize("1.234 KWD", MONEY, CTX) == Ambiguous("decimal_separator", readings)
    assert money.normalize("1.234 KWD", MONEY, _ctx(html_lang="en-US")) == _ok("1.234", "KWD")
    assert money.normalize("1.234 KWD", MONEY, _ctx(html_lang="de-DE")) == _ok("1234", "KWD")
    # Locales that disagree decline; the decimal symbol of the page's structured prices settles it.
    disagree = _ctx(html_lang="en-US", og_locale="de_DE")
    assert money.normalize("1.234 KWD", MONEY, disagree) == Ambiguous("decimal_separator", readings)
    assert money.normalize("1.234 KWD", MONEY, disagree.model_copy(update={"page_convention": "."})) == _ok(
        "1.234", "KWD"
    )
    assert money.normalize("1.234", MONEY, _ctx(page_convention=".")) == _ok("1.234", None, "unknown", checked=False)
    # With no currency settled, the sign's primary one (USD: two decimals) leaves the one reading it fits.
    assert money.normalize("$10.999", MONEY, CTX) == _ok("10999", "USD", "symbol")


def test_a_reading_with_more_decimals_than_the_currency_minor_unit_is_no_amount_of_it() -> None:
    # ISO 4217 minor units (CLDR): yen has none, the US dollar two.
    assert money.normalize("¥1,234", MONEY, _ctx(page_currencies=("JPY",))) == _ok("1234", "JPY")
    assert money.normalize("$10.999", MONEY, _ctx(html_lang="en-US")) == Invalid("minor_unit")


@pytest.mark.parametrize(
    ("raw", "tag", "expected"),
    [
        # CLDR's SAR symbol is `ر.س.` with a right-to-left mark; pages write it with or without both.
        ("ر.س 120", "ar-SA", ("120", "SAR")),
        ("ر.س.\u200f120", "ar-SA", ("120", "SAR")),
        # The ASCII apostrophe for the `de-CH` group symbol U+2019.
        ("CHF 1'234.50", "de-CH", ("1234.50", "CHF")),
        # Arabic-Indic and Extended Arabic-Indic digits with the `arab`/`arabext` separators.
        ("EGP \u0661\u066c\u0662\u0663\u0664\u066b\u0665\u0660", "ar-EG", ("1234.50", "EGP")),
        ("IRR \u06f1\u066c\u06f2\u06f3\u06f4", "fa-IR", ("1234", "IRR")),
    ],
)
def test_cldr_symbol_and_number_forms_pages_write(raw: str, tag: str, expected: tuple[str, str]) -> None:
    assert money.normalize(raw, MONEY, _ctx(html_lang=tag)) == _ok(*expected)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("-12.50 EUR", "not_positive"),
        ("\u221212.50 EUR", "not_positive"),
        ("0.00 EUR", "not_positive"),
        ("EUR 10 - 20", "range"),
        ("EUR 10 to 20", "range"),
        ("EUR", "no_amount"),
        ("EUR 12\ud800", "surrogate"),
        (True, "not_money"),
        (None, "not_money"),
        ({"priceCurrency": "EUR"}, "no_amount"),
        ({"price": "€12.50", "priceCurrency": "USD"}, "currency_conflict"),
        ({"price": "$10 - $20", "priceCurrency": "USD"}, "range"),
        ({"price": "Free", "priceCurrency": "EUR"}, "no_amount"),
        ({"amount": "$12.50", "currency": "USD"}, "unparsable_amount"),
        ({"price": float("nan"), "priceCurrency": "EUR"}, "unparsable_amount"),
        ({"price": "1000000000000", "priceCurrency": "EUR"}, "out_of_range"),
        ({"price": "0", "priceCurrency": "EUR"}, "not_positive"),
        ("1." + "9" * 40, "out_of_range"),
        ("EUR 1.9999", "minor_unit"),
    ],
)
def test_what_is_no_positive_single_price_is_invalid(raw: JsonValue, reason: str) -> None:
    assert money.normalize(raw, MONEY, CTX) == Invalid(reason)


@pytest.mark.parametrize(
    ("price", "tag", "amount"),
    [("$1,295.99", "en-US", "1295.99"), ("1.295,99", "de-DE", "1295.99")],
)
def test_a_structured_price_with_grouping_reads_in_the_page_locale(price: str, tag: str, amount: str) -> None:
    raw: JsonValue = {"price": price, "priceCurrency": "USD"}

    assert money.normalize(raw, MONEY, _ctx(html_lang=tag)) == _ok(amount, "USD")


# Normalization spec, money: an amount written with a CLDR per pattern and a unit is a unit's or a period's price.
PRICE = next(p for p in load_presets() if p.name == "commerce_detail").fields["price"]
UNIT_PRICE = FieldSpec(
    type=FieldType.MONEY, schema_org="Offer.priceSpecification{referenceQuantity}.price+priceCurrency"
)
_PER_LOCALES = ("en", "de", "fr", "es", "it", "nl", "pl", "sv", "ja", "ru", "ar", "he", "hi", "tr", "zh")
_PER_CATEGORIES = ("mass", "volume", "length", "area", "duration")


def _per_texts(identifier: str) -> list[str]:
    """Every way the locale's CLDR unit data writes an amount per one of its units, the amount first (`{0}`): each
    unit's per pattern (`{0}/kg`), and the compound per pattern (`{0} per {1}`) with each of the unit's names; a
    one-letter symbol only right after the separator (`{0}/{1}`), apart from it being a size (`$40 / M`)."""
    data = Locale.parse(identifier)._data
    compounds = [
        c for w in data["compound_unit_patterns"]["per"].values() if (c := w.get("compound", "")).startswith("{0}")
    ]
    units = [u for u in data["unit_patterns"] if u.split("-")[0] in _PER_CATEGORIES or u == "concentr-item"]
    own = [p for u in units for w in data["unit_patterns"][u].values() if (p := w.get("per", "")).startswith("{0}")]
    names = [n for u in units for n in data["unit_display_names"].get(u, {}).values()]
    return own + [c.replace("{1}", n) for c in compounds for n in names if _attached(c) or _letters(n) > 1]


def _attached(compound: str) -> bool:
    return not compound.partition("{1}")[0][-1:].isspace()


def _letters(name: str) -> int:
    return sum(char.isalpha() for char in name)


@given(st.sampled_from(_PER_LOCALES), _amounts, st.data())
def test_an_amount_written_per_a_cldr_unit_of_the_page_locale_is_never_the_price(
    identifier: str, amount: Decimal, data: st.DataObject
) -> None:
    text = data.draw(st.sampled_from(_per_texts(identifier))).replace("{0}", f"€{amount}")
    ctx = _ctx(html_lang=identifier)

    assert not isinstance(money.normalize(text, PRICE, ctx), Ok)
    assert money.normalize(text, UNIT_PRICE, ctx) == _ok(str(amount), "EUR")


def test_a_size_letter_apart_from_the_slash_is_no_unit() -> None:
    # A one-letter CLDR symbol is read only as written and right after the separator: `M` is German `Monat`.
    assert money.normalize("$25 / S", PRICE, _ctx(html_lang="en-US")) == _ok("25", "USD", "locale")
    assert money.normalize("$40 / M", PRICE, _ctx(html_lang="de-DE")) == _ok("40", "USD", "locale")
    assert money.normalize("$40/M", PRICE, _ctx(html_lang="de-DE")) == Invalid("per_period")


@given(_amounts)
def test_a_code_that_is_also_a_cldr_unit_is_a_currency_only_where_the_page_names_it(amount: Decimal) -> None:
    # `CUP` is ISO 4217 and the CLDR volume unit `cup`: a product name's size is no amount of money; `SEK` is also the
    # Swedish CLDR abbreviation of `sekund`, and a Swedish page names the krona.
    text = f"{amount} CUP FOOD PROCESSOR"
    assert money.normalize(text, MONEY, _ctx(html_lang="en-US", page_currencies=("USD",))) == Invalid("measure")
    assert money.normalize(f"{amount} CUP", MONEY, _ctx(page_currencies=("CUP",))) == _ok(str(amount), "CUP")
    assert money.normalize(f"{amount} SEK", MONEY, _ctx(html_lang="sv-SE")) == _ok(str(amount), "SEK")
    assert money.normalize(f"{amount} USD", MONEY, _ctx(html_lang="en-US", page_currencies=("EUR",))) == _ok(
        str(amount), "USD"
    )
    # `L` is a CLDR litre symbol (`l` in Portuguese) and a CLDR symbol of the lempira and the leu: a volume on a page
    # naming neither.
    assert money.normalize(f"Kettle {amount} L", MONEY, _ctx(html_lang="pt-BR")) == Invalid("measure")


def test_the_cldr_release_is_the_one_the_manifest_pins() -> None:
    # C4: vocabularies come from versioned data; the CLDR unit data ships in Babel, so its release is pinned.
    manifest = json.loads((Path(__file__).parents[2] / "data" / "manifest.json").read_text(encoding="utf-8"))
    [pin] = (p for p in manifest["packaged"] if p["name"] == "CLDR units")

    assert (babel.__version__, get_cldr_version()) == (pin["package_version"], pin["version"])


@given(st.integers(1, 10**9), st.sampled_from([*_CODES, None]))
def test_a_bare_number_reads_in_major_units_only_when_its_digits_or_currency_settle_it(
    count: int, code: str | None
) -> None:
    # ISO 4217 minor unit: a whole number counts the major or the minor unit unless the currency has none; one written
    # with decimals is in major units. The currency is the page's, as for any amount written without one.
    ctx = _ctx(page_currencies=() if code is None else (code,))
    places = None if code is None else babel.numbers.get_currency_precision(code)
    whole = money.normalize(count, MONEY, ctx)
    if places == 0:
        assert whole == _ok(str(count), code, "page", checked=False)
    else:
        assert isinstance(whole, Ambiguous)
        assert whole.reason == "minor_unit"
        assert {"amount": str(count), "currency": code} in whole.options
    decimals = count / 100
    read = money.normalize(decimals, MONEY, ctx)
    written = Decimal(repr(decimals))
    if places is not None and -int(written.as_tuple().exponent) > places:
        assert read == Invalid("minor_unit")
    else:
        assert read == _ok(format(written, "f"), code, "unknown" if code is None else "page", checked=False)
