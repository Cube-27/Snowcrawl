"""Rows of a row-mapped `list<object>` field: which entities are the
field's rows, which sub-field candidates belong to which row, which rows are one variant, and what each publishes.

The rows are found in the entity graph, not by the collectors: the row path's alternatives that start at the target's
type are followed through `Entity.via`. A row's scope is its entity and those below it, as far as the next row or a
thing the page only relates to (exclusions). Rows are matched by key, never by position: rows that share
a key value are one variant, and a variant whose key sub-fields disagree is dropped (C7: decline rather than guess).
Each variant resolves its sub-fields with the resolver's own rules, its scope standing in for ownership.
"""

from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeGuard
from urllib.parse import parse_qsl, urlsplit

from pydantic import JsonValue

from snowcrawl.collect.base import Candidate, Entity
from snowcrawl.collect.scope import LISTS, RELATIONS
from snowcrawl.normalize.base import Ok
from snowcrawl.normalize.dispatch import equal, equality_key
from snowcrawl.normalize.url_key import DocumentKey, document_key
from snowcrawl.resolve.check_digits import CHECK_DIGIT_CHANCE, chance_passes
from snowcrawl.resolve.evidence import BELOW_MIN_CONFIDENCE, Calibration, assess
from snowcrawl.resolve.identity import VARIANT
from snowcrawl.resolve.offers import source_of
from snowcrawl.resolve.ownership import AGGREGATE_OFFER, EntityGraph
from snowcrawl.resolve.preference import Orders, PageFacts, Resolver
from snowcrawl.resolve.types import DroppedRow, Evidence, FieldOutcome, FieldStatus, RowOutcome
from snowcrawl.schemas.paths import MappedPath, parse_paths
from snowcrawl.schemas.types import FieldSpec, FieldType

# The row whose scope holds a candidate's owner, or None.
_Keys = dict[str, list[JsonValue]]  # a row's key values, by key sub-field
RowOf = Callable[[Candidate], str | None]
NOT_IN_ROW: Final = "not_in_row"
SINGLE_ROW: Final = "single_row"
# Why a row-mapped field publishes nothing, from the earliest stage to the furthest; the furthest reached is the reason.
_NO_ROWS = ("no_rows", "no_row_key", "row_key_conflict", "key_unpublished", "url_only", "other_page")
# Own variants: a row that states nothing of itself but a URL is a link (schema.org: a Product node with
# only `url` names a thing described elsewhere), and a row whose URL names another resource than the page (RFC 3986
# §3.2, §3.3: authority and path identify the resource; the query may select a variant of it) is another product. A
# `hasVariant` member's Offer URL is not such a URL: schema.org `hasVariant` makes the member the group's variant, and
# an Offer's `url` names where it is offered, not the item.
URL_ONLY: Final = "url_only"
OTHER_PAGE: Final = "other_page"
_URL = FieldSpec(type=FieldType.URL)
# Owner, 2026-10-16 (Appendix A #48): a product without variants has no variant list; the list is published only for
# two or more variants, and one row's values publish at the top level by the common-value rule.
_MIN_ROWS = 2


@dataclass(frozen=True, slots=True)
class RowField:
    """A row-mapped field of a detail page's target, and what its rows are resolved with."""

    name: str
    spec: FieldSpec
    graph: EntityGraph
    target: Entity
    page: PageFacts
    orders: Orders
    calibration: Calibration | None
    min_confidence: float | None
    page_urls: tuple[str, ...]  # the URLs naming the page (`collect.run`'s `page_urls`), canonicalized
    # The query parameters by which the page's platform selects a variant on the product's own URL (`Collected`).
    variant_params: tuple[str, ...] = ()

    @property
    def sub_fields(self) -> Mapping[str, FieldSpec]:
        return (self.spec.item.fields or {}) if self.spec.item is not None else {}

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(self.spec.key or ())


@dataclass(slots=True)
class _Variant:
    """Rows matched as one variant (a cluster), in collector order."""

    rows: list[Entity]
    keys: dict[str, list[JsonValue]] = field(default_factory=dict)


def resolve_rows(row_field: RowField, candidates: Sequence[Candidate]) -> FieldOutcome:
    """The field's outcome: the published rows as its value, with `items` and `dropped`."""
    mine = [c for c in candidates if c.field.startswith(f"{row_field.name}.")]
    rows = row_entities(row_field.graph, row_field.target, row_field.spec)
    row_of = _row_of(row_field.graph, rows)
    in_rows = [c for c in mine if row_of(c) is not None]
    # Only the target's own rows are siblings: another product's values say nothing of this one's.
    chance = chance_passes(row_field.name, row_field.sub_fields, in_rows)
    in_rows = [c for c in in_rows if c not in chance]
    variants, dropped, reasons = _variants(row_field, rows, in_rows, row_of)
    resolved = _Resolved(rejected=[(c, NOT_IN_ROW) for c in mine if row_of(c) is None], dropped=dropped)
    resolved.rejected.extend((c, CHECK_DIGIT_CHANCE) for c in chance)
    resolved.rejected.extend(_dropped_candidates(in_rows, row_of, dropped))
    for variant in variants:
        resolved.add(row_field, variant, in_rows, row_of)
    if len(resolved.published) >= _MIN_ROWS:
        return _published(resolved.published, tuple(resolved.dropped), tuple(resolved.rejected))
    if resolved.published:
        return _single_row(resolved, in_rows)
    reasons += resolved.reasons
    abstained = bool(variants) and resolved.abstained and set(reasons) == {"key_unpublished"}
    status: FieldStatus = "abstained" if abstained else "missing"
    return FieldOutcome(
        status=status,
        reason=BELOW_MIN_CONFIDENCE if abstained else max(reasons, key=_NO_ROWS.index),
        value=None,
        evidence=None,
        confidence=None,
        calibrated=False,
        source_kind=None,
        locator=None,
        derived_by=None,
        chosen=None,
        rejected=tuple(resolved.rejected),
        dropped=tuple(resolved.dropped),
    )


def _single_row(resolved: "_Resolved", candidates: Sequence[Candidate]) -> FieldOutcome:
    """The field's outcome when one row publishes: `missing` (`single_row`), its candidates rejected with that
    reason."""
    rejected = {id(c) for c, _ in resolved.rejected}
    single = [(c, SINGLE_ROW) for c in candidates if id(c) not in rejected]
    return FieldOutcome(
        status="missing",
        reason=SINGLE_ROW,
        value=None,
        evidence=None,
        confidence=None,
        calibrated=False,
        source_kind=None,
        locator=None,
        derived_by=None,
        chosen=None,
        rejected=(*resolved.rejected, *single),
        dropped=tuple(resolved.dropped),
    )


@dataclass(slots=True)
class _Resolved:
    """The variants resolved so far: those that published (each with its first row entity), the dropped rows, the
    rejected candidates, and whether every dropped variant was dropped because its key sub-fields abstained."""

    rejected: list[tuple[Candidate, str]]
    dropped: list[DroppedRow]
    published: list[tuple[str, RowOutcome]] = field(default_factory=list)
    abstained: bool = True
    reasons: list[str] = field(default_factory=list)  # why each resolved variant that did not publish was dropped

    def add(self, row_field: RowField, variant: "_Variant", candidates: Sequence[Candidate], row_of: RowOf) -> None:
        ids = {row.id for row in variant.rows}
        own = [c for c in candidates if row_of(c) in ids]
        outcome = _resolve_variant(row_field, own, ids, row_of)
        keys = [outcome.fields[sub].status for sub in row_field.key]
        # §4.5: a row publishes when at least one key sub-field publishes, and it is the target's own variant.
        reason = _not_own(row_field, outcome, variant.rows) if "published" in keys else "key_unpublished"
        if reason is None:
            self.published.append((variant.rows[0].id, outcome))
            return
        self.abstained = self.abstained and "abstained" in keys
        self.reasons.append(reason)
        self.dropped.append(DroppedRow(locators=tuple(row.id for row in variant.rows), reason=reason))
        self.rejected.extend((c, reason) for c in own)


def _not_own(row_field: RowField, outcome: RowOutcome, rows: Sequence[Entity]) -> str | None:
    """Why a published variant is not one of the target's own (`URL_ONLY`, `OTHER_PAGE`), or None when it is. A row
    stating only URLs is the page's own variant when each names the page's resource with a query parameter by which
    the page's platform selects a variant (`RowField.variant_params`): a variant addressed by its URL, not a link. A
    URL a `hasVariant` member states only on its Offers names no other product (`_offered_only`)."""
    published = {sub: o.value for sub, o in outcome.fields.items() if o.status == "published"}
    urls = {sub for sub in published if row_field.sub_fields[sub].type is FieldType.URL}
    pages = {_document(u) for u in (*row_field.page_urls, *row_field.target.urls)} - {None}
    offered = _offered_only(rows)
    stated = {_document(published[sub]) for sub in urls if published[sub] not in offered} - {None}
    if not published.keys() - urls and not _variant_urls(row_field, [published[sub] for sub in urls], pages):
        return URL_ONLY
    return OTHER_PAGE if pages and stated - pages else None


def _offered_only(rows: Sequence[Entity]) -> frozenset[str]:
    """The URLs the rows' `hasVariant` members state on their Offers only, which no row of the variant states as its own
    (`Entity.urls` holds both, `Entity.item_urls` the member's own)."""
    own = {u for e in rows for u in e.item_urls}
    return frozenset(u for e in rows if e.via == VARIANT for u in e.urls if u not in own)


def _variant_urls(row_field: RowField, values: Sequence[JsonValue], pages: set[DocumentKey | None]) -> bool:
    """Whether every value is a URL of the page's resource whose query selects a variant (`RowField.variant_params`:
    a parameter named so, or a name starting with a prefix that ends in `_`)."""
    params = row_field.variant_params
    return bool(params) and all(
        isinstance(v, str)
        and _document(v) in pages
        and any(_selects(name, params) for name, _ in parse_qsl(urlsplit(v).query))
        for v in values
    )


def _selects(name: str, params: Sequence[str]) -> bool:
    return any(name == p or (p.endswith("_") and name.startswith(p)) for p in params)


def _document(value: JsonValue) -> DocumentKey | None:
    """The document a URL names (`normalize.url_key.document_key`), however reached; None for anything that is no
    URL."""
    return document_key(value) if isinstance(value, str) else None


def row_entities(graph: EntityGraph, target: Entity, spec: FieldSpec) -> list[Entity]:
    """§4.3 steps 1 and 2: the entities the row path's alternatives that start at the target's type reach, in collector
    order, without an AggregateOffer (schema.org: a summary of offers, not one item's offer) and without one that has
    another reached entity below it (rows do not nest; the outer node groups them)."""
    rows = [e for e in _reached(graph, target, spec) if not graph.is_a(e, AGGREGATE_OFFER)]
    ids = {e.id for e in rows}
    outer = {a.id for e in rows for a in graph.ancestors(e) if a.id in ids}
    order = {entity_id: i for i, entity_id in enumerate(graph.entities)}
    return sorted((e for e in rows if e.id not in outer), key=lambda e: order[e.id])


def _reached(graph: EntityGraph, target: Entity, spec: FieldSpec) -> list[Entity]:
    """What the row path's alternatives that start at the target's type reach, each entity once."""
    paths = [path for path in parse_paths(spec.schema_org or "") if graph.is_a(target, path.root)]
    return list({e.id: e for path in paths for e in _follow(graph, target, path)}.values())


def _follow(graph: EntityGraph, start: Entity, path: MappedPath) -> list[Entity]:
    # Step `p` from entity E reaches the entities whose parent is E and whose `via` is `p` (§4.3 step 1).
    frontier = [start]
    for step in path.steps:
        ids = {e.id for e in frontier}
        frontier = [e for e in graph.entities.values() if e.parent in ids and e.via == step]
    return frontier


def _row_of(graph: EntityGraph, rows: Sequence[Entity]) -> RowOf:
    """§4.3 step 3: the row whose scope holds a candidate's owner, or None. The scope stops at a thing the row only
    relates to (`isRelatedTo`, `isSimilarTo`) and at a list (ItemList, ListItem)."""
    row_ids = {row.id for row in rows}
    cache: dict[str, str | None] = {}

    def row_of(candidate: Candidate) -> str | None:
        owner = graph.owner_of(candidate)
        if owner.id not in cache:
            cache[owner.id] = _scope_row(graph, owner, row_ids)
        return cache[owner.id]

    return row_of


def _scope_row(graph: EntityGraph, entity: Entity, row_ids: set[str]) -> str | None:
    for node in (entity, *graph.ancestors(entity)):
        if node.id in row_ids:
            return node.id
        if node.via in RELATIONS or any(graph.is_a(node, t) for t in LISTS):
            return None
    return None


def _variants(
    row_field: RowField, rows: Sequence[Entity], candidates: Sequence[Candidate], row_of: RowOf
) -> tuple[list[_Variant], list[DroppedRow], list[str]]:
    """§4.4: rows linked by a shared key value (`linked`), as variants in order of their first row; rows with no key
    value and variants whose key sub-fields disagree are dropped."""
    if not rows:
        return [], [], ["no_rows"]
    keys = _key_values(row_field, candidates, row_of)
    keyed = [row for row in rows if keys.get(row.id)]
    dropped = [DroppedRow(locators=(row.id,), reason="no_row_key") for row in rows if not keys.get(row.id)]
    reasons = ["no_row_key"] * len(dropped)
    variants: list[_Variant] = []
    key_fields = {sub: row_field.sub_fields[sub] for sub in row_field.key}
    for group in _lone_rows(row_field, rows, keys, linked(key_fields, keyed, keys)):
        variant = _Variant(group, _merged_keys(group, keys))
        if _conflicting(row_field, variant):
            dropped.append(DroppedRow(locators=tuple(row.id for row in group), reason="row_key_conflict"))
            reasons.append("row_key_conflict")
        else:
            variants.append(variant)
    return variants, dropped, reasons


def _lone_rows(
    row_field: RowField, rows: Sequence[Entity], keys: Mapping[str, _Keys], groups: list[list[Entity]]
) -> list[list[Entity]]:
    """The keyed rows as one variant when every source (`source_of`) states one row of the target, keyed or not, and
    their keys do not conflict: a source's one offer of the item is the item's one offer (atomic offers), whatever key
    each source happens to state it by (one a URL, another a SKU); else `groups`."""
    if len(groups) < 2 or len({source_of(row.id) for row in rows}) != len(rows):
        return groups
    keyed = [row for group in groups for row in group]
    merged = _Variant(keyed, _merged_keys(keyed, keys))
    return groups if _conflicting(row_field, merged) else [keyed]


def _key_values(
    row_field: RowField, candidates: Sequence[Candidate], row_of: RowOf
) -> dict[str, dict[str, list[JsonValue]]]:
    """Each row's key values by key sub-field: `Ok` values that pass screening, without a URL that
    names the page or the target rather than one variant."""
    screen = Resolver(row_field.graph, row_field.target, row_field.page, row_field.orders, owns=lambda _: True)
    excluded, shared = _not_keys(row_field)
    urls = frozenset(sub for sub in row_field.key if row_field.sub_fields[sub].type is FieldType.URL)
    values: dict[str, dict[str, list[JsonValue]]] = {}
    for sub in row_field.key:
        kept, _ = screen.screen([c for c in candidates if c.field == f"{row_field.name}.{sub}"])
        for candidate in kept:
            value = _key_value(candidate, excluded if sub in urls else frozenset())
            row = row_of(candidate)
            if value is not None and row is not None:
                values.setdefault(row, {}).setdefault(sub, []).append(value)
    return _without_group_urls(values, shared, urls)


def _key_value(candidate: Candidate, excluded: frozenset[str]) -> JsonValue:
    """The candidate's normalized value, unless it is a URL in `excluded` (only url keys are screened)."""
    value = candidate.normalized.value if isinstance(candidate.normalized, Ok) else None
    return None if isinstance(value, str) and value in excluded else value


def _not_keys(row_field: RowField) -> tuple[frozenset[str], frozenset[str]]:
    """URLs that name the page or the target, not one variant: the page URLs and the target's own URLs; and
    the target's URLs that one of its Offers repeats. `Entity.urls` includes the Offers' URLs, so a URL both declare
    is either the target's or an Offer's: `_without_group_urls` decides by how many rows carry it."""
    graph, target = row_field.graph, row_field.target
    # An AggregateOffer is no offer of one item: a URL it repeats is not one row's.
    offers = {u for e in graph.offers(target) for u in e.urls}
    own = [u for u in target.urls if u not in offers]
    return frozenset((*row_field.page_urls, *own)), frozenset(u for u in target.urls if u in offers)


def _without_group_urls(
    values: dict[str, dict[str, list[JsonValue]]], shared: frozenset[str], url_subs: frozenset[str]
) -> dict[str, _Keys]:
    """Drops a URL the target and its Offers both carry from the url key sub-fields when more than one row carries
    it: one row's URL is that variant's key (the one-Offer-per-variant shape); a URL many rows repeat names the
    group, and as a key it would join every row into one variant. Only url sub-fields are screened (`_key_value`)."""
    carriers: dict[str, set[str]] = {}
    for row, keys in values.items():
        for url in _urls_in(keys, shared, url_subs):
            carriers.setdefault(url, set()).add(row)
    group = frozenset(url for url, rows in carriers.items() if len(rows) > 1)
    return {row: _without(keys, group, url_subs) for row, keys in values.items()} if group else values


def _urls_in(keys: _Keys, urls: frozenset[str], url_subs: frozenset[str]) -> set[str]:
    return {v for sub in keys.keys() & url_subs for v in keys[sub] if _in(v, urls)}


def _in(value: JsonValue, urls: frozenset[str]) -> TypeGuard[str]:
    return isinstance(value, str) and value in urls


def _without(keys: _Keys, urls: frozenset[str], url_subs: frozenset[str]) -> _Keys:
    kept = {sub: [v for v in vs if sub not in url_subs or not _in(v, urls)] for sub, vs in keys.items()}
    return {sub: vs for sub, vs in kept.items() if vs}


def linked(
    key_fields: Mapping[str, FieldSpec], rows: Sequence[Entity], keys: Mapping[str, Mapping[str, list[JsonValue]]]
) -> list[list[Entity]]:
    """Connected components of rows that share a value of a key sub-field and state no different value of a key
    sub-field listed before it: `key_fields` runs from the most specific key to the least (the preset's
    `key`: a GTIN names one trade item, a seller's SKU may name its sizes together, a URL its colours). Two rows one
    document lists side by side (`_siblings`) are told apart by any key sub-field: one list names each of its items
    once, so two entries with other SKUs are two variants, whatever barcode they repeat; two documents may write one
    item's SKU each their own way, which only an earlier key overrules. Order-independent, each in collector order,
    ordered by its first row."""
    parent = {row.id: row.id for row in rows}
    by_id = {row.id: row for row in rows}

    def root(row_id: str) -> str:
        while parent[row_id] != row_id:
            parent[row_id] = parent[parent[row_id]]  # path halving keeps the trees shallow
            row_id = parent[row_id]
        return row_id

    earlier: dict[str, FieldSpec] = {}
    for sub, spec in key_fields.items():
        values = [(row.id, v) for row in rows for v in keys[row.id].get(sub, ())]
        for a, b in _sharing(values, spec):
            if not _told_apart(keys[a], keys[b], key_fields if _siblings(by_id[a], by_id[b]) else earlier):
                parent[root(b)] = root(a)
        earlier[sub] = spec
    groups: dict[str, list[Entity]] = {}
    for row in rows:
        groups.setdefault(root(row.id), []).append(row)
    return list(groups.values())


def _sharing(values: Sequence[tuple[str, JsonValue]], spec: FieldSpec) -> Iterator[tuple[str, str]]:
    """Every pair of rows whose values of one sub-field are equal: values meet by their equality key in one pass; only
    a value with no key (its type's equality is not transitive) is compared with the sub-field's earlier values."""
    seen: dict[Hashable, list[str]] = {}
    for i, (row_id, value) in enumerate(values):
        key = equality_key(value, spec)
        if key is not None:
            yield from ((other_id, row_id) for other_id in seen.setdefault(key, []))
            seen[key].append(row_id)
            continue
        yield from ((other_id, row_id) for other_id, other in values[:i] if equal(value, other, spec))


def _siblings(a: Entity, b: Entity) -> bool:
    """Whether two rows are values of one property of one node: entries of one list the document states."""
    return a.parent is not None and (a.parent, a.via) == (b.parent, b.via)


def _told_apart(
    a: Mapping[str, list[JsonValue]], b: Mapping[str, list[JsonValue]], earlier: Mapping[str, FieldSpec]
) -> bool:
    """Whether two rows state different values of one of the `earlier`, more specific, key sub-fields."""
    return any(not equal(x, y, spec) for sub, spec in earlier.items() for x in a.get(sub, ()) for y in b.get(sub, ()))


def _merged_keys(rows: Sequence[Entity], keys: Mapping[str, _Keys]) -> _Keys:
    merged: dict[str, list[JsonValue]] = {}
    for row in rows:
        for sub, values in keys[row.id].items():
            merged.setdefault(sub, []).extend(values)
    return merged


def _conflicting(row_field: RowField, variant: _Variant) -> bool:
    """A key sub-field with two different values means two variants were joined, or one source is wrong."""
    spec = row_field.sub_fields
    return any(not equal(values[0], other, spec[sub]) for sub, values in variant.keys.items() for other in values[1:])


def _resolve_variant(row_field: RowField, candidates: Sequence[Candidate], ids: set[str], row_of: RowOf) -> RowOutcome:
    """The variant's sub-fields, chosen by the resolver with the variant's scope as owner (atomic offers per row) and
    assessed in the `list.<type>` buckets."""
    resolver = Resolver(
        row_field.graph,
        row_field.target,
        row_field.page,
        row_field.orders,
        owns=lambda c: row_of(c) in ids,
        variant=True,
    )
    preferences = resolver.prefer_fields(row_field.sub_fields, candidates, prefix=f"{row_field.name}.")
    outcomes: dict[str, FieldOutcome] = {}
    for sub, spec in row_field.sub_fields.items():
        outcomes[sub] = assess(
            spec.type,
            preferences[sub],
            row_field.calibration,
            min_confidence=row_field.min_confidence,
            corroborated=False,
            row=True,
        )
    return RowOutcome(fields=outcomes)


def _dropped_candidates(
    candidates: Sequence[Candidate], row_of: RowOf, dropped: Sequence[DroppedRow]
) -> list[tuple[Candidate, str]]:
    """The candidates of rows dropped before resolution, each with its row's reason."""
    reason_of = {row_id: row.reason for row in dropped for row_id in row.locators}
    return [(c, reason_of[r]) for c in candidates if (r := row_of(c)) is not None and r in reason_of]


def _published(
    rows: Sequence[tuple[str, RowOutcome]], dropped: tuple[DroppedRow, ...], rejected: tuple[tuple[Candidate, str], ...]
) -> FieldOutcome:
    """§4.5 "The field's outcome": the published rows' published sub-fields as the value; `agreed` when every published
    sub-field is, `adapter` when every one is an adapter's, else `structured`; the least confidence,
    calibrated only when all are; the first row's source kind and its entity's locator."""
    published = [o for _, row in rows for o in row.fields.values() if o.status == "published"]
    confidences = [o.confidence for o in published]
    first_row, first = rows[0][0], next(o for o in rows[0][1].fields.values() if o.status == "published")
    buckets = {o.evidence for o in published}
    evidence: Evidence = "structured"
    if buckets == {"agreed"}:
        evidence = "agreed"
    elif buckets == {"adapter"}:
        evidence = "adapter"
    return FieldOutcome(
        status="published",
        reason=None,
        value=[{sub: o.value for sub, o in row.fields.items() if o.status == "published"} for _, row in rows],
        evidence=evidence,
        confidence=None if None in confidences else min(c for c in confidences if c is not None),
        calibrated=all(o.calibrated for o in published),
        source_kind=first.source_kind,
        locator=first_row,
        derived_by=None,
        chosen=None,
        rejected=rejected,
        items=tuple(row for _, row in rows),
        dropped=dropped,
    )
