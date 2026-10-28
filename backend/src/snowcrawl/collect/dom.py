"""The structural DOM collector (the §22 candidate "generic structural DOM collectors", label-value pairs, admitted by
the owner on 2026-10-16): values a detail page shows in HTML structures
whose meaning the HTML Standard defines, for pages whose structured data leaves fields without candidates.

- **Label-value pairs.** In the page's main region (the `main` element, else the one `role="main"` landmark, else the
  one `article`, else the body) outside its tangential content, dialogs and media fallback: a table row of exactly two
  cells, the first its header (HTML Standard §4.9: a `th`, or a `td` heading its row); a description list's terms with
  their descriptions (§4.4.9: groups of `dt` then `dd`, each group optionally wrapped in a `div`); an element that
  starts with a label and a colon, the value being the text after it or the one element holding it; and sibling
  containers of one shape (two or more) that each hold a label element then a value element. A label names a field
  when its words are one of the field's labels (`collect.labels`: its name, the schema.org property its path names,
  its description and the synonyms role S drafted for the schema), never words from a list in code (C4). Lines and
  sibling pairs repeat for the items a page lists besides its own (related products, variants, reviews), so a field
  they give several values, or one value in each of three or more sibling blocks of one template, reads none from
  them; an identifier field that pairs of any kind give two values reads none.
- **Other items.** Values held by other items than the page's own are not read: schema.org microdata's related and
  similar items and reviews, sibling blocks that each link to one other document (a list of cards), and the card of
  any other item whose price the page shows (`collect.product_scope`, also read by the description block). Nor is an
  identifier inside a hyperlink to another document, which names that document's item.
- **Typed values.** A money field's or a checked identifier's value the main region shows once, read with the item
  layer's spotting (`collect.items.spot`): the text an element holds itself that the field's normalizer parses, not
  struck through (`content.text.struck`), not set apart, shown; for a checked identifier (a GS1 or ISO 2108 check
  digit), also the value of a pair whose label names no field. A money field reads by its price role
  (`schemas.paths.price_role`): a list price only struck text, a unit price only an amount written per unit
  (`normalize.units`: `€25/kg`), a member price none (its labels read it), any other money field no amount written per
  unit or per period. Several distinct values read none. An identifier without a check digit (sku, mpn) is read only
  next to one of its labels.
- **Images.** The main image and its gallery (`collect.dom_images`), which also holds the rules of the main
  region, the elements set apart from it and where its hyperlinks lead, shared with the passes here.

A value must be shown on the page (`VisibleIndex`). Every candidate is the page's, like an Open Graph value, and
independent; it goes through normalization and resolution like any other (no trust of its own). Listing schemas are
skipped: a page-level pair says nothing of one row.
"""

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from selectolax.lexbor import LexborNode

from snowcrawl.collect.base import PAGE, CollectResult
from snowcrawl.collect.dom_images import (
    A_HREF,
    Found,
    Links,
    apart_ids,
    children,
    image_found,
    up_to,
)
from snowcrawl.collect.items.spot import is_typed, reads, spots
from snowcrawl.collect.labels import Words, field_labels, words
from snowcrawl.collect.locator import format_locator
from snowcrawl.collect.product_scope import SPOT_LENGTH, inside, other_items, short_texts
from snowcrawl.collect.schema_org_map import candidate
from snowcrawl.content.page import ParsedPage, node_xpath
from snowcrawl.content.regions import content_region, main_region
from snowcrawl.content.repeats import MIN_REPEATS, template_siblings
from snowcrawl.content.text import CELLS, shown, struck_ids
from snowcrawl.normalize.base import Context
from snowcrawl.normalize.dispatch import equality_key
from snowcrawl.normalize.strings import collapse
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# A label's end: a colon (U+003A, or the fullwidth U+FF1A that CJK text writes) after optional white space.
_COLON = re.compile(r"(?<!\s)\s*+[:\uff1a]")

# The structures a pair is read from: rows and description lists (HTML Standard §4.9), `Label: value` lines,
# and sibling containers of one shape.
_TABLE, _LINE, _SIBLINGS = "table", "line", "siblings"
# HTML Standard §4.3.6: the heading elements.
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_REPEATING = (_LINE, _SIBLINGS)
# HTML Standard §4.9 (tables) and §4.4.9 (description lists): the elements whose rows and groups `_pairs` reads itself.
_TABULAR = frozenset({"table", "thead", "tbody", "tfoot", "tr", "dl"})
# The typed fields read without a label: dates and datetimes have sources of their own.
_SPOTTED_TYPES = frozenset({FieldType.MONEY, FieldType.IDENTIFIER})


class DomCollector:
    def __init__(self, outside: frozenset[int] = frozenset(), primary: Sequence[str] = ()) -> None:
        # The elements outside the page's product container (`collect.product_scope.product_apart`).
        self.outside = outside
        # The main image the page's structured data states (`collect.dom_images.stated_images`).
        self.primary = primary

    def collect(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> CollectResult:
        if schema.page_kind != "detail" or page.json_body:
            return CollectResult(entities=(), candidates=())
        diagnostics: list[str] = []
        candidates = []
        content = _Content.of(page, ctx, self.outside)
        labels = field_labels(schema)
        pairs = content.pairs(page) if content is not None else []
        listed = content.listed_pairs(page) if content is not None else []
        for name, raw, xpath, read in (
            *_pairs_found(page, pairs, labels, schema, content),
            *_pairs_found(page, listed, field_labels(lists := _list_fields(schema)), lists, content),
            *_typed_found(page, schema, ctx, content, pairs, labels),
            *image_found(page, schema, ctx, self.outside, self.primary),
        ):
            locator = format_locator("dom", xpath)
            found = candidate(name, schema.fields[name], PAGE, "dom", raw, locator, ctx, diagnostics, read=read or None)
            if found is not None:
                candidates.append(found)
        return CollectResult(entities=(), candidates=tuple(candidates), diagnostics=tuple(diagnostics))


Pair = tuple[str, LexborNode, str, str]  # a label's text, the element holding its value, its text and the kind


@dataclass(frozen=True, slots=True)
class _Content:
    """The page's content region with the elements of it whose values are not the page's own: those `apart` (its
    tangential content, dialogs and media fallback, and other items' content: `other_items` and the blocks outside the
    product container, `product_apart`, of `collect.product_scope`), and those `linked` (the content of a hyperlink to
    another document, which names that document's item)."""

    region: LexborNode
    apart: frozenset[int]
    linked: frozenset[int]

    @classmethod
    def of(cls, page: ParsedPage, ctx: Context, outside: frozenset[int]) -> "_Content | None":
        region = content_region(page)
        if region is None:
            return None
        links = Links.of(region, ctx)
        apart = apart_ids(region) | inside(other_items(region, links)) | outside
        return cls(region, apart, inside(a for a in region.css(A_HREF) if links.of_block(a)))

    def links_out(self, element: LexborNode) -> bool:
        """Whether the element is or holds content of a hyperlink to another document."""
        return element.mem_id in self.linked or any(a.mem_id in self.linked for a in element.css(A_HREF))

    def pairs(self, page: ParsedPage) -> list[Pair]:
        """The region's label-value pairs whose value is shown and is not set apart."""
        return [
            pair
            for pair in _pairs(self.region)
            if pair[2] and pair[1].mem_id not in self.apart and page.visible_values.visible(pair[2])
        ]

    def listed_pairs(self, page: ParsedPage) -> list[Pair]:
        """The region's pairs whose value is a list whose items are each shown, though its text is on no line (a list's
        items are lines of their own): values only a list field reads."""
        visible = page.visible_values.visible
        return [
            pair
            for pair in _pairs(self.region)
            if pair[2]
            and pair[1].mem_id not in self.apart
            and not visible(pair[2])
            and (items := list_items(pair[1], page))
            and all(map(visible, items))
        ]


def _list_fields(schema: SchemaDef) -> SchemaDef:
    """The schema with its list fields only."""
    fields = {name: spec for name, spec in schema.fields.items() if spec.type is FieldType.LIST}
    return schema.model_copy(update={"fields": fields})


def _pairs_found(
    page: ParsedPage,
    pairs: Sequence[Pair],
    labels: Mapping[Words, Sequence[str]],
    schema: SchemaDef,
    content: "_Content | None",
) -> Iterator[Found]:
    found, kinds, places = _labelled(pairs, labels, schema, content)
    for (name, raw), value in found.items():
        # A value given in sibling items of one template is theirs (each review's `Reviews: 1`, `content.repeats`); one
        # shown in other places of the layout (a responsive copy) is the page's own.
        repeated = bool(template_siblings(places.get((name, raw), ()))) and raw not in kinds.get((name, _TABLE), ())
        if not repeated and _unambiguous(name, raw, kinds, schema.fields[name]):
            yield (
                name,
                raw,
                node_xpath(value),
                [*list_items(value, page)] if schema.fields[name].type is FieldType.LIST else None,
            )


Labelled = tuple[
    dict[tuple[str, str], LexborNode],  # each field's value: the element giving it
    dict[tuple[str, str], set[str]],  # the values each kind of pair gives a field
    dict[tuple[str, str], list[str]],  # the XPaths of the line and sibling elements giving a field one value
]


def _labelled(
    pairs: Sequence[Pair],
    labels: Mapping[Words, Sequence[str]],
    schema: SchemaDef,
    content: "_Content | None",
) -> Labelled:
    """The values pairs whose label names a field give it, by field and value; an identifier held in a hyperlink to
    another document names that document's item, not the page's."""
    # A value's wrappers read it too: of the elements giving a field the same value, the innermost (last in document
    # order) is its place.
    found: dict[tuple[str, str], LexborNode] = {}
    kinds: dict[tuple[str, str], set[str]] = {}
    places: dict[tuple[str, str], list[str]] = {}
    for label, value, raw, kind in pairs:
        for name in labels.get(words(label), ()):
            if schema.fields[name].type is FieldType.IDENTIFIER and content is not None and content.links_out(value):
                continue
            found[(name, raw)] = value
            kinds.setdefault((name, kind), set()).add(raw)
            if kind in _REPEATING:
                places.setdefault((name, raw), []).append(node_xpath(value))
    return found, kinds, places


def _unambiguous(name: str, raw: str, kinds: Mapping[tuple[str, str], set[str]], spec: FieldSpec) -> bool:
    """Whether a field's pair value is the page's own. Lines and sibling pairs repeat for the items a page lists
    besides its own (related products, variants): a field they give several values is ambiguous there, while rows and
    description lists are the page's own. An identifier identifies one thing (GS1, ISO 2108, a seller's sku), so a
    field that pairs of any kind give two identifiers reads neither: one may be another item's."""
    if spec.type is FieldType.IDENTIFIER:
        return all(given <= {raw} for (field, _), given in kinds.items() if field == name)
    if raw in kinds.get((name, _TABLE), ()):
        return True
    return all(len(kinds[(name, kind)]) == 1 for kind in _REPEATING if raw in kinds.get((name, kind), ()))


def _text(node: LexborNode) -> str:
    return collapse(node.text(deep=True, separator=" "))


# HTML Standard §4.4.5-§4.4.7: the list elements, whose `li` children are their items.
_LISTS = frozenset({"ul", "ol", "menu"})


def list_items(element: LexborNode, page: ParsedPage) -> list[str]:
    """The text of each shown item of the lists an element is or holds (HTML Standard §4.4.5-§4.4.8: the `li` children
    of a `ul`, `ol` or `menu`), in document order: an item that renders no text in the page's cleaned text
    (`ParsedPage.text_spans`) is not shown. A list inside an item is part of that item, not a list of its own."""
    lists = [element] if element.tag in _LISTS else element.css(",".join(_LISTS))
    items = [
        item
        for lst in lists
        if not any(ancestor.tag == "li" for ancestor in up_to(lst, element))
        for item in children(lst)
        if item.tag == "li" and node_xpath(item) in page.text_spans
    ]
    return [text for text in map(_text, items) if text]


def _pairs(region: LexborNode) -> Iterator[Pair]:
    """Each label with its value, in the two-column structures of the region: table rows of two cells, description
    list groups, elements that start with `Label:` and sibling containers of one shape that each hold a label element
    and a value element."""
    for row in region.css("tr"):
        cells = children(row)
        if len(cells) == 2 and all(cell.tag in CELLS for cell in cells):
            yield _text(cells[0]), cells[1], _text(cells[1]), _TABLE
    for described in region.css("dl"):
        for term, description in _groups(described):
            yield _text(term), description, _text(description), _TABLE
    for element in (region, *region.css("*")):
        yield from _colon_line(element)
        yield from _repeated_pairs(element)


def _groups(dl: LexborNode) -> Iterator[tuple[LexborNode, LexborNode]]:
    """HTML Standard §4.4.9: a `dl` is groups of one or more terms followed by one or more descriptions; every term of a
    group labels every description of it. A `div` child of a `dl` wraps one group."""
    terms: list[LexborNode] = []
    described = False
    items = (grand for child in children(dl) for grand in (children(child) if child.tag == "div" else (child,)))
    for item in items:
        if item.tag == "dt":
            if described:
                terms, described = [], False
            terms.append(item)
        elif item.tag == "dd":
            described = True
            yield from ((term, item) for term in terms)


def _colon_line(element: LexborNode) -> Iterator[Pair]:
    """An element whose text starts with a label and a colon, in its first non-blank child (`Model: <span>x</span>`, a
    label element and a value element, `<b>Model:</b> x`): the label is the text before the colon, the value what
    follows it. A value with another colon in it holds another label (`Brand: x SKU: y`): nothing is read. Without a
    colon, an element holding only a text and then one element (`Item no. <span>x</span>`) is a label and its value,
    unless it is a heading, which titles its section (HTML Standard §4.3.6: `<h4>Product <b>Specs</b></h4>`)."""
    parts = [] if _structured(element) else [c for c in element.iter(include_text=True) if _text(c)]
    if not parts or _structured(first := parts[0]):
        return
    lead, full = _text(first), _text(element)
    cut = _COLON.search(full)
    if cut is None or cut.start() > len(lead):
        if len(parts) == 2 and not first.is_element_node and parts[1].is_element_node and element.tag not in _HEADINGS:
            yield lead, parts[1], _unseparated(_text(parts[1])), _LINE
        return
    value = _line_value(element, first, full[cut.end() :].strip(), lead[cut.end() :].strip())
    if value is not None and not _COLON.search(value[1]):
        yield full[: cut.start()], value[0], value[1], _LINE


def _line_value(element: LexborNode, first: LexborNode, rest: str, inner: str) -> tuple[LexborNode, str] | None:
    """The element holding a line's value and the value: what follows the colon in the first child when it holds some
    (`inner`), else the rest of the element (`rest`), held by its one other child when that child fills it."""
    if inner:
        return (first if first.is_element_node else element), inner
    if not rest:
        return None
    others = [child for child in children(element) if child != first]
    return (others[0] if len(others) == 1 and _text(others[0]) == rest else element), rest


def _structured(element: LexborNode) -> bool:
    """Whether the element is part of a table or a description list, whose rows and groups are read as such."""
    parent = element.parent
    return element.is_element_node and (element.tag in _TABULAR or (parent is not None and parent.tag == "dl"))


def _repeated_pairs(parent: LexborNode) -> Iterator[Pair]:
    """Sibling containers of one shape (the same tag, each with two children of the same tags) hold a label and its
    value: the first child labels the second. One such container is no repetition, so no structure."""
    shapes: dict[tuple[str, ...], list[LexborNode]] = {}
    for child in () if _structured(parent) else children(parent):
        parts = children(child)
        if len(parts) == 2:
            shapes.setdefault((child.tag or "", parts[0].tag or "", parts[1].tag or ""), []).append(child)
    for group in shapes.values():
        if len(group) >= MIN_REPEATS:
            for container in group:
                label, value = children(container)
                yield _text(label), value, _unseparated(_text(value)), _SIBLINGS


def _unseparated(value: str) -> str:
    """A value element's text without the separator that opens it: a dash (Unicode `Dash_Punctuation`, Pd) and white
    space between a label and its value (`Color <span>— Red</span>`) part them as a colon does, and is no part of the
    value. A dash written against what follows (`-5`) is the value's own."""
    rest = value[1:].lstrip()
    separated = value[:1] and unicodedata.category(value[0]) == "Pd" and value[1:2].isspace()
    return rest if separated and rest else value


def _typed_found(
    page: ParsedPage,
    schema: SchemaDef,
    ctx: Context,
    content: _Content | None,
    pairs: Sequence[Pair],
    labels: Mapping[Words, Sequence[str]],
) -> Iterator[Found]:
    """A money or checked-identifier field's value the main region shows once: the elements whose own text the field's
    normalizer parses (`items.spot`), not set apart and shown, current (not struck) unless the field is a list price
    (`items.spot.reads`); for an identifier, also the value of a pair whose label names no field (`EAN: <digits>`),
    outside hyperlinks to other documents, and never a value a pair's label gives to other fields only (`Item no.
    <digits>` is a sku, though its digits pass a GTIN check digit). A check digit (GS1, ISO 2108) makes the syntax
    checkable; an identifier without one needs a label (`_pairs_found`). Several distinct values are ambiguous (a
    price among related products' prices): nothing is read."""
    fields = [n for n, spec in schema.fields.items() if is_typed(spec) and spec.type in _SPOTTED_TYPES]
    if not fields or content is None or main_region(page) is None:
        return
    shorts = short_texts(content.region, content.apart)
    struck = struck_ids(content.region)
    named = _named(pairs, labels)
    for name in fields:
        spec = schema.fields[name]
        shown_texts = (
            _identifier_texts(shorts, pairs, named, name, content) if spec.type is FieldType.IDENTIFIER else shorts
        )
        texts = [(e, text) for e, text in shown_texts if reads(spec, text, ctx, struck=e.mem_id in struck)]
        if (one := _one_value(texts, spec, ctx)) is not None:
            yield name, *one, None


def _named(pairs: Sequence[Pair], labels: Mapping[Words, Sequence[str]]) -> dict[int, set[str]]:
    """The fields pairs' labels give each value element and the elements it holds, by element."""
    named: dict[int, set[str]] = {}
    for label, value, _, _ in pairs:
        for element in (value, *value.css("*")):
            named.setdefault(element.mem_id, set()).update(labels.get(words(label), ()))
    return named


def _one_value(texts: Sequence[tuple[LexborNode, str]], spec: FieldSpec, ctx: Context) -> tuple[str, str] | None:
    """The raw value and the XPath of its element when the shown texts the field's normalizer parses give one value."""
    hits = [(element, spot) for element, text in texts for spot in spots(text, spec, ctx, "", "") if shown(element)]
    if len({equality_key(spot.normalized.value, spec) for _, spot in hits}) != 1:
        return None
    return hits[0][1].raw, node_xpath(hits[0][0])


def _identifier_texts(
    shorts: Sequence[tuple[LexborNode, str]],
    pairs: Sequence[Pair],
    named: Mapping[int, set[str]],
    name: str,
    content: _Content,
) -> list[tuple[LexborNode, str]]:
    """The short texts and the short values of pairs no field's label names, neither in nor holding hyperlinks to
    other documents nor values labels give to other fields than `name` only."""
    values = ((value, raw) for _, value, raw, _ in pairs if not named[value.mem_id] and len(raw) <= SPOT_LENGTH)
    return [
        (element, text)
        for element, text in (*shorts, *values)
        if not content.links_out(element) and (not (fields := named.get(element.mem_id)) or name in fields)
    ]
