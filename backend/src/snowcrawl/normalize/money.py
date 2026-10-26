"""Money normalizer: `{"amount": "<decimal>", "currency": "<ISO 4217>" | null}`.

Accepts:
- a string, from the DOM or an LLM: the one amount in it (a percentage beside it is a discount, not an amount; a
  CLDR compact unit after it multiplies it, `3.5万` = 35000), and the currency sign written next to it (a CLDR symbol,
  one-word CLDR currency count name such as `円`, or ISO 4217 code) or, failing that, the sign `price-parser`
  locates. Unicode CLDR data (Babel) resolves both for the locales the page and the request state;
- an amount written with a CLDR per pattern and a unit after it (`€25/kg`, `$25/mo`: `normalize.units`) is the price of
  a unit or a period: for a field with the active-price role (`schemas.paths.price_role`) it is
  `Invalid("per_unit")` or `Invalid("per_period")`, and every other money field reads the amount before the pattern
  (a `unit_price` field reads `€25/kg` as 25);
- a JSON object from a structured source: schema.org `price` + `priceCurrency` (Offer, PriceSpecification), or the
  normalized form `amount` + `currency`. The amount is a JSON number or a string of digits with `.` as the decimal
  point (schema.org `price`: no grouping, no currency sign); a `price` written as display text instead (`$1,295.99`)
  is read like text, in the stated currency, which any sign it writes must denote;
- a bare JSON number (RFC 8259 §6), as a map reads it from an API answer: an amount without a currency, which takes the
  page's (`_unwritten`). Written with decimals it is in major units; a whole number may count the currency's minor unit
  (ISO 4217: cents) as well as its major one, so it is `Ambiguous("minor_unit")` unless the currency has no minor unit.

A grounded amount always gives a value; its currency comes from the best evidence: the
sign or code written with it, the item's structured data, a page-level declaration, the page's locale and region
narrowing a shared sign to one currency, then the territory the page's URL names (owner 2026-10-19, basis `url`),
then the currency CLDR's English locale gives a sign shared by several (`$` is USD, `¥` JPY; owner 2026-10-23, basis
`symbol`), else none (`null`, basis `unknown`). Any other JSON value is `Invalid("not_money")`; an amount the locales
read differently is `Ambiguous`.
"""

import re
import unicodedata
from collections.abc import Callable, Hashable, Mapping, Sequence, Set
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from urllib.parse import urlsplit

from babel import Locale
from babel.core import get_global
from babel.numbers import get_currency_precision, get_territory_currencies, list_currencies
from price_parser import Price
from pydantic import JsonValue

from snowcrawl.normalize import locales, numbers, units
from snowcrawl.normalize.base import Ambiguous, Basis, Context, Invalid, NormalizeResult, Ok, Raw, raw_key
from snowcrawl.schemas.paths import price_role
from snowcrawl.schemas.types import FieldSpec, FieldType

# schema.org `price`: ASCII digits with `.` (U+002E) as the decimal point and no grouping symbols.
_SCHEMA_ORG_PRICE = re.compile(r"\d+(?:\.\d+)?", re.ASCII)
# A URL path segment naming a locale, language then region (BCP 47 order, `-` or `_`): `/en-gb/`, `/fr_CA/`.
_LOCALE_SEGMENT = re.compile(r"([a-z]{2,3})[-_]([a-z]{2})", re.ASCII | re.IGNORECASE)
# Unicode bidi controls CLDR writes inside currency symbols (`ر.س.\u200f`): LEFT-TO-RIGHT MARK, RIGHT-TO-LEFT MARK and
# ARABIC LETTER MARK. A page writes the symbol with or without them.
_BIDI_CONTROLS = str.maketrans("", "", "\u200e\u200f\u061c")
# `numbers.stated` declines a text without one number as `no_number` or `several_numbers`; money names them by what
# they mean for an amount: none is no amount, and two are a range, not one value.
_NUMBER_REASONS = {"no_number": "no_amount", "several_numbers": "range"}


def normalize(raw: Raw, spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """Normalize a money field."""
    if spec.type is not FieldType.MONEY:
        raise TypeError(f"the money normalizer does not handle type {spec.type}")
    active = price_role(spec) == "price"
    if isinstance(raw, dict):
        return _structured(raw, ctx, active=active)
    if isinstance(raw, str):
        return _per_text(raw, ctx, active=active)
    if (number := numbers.json_decimal(raw)) is not None:
        return _bare(number, ctx)
    return Invalid("not_money")


def _bare(amount: Decimal, ctx: Context) -> NormalizeResult:
    """A bare JSON number (module docstring). ISO 4217 minor unit (CLDR currency data): an amount written with decimals
    is in major units, and no more of them than the currency has; a whole one is the count of major or of minor units,
    which nothing settles where the currency has a minor unit or is unknown."""
    currency = _unwritten(ctx, written=False)
    places = None if currency.code is None else get_currency_precision(currency.code)
    written = _places(amount)
    if places is not None and written > places:
        return Invalid("minor_unit")
    major = _money(amount, currency)
    if written > 0 or places == 0 or not isinstance(major, Ok):
        return major
    readings = [amount] if places is None else [amount, _shifted(amount, -places)]
    return Ambiguous("minor_unit", tuple(_value(a, currency.code) for a in readings))


def _per_text(raw: str, ctx: Context, *, active: bool, stated: str | None = None) -> NormalizeResult:
    """Money written as text, its amount read before a CLDR per pattern and unit written after it (`units`); for the
    active price such an amount is no candidate: it prices a unit or a period, not the item."""
    per = units.per_suffix(raw, ctx)
    if per is None:
        return _text(raw, ctx, stated)
    head, kinds = per
    if active:
        return Invalid(units.PER_UNIT if units.PER_UNIT in kinds else units.PER_PERIOD)
    return _text(head, ctx, stated)


@dataclass(frozen=True, slots=True)
class _Currency:
    """A value's currency (`None`: unknown), the weaker evidence it rests on, and whether a sign or a structured
    currency is written with the amount (`Ok.checked`)."""

    code: str | None
    basis: Basis | None
    written: bool


def _structured(raw: Mapping[str, JsonValue], ctx: Context, *, active: bool) -> NormalizeResult:
    amount_key, currency_key = ("price", "priceCurrency") if "price" in raw else ("amount", "currency")
    amount, currency = raw.get(amount_key), raw.get(currency_key)
    if amount is None:
        return Invalid("no_amount")
    # ISO 4217 as written; a code ISO 4217 does not list (`eur`, `$`) states no currency (C7: the case is not guessed).
    stated = currency if isinstance(currency, str) and currency in _currencies() else None
    if (number := numbers.json_decimal(amount)) is not None:
        value = number
    elif isinstance(amount, str) and _SCHEMA_ORG_PRICE.fullmatch(stripped := amount.strip()):
        value = Decimal(stripped)
    elif isinstance(amount, str) and amount_key == "price":
        # schema.org `priceCurrency` states the currency of a `price` the page wrote as display text (a sign,
        # grouping), so the text reader reads it with that currency stated (CLDR symbols and number symbols).
        return _per_text(amount, ctx, active=active, stated=stated)
    else:
        return Invalid("unparsable_amount")
    # The structured value names its amount a price, so it publishes; without a currency it takes the page's.
    found = _Currency(stated, None, True) if stated is not None else _unwritten(ctx, written=True)
    return _money(value, found)


def _text(raw: str, ctx: Context, stated: str | None = None) -> NormalizeResult:
    """Money written as text; `stated` is the currency structured data states beside it, which any sign the text
    writes must denote, and which a text without a sign is in."""
    # A percentage beside the amount (`-20%`) is a discount, never a second amount (`numbers.stated`).
    found = numbers.stated(raw, percentages=False)
    if isinstance(found, str):
        return Invalid(_NUMBER_REASONS.get(found, found))
    text, before, after = found.text, found.before, found.after
    if numbers.negated(before):
        return Invalid("not_positive")
    locales = numbers.locale_set(ctx)
    # A CLDR compact unit written after the digits multiplies them (`3.5万円` is 35000 yen); the sign follows it.
    power, length = numbers.compact_unit(after, locales) or (0, 0)
    after = after[length:]
    currency = _currency_of(text, before, after, ctx, stated)
    if isinstance(currency, Invalid):
        return currency
    amounts = [_shifted(a, power) for a in numbers.readings(found.written, locales, ctx.page_convention)]
    if not amounts:
        return Invalid("unparsable_amount")
    if currency.code is not None:
        # ISO 4217 minor unit (CLDR currency data): a reading with more decimals than the currency has is not an
        # amount of it (`$10.999` is not USD 10.999; `¥1,234` in yen is 1234).
        places = get_currency_precision(currency.code)
        amounts = [a for a in amounts if _places(a) <= places]
        if not amounts:
            return Invalid("minor_unit")
    if len(amounts) == 1:
        return _money(amounts[0], currency)
    return Ambiguous("decimal_separator", tuple(_value(a, currency.code) for a in amounts))


def _places(amount: Decimal) -> int:
    """The decimal places `amount` is written with, trailing zeros included."""
    exponent = amount.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _shifted(amount: Decimal, power: int) -> Decimal:
    """`amount` times `10^power`, exactly (`Decimal.scaleb` rounds to the context's 28 digits)."""
    sign, digits, exponent = amount.as_tuple()
    return Decimal((sign, digits, exponent + power)) if isinstance(exponent, int) else amount


def _currency_of(text: str, before: str, after: str, ctx: Context, stated: str | None) -> _Currency | Invalid:
    """The currency of a text amount: a `stated` one, which any sign must be able to denote; else the one the signs
    denote, narrowed by the evidence (`_narrowed`); else, with no sign, the page's (`_unwritten`). Two signs that share
    no currency (`USD 12 EUR`), or a sign that cannot denote the stated currency, contradict each other: `Invalid`."""
    signs = _signs(text, before, after)
    if isinstance(signs, Invalid):
        return _Currency(stated, None, True) if stated is not None else _unwritten(ctx, written=False)
    if any(_measure(sign, ctx) for sign in signs):
        return Invalid("measure")
    denoted = _agreed_currencies(signs)
    if isinstance(denoted, Invalid):
        return denoted
    if stated is not None:
        return _Currency(stated, None, True) if stated in denoted else Invalid("currency_conflict")
    if not denoted:
        # A sign `price-parser` locates that no CLDR symbol or ISO 4217 code is: written, but no currency.
        return _unwritten(ctx, written=True)
    return _narrowed(signs, denoted, ctx)


def _measure(sign: str, ctx: Context) -> bool:
    """Whether a sign written as an ISO 4217 code or a CLDR currency symbol is a CLDR unit's name or symbol in the
    page's locales instead (`units`: `3.5 CUP` is a volume, not Cuban pesos; `1,7 L` a volume, not lempiras or lei): a
    sign that is also a unit counts as a currency only where the page names a currency it denotes
    (`_page_currencies`), so a quantity in a product's name is never read as an amount of money. A one-letter sign is
    a unit in either case here: CLDR writes the litre `l` in the root locale and `L` in others, and a page writes either
    whatever its locale."""
    unit = any(units.is_unit(form, ctx) for form in {sign, sign.lower(), sign.upper()})
    return unit and not _currencies_for(sign) & _page_currencies(ctx)


def _page_currencies(ctx: Context) -> frozenset[str]:
    """The currencies the page names: its structured and declared currencies, its URL's, and the tender currencies
    of the territories of the CLDR locales its declarations leave possible (`numbers.regional_locales`), the locale a
    request asked for aside: that is the caller's, not the page's (as `_regional`)."""
    territories = {Locale.parse(loc).territory for loc in numbers.regional_locales(ctx)}
    tender = {code for t in territories if t and (code := _tender_currency(t, ctx)) is not None}
    declared = {*ctx.page_currencies, *ctx.declared_currencies, *ctx.state_currencies}
    return frozenset({*declared, *filter(None, (url_currency(ctx),)), *tender})


def _unwritten(ctx: Context, *, written: bool) -> _Currency:
    """The currency of an amount written without one: the one currency the page states and declares in its own name,
    when every such statement agrees;
    when it makes none, the one its state members hold; else the one its URL names (`url_currency`); else unknown."""
    own = set(ctx.page_currencies) | set(ctx.declared_currencies)
    declared = own or set(ctx.state_currencies)
    if len(declared) == 1:
        return _Currency(declared.pop(), "page", written)
    if (code := url_currency(ctx)) is not None:
        return _Currency(code, "url", written)
    return _Currency(None, "unknown", written)


def _narrowed(signs: Sequence[str], denoted: frozenset[str], ctx: Context) -> _Currency:
    """Of the currencies a written sign can denote, the one the evidence settles, strongest first: the page's
    structured data (schema.org `priceCurrency`), its page-level declarations in its own name, then its state members,
    then its locale and region (CLDR currency symbols of the locales its language tags and ccTLD name), then the
    currency its URL names. Each step narrows what the one before left; none settling it leaves the sign's primary
    currency (`_primary`), flagged `symbol`, if it has one, else the currency unknown."""
    if len(denoted) == 1:
        return _Currency(next(iter(denoted)), None, True)
    steps: tuple[tuple[Callable[[], frozenset[str]], Basis | None], ...] = (
        (lambda: frozenset(ctx.page_currencies), None),
        (lambda: frozenset(ctx.declared_currencies), "page"),
        (lambda: frozenset(ctx.state_currencies), "page"),
        (lambda: _regional(signs, ctx), "locale"),
        (lambda: frozenset(filter(None, (url_currency(ctx),))), "url"),
    )
    left = denoted
    for evidence, basis in steps:
        narrowed = left & evidence()
        if len(narrowed) == 1:
            return _Currency(next(iter(narrowed)), basis, True)
        left = narrowed or left
    primary = left & _primary(signs)
    if len(primary) == 1:
        return _Currency(next(iter(primary)), "symbol", True)
    return _Currency(None, "unknown", True)


def _primary(signs: Sequence[str]) -> frozenset[str]:
    """The currencies every sign stands for in CLDR's English locale, the one reading a sign shared by several
    currencies has without a regional hint (`$` is USD, `£` GBP, `¥` JPY; `CA$`, `CN¥` are the others')."""
    reading = _locale_symbols("en")
    forms = [
        {c for form in _sign_forms(unicodedata.normalize("NFKC", sign)) for c in reading.get(form, ())}
        for sign in signs
    ]
    return frozenset.intersection(*map(frozenset, forms)) if forms else frozenset()


def _regional(signs: Sequence[str], ctx: Context) -> frozenset[str]:
    """The currencies the signs stand for in the CLDR locales of the page's language and region: the locales it
    declares (`numbers.regional_locales`: a language alone stands for every territory it is used in, so it settles
    no shared sign) and the territory its ccTLD names."""
    territory = ctx.cctld_territory or cctld_territory(ctx.page_url)
    page = ctx.model_copy(update={"cctld_territory": territory})
    locales = numbers.regional_locales(page)
    symbols = [form for sign in signs for form in _sign_forms(unicodedata.normalize("NFKC", sign))]
    return frozenset(code for loc in locales for sign in symbols for code in _locale_symbols(loc).get(sign, ()))


def cctld_territory(url: str | None) -> str | None:
    """The territory a URL's country-code top-level domain names (RFC 1591 §2: a two-letter ISO 3166-1 code), when
    CLDR knows it as a territory, or as the alias of one (`uk` is GB); `None` for a generic TLD or no URL."""
    host = urlsplit(url).hostname if url else None
    return _territory((host or "").rstrip(".").rpartition(".")[2])


def _territory(code: str) -> str | None:
    """The CLDR territory a two-letter ISO 3166-1 code names, itself or through the one territory CLDR aliases it to
    (`uk` is GB); `None` for any other label."""
    label = code.upper()
    if len(label) != 2:
        return None
    regions = locales.iso_3166_alpha2(reserved=True)
    if label in regions:
        return label
    alias = get_global("territory_aliases").get(label, ())
    return alias[0] if len(alias) == 1 and alias[0] in regions else None


def url_currency(ctx: Context) -> str | None:
    """The currency of the territory the page's URL names (owner 2026-10-19), for an amount the page states no currency
    for: of the final URL, then the requested one, the territory a locale path segment names (`/en-gb/`), else a
    country subdomain (`in.`), else the ccTLD; its one tender currency on the capture date (CLDR territory
    currencies). A segment whose language is not used in its region (CLDR territory languages) is no locale segment.
    The first URL and step that names a territory decides: several territories, one that none of the page's stated
    languages is used in, or one with no single tender currency name none (`None`), as does a generic TLD. No host
    table (C7)."""
    languages = locales.page_languages(ctx, request=False)
    for url in dict.fromkeys(u for u in (ctx.page_url, ctx.request_url) if u):
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower().split(".")
        steps = (_segment_territories(parts.path), _subdomain_territories(host), _cctld_territories(url))
        territories = next((step for step in steps if step), None)
        if territories is None:
            continue
        # The first URL that names a territory decides: an unsettled one never falls through to the next URL.
        if len(territories) == 1 and _speaks(territory := territories.pop(), languages):
            return _tender_currency(territory, ctx)
        return None
    return None


def _segment_territories(path: str) -> set[str]:
    """The territories the path's locale segments name, each segment's language used there (`/en-gb/`)."""
    found: set[str] = set()
    for segment in path.split("/"):
        m = _LOCALE_SEGMENT.fullmatch(segment)
        if m and (territory := _territory(m.group(2))) and _speaks(territory, {m.group(1).lower()}):
            found.add(territory)
    return found


def _subdomain_territories(labels: Sequence[str]) -> set[str]:
    """The territory a host's first label names, when it is a subdomain (two more labels follow) of two letters that
    CLDR knows as a territory (`in.example.com`, `uk.example.com`)."""
    territory = _territory(labels[0]) if len(labels) > 2 else None
    return set() if territory is None else {territory}


def _cctld_territories(url: str) -> set[str]:
    territory = cctld_territory(url)
    return set() if territory is None else {territory}


def _speaks(territory: str, languages: Set[str]) -> bool:
    """Whether one of `languages` is used in `territory` (CLDR territory languages); true when none is stated or CLDR
    lists none for it."""
    used = {language.split("_")[0] for language in get_global("territory_languages").get(territory, {})}
    return not languages or not used or bool(used & languages)


def _tender_currency(territory: str, ctx: Context) -> str | None:
    """The territory's one legal tender currency on the capture date (CLDR territory currencies), else `None`."""
    current = get_territory_currencies(territory, ctx.captured_at.date(), tender=True)
    return current[0] if len(current) == 1 else None


def _signs(text: str, before: str, after: str) -> list[str] | Invalid:
    """The currency signs written next to the amount, or else the one `price-parser` locates in `text` when it is
    written on its own."""
    signs = _adjacent_signs(before.rstrip(), after.lstrip())
    if signs:
        return signs
    located = Price.fromstring(text).currency
    if located is None:
        return Invalid("no_currency")
    if not _whole_sign(text, located):
        return Invalid("unknown_currency")
    return [located]


def _agreed_currencies(signs: list[str]) -> frozenset[str] | Invalid:
    """The ISO 4217 codes every sign can denote (CLDR currency symbols anywhere); empty for a lone sign CLDR does not
    know."""
    # A sign on each side of the amount (`$5 USD`) must agree: the currencies both can denote. Two signs that share
    # none (`USD 12 EUR`) state two currencies, never one money value.
    each = [_currencies_for(sign) for sign in signs]
    currencies = each[0].intersection(*each[1:])
    if not currencies and len(each) > 1 and all(each):
        return Invalid("currency_conflict")
    return currencies


def _adjacent_signs(before: str, after: str) -> list[str]:
    """The longest CLDR currency symbol or ISO 4217 code written right before the amount, then the longest written
    right after it. A symbol can hold a shorter one (`$ AU`, `BS$`), which `price-parser` would locate instead."""
    tail = _longest_sign(before, lambda n: (len(before) - n, len(before)))
    head = _longest_sign(after, lambda n: (0, n))
    return [sign for sign in (tail, head) if sign is not None]


def _longest_sign(text: str, span: Callable[[int], tuple[int, int]]) -> str | None:
    for length, signs in _signs_by_length():
        start, end = span(length)
        if start >= 0 and (sign := text[start:end]) in signs and _bounded(text, start, end):
            return sign
    return None


def _whole_sign(text: str, sign: str) -> bool:
    """Whether `sign` is written on its own: not inside a longer run of letters, which would be a different sign
    (`S$` inside `BS$`)."""
    start = text.find(sign)
    while start >= 0:
        if _bounded(text, start, start + len(sign)):
            return True
        start = text.find(sign, start + 1)
    return False


def _bounded(text: str, start: int, end: int) -> bool:
    """Whether `text[start:end]` has no letter (Unicode category L) right before or right after it."""
    return not _is_letter(text[start - 1 : start]) and not _is_letter(text[end : end + 1])


def _is_letter(char: str) -> bool:
    return bool(char) and unicodedata.category(char).startswith("L")


def _money(amount: Decimal, currency: _Currency) -> NormalizeResult:
    # A JSON number is finite (RFC 8259 §6); a NaN or infinity reaching here is no amount.
    if not amount.is_finite():
        return Invalid("unparsable_amount")
    # A price that is not positive is not a price.
    if amount <= 0:
        return Invalid("not_positive")
    if not numbers.storable(amount):
        return Invalid("out_of_range")
    return Ok(_value(amount, currency.code), basis=currency.basis, checked=currency.written)


def _value(amount: Decimal, currency: str | None) -> JsonValue:
    # Fixed-point notation, with the digits as written: `19.90` stays `19.90` (§4.5 compares amounts as Decimals).
    return {"amount": format(amount, "f"), "currency": currency}


def _currencies_for(sign: str) -> frozenset[str]:
    """The ISO 4217 codes `sign` can denote anywhere."""
    if sign in _currencies():
        # ISO 4217 alphabetic codes are three uppercase letters; a code names itself.
        return frozenset({sign})
    # CLDR currency symbols: the currencies the sign stands for in any locale.
    symbols = _symbol_currencies()
    return frozenset(
        code for form in _sign_forms(unicodedata.normalize("NFKC", sign)) for code in symbols.get(form, ())
    )


def _sign_forms(sign: str) -> tuple[str, ...]:
    """The forms of a CLDR symbol or written sign: itself, without bidi controls (`_BIDI_CONTROLS`), and, for one
    ending in a full stop, the abbreviation without it. A full stop marks an abbreviation (CLDR writes one symbol both
    ways, `kr` and `kr.`), so `Rs.` is also the symbol `Rs`, and `ر.س.\u200f` (SAR) is also `ر.س`; both the symbol
    table and the written sign take every form, so each denotes the other's currencies, for the evidence to narrow."""
    unmarked = sign.translate(_BIDI_CONTROLS)
    return tuple(form for form in dict.fromkeys((sign, unmarked, unmarked.removesuffix("."))) if form)


def is_currency_code(code: str) -> bool:
    """Whether `code` is an ISO 4217 alphabetic code as written: three uppercase letters that ISO 4217 lists (through
    CLDR). A lowercase `usd` is not one: guessing the case is guessing (C7)."""
    return code in _currencies()


def warm() -> None:
    """Build the tables read across every CLDR locale (currency symbols and count names) now, so that the first page a
    process reads does not wait for them."""
    _symbol_currencies()
    _signs_by_length()


@cache
def _currencies() -> frozenset[str]:
    return frozenset(list_currencies())


@cache
def _locale_symbols(identifier: str) -> dict[str, frozenset[str]]:
    """The currencies each CLDR symbol (NFKC, in each of its `_sign_forms`) stands for in one locale, and each one-word
    name CLDR gives a count of it (`currencyNames` by plural form: `円` for yen in Japanese), which pages write after an
    amount as a sign."""
    locale = Locale.parse(identifier)
    named = locale._data.get("currency_names_plural", {})  # Babel exposes the count forms only through its data
    pairs = [*((code, symbol) for code, symbol in locale.currency_symbols.items())]
    pairs += [(code, name) for code in named for name in dict(named[code]).values() if name and not _spaced(name)]
    by_symbol: dict[str, set[str]] = {}
    for code, symbol in pairs:
        for form in _sign_forms(unicodedata.normalize("NFKC", symbol)):
            by_symbol.setdefault(form, set()).add(code)
    return {symbol: frozenset(codes) for symbol, codes in by_symbol.items()}


def _spaced(name: str) -> bool:
    return any(c.isspace() for c in name)


@cache
def _symbol_currencies() -> dict[str, frozenset[str]]:
    by_symbol: dict[str, set[str]] = {}
    for identifier in numbers.identifiers():
        for symbol, codes in _locale_symbols(identifier).items():
            by_symbol.setdefault(symbol, set()).update(codes)
    return {symbol: frozenset(codes) for symbol, codes in by_symbol.items()}


@cache
def _signs_by_length() -> tuple[tuple[int, frozenset[str]], ...]:
    """Every CLDR currency symbol and ISO 4217 code, grouped by length, longest first. A symbol of format characters
    only (Unicode Cf, such as a zero-width space) is not written on a page."""
    by_length: dict[int, set[str]] = {}
    for sign in [*_symbol_currencies(), *_currencies()]:
        if any(unicodedata.category(c) != "Cf" for c in sign):
            by_length.setdefault(len(sign), set()).add(sign)
    return tuple((length, frozenset(by_length[length])) for length in sorted(by_length, reverse=True))


def key(value: JsonValue, _spec: FieldSpec) -> Hashable:
    """Equality as a key: the amounts equal as Decimals, without rounding (`19.9` equals `19.90`),
    and the same currency."""
    money = _money_value(value)
    return raw_key(value) if money is None else ("money", money)


def same_amount(values: Sequence[JsonValue]) -> bool:
    """Whether the values are normalized money values of one amount (as Decimals: `19.9` equals `19.90`), whatever
    currencies they state."""
    moneys = [_money_value(value) for value in values]
    return all(m is not None for m in moneys) and len({m[0] for m in moneys if m is not None}) == 1


def amount_and_currency(value: JsonValue) -> tuple[Decimal, str | None] | None:
    """A normalized money value's amount and currency (`None`: unknown), or `None` for any other value."""
    return _money_value(value)


def without_currency(value: JsonValue) -> JsonValue:
    """A normalized money value's amount with its currency unknown (`null`)."""
    money = _money_value(value)
    if money is None:
        raise ValueError("not a normalized money value")
    return _value(money[0], None)


def served(value: JsonValue) -> JsonValue:
    """A published money value as a record serves it: its amount a JSON number
    (`numbers.json_number`), its currency as it is; any other value as it is."""
    money = _money_value(value)
    return value if money is None else {"amount": numbers.json_number(money[0]), "currency": money[1]}


def _money_value(value: JsonValue) -> tuple[Decimal, str | None] | None:
    """A normalized money value (`{amount, currency}`) as (amount, ISO 4217 code or `None` when unknown), or `None` for
    any other value."""
    if not isinstance(value, dict) or value.keys() != {"amount", "currency"}:
        return None
    amount, currency = value["amount"], value["currency"]
    if not (isinstance(amount, str) and _SCHEMA_ORG_PRICE.fullmatch(amount)):
        return None
    if not (currency is None or isinstance(currency, str)):
        return None
    return Decimal(amount), currency
