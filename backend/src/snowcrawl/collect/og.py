"""The OpenGraph collector: `og:*` and `product:*` meta properties.

OpenGraph names no entity types, so every value describes the page's one object: the entity is `page`, and its URL is
`og:url`. A property maps to a field through the fixed table below, keyed by the OGP property and the field's role:
its type and the one property of the root type its `schema_org` path reads (`Product.name` has the role `name`, and
`Product.brand.name` the role `brand`, the text standing in for the brand; `Product.offers.seller.url` reads another
thing's URL, so it has none), and for money the field's price role (`schemas.paths.price_role`). The table is justified
by the OGP specification and the `product:` namespace that Open Graph product markup documents (C2), not by any page.

OGP's basic metadata (`og:title`, `og:image`, `og:url`) describes one object. Client-rendered pages often keep the
site's own defaults there: an `og:title` that names the site (`site_names`) says the object is the site, so its
`og:title`, `og:image` and `og:description` are not the entity's. A title that ends in separator segments naming the
site is read without them, and a title that holds, after words of its own, the words of the page's one top-level
heading or of a slug in the page URL's path is read as those words: the heading and the slug name the item, the words
around them are the site's wording ("Buy ... Online"). An `og:description` naming the site (its `og:site_name`, else
the title's site segments) where the item's name does not is the site's summary, not the item's description.
A candidate of another source stating such a summary's text (a JSON-LD `description` copied from the same default) is
that summary too, unless the page renders the text in its body (`site_summaries`).
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from pydantic import JsonValue

from snowcrawl.collect.base import PAGE, Candidate, CollectResult, Entity
from snowcrawl.collect.site_names import SiteNames
from snowcrawl.content.page import ParsedPage
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import normalize
from snowcrawl.normalize.strings import collapse, fold, fold_words, solid
from snowcrawl.schemas.paths import NAME, MappedPath, PriceRole, parse_paths, price_role
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

_URL = FieldSpec(type=FieldType.URL)


@dataclass(frozen=True, slots=True)
class _Row:
    """OGP properties (read as one, in document order) whose values fill fields of type `type` with the role `role`."""

    properties: tuple[str, ...]
    type: FieldType
    role: str


_OG_URL = "og:url"
_TITLE = "og:title"
_IMAGES = ("og:image", "og:image:url")
_DESCRIPTION = "og:description"
# Open Graph protocol, "Basic Metadata": `og:title` is the object's title, `og:image` its image, `og:url` its
# canonical URL; "Structured Properties": `og:image:url` is identical to `og:image`; "Optional Metadata":
# `og:description` is "a one to two sentence description" of the object, prose for a long_text description. The
# `product` namespace: `product:availability` (and the historic OGP product type's `og:availability`) is the item's
# availability, `product:condition` its condition, `product:retailer_item_id` the retailer's id of it (its sku) and
# `product:brand` its brand. The prices are the money rows (`_MONEY`). Only properties these define are read (owner);
# others are left to learned configs.
_TABLE = (
    _Row((_TITLE,), FieldType.TEXT, "name"),
    _Row(_IMAGES, FieldType.IMAGE_URL, "image"),
    _Row((_OG_URL,), FieldType.URL, "url"),
    _Row((_DESCRIPTION,), FieldType.LONG_TEXT, "description"),
    _Row(("product:availability", "og:availability"), FieldType.ENUM, "availability"),
    _Row(("product:condition",), FieldType.ENUM, "itemCondition"),
    _Row(("product:retailer_item_id",), FieldType.IDENTIFIER, "sku"),
    _Row(("product:brand",), FieldType.TEXT, "brand"),
)
# The `product` namespace's prices, each an amount with its ISO 4217 currency: `product:price:*` the price (also as
# `og:price:*`, the historic OGP product type), `product:sale_price:*` the sale price, the price paid when stated, which
# makes `product:price:*` the price it was reduced from, and `product:original_price:*` that former price itself.
_PRICE = ("product:price:amount", "product:price:currency")
_OG_PRICE = ("og:price:amount", "og:price:currency")
_SALE_PRICE = ("product:sale_price:amount", "product:sale_price:currency")
_ORIGINAL_PRICE = ("product:original_price:amount", "product:original_price:currency")
_MONEY = (_PRICE, _OG_PRICE, _SALE_PRICE, _ORIGINAL_PRICE)
# `product:sale_price_dates:end`: the date and time (ISO 8601) the sale ends; after it the sale price is no price.
_SALE_END = "product:sale_price_dates:end"
# A time written without its offset may be in any time zone, UTC-12:00 to UTC+14:00 (ISO 8601 offsets in use): it has
# passed everywhere only this long after it has passed in UTC.
_LATEST_OFFSET = timedelta(hours=12)
_DAY = timedelta(days=1)
# Open Graph protocol, "Arrays": a property stated more than once is an array of values.
_SEVERAL = 2
# OGP "Optional Metadata": `og:site_name` is "the name of the overall site" the object is part of.
_SITE_NAME = "og:site_name"
_READ = (
    frozenset(p for row in _TABLE for p in row.properties)
    | {p for pair in _MONEY for p in pair}
    | {
        _SITE_NAME,
        _SALE_END,
    }
)
_SITE_TITLE = "og: og:title names the site, so og:title, og:image and og:description are not read"
_SITE_SUMMARY = "og: an og:description names the site, so it is not read"
# The values a site-default block holds about the site rather than the object.
_SITE_DEFAULTS = (_TITLE, *_IMAGES, _DESCRIPTION)


@dataclass(frozen=True, slots=True)
class _Meta:
    """One OGP property; `reading` is the content as a field reads it (a title without its site segments)."""

    property: str
    content: str
    reading: str


@dataclass(frozen=True, slots=True)
class _Value:
    raw: JsonValue
    locator: str
    reading: JsonValue  # what is normalized: `raw`, or a title without its site segments


# A field and a text it states, white space aside (`strings.solid`).
SiteSummary = tuple[str, str]


class OpenGraphCollector:
    def collect(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> CollectResult:
        metas = _metas(page)
        if not metas:
            return CollectResult(entities=(), candidates=())
        metas, diagnostics = _without_site_values(metas, ctx, _heading(page))
        candidates = tuple(_candidates(metas, schema, ctx))
        entity = Entity(id=PAGE, types=(PAGE,), urls=_urls(metas, ctx), parent=None)
        return CollectResult(entities=(entity,), candidates=candidates, diagnostics=diagnostics)


def site_summaries(page: ParsedPage, schema: SchemaDef, ctx: Context) -> frozenset[SiteSummary]:
    """Each field and text an `og:description` the collector does not read would give it (a site default's or a
    summary naming the site, `_without_site_values`), normalized as the field reads it, white space aside, unless the
    page renders the text: the summary is the site's head metadata (HTML Standard §4.2.5 `meta`), while text the body
    shows is the page's own statement of it (`ParsedPage.cleaned_text`)."""
    metas = _metas(page)
    kept = {id(m) for m in _without_site_values(metas, ctx, _heading(page))[0]}
    dropped = [m for m in metas if m.property == _DESCRIPTION and id(m) not in kept]
    if not dropped:
        return frozenset()
    shown = solid(page.cleaned_text.text)
    return frozenset(
        (c.field, text)
        for c in _candidates(dropped, schema, ctx)
        if (text := _solid_text(c)) is not None and text not in shown
    )


def restates_site_summary(candidate: Candidate, summaries: frozenset[SiteSummary]) -> bool:
    """Whether the candidate states, white space aside, a text `site_summaries` found the site's summary of its field:
    another source's copy of the summary (structured data stating the same default) is no more the item's."""
    text = _solid_text(candidate) if summaries else None
    return text is not None and (candidate.field, text) in summaries


def _solid_text(candidate: Candidate) -> str | None:
    value = candidate.normalized.value if isinstance(candidate.normalized, Ok) else None
    return solid(value) or None if isinstance(value, str) else None


def price_currencies(page: ParsedPage) -> list[str]:
    """The currencies of the prices the page writes (`product:price:currency`, `og:price:currency`, the sale and
    original price's), in document order, read as `_metas` reads every OGP property."""
    currencies = {currency for _, currency in _MONEY}
    return [meta.content for meta in _metas(page) if meta.property in currencies]


def _metas(page: ParsedPage) -> list[_Meta]:
    """The OGP properties this collector reads, in document order. OGP writes properties in RDFa's `property`
    attribute; the HTML Standard's `name` attribute is read the same way (the `meta:` locator)."""
    metas: list[_Meta] = []
    for node in page.tree.css("meta"):
        attributes = node.attributes
        key = attributes.get("property") or attributes.get("name")
        content = attributes.get("content")
        if key in _READ and content is not None:
            metas.append(_Meta(key, content, content))
    return metas


def _without_site_values(metas: list[_Meta], ctx: Context, heading: str | None) -> tuple[list[_Meta], tuple[str, ...]]:
    """The metas the fields read: without `og:title`, `og:image` and `og:description` when the (first) title names the
    site, else with each title read without its trailing site segments, and as the item's name it holds
    (`named_span`), and without each `og:description` that names the site (`_site_summary`)."""
    title = next((m.content for m in metas if m.property == _TITLE), None)
    stated = [m.content for m in metas if m.property == _SITE_NAME and fold(m.content)]
    site = SiteNames.of(stated, ctx.page_url)
    if title is None:
        return _without_site_summaries(metas, stated, "")
    if site.names(title):
        return [m for m in metas if m.property not in _SITE_DEFAULTS], (_SITE_TITLE,)
    read = [
        replace(m, reading=named_span(site.without_site(m.content), heading, ctx.page_url))
        if m.property == _TITLE
        else m
        for m in metas
    ]
    return _without_site_summaries(read, stated or site.site_segments(title), site.without_site(title))


def _without_site_summaries(metas: list[_Meta], names: Sequence[str], own: str) -> tuple[list[_Meta], tuple[str, ...]]:
    kept = [m for m in metas if m.property != _DESCRIPTION or not _site_summary(m.content, names, own)]
    return kept, (_SITE_SUMMARY,) if len(kept) < len(metas) else ()


def _site_summary(description: str, names: Sequence[str], own: str) -> bool:
    """Whether `description` holds, as a run of its words, a name of the site (`og:site_name`, else the title's
    trailing site segments) that `own`, the title without its site segments, does not hold. OGP: `og:description`
    describes the object and `og:site_name` names the overall site, so a description naming the site is the site's
    summary ("Shop ... at Example"), which a site writes into every page's default block; a name the item's title holds
    as well is the item's own word (a maker's store named for its brand)."""
    words, own_words = fold_words(description), fold_words(own)
    return any(
        named and _holds(words, named) and not _holds(own_words, named)
        for named in (fold_words(name) for name in names)
    )


def _holds(words: Sequence[str], run: Sequence[str]) -> bool:
    """Whether `run` occurs in `words` as consecutive words."""
    return any(list(words[i : i + len(run)]) == list(run) for i in range(len(words) - len(run) + 1))


# A word: a run of letters and digits (Unicode categories L* and N*).
_WORD = re.compile(r"[^\W_]+")
# A name the title is read as has two words or more: one word inside a title may begin a longer name.
_MIN_WORDS = 2


def _heading(page: ParsedPage) -> str | None:
    """The text of the document's one `h1` (HTML Standard §4.3.6: the top-level heading, the document's title), or
    none when it has none or several."""
    headings = page.tree.css("h1")
    if len(headings) != 1:
        return None
    return collapse(headings[0].text(deep=True, separator=" ")) or None


def named_span(title: str, heading: str | None, page_url: str | None) -> str:
    """The run of the title's words that the page's top-level heading states, after words of the title's own, else the
    run a slug of the page URL's path spells (`slug_span`), else the title. The heading is the document's title as the
    page shows it, so a title holding it among other words is the heading wrapped in the site's wording."""
    named = [fold(w, marks=False) for w in _WORD.findall(heading)] if heading is not None else []
    span = _span(title, named) if len(named) >= _MIN_WORDS else None
    return span if span is not None else slug_span(title, page_url)


def slug_span(title: str, page_url: str | None) -> str:
    """The run of the title's words that a slug of the page URL's path spells, after words of the title's own, else the
    title. A slug is a path segment (RFC 3986 §3.3, percent-decoded) of two or more words, one with a letter, joined by
    separators: a hierarchical path names its item below its categories, so of the slugs the title holds the last one
    names the item, and the earlier are its categories; a slug the title does not hold (a fixed segment naming the
    page's kind) says nothing. The slug names the document's item, so the words before it are the site's wording
    ("Buy"), and so are the words after it. A title that starts with the slug's words is read as written: its further
    words may go on naming the item. Words compare folded (`strings.fold` with diacritics folded)."""
    for slug in reversed(_slugs(page_url) if page_url is not None else []):
        span = _span(title, slug)
        if span is not None:
            return span
    return title


def _span(title: str, named: list[str]) -> str | None:
    """The title when it starts with the `named` words, else the run of its words they are, else none."""
    words = list(_WORD.finditer(title))
    keys = [fold(w.group(), marks=False) for w in words]
    if keys[: len(named)] == named:
        return title
    start = next((i for i in range(1, len(keys) - len(named) + 1) if keys[i : i + len(named)] == named), None)
    return None if start is None else title[words[start].start() : words[start + len(named) - 1].end()]


def _slugs(page_url: str) -> list[list[str]]:
    """The path segments of the page URL that read as slugs (`slug_span`), as their folded words, in path order."""
    segments = [fold_words(unquote(segment), marks=False) for segment in urlsplit(page_url).path.split("/")]
    return [words for words in segments if len(words) >= _MIN_WORDS and any(not w.isdigit() for w in words)]


def _candidates(metas: Sequence[_Meta], schema: SchemaDef, ctx: Context) -> Iterator[Candidate]:
    for name, spec in schema.fields.items():
        if (found := _value(metas, spec, ctx)) is not None:
            yield Candidate(
                field=name,
                entity_id=PAGE,
                source_kind="og",
                raw=found.raw,
                normalized=normalize(found.reading, spec, ctx),
                locator=found.locator,
                independent=True,
            )


def _value(metas: Sequence[_Meta], spec: FieldSpec, ctx: Context) -> _Value | None:
    """The value of the table row that fits `spec`'s role and type; a list field is matched by its item's type."""
    if spec.type is FieldType.MONEY:
        return _money(metas, price_role(spec), ctx)
    role = _role(spec)
    item = spec.item if spec.type is FieldType.LIST else None
    value_type = (item or spec).type
    for row in _TABLE:
        if row.role == role and value_type is row.type:
            return _first_or_all([m for m in metas if m.property in row.properties], as_list=item is not None)
    return None


def _role(spec: FieldSpec) -> str | None:
    """The one property of its root type a `schema_org` path reads (`Offer.price+priceCurrency` gives `price`, and
    `Product.brand.name` `brand`: a final `.name` is the text standing in for the thing, `NAME`), the same in
    every alternative, or `None` when an alternative goes through another object (`Product.brand.image`) or the
    alternatives name different properties. A row-mapped field's path names rows, so it has no role."""
    if spec.schema_org is None or spec.row_mapped:
        return None
    roles = {_root_property(path) for path in parse_paths(spec.schema_org)}
    return roles.pop() if len(roles) == 1 else None


def _root_property(path: MappedPath) -> str | None:
    if len(path.steps) == 1 or (path.steps[1:] == NAME and not path.joined):
        return path.steps[0]
    return None


def _first_or_all(found: Sequence[_Meta], *, as_list: bool) -> _Value | None:
    """Open Graph protocol, "Arrays": a property may repeat, and the first tag, top to bottom, is preferred in a
    conflict. A scalar field takes that first value; a list field takes them all, in document order, when they are
    several: one value (`og:image` and `og:image:url` stating one file are one) is the object's one image, not a list of
    images, which the views the page's markup or structured data list fill. The locator names the first."""
    if not found or (as_list and len({m.content for m in found}) < _SEVERAL):
        return None
    raw: JsonValue = [m.content for m in found] if as_list else found[0].content
    reading: JsonValue = [m.reading for m in found] if as_list else found[0].reading
    return _Value(raw, f"meta:{found[0].property}", reading)


def _money(metas: Sequence[_Meta], role: PriceRole | None, ctx: Context) -> _Value | None:
    """The price a money field of the role reads: the active price is the sale price when the page states one whose
    sale has not ended at capture, else the price; the list price is the original price, else, beside such a sale
    price, the price."""
    sale = None if _sale_ended(metas, ctx) else _amount(metas, _SALE_PRICE)
    stated = _amount(metas, _PRICE) or _amount(metas, _OG_PRICE)
    if role == "price":
        return sale or stated
    if role == "list_price":
        return _amount(metas, _ORIGINAL_PRICE) or (stated if sale is not None else None)
    return None


def _sale_ended(metas: Sequence[_Meta], ctx: Context) -> bool:
    """Whether the first `product:sale_price_dates:end` is before the capture: a date alone ends with its day, a time
    without an offset once it has passed in every time zone. An end that is no ISO 8601 date leaves the sale on."""
    written = next((m.content.strip() for m in metas if m.property == _SALE_END), None)
    if written is None:
        return False
    try:
        end = datetime.fromisoformat(written)
    except ValueError:
        return False
    if len(written) == len("YYYY-MM-DD"):
        end += _DAY
    if end.tzinfo is None:
        return end + _LATEST_OFFSET < ctx.captured_at.astimezone(UTC).replace(tzinfo=None)
    return end < ctx.captured_at


def _amount(metas: Sequence[_Meta], pair: tuple[str, str]) -> _Value | None:
    """The first amount of the pair with its first currency, in the money normalizer's structured form. A missing
    currency is left out, never filled in here: the money rules take the page's."""
    amount, currency = pair
    amounts = [m.content for m in metas if m.property == amount]
    if not amounts:
        return None
    raw: dict[str, JsonValue] = {"amount": amounts[0]}
    if currencies := [m.content for m in metas if m.property == currency]:
        raw["currency"] = currencies[0]
    return _Value(raw, f"meta:{amount}", raw)


def _urls(metas: Sequence[_Meta], ctx: Context) -> tuple[str, ...]:
    """The page entity's URL: the first `og:url` (Open Graph protocol: the object's canonical URL)."""
    first = next((m.content for m in metas if m.property == _OG_URL), None)
    result = None if first is None else normalize(first, _URL, ctx)
    return (result.value,) if isinstance(result, Ok) and isinstance(result.value, str) else ()
