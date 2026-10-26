"""Listing recipe learning (the adapter read for a listing): the endpoint whose calls gave a
listing's rows, its field map relative to a row, its pagination, and each page as replay reads it.

A listing page's calls are read as the `xhr` collector reads them (`network_match.state_calls`, `row_series`): series of
one method, origin, path, body shape and row query. A series' row field path is one whose value, normalized as the
field's type, is `equal` to a value the engine published for that field in a row of the page (`record.data`), in
`MIN_ITEMS` rows or more (or in every published row, when fewer were published); an endpoint is a series found on
every sample page with such a path for a required field, and its map takes, per field, the path found on every page
that published the field, the one matching the most rows. The pagination is inferred from each sample page's series
of two or more calls (`pagination.infer`), and every page that shows one must show the same.

A page's request is the first call of its series with its pagination value set to where the listing starts
(`pagination.first_request`), so that binding sees the page value as the constant it is in a recipe; replay compares
the first two pages with the page's first two captured calls (`replay`).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic import JsonValue

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.acquire.items import MIN_ITEMS
from snowcrawl.collect import json_doc
from snowcrawl.collect.network_match import DataCall, SeriesKey, row_series, series_key, state_calls
from snowcrawl.config_ir.api_recipe.binding import CapturedRequest, PageData, Sample, page_data
from snowcrawl.config_ir.api_recipe.discovery import Reading, member_pairs, normalized
from snowcrawl.config_ir.api_recipe.model import Pagination
from snowcrawl.config_ir.api_recipe.pagination import first_request, infer
from snowcrawl.config_ir.api_recipe.replay import REPLAYED_PAGES, ReplayPage
from snowcrawl.config_ir.held_out import StoredPage
from snowcrawl.content.page import ParsedPage
from snowcrawl.db.models import CaptureId
from snowcrawl.normalize.base import Context
from snowcrawl.normalize.dispatch import equal
from snowcrawl.resolve.evidence import required_fields
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef
from snowcrawl.storage.capture_bundle import CaptureBundle


@dataclass(frozen=True)
class ListingPage:
    """A stored listing page as listing learning reads it: its capture, URL, the rows the engine published from it,
    its normalize context and JSON documents, and the document states whose calls it reads (its own capture, or the
    states a browser render with paging kept)."""

    capture_id: CaptureId
    url: str
    rows: tuple[Mapping[str, JsonValue], ...]
    ctx: Context
    page_data: PageData
    states: tuple[ParsedPage, ...]

    @classmethod
    def of(cls, stored: StoredPage) -> "ListingPage":
        held = stored.held_out
        rows = tuple(row.published for row in held.rows)
        page = held.page
        return cls(held.capture_id, stored.bundle.manifest.url, rows, held.ctx, page_data(page), (page,))

    def rendered(self, bundles: Sequence[CaptureBundle]) -> "ListingPage":
        """The page with the states of a browser render instead of its capture; as it was when the render kept none."""
        return replace(self, states=tuple(ParsedPage.of(bundle) for bundle in bundles)) if bundles else self

    @property
    def has_calls(self) -> bool:
        return any(state.network for state in self.states)


@dataclass(frozen=True)
class ListingEndpoint:
    """A listing endpoint found on every sample page: its series key, the map of each row field (a path relative to a
    row) and how many rows the map matched on all pages."""

    key: SeriesKey
    map: dict[str, str]
    score: int


def page_series(page: ListingPage) -> dict[SeriesKey, tuple[DataCall, ...]]:
    """The page's series by key, in the order of their best call; a series whose request method the capture did not
    record (older bundles) is left out, since a recipe can only replay a request it knows the method of."""
    found: dict[SeriesKey, tuple[DataCall, ...]] = {}
    for series in row_series(state_calls(page.states, page.rows)):
        key = series_key(series[0])
        if key is not None and key[0] is not None:
            found[key] = series
    return found


def discover_listing(pages: Sequence[ListingPage], schema: SchemaDef) -> list[ListingEndpoint]:
    """The listing endpoints of the sample `pages`, best first (by score; ties keep the first page's order)."""
    if not pages:
        return []
    series = [page_series(page) for page in pages]
    required = required_fields(schema)
    found: list[ListingEndpoint] = []
    for key in series[0]:
        if all(key in own for own in series[1:]):
            hits = [_field_hits(own[key], page, schema) for own, page in zip(series, pages, strict=True)]
            endpoint = _endpoint(key, hits, pages, schema)
            if endpoint is not None and required & endpoint.map.keys():
                found.append(endpoint)
    return sorted(found, key=lambda endpoint: -endpoint.score)


def listing_pagination(pages: Sequence[ListingPage], key: SeriesKey) -> Pagination | None:
    """The pagination every page's series of two or more calls follows; None when none has two calls, one of them
    follows none, or two follow different ones."""
    return series_pagination([page_series(page).get(key, ()) for page in pages], key[3])


def series_pagination(series: Sequence[Sequence[DataCall]], rows: str) -> Pagination | None:
    """The pagination every series of two or more calls follows, with `rows` as its row query (`listing_pagination`),
    one series per page."""
    found = [infer([data.call for data in calls], rows) for calls in series if len(calls) >= 2]
    if not found or found[0] is None or any(other != found[0] for other in found[1:]):
        return None
    return found[0]


def replay_page(page: ListingPage, key: SeriesKey, pagination: Pagination) -> ReplayPage | None:
    """The page as replay reads it: its first call of the endpoint as the listing's first request, and the responses
    of its first two calls; None when it made no call of the endpoint."""
    series = page_series(page).get(key, ())
    if not series:
        return None
    request = first_page_request(series[0], pagination)
    if request is None:
        return None
    documents = tuple(data.call.document for data in series[:REPLAYED_PAGES])
    return ReplayPage(Sample(page.url, request, page.page_data), {}, page.ctx, documents)


def first_page_request(data: DataCall, pagination: Pagination) -> CapturedRequest | None:
    """The captured request of `data` with the pagination's value set to where the listing starts; None when its body
    holds no value at the pagination's path."""
    entry = data.call.entry
    method = str(entry.method).upper()
    request = JsonRequest(entry.url, entry.url, "POST" if method == "POST" else "GET", body=entry.request_body)
    first = first_request(pagination, request)
    if first is None:
        return None
    return CapturedRequest(method, first.url, first.body, entry.request_headers or ())


# --- field paths ------------------------------------------------------------------------------------------------------

# Per field, the row paths whose value equals a published row value, with how many rows matched there.
_Hits = dict[str, dict[str, int]]


def _endpoint(
    key: SeriesKey, hits: Sequence[_Hits], pages: Sequence[ListingPage], schema: SchemaDef
) -> ListingEndpoint | None:
    paths: dict[str, str] = {}
    score = 0
    for name in schema.fields:
        proving = [
            (own.get(name, {}), n) for own, page in zip(hits, pages, strict=True) if (n := _published(page, name))
        ]
        best = _best_path(proving)
        if best is not None:
            paths[name] = best
            score += sum(found.get(best, 0) for found, _ in proving)
    return ListingEndpoint(key, paths, score) if paths else None


def _best_path(proving: Sequence[tuple[Mapping[str, int], int]]) -> str | None:
    """Of the paths matching a field's values on every page that published it (`MIN_ITEMS` rows, or every published
    row when fewer), the one matching the most rows; None when none does."""
    if not proving:
        return None
    shared = [
        path
        for path in proving[0][0]
        if all(found.get(path, 0) >= min(MIN_ITEMS, published) for found, published in proving)
    ]
    return max(shared, key=lambda path: sum(found.get(path, 0) for found, _ in proving)) if shared else None


def _published(page: ListingPage, name: str) -> int:
    return sum(row.get(name) is not None for row in page.rows)


def _field_hits(series: Sequence[DataCall], page: ListingPage, schema: SchemaDef) -> _Hits:
    """Per row field the engine published on the page, each row path whose value equals one of its published row
    values, with the number of rows where it does."""
    wanted = _wanted(page, schema)
    money = any(schema.fields[name].type is FieldType.MONEY for name in wanted)
    hits: _Hits = {name: {} for name in wanted}
    for row in _rows(series):
        readings = [Reading(path, raw) for path, raw in json_doc.scalars(row)]
        if money:
            readings += list(member_pairs(row))
        for name, values in wanted.items():
            for path in _matching(readings, values, schema.fields[name], page.ctx):
                hits[name][path] = hits[name].get(path, 0) + 1
    return hits


def _wanted(page: ListingPage, schema: SchemaDef) -> dict[str, list[JsonValue]]:
    """The values the engine published for each row field in the page's rows, for the fields it published."""
    found: dict[str, list[JsonValue]] = {}
    for name, spec in schema.fields.items():
        values = [row[name] for row in page.rows if row.get(name) is not None] if spec.scope == "row" else []
        if values:
            found[name] = values
    return found


def _matching(readings: list[Reading], values: list[JsonValue], spec: FieldSpec, ctx: Context) -> dict[str, None]:
    return dict.fromkeys(
        reading.path
        for reading, value in normalized(readings, spec, ctx)
        if any(equal(value, published, spec) for published in values)
    )


def _rows(series: Sequence[DataCall]) -> Iterator[dict[str, JsonValue]]:
    for data in series:
        if data.rows is None:
            continue
        for _, row in json_doc.select(data.rows, data.call.document):
            if isinstance(row, dict):
                yield row
