"""The admission rule for JSON without standard keys: embedded JSON state
(`js_state`) and captured network bodies (`xhr`).

Their keys follow no standard, so no key is mapped to a field. Every string and number in a document is tried against
each field's normalizer, the only filter, and becomes a candidate only when:
- the field has candidates from another source kind and the value is `equal` to one of them. It is
  then owned by that candidate's entity and is not independent: it agrees with a value it was admitted for, so it
  never counts toward `agreed`;
- the field has no candidate from another source kind, its type has a syntax the normalizer checks
  (`normalize.dispatch.checkable`: money, enum, date, range, a check-digit identifier, an absolute URL), and the value
  is visible in the page text. It is then owned by the page, like other page-level values, and is independent. An
  identifier another source kind gives to another identifier field is that field's (a sku whose digits pass a GTIN
  check digit is no GTIN): it is not admitted this way. Text,
  location and other opaque types admit by equality only: any visible string normalizes as text or as a location's
  text, so visibility alone says nothing of which field a leaf is (owner, accuracy Q2 option a). An enum's values are
  words (a vocabulary's codes and terms: "Men", "New", "Available"), which a script also holds as its interface's
  text (a message catalog, a menu); so a visible enum value is admitted this way only from an object that states the
  page's item: one holding, as a member, the very string (in the text form) another source kind states for a field
  that names or identifies the item, schema.org `name`, an identifier (`sku`, `gtin`, `mpn`, `productID`, the
  subproperties of `identifier`) or `url`. A brand or colour names many items, and a message catalog writes the
  store's name, so they state none. Where the page states a word, not which table maps it, tells the item's value
  from its interface's text.

A visible value inside an array row is owned by that row, not by the page (owner, accuracy Q2 option b, Appendix A
#55): an object item of a JSON array holding two or more objects is one of a set of items (related products,
recommendations, the variants of one), and which of them, if any, is the page's subject is resolution's question
(`resolve.ownership`). The row's entity carries the URLs written in its scope, the row up to its nested rows, as
a variants row scopes a row, so resolution can tell a row naming the page from another page's item. One object in an
array is its holder's only item, as an object member is, and stays in its holder, unless it names a document of its own:
a related item alone in its array is still another page's item.

A value repeated under many paths of the documents is one candidate per field, normalized value and owner.

A field with candidates from other kinds admits values only by equality ("leaves are considered only for fields still
lacking a candidate or for agreement checks"), so a value both equal and visible is not independent.
"""

import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from snowcrawl.collect.base import ARRAY_ROW, PAGE, Candidate, Entity
from snowcrawl.collect.json_doc import ROOT, is_scalar, member
from snowcrawl.collect.labels import properties
from snowcrawl.collect.locator import format_locator
from snowcrawl.collect.schema_org_map import canonical_urls
from snowcrawl.content.page import ParsedPage
from snowcrawl.normalize.base import Context, Ok, raw_key
from snowcrawl.normalize.dispatch import checkable, equal, equality_key, normalize
from snowcrawl.normalize.text import text_form
from snowcrawl.normalize.url import media_type
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

StateKind = Literal["js_state", "xhr"]


class Admitted(BaseModel):
    """The candidates a `js_state` or `xhr` collector admitted. They name the entities of the candidates they equal,
    `page`, or the array row holding a visible value: `entities` are those rows, each with its enclosing rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[Candidate, ...]
    entities: tuple[Entity, ...] = ()
    # A listing's rows read from captured calls (`collect.xhr`), also among `entities`.
    rows: tuple[Entity, ...] = ()
    diagnostics: tuple[str, ...] = ()


class AdmittingCollector(Protocol):
    """A collector whose values are admitted against `others`: the candidates every other collector proposed for the
    page. It reads the `ParsedPage` only and never raises on page content."""

    def collect(self, page: ParsedPage, schema: SchemaDef, ctx: Context, others: Sequence[Candidate]) -> Admitted: ...


@dataclass(frozen=True, slots=True)
class Document:
    """One parsed JSON document and the locator part that addresses it (a script's XPath, a body's index, an
    attribute's `<xpath>@<name>`), under `scheme` when it is not its collector's kind (an attribute's `attr`; the
    page's inline Flight stream's `rsc`, whose document holds its rows by row id and whose locators address the row:
    `rsc:<row>:<path in the row>`)."""

    data: JsonValue
    address: str
    scheme: Literal["attr", "rsc"] | None = None

    def locator(self, kind: StateKind, path: str) -> str:
        """The locator of the value at `path` in the document, read by a `kind` collector."""
        if self.scheme == "rsc":
            row, rest = _row_and_rest(path)
            return format_locator("rsc", row, rest)
        return format_locator(self.scheme or kind, self.address, path)


# The first segment of a normalized path into a Flight document: its row id (a hex number, Flight's own row ids).
_ROW = re.compile(r"\$\['([^'\\]+)'\]")


def _row_and_rest(path: str) -> tuple[str, str]:
    """A Flight document's leaf path split into its row id (the first member name; every leaf is in a row) and the path
    in that row."""
    found = _ROW.match(path)
    if found is None:
        raise ValueError(f"not a path into a Flight row: {path!r}")
    return found[1], ROOT + path[found.end() :]


@dataclass(frozen=True, slots=True)
class _Anchor:
    entity_id: str
    value: JsonValue


@dataclass(slots=True)
class FieldAnchors:
    """One field's anchors, and their owners by equality key (`normalize.dispatch.equality_key`), built once per page:
    a value then finds the owners of its equal anchors with one key lookup, not one equality test per anchor. `keyed`
    is False when some anchor has no key (the field's equality is no equivalence, as location's): its values are then
    compared with every anchor."""

    anchors: list[_Anchor] = field(default_factory=list)
    owners_by_key: dict[Hashable, list[str]] = field(default_factory=dict)
    keyed: bool = True

    def add(self, anchor: _Anchor, spec: FieldSpec) -> None:
        self.anchors.append(anchor)
        if not self.keyed:
            return
        key = equality_key(anchor.value, spec)
        if key is None:
            self.keyed = False
            return
        owners = self.owners_by_key.setdefault(key, [])
        if anchor.entity_id not in owners:
            owners.append(anchor.entity_id)

    def owners(self, value: JsonValue, spec: FieldSpec) -> list[str]:
        """The owners of the anchors `equal` to `value`, each once, in anchor order."""
        key = equality_key(value, spec) if self.keyed else None
        if key is not None:
            return self.owners_by_key.get(key, [])
        return list(dict.fromkeys(a.entity_id for a in self.anchors if equal(value, a.value, spec)))


def admit(
    kind: StateKind,
    documents: Sequence[Document],
    diagnostics: Sequence[str],
    page: ParsedPage,
    schema: SchemaDef,
    ctx: Context,
    others: Sequence[Candidate],
) -> Admitted:
    """The candidates of `documents` under the rule above, in document order, then schema field order; one per
    field, normalized value and owner (`_Distinct`)."""
    anchors = field_anchors(others, kind, schema)
    # What a value admits depends on the value alone, so a value repeated across the documents is read once.
    readings: dict[tuple[type, str | int | float], tuple[_Reading, ...]] = {}
    candidates = _Distinct()
    rows = _Rows(kind, ctx)
    scopes = _ItemScopes(schema, kind, others)
    for index, document in enumerate(documents):
        for leaf in _leaves(document.data, ctx):
            rows.see(index, leaf)
            key = (type(leaf.value), leaf.value)
            if key not in readings:
                readings[key] = tuple(_readings(leaf.value, page, schema, ctx, anchors))
            locator = document.locator(kind, leaf.path)
            for reading in readings[key]:
                if reading.independent and not scopes.admits(reading.field, leaf.holder):
                    continue
                candidates.add(
                    reading.normalized.value,
                    Candidate(
                        field=reading.field,
                        # Only a visible value is independent; one admitted by equality keeps its anchor's owner.
                        entity_id=rows.owner(index, leaf) if reading.independent else reading.owner,
                        source_kind=kind,
                        raw=leaf.value,
                        normalized=reading.normalized,
                        locator=locator,
                        independent=reading.independent,
                    ),
                )
    return Admitted(candidates=candidates.kept(), entities=rows.entities(), diagnostics=tuple(diagnostics))


class _Distinct:
    """The candidates of one collector, one per field, normalized value and owner. A large state blob repeats
    one value under many paths (a price in every cached copy of the product); each repeat is the same evidence, not
    more of it, as one owner's equal values are one value to resolution, so only one is kept: the one with the
    shortest locator, the value nearest the document root or the first document (on a tie, the first in document
    order). It keeps the place of the first repeat, so the order stays document order."""

    def __init__(self) -> None:
        self._kept: dict[tuple[str, str, bool, Hashable], Candidate] = {}

    def add(self, normalized: JsonValue, candidate: Candidate) -> None:
        key = (candidate.field, candidate.entity_id, candidate.independent, raw_key(normalized))
        kept = self._kept.get(key)
        if kept is None or len(candidate.locator) < len(kept.locator):
            self._kept[key] = candidate

    def kept(self) -> tuple[Candidate, ...]:
        return tuple(self._kept.values())


@dataclass(frozen=True, slots=True)
class _Leaf:
    """A string or number of a document, its normalized path, and the paths of the array rows holding it, outermost
    first."""

    path: str
    value: str | int | float
    rows: tuple[str, ...]
    holder: JsonValue = field(compare=False)  # the innermost object holding it, else the document


def _leaves(document: JsonValue, ctx: Context) -> Iterator[_Leaf]:
    """Every string and number in `document` (as `json_doc.scalars`) with the rows holding it and its innermost
    object. Iterative, as `json_doc.nodes`."""
    stack: list[tuple[str, JsonValue, tuple[str, ...], JsonValue]] = [(ROOT, document, (), document)]
    while stack:
        path, value, rows, holder = stack.pop()
        if is_scalar(value):
            yield _Leaf(path, value, rows, holder)
        else:
            inner = value if isinstance(value, dict) else holder
            stack.extend((p, child, r, inner) for p, child, r in reversed(_children(path, value, rows, ctx)))


def _children(
    path: str, value: JsonValue, rows: tuple[str, ...], ctx: Context
) -> list[tuple[str, JsonValue, tuple[str, ...]]]:
    if isinstance(value, dict):
        return [(member(path, key), child, rows) for key, child in value.items()]
    if not isinstance(value, list):
        return []
    objects = [child for child in value if isinstance(child, dict)]
    # An array of two or more objects is a set of items, each object one row. One object is its holder's own item,
    # unless it names a document of its own (`_names_a_document`): then it is one item of a set that holds one now.
    items = len(objects) >= _MIN_ROWS or (len(objects) == 1 and _names_a_document(objects[0], ctx))
    children: list[tuple[str, JsonValue, tuple[str, ...]]] = []
    for i, child in enumerate(value):
        item = f"{path}[{i}]"
        children.append((item, child, (*rows, item) if items and isinstance(child, dict) else rows))
    return children


_MIN_ROWS = 2


def _names_a_document(item: Mapping[str, JsonValue], ctx: Context) -> bool:
    """Whether an object writes, in its own scope (its members and the objects among them, not its arrays), a URL that
    names a document. The array holding it is then a set of items, each another page or the page itself, as
    the rows of an array of two or more are, and ownership tells them apart by their URLs (`resolve.ownership`): a lone
    related item names another page, while the page's own item names the page or nothing. A URL whose path names a
    media file (`_media`) addresses an image or a video of the item, not a document, so the page's own item holding its
    image stays its holder's."""
    stack: list[JsonValue] = list(item.values())
    written: list[str] = []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, str) and _WRITTEN_URL.match(value):
            written.append(value)
    return any(not _media(url) for url in canonical_urls(written, ctx))


# RFC 6838 §4.2: the top-level types of media content rather than documents.
_MEDIA_TOP_LEVEL = frozenset({"image", "audio", "video", "font"})


def _media(url: str) -> bool:
    """Whether the URL's path names a file of an image, audio, video or font media type by its extension."""
    found = media_type(url)
    return found is not None and found.partition("/")[0] in _MEDIA_TOP_LEVEL


class _Rows:
    """The array rows of one collector's documents: the URLs written in each row's scope, and the entities of the rows
    that own a value."""

    def __init__(self, kind: StateKind, ctx: Context) -> None:
        self._kind = kind
        self._ctx = ctx
        self._urls: dict[tuple[int, str], list[str]] = {}
        self._owners: dict[tuple[int, str], str | None] = {}  # each owning row and its enclosing rows, with its parent

    def see(self, index: int, leaf: _Leaf) -> None:
        """Note `leaf` as one of its innermost row's URLs when it is written as one (`_WRITTEN_URL`); a row writing a
        URL names an item, so it is an entity (with its enclosing rows) even when it owns no value: a listing's rows
        bind to it by that URL (`collect.items.bind`)."""
        if leaf.rows and isinstance(leaf.value, str) and _WRITTEN_URL.match(leaf.value):
            self._urls.setdefault((index, leaf.rows[-1]), []).append(leaf.value)
            self.owner(index, leaf)

    def owner(self, index: int, leaf: _Leaf) -> str:
        """The entity owning a visible value: its innermost row, else the page."""
        parent: str | None = None
        for row in leaf.rows:
            self._owners.setdefault((index, row), parent)
            parent = self._id(index, row)
        return parent or PAGE

    def entities(self) -> tuple[Entity, ...]:
        return tuple(
            Entity(
                id=self._id(index, row),
                types=(ARRAY_ROW,),
                # RFC 3986 §5.2: a relative reference resolves against the document base URL; one that does not
                # canonicalize names nothing.
                urls=canonical_urls(self._urls.get((index, row), ()), self._ctx),
                parent=parent or PAGE,
            )
            for (index, row), parent in self._owners.items()
        )

    def _id(self, index: int, row: str) -> str:
        # Unique per collector: the document's position among its documents (a script can hold several), and the path.
        return f"{self._kind}:{index}:{row}"


# The start of an http(s) URL (RFC 3986 §3.1, scheme case-insensitive) or of a path-absolute or network-path reference;
# which of them name a document is `canonical_urls`'s, asked only for the rows that own a value.
_WRITTEN_URL = re.compile(r"(?:https?:)?/", re.IGNORECASE)


class _ItemScopes:
    """Whether an object states the page's item (module docstring), asked once per object."""

    def __init__(self, schema: SchemaDef, kind: StateKind, others: Sequence[Candidate]) -> None:
        self._schema = schema
        # Strings as stated, not normalized: a normalizer reads a value out of a sentence ("up to $100") or resolves
        # any fragment to the page's URL, and an object holding such text states no item.
        self._stated = frozenset(
            text_form(o.raw)
            for o in others
            if o.source_kind != kind
            and isinstance(o.raw, str)
            and (spec := schema.fields.get(o.field)) is not None
            and _identifies(spec)
        ) - {""}
        self._states: dict[int, bool] = {}

    def admits(self, name: str, holder: JsonValue) -> bool:
        """Whether a visible value of field `name` held by `holder` may be admitted: any but an enum's, and an enum's
        when `holder` states the page's item. Objects are told apart by identity, as the documents live through
        `admit`."""
        if _leaf(self._schema.fields[name]).type is not FieldType.ENUM:
            return True
        key = id(holder)
        if key not in self._states:
            members = holder.values() if isinstance(holder, dict) else holder if isinstance(holder, list) else ()
            self._states[key] = any(isinstance(m, str) and text_form(m) in self._stated for m in members)
        return self._states[key]


# The schema.org properties of a Thing that name or identify one item: `name`, `url` and `identifier`, whose
# subproperties (sku, gtin, mpn, productID) an identifier field reads.
_NAME = "name"
_IDENTIFYING = frozenset({FieldType.IDENTIFIER, FieldType.URL})


def _leaf(spec: FieldSpec) -> FieldSpec:
    """The spec of a field's values: a list's item, as `normalize.dispatch.checkable` reads it."""
    while spec.type is FieldType.LIST and spec.item is not None:
        spec = spec.item
    return spec


def _identifies(spec: FieldSpec) -> bool:
    return spec.type in _IDENTIFYING or _NAME in properties(spec)


@dataclass(frozen=True, slots=True)
class _Reading:
    """One admission of a value: its field, its normalized form, its owner and whether it is independent."""

    field: str
    normalized: Ok
    owner: str
    independent: bool


def _readings(
    raw: str | int | float,
    page: ParsedPage,
    schema: SchemaDef,
    ctx: Context,
    anchors: Mapping[str, FieldAnchors],
) -> Iterator[_Reading]:
    # The same for every field, so computed at most once, and only when a field needs it.
    visible = cache(lambda: page.visible_values.visible(str(raw)))
    for name, spec in schema.fields.items():
        own = anchors.get(name)
        normalized = _normalized(raw, spec, ctx, visible, own)
        if normalized is None or (own is None and _identifies_other(raw, name, schema, ctx, anchors)):
            continue
        for owner, independent in _owners(normalized.value, spec, own):
            yield _Reading(name, normalized, owner, independent)


def _identifies_other(
    raw: str | int | float, name: str, schema: SchemaDef, ctx: Context, anchors: Mapping[str, FieldAnchors]
) -> bool:
    """Whether `name` is an identifier field and another identifier field has a candidate from another source kind
    equal to `raw`."""
    if schema.fields[name].type is not FieldType.IDENTIFIER:
        return False
    for other, spec in schema.fields.items():
        if other == name or spec.type is not FieldType.IDENTIFIER or (own := anchors.get(other)) is None:
            continue
        if isinstance(normalized := normalize(raw, spec, ctx), Ok) and own.owners(normalized.value, spec):
            return True
    return False


def admits(
    raw: str | int | float, spec: FieldSpec, page: ParsedPage, ctx: Context, anchors: FieldAnchors | None
) -> bool:
    """Whether the rule above admits `raw` for a field of `spec` with `anchors` (its `field_anchors` entry): equal to
    an anchor, or, for a field without one, checkable and visible."""
    normalized = _normalized(raw, spec, ctx, lambda: page.visible_values.visible(str(raw)), anchors)
    return normalized is not None and (anchors is None or bool(anchors.owners(normalized.value, spec)))


def _normalized(
    raw: str | int | float,
    spec: FieldSpec,
    ctx: Context,
    visible: Callable[[], bool],
    anchors: FieldAnchors | None,
) -> Ok | None:
    """`raw` normalized as `spec` when the rule lets it be tried: the field has anchors, or its type is checkable
    and `raw` is visible."""
    if anchors is None and not (checkable(raw, spec) and visible()):
        return None
    normalized = normalize(raw, spec, ctx)
    if not isinstance(normalized, Ok) or (anchors is None and not normalized.checked):
        # Without an anchor, only the checked syntax makes a visible value the field's (`Ok.checked`).
        return None
    return normalized


def _owners(value: JsonValue, spec: FieldSpec, anchors: FieldAnchors | None) -> Iterator[tuple[str, bool]]:
    """Who owns an admitted value and whether it is independent: the page for a visible value of a field without
    anchors; else each owner of an equal anchor once, in the order of `others`."""
    if anchors is None:
        yield PAGE, True
        return
    for owner in anchors.owners(value, spec):
        yield owner, False


def field_anchors(others: Sequence[Candidate], kind: StateKind, schema: SchemaDef) -> Mapping[str, FieldAnchors]:
    """Each schema field's normalized values from another source kind, with their owners. A candidate that did not
    normalize still gives its field an entry, so the field admits by equality only (the conservative reading of
    "lacking a candidate")."""
    anchors: dict[str, FieldAnchors] = {}
    for other in others:
        spec = schema.fields.get(other.field)
        if other.source_kind != kind and spec is not None:
            field_anchors = anchors.setdefault(other.field, FieldAnchors())
            if isinstance(other.normalized, Ok):
                field_anchors.add(_Anchor(other.entity_id, other.normalized.value), spec)
    return anchors
