"""API discovery: from the learning sample's browser captures, the endpoints
whose JSON responses carry the page's published values on every sample page, each with a JSONPath per field.

Captured calls are read through `collect/network_match.py` (Appendix A #59), the one reader the `xhr` collector
shares: a readable call (a success, JSON) counts only when the capture recorded the method of the request behind it,
since a recipe can only replay such a request. Calls group into endpoints by `network_match.EndpointKey` (request
method, URL shape, request body shape); on each page the best-scoring body of a group stands for it.

A body scores one point per required field (required groups included) whose published value is `equal` to a
reading of the body, and one per other field with a visible leaf of its type. A reading is one scalar leaf,
or for money, two scalar members of one object joined with `+` (atomic money). A field's map path is the first
path, in the first page's document order, that reads its value on every sample page; an endpoint is a candidate when
the map holds a required field. Candidates come best first; the first is the one a recipe is bound from (one
endpoint per config).
"""

from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from snowcrawl.collect import json_doc
from snowcrawl.collect.api import joined_raw, map_paths
from snowcrawl.collect.network_match import Call, EndpointKey, endpoint_key, read_calls, visible_paths
from snowcrawl.config_ir.api_recipe.binding import CapturedRequest
from snowcrawl.content.page import ParsedPage
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import equal, equality_key, normalize
from snowcrawl.resolve.evidence import required_fields
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef
from snowcrawl.storage.capture_bundle import CaptureBundle

Scalar = str | int | float


@dataclass(frozen=True)
class DiscoverySample:
    """One sample page: its browser capture with network bodies, its published values (`record.data`) and the
    normalize context of the page."""

    bundle: CaptureBundle
    values: Mapping[str, JsonValue]
    ctx: Context


@dataclass(frozen=True)
class EndpointCandidate:
    """An endpoint found on every sample page: its request on each (in sample order, for binding), the JSONPath of
    each field it carries (a `map` entry of the recipe) and its score summed over the pages."""

    requests: tuple[CapturedRequest, ...]
    map: dict[str, str]
    score: int


def discover(samples: Sequence[DiscoverySample], schema: SchemaDef) -> list[EndpointCandidate]:
    """The candidate endpoints of `samples`, best first (by score; ties keep the first page's capture order)."""
    if not samples:
        return []
    required = required_fields(schema)
    pages = [_bodies(sample, schema, required) for sample in samples]
    candidates: list[EndpointCandidate] = []
    for key in pages[0]:
        if all(key in page for page in pages[1:]):
            candidate = _candidate([page[key] for page in pages], schema, required)
            if candidate is not None:
                candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: -candidate.score)


def endpoint_request(bundle: CaptureBundle, endpoint: EndpointCandidate) -> CapturedRequest | None:
    """The request to `endpoint` a capture holds a readable answer to, the first in capture order; `None` when it holds
    none. Held-out validation reads the captures that hold one."""
    first = endpoint.requests[0]
    key = endpoint_key(first.method, first.url, first.body, first.headers)
    return next((_request(call) for call in _replayable(bundle) if call.endpoint == key), None)


@dataclass(frozen=True)
class Reading:
    """A value a body offers a field: the map path that gives it and the raw value."""

    path: str
    raw: Scalar


# The readings of a body that normalize as one field's type, each with its normalized value.
Normalized = list[tuple[Reading, JsonValue]]


@dataclass(frozen=True)
class _Body:
    request: CapturedRequest
    hits: dict[str, dict[str, None]]  # field -> the map paths that read its published value, in document order
    score: int


def _candidate(bodies: list[_Body], schema: SchemaDef, required: frozenset[str]) -> EndpointCandidate | None:
    paths: dict[str, str] = {}
    for name in schema.fields:
        shared = [
            path for path in bodies[0].hits.get(name, {}) if all(path in body.hits.get(name, {}) for body in bodies)
        ]
        if shared:
            paths[name] = shared[0]
    if not required & paths.keys():
        return None
    return EndpointCandidate(
        requests=tuple(body.request for body in bodies), map=paths, score=sum(body.score for body in bodies)
    )


def _bodies(sample: DiscoverySample, schema: SchemaDef, required: frozenset[str]) -> dict[EndpointKey, _Body]:
    """The page's replayable bodies, the best-scoring one per endpoint, in capture order of first appearance."""
    page = ParsedPage.of(sample.bundle)
    best: dict[EndpointKey, _Body] = {}
    memo: dict[str, bool] = {}
    for call in _replayable(sample.bundle):
        body = _scored(_request(call), call.document, page, memo, sample, schema, required)
        if call.endpoint not in best or body.score > best[call.endpoint].score:
            best[call.endpoint] = body
    return best


def _replayable(bundle: CaptureBundle) -> Iterator[Call]:
    """The capture's readable calls whose request method was recorded, in capture order."""
    return (call for call in read_calls(bundle.manifest.network_bodies, bundle.network) if call.entry.method)


def _request(call: Call) -> CapturedRequest:
    entry = call.entry
    return CapturedRequest(str(entry.method).upper(), entry.url, entry.request_body, entry.request_headers or ())


def _scored(
    request: CapturedRequest,
    document: JsonValue,
    page: ParsedPage,
    memo: dict[str, bool],
    sample: DiscoverySample,
    schema: SchemaDef,
    required: frozenset[str],
) -> _Body:
    # Each leaf is normalized once per field: for equality with the published value and for the visible-type count.
    readings = body_readings(document, sample.ctx, schema)
    typed, hits = readings.typed, readings.hits(sample.values)
    matched = required & hits.keys()
    others = [name for name in schema.fields if name not in matched]
    used = {leaf for name in matched for path in hits[name] for leaf in map_paths(path)}
    visible = visible_paths(document, page, memo) - used if others else set()
    shown = [name for name in others if any(reading.path in visible for reading, _ in typed[name])]
    return _Body(request, hits, len(matched) + len(shown))


@dataclass(frozen=True)
class _FieldReadings:
    """One field's readings of a body that normalize as its type, in document order, indexed by their value's
    equality key (`normalize.dispatch.equality_key`); `unkeyed` holds the positions of those whose type's equality is
    no equivalence (a location), which are compared one by one."""

    readings: Normalized
    keyed: dict[Hashable, list[int]]
    unkeyed: list[int]

    @classmethod
    def of(cls, readings: Normalized, spec: FieldSpec) -> "_FieldReadings":
        keyed: dict[Hashable, list[int]] = {}
        unkeyed: list[int] = []
        for position, (_, value) in enumerate(readings):
            if (key := equality_key(value, spec)) is None:
                unkeyed.append(position)
            else:
                keyed.setdefault(key, []).append(position)
        return cls(readings, keyed, unkeyed)

    def paths(self, value: JsonValue, spec: FieldSpec) -> dict[str, None]:
        """The map paths, in document order, whose reading is `equal` to `value`: looked up by its key (`equal`
        exactly when the keys are), else compared with every reading."""
        key = equality_key(value, spec)
        if key is None:
            positions: Iterable[int] = range(len(self.readings))
        else:
            positions = sorted([*self.keyed.get(key, ()), *self.unkeyed])
        return dict.fromkeys(self.readings[i][0].path for i in positions if equal(self.readings[i][1], value, spec))


@dataclass(frozen=True)
class BodyReadings:
    """A body's readings of each field: `typed`, those of one scalar leaf that normalize as the field's type; and for
    matching a published value, those with money's joined member pairs, normalized once per body and indexed by value,
    so that each published row of a listing is looked up rather than compared with every reading."""

    typed: dict[str, Normalized]
    schema: SchemaDef
    matching: dict[str, _FieldReadings]

    def hits(self, values: Mapping[str, JsonValue]) -> dict[str, dict[str, None]]:
        """Each field published in `values`: its map paths in the body, in document order."""
        hits: dict[str, dict[str, None]] = {}
        for name, spec in self.schema.fields.items():
            if name in values and (paths := self.matching[name].paths(values[name], spec)):
                hits[name] = paths
        return hits


def body_readings(document: JsonValue, ctx: Context, schema: SchemaDef) -> BodyReadings:
    """A body's readings of each field (`BodyReadings`); money also reads joined member pairs."""
    leaves = [Reading(path, raw) for path, raw in json_doc.scalars(document)]
    typed = {name: normalized(leaves, spec, ctx) for name, spec in schema.fields.items()}
    money = any(spec.type is FieldType.MONEY for spec in schema.fields.values())
    pairs = list(member_pairs(document)) if money else []
    matching = {
        name: _FieldReadings.of(
            typed[name] + (normalized(pairs, spec, ctx) if spec.type is FieldType.MONEY else []), spec
        )
        for name, spec in schema.fields.items()
    }
    return BodyReadings(typed, schema, matching)


def normalized(readings: list[Reading], spec: FieldSpec, ctx: Context) -> Normalized:
    return [
        (reading, result.value) for reading in readings if isinstance(result := normalize(reading.raw, spec, ctx), Ok)
    ]


def member_pairs(document: JsonValue) -> Iterator[Reading]:
    """The ordered pairs of scalar members of one object that may join into one money value (`+`), in
    document order: one member holds a digit and the other none.

    The schema money text reading (`normalize/numbers.py`, `AMOUNT_RUN`): a money value holds exactly one run of
    amount digits, so a joined pair is an amount member and a currency member without digits. Two digit-free members
    state no amount, and two amounts are two runs (a range or a list). So an object gives amounts x digit-free members
    pairs, not the square of its size.
    """
    for path, value in json_doc.nodes(document):
        if isinstance(value, dict):
            members = [(json_doc.member(path, key), child) for key, child in value.items() if json_doc.is_scalar(child)]
            yield from _amount_pairs(members)


def _amount_pairs(members: list[tuple[str, Scalar]]) -> Iterator[Reading]:
    amounts = [member for member in members if _has_digit(member[1])]
    plain = [member for member in members if not _has_digit(member[1])]
    for first, a in members:
        for second, b in plain if _has_digit(a) else amounts:
            yield Reading(f"{first}+{second}", joined_raw([a, b]))


def _has_digit(value: Scalar) -> bool:
    return not isinstance(value, str) or any(c.isdigit() for c in value)
