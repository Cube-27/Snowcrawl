"""Captured network calls as a data source (Appendix A #59): the one reader of a browser
capture's network bodies that the `xhr` collector and API discovery share (C8).

A call is readable when its response was a success (RFC 9110 §15.3) and its body is JSON (`network_json`, RFC 8259).
Calls group into endpoints by request method, URL shape (the origin, whether there is a path, its segment count, the
suffix of its last segment and the query names in order: the parts that vary are what a recipe binds) and request body
shape (the paths of a JSON body's values, array indices written `[*]`; a form body's media type and names in order, as
a query's; any other body's media type alone, since its bytes hold the values that vary).

A call carries data for the page when one of its strings is visible in the page text (`VisibleIndex`): the page shows
what the call loaded. Its rows are the object items of the one array whose items show the most of their own (when the
rows the engine published from the page are known, the one whose items hold the most of their strings first): an item
counts when it holds a visible string no other item of the array holds (a value every item repeats, a currency or a
category, says nothing of which item is shown). Strings are compared in the text form the page text is indexed in
(`normalize.text.text_form`: references resolved, composed, white space collapsed and trimmed, as HTML rendering
collapses it), so values that differ only in white space are one value. JSON numbers are not counted: a page shows a
number formatted (a currency sign, grouping), and a short number matches any counter on the page. An array whose items
show fewer than `MIN_ITEMS` is no listing's rows (`acquire.items`).
"""

import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Final
from urllib.parse import urlsplit

from pydantic import JsonValue

from snowcrawl.acquire.items import MIN_ITEMS
from snowcrawl.collect import json_doc
from snowcrawl.content.page import ParsedPage
from snowcrawl.core import json_text
from snowcrawl.normalize.text import text_form
from snowcrawl.storage.capture_bundle import HeaderList, NetworkBody, network_json

# RFC 9110 §15.3: the 2xx class.
SUCCESS: Final = range(200, 300)
# What the URLs of one endpoint's requests share: the origin, whether there is a path, its segment count, the suffix of
# its last segment (`path_suffix`, "" for none) and the query names in order.
UrlShape = tuple[str, bool, int, str, tuple[str, ...]]
# The suffix a path's last segment names its representation's format with (`.json`, `.js`, `.xml`): a dot and a short
# name, as a media type registration lists its file extensions (RFC 6838 §5.6). `/items/a` and `/items/a.json` are two
# resources (RFC 3986 §3.3), and a suffix tells their answers' shapes apart.
_SUFFIX: Final = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}\Z")
# HTML §4.10.21.7: the media type of a form body (a recipe's `form` body); RFC 9110 §8.3: a body of no stated type may
# be taken as `application/octet-stream`.
FORM_TYPE: Final = "application/x-www-form-urlencoded"
_UNSTATED_TYPE: Final = "application/octet-stream"
# RFC 9535 §2.5.1.2: the wildcard selector, which a row query ends with.
_EVERY_ITEM: Final = "[*]"


@dataclass(frozen=True)
class EndpointKey:
    """What the requests of one endpoint share across calls and pages. `method` is None in bundles written before
    the request method was kept."""

    method: str | None
    url: UrlShape
    body: tuple[str, ...] | None


def endpoint_key(method: str | None, url: str, body: str | None, headers: HeaderList | None = None) -> EndpointKey:
    """The endpoint of a request; `headers` are the request's, whose `Content-Type` names a body's media type."""
    shape = body_shape(body, media_type(headers or ()))
    return EndpointKey(None if method is None else method.upper(), url_shape(url), shape)


def url_shape(url: str) -> UrlShape:
    parts = urlsplit(url)
    names = tuple(item.partition("=")[0] for item in parts.query.split("&")) if parts.query else ()
    segments = parts.path[1:].split("/") if parts.path else []
    return f"{parts.scheme}://{parts.netloc}", bool(parts.path), len(segments), path_suffix(parts.path), names


def path_suffix(segment: str) -> str:
    """The format suffix (`_SUFFIX`) that ends `segment` (or a path), dot included; "" when there is none."""
    found = _SUFFIX.search(segment)
    return "" if found is None else found[0]


def media_type(headers: HeaderList) -> str | None:
    """The essence of the request's `Content-Type` (RFC 9110 §8.3.1: type and subtype, compared case-insensitively,
    parameters dropped); None when it states none."""
    value = next((value for name, value in headers if name.lower() == "content-type"), None)
    return None if value is None else value.split(";", 1)[0].strip().lower() or None


def body_shape(body: str | None, media: str | None = None) -> tuple[str, ...] | None:
    """A JSON body's shape is the paths of its leaves; a form body's (`media` is `FORM_TYPE`) its media type and names
    in order; any other body's its media type (module docstring)."""
    if body is None:
        return None
    try:
        document = json_text.parse(body)
    except ValueError:
        if media == FORM_TYPE:
            # WHATWG URL §5.1: the body is `&`-separated `name=value` items, as a query is.
            return (FORM_TYPE, *(item.partition("=")[0] for item in body.split("&") if item))
        return (media or _UNSTATED_TYPE,)
    leaves = {path for path, value in json_doc.nodes(document, wildcard=True) if not isinstance(value, (dict, list))}
    return tuple(sorted(leaves))


@dataclass(frozen=True)
class Call:
    """One readable captured call: its index among the bundle's network bodies, its manifest entry and its body."""

    index: int
    entry: NetworkBody
    document: JsonValue

    @cached_property
    def endpoint(self) -> EndpointKey:
        entry = self.entry
        return endpoint_key(entry.method, entry.url, entry.request_body, entry.request_headers)


def read_calls(entries: Sequence[NetworkBody], bodies: Sequence[bytes]) -> Iterator[Call]:
    """The readable calls of a capture, in capture order."""
    for index, (entry, data) in enumerate(zip(entries, bodies, strict=True)):
        if entry.status not in SUCCESS:
            continue
        try:
            document = network_json(data)
        except ValueError:
            continue
        yield Call(index, entry, document)


def visible_paths(document: JsonValue, page: ParsedPage, memo: dict[str, bool] | None = None) -> set[str]:
    """The paths of the strings and numbers of `document` the page shows; `memo` keeps the answers for texts already
    looked up on this page."""
    memo = {} if memo is None else memo
    return {path for path, value in json_doc.scalars(document) if _shown(str(value), page, memo)}


def _shown(text: str, page: ParsedPage, memo: dict[str, bool]) -> bool:
    if text not in memo:
        memo[text] = page.visible_values.visible(text)
    return memo[text]


@dataclass(frozen=True)
class DataCall:
    """A call that carries data for the page: how many of its strings and numbers the page shows, and its row query
    (`$…[*]`, selecting the items of its rows array) with how many of those items show a string of their own."""

    call: Call
    visible: int
    rows: str | None
    row_hits: int


def data_calls(page: ParsedPage, published: Sequence[Mapping[str, JsonValue]] = ()) -> list[DataCall]:
    """The page's calls that carry data, best first: by row hits, then visible values; ties keep capture order. Empty
    when the page does not hold its bodies' manifest entries. `published` are the rows the engine published from the
    page, when known: a call's rows are then the array whose items hold the most of them (`_rows`), and the calls of
    one endpoint list one array (`_endpoint_rows`)."""
    if len(page.network_entries) != len(page.network):
        return []
    found: list[DataCall] = []
    held: dict[int, int] = {}  # per call, how many of its rows hold a published row's string
    memo: dict[str, bool] = {}
    stated = _published_strings(published)
    for call in read_calls(page.network_entries, page.network):
        visible = len(visible_paths(call.document, page, memo))
        if visible:
            rows, hits, held[call.index] = _rows(call.document, page, memo, stated)
            found.append(DataCall(call, visible, rows, hits))
    if stated:
        found = _endpoint_rows(found, held)
    return sorted(found, key=lambda data: (-data.row_hits, -data.visible))


def _endpoint_rows(found: list[DataCall], held: Mapping[int, int]) -> list[DataCall]:
    """The calls with each taking the row query of its endpoint's call whose rows hold the most published rows, when
    its own answer holds `MIN_ITEMS` object items there: the calls of one endpoint answer the pages of one listing (a
    first batch, then the next), and a capture whose page steps replaced the items shown (a "next" click) shows only
    the last call's, so an earlier call's rows are read from that structure, not from what the page still shows."""
    best: dict[EndpointKey, DataCall] = {}
    for data in found:
        here = held[data.call.index]
        current = best.get(data.call.endpoint)
        if data.rows is not None and here > 0 and (current is None or here > held[current.call.index]):
            best[data.call.endpoint] = data
    return [_with_rows(data, best.get(data.call.endpoint)) for data in found]


def _with_rows(data: DataCall, best: DataCall | None) -> DataCall:
    if best is None or best.rows is None or best.rows == data.rows:
        return data
    items = json_doc.select(best.rows, data.call.document)
    return data if sum(isinstance(item, dict) for _, item in items) < MIN_ITEMS else replace(data, rows=best.rows)


# One listing's series: the request method, the literal origin and path its calls share, the request body shape and
# the row query.
SeriesKey = tuple[str | None, str, tuple[str, ...] | None, str]


def row_series(calls: Sequence[DataCall]) -> list[tuple[DataCall, ...]]:
    """The calls with rows grouped into the pages of one listing a page loaded (a first batch, then more on scrolling):
    one method, body shape and row query, and the same origin and path, so that only the query or body values vary
    (RFC 3986 §3.3, §3.4: a path names the resource, a query the part of it asked for; a URL shape alone lets two
    resources' paths meet). The query's names may differ too: a listing's first call often leaves out the page or
    cursor parameter its next calls add. Each group is in capture order. A request sent again (same method, URL and
    body) answers with the same items, so only its first call counts. Groups come in the order of their best call in
    `calls`."""
    best: dict[SeriesKey, int] = {}  # each group's best rank in `calls`
    for rank, data in enumerate(calls):
        if (key := series_key(data)) is not None:
            best.setdefault(key, rank)
    groups: dict[SeriesKey, dict[tuple[str | None, str, str | None], DataCall]] = {}
    for data in sorted(calls, key=lambda data: data.call.index):
        if (key := series_key(data)) is not None:
            entry = data.call.entry
            groups.setdefault(key, {}).setdefault((entry.method, entry.url, entry.request_body), data)
    return [tuple(groups[key].values()) for key in sorted(groups, key=best.__getitem__)]


def series_key(data: DataCall) -> SeriesKey | None:
    """The series a call with rows belongs to (`row_series`); None for a call without rows."""
    if data.rows is None:
        return None
    parts = urlsplit(data.call.entry.url)
    endpoint = data.call.endpoint
    return endpoint.method, f"{parts.scheme}://{parts.netloc}{parts.path}", endpoint.body, data.rows


@dataclass(frozen=True)
class LearnedCalls:
    """A learned listing recipe (Appendix A #59) as the calls a page makes show it: the request method, the origin and
    path segments of its endpoint URL template (None for a segment holding a placeholder, which any value fills), the
    format suffix its path ends with (`path_suffix`, which a placeholder segment keeps literal: `{p1}.json`), the
    row query and the field map, paths relative to a row. The `xhr` collector reads the calls it matches with that
    map, text fields included, instead of proving paths on the page."""

    method: str
    origin: str
    segments: tuple[str | None, ...]
    suffix: str
    rows: str
    map: Mapping[str, str]

    def matches(self, data: DataCall) -> bool:
        entry = data.call.entry
        parts = urlsplit(entry.url)
        segments = parts.path[1:].split("/") if parts.path else []
        return (
            data.rows == self.rows
            and str(entry.method).upper() == self.method
            and f"{parts.scheme}://{parts.netloc}" == self.origin
            and len(segments) == len(self.segments)
            and path_suffix(parts.path) == self.suffix
            and all(want is None or want == got for want, got in zip(self.segments, segments, strict=True))
        )


def state_calls(states: Sequence[ParsedPage], published: Sequence[Mapping[str, JsonValue]] = ()) -> list[DataCall]:
    """The calls of the document states one listing's browser actions kept, in order (a kept state's calls are those
    since the state before it), as `data_calls` gives them for each (with the rows `published` from the listing),
    best first; a call's index counts the network bodies of the states before its own, so calls of different states
    never share one."""
    found: list[DataCall] = []
    offset = 0
    for state in states:
        for data in data_calls(state, published):
            found.append(replace(data, call=replace(data.call, index=offset + data.call.index)))
        offset += len(state.network)
    return sorted(found, key=lambda data: (-data.row_hits, -data.visible))


def holds_rows(data: DataCall, published: Sequence[Mapping[str, JsonValue]]) -> bool:
    """Whether the call's rows hold every row in `published` (one at least): each published row has a string, in the
    text form, that some of the rows hold and not every one (a value every row repeats, a currency, says nothing of
    which row it is). A row the engine published twice is held once."""
    items = [] if data.rows is None else json_doc.select(data.rows, data.call.document)
    strings = [_strings(item) for _, item in items if isinstance(item, dict)]
    holders = Counter(text for own in strings for text in own)
    held = {text for text, count in holders.items() if count < len(strings)}
    return bool(published) and all(_published_strings([row]) & held for row in published)


def _published_strings(rows: Sequence[Mapping[str, JsonValue]]) -> frozenset[str]:
    """The strings of published row values, in the text form (module docstring)."""
    return frozenset(
        form
        for row in rows
        for value in row.values()
        for _, leaf in json_doc.scalars(value)
        if isinstance(leaf, str) and (form := text_form(leaf))
    )


def _rows(
    document: JsonValue, page: ParsedPage, memo: dict[str, bool], published: frozenset[str] = frozenset()
) -> tuple[str | None, int, int]:
    """The row query of the array whose items show the most strings of their own, how many items do, and how many hold
    a `published` row string. With `published` row strings, the array whose items hold the most of those first: a
    page also shows arrays that are not its rows (filter facets, menus), whose items' strings are no row's values."""
    best: tuple[str | None, int, int] = (None, 0, 0)
    rank = (0, 0)
    for path, value in json_doc.nodes(document):
        if isinstance(value, list) and json_doc.is_query(query := path + _EVERY_ITEM):
            hits = _own_hits(value, page, memo)
            if hits < MIN_ITEMS:
                continue
            here = (_own_hits(value, page, memo, published) if published else 0, hits)
            if here > rank:
                best, rank = (query, hits, here[0]), here
    return best


def _own_hits(
    items: list[JsonValue], page: ParsedPage, memo: dict[str, bool], among: frozenset[str] | None = None
) -> int:
    """How many object items of `items` hold a visible string no other item holds (one of `among`, when given)."""
    strings = [_strings(item) for item in items if isinstance(item, dict)]
    if len(strings) < MIN_ITEMS:
        return 0
    holders: dict[str, int] = {}
    for own in strings:
        for text in own:
            holders[text] = holders.get(text, 0) + 1
    return sum(
        any(holders[text] == 1 and (among is None or text in among) and _shown(text, page, memo) for text in own)
        for own in strings
    )


def _strings(item: JsonValue) -> set[str]:
    """The item's strings in the text form (module docstring), white space alone dropped."""
    return {form for _, value in json_doc.scalars(item) if isinstance(value, str) and (form := text_form(value))}
