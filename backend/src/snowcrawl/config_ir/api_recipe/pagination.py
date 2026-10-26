"""A listing recipe's pagination: which kind a series of captured calls follows, and
the next request of each kind.

Inference reads two or more calls of one listing's series (`network_match.row_series`: one endpoint, origin, path and
row query), in the order the page made them, and tries the kinds from the one that names the next request most exactly
to the one that names it least:

- `link`: each response's `Link` field (RFC 8288 §3) has a target with relation type `next` that is the next call's
  URL (RFC 3986 §5 resolution against the call's URL);
- `next_url`: one path of each response holds a string that resolves to the next call's URL;
- `param`: exactly one request value varies, a query parameter or a JSON body value, and its integers step by one
  constant amount (a page number or an item offset); the first call may leave it out when three calls show the step;
- `cursor`: exactly one request value varies, and each call's value is the scalar one path of the previous response
  holds (the first call may leave it out: there is no previous response).

Any other varying request value (a timestamp, a token) leaves the series without pagination: replaying it is not
proven. The same functions build the next request when a recipe is served (`execute.pages`) and when learning replays
its second page (`replay`), so the two cannot disagree (C8).
"""

import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Final
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import JsonValue

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.collect import json_doc
from snowcrawl.collect.network_match import Call
from snowcrawl.config_ir.api_recipe.binding import with_query_value
from snowcrawl.config_ir.api_recipe.model import (
    CursorPagination,
    LinkPagination,
    NextUrlPagination,
    OnePagePagination,
    PageWhere,
    Pagination,
    ParamPagination,
)
from snowcrawl.core import json_text
from snowcrawl.storage.capture_bundle import HeaderList

Scalar = str | int | float
# A request value: where it sits (`query` name, or `body` normalized path) and its name there.
_Slot = tuple[PageWhere, str]
# RFC 8288 §3: `<target>` then `;`-separated parameters; a link-value list is `,`-separated (RFC 9110 §5.6.1).
_QUOTED: Final = r'"(?:[^"\\]|\\.)*"'
_LINK_VALUE: Final = re.compile(r"<([^>]*)>((?:\s*;[^;,\"]*(?:" + _QUOTED + r")?)*)")
_LINK_PARAM: Final = re.compile(r';\s*([^\s=;,]+)\s*(?:=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s;,]*)))?')
# RFC 8288 §3.3: `rel` holds one or more relation types separated by whitespace, compared case-insensitively.
_NEXT: Final = "next"
# ASCII digits only: a page number or offset as a URL or a JSON string writes it.
_INTEGER: Final = re.compile(r"0|[1-9]\d*", re.ASCII)


# --- inference --------------------------------------------------------------------------------------------------------


def infer(calls: Sequence[Call], rows: str) -> Pagination | None:
    """The pagination the calls of one listing series follow (module docstring), with `rows` as its row query; None
    when there are fewer than two calls, they differ in method, or no kind explains every step."""
    if len(calls) < 2 or len({call.entry.method for call in calls}) != 1:
        return None
    for kind in _KINDS:
        found = kind(calls, rows)
        if found is not None:
            return found
    return None


def _link(calls: Sequence[Call], rows: str) -> Pagination | None:
    for before, after in _pairs(calls):
        target = next_link(before.entry.response_headers or (), before.entry.url)
        if target != after.entry.url or before.entry.request_body != after.entry.request_body:
            return None
    return LinkPagination(kind="link", rows=rows)


def _next_url(calls: Sequence[Call], rows: str) -> Pagination | None:
    def targets(before: Call, after: Call) -> list[str] | None:
        if before.entry.request_body != after.entry.request_body:
            return None
        return [
            path
            for path, value in json_doc.scalars(before.document)
            if isinstance(value, str) and urljoin(before.entry.url, value) == after.entry.url
        ]

    paths = _shared_paths(calls, targets)
    return None if not paths else NextUrlPagination(kind="next_url", next=paths[0], rows=rows)


def _param(calls: Sequence[Call], rows: str) -> Pagination | None:
    slot = _varying(calls)
    if slot is None:
        return None
    values = [_integer(_values(call).get(slot)) for call in calls]
    if values[0] is None and len(values) >= 3:
        values = values[1:]
        lead = 1
    else:
        lead = 0
    if any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    step = present[1] - present[0]
    if step < 1 or any(b - a != step for a, b in pairwise(present)):
        return None
    start = present[0] - lead * step
    if start < 0:
        return None
    return ParamPagination(kind="param", where=slot[0], name=slot[1], start=start, step=step, rows=rows)


def _cursor(calls: Sequence[Call], rows: str) -> Pagination | None:
    slot = _varying(calls)
    if slot is None:
        return None

    def holders(before: Call, after: Call) -> list[str] | None:
        value = _text(_values(after).get(slot))
        if value is None:
            return None
        return [path for path, leaf in json_doc.scalars(before.document) if _text(leaf) == value]

    paths = _shared_paths(calls, holders)
    return (
        None if not paths else CursorPagination(kind="cursor", where=slot[0], name=slot[1], cursor=paths[0], rows=rows)
    )


def _shared_paths(calls: Sequence[Call], found: Callable[[Call, Call], list[str] | None]) -> list[str]:
    """The response paths `found` gives for every pair of successive calls (None: the pair rules the kind out), in the
    first response's document order."""
    paths: list[str] | None = None
    for before, after in _pairs(calls):
        here = found(before, after)
        if here is None:
            return []
        paths = here if paths is None else [path for path in paths if path in here]
        if not paths:
            return []
    return paths or []


_KINDS: Final[tuple[Callable[[Sequence[Call], str], Pagination | None], ...]] = (_link, _next_url, _param, _cursor)


def _pairs(calls: Sequence[Call]) -> Iterator[tuple[Call, Call]]:
    return pairwise(calls)


def _varying(calls: Sequence[Call]) -> _Slot | None:
    """The one request value that differs between the calls (present in some and absent in others counts), or None
    when none or several do."""
    every = [_values(call) for call in calls]
    slots = dict.fromkeys(slot for values in every for slot in values)
    varying = [slot for slot in slots if len({_text(values.get(slot)) for values in every}) > 1]
    return varying[0] if len(varying) == 1 else None


def _values(call: Call) -> dict[_Slot, Scalar]:
    """The request's query values by decoded name (RFC 3986 §2.1; a repeated name keeps its last value) and its JSON
    body's scalar values by normalized path."""
    found: dict[_Slot, Scalar] = {}
    query = urlsplit(call.entry.url).query
    for item in query.split("&") if query else ():
        name, _, value = item.partition("=")
        found["query", unquote(name.replace("+", " "))] = unquote(value.replace("+", " "))
    body = call.entry.request_body
    if body is not None:
        try:
            document = json_text.parse(body)
        except ValueError:
            return found
        found.update((("body", path), leaf) for path, leaf in json_doc.scalars(document))
    return found


def _text(value: Scalar | None) -> str | None:
    """A request or response value as text: a string as is, a number as its JSON text (RFC 8259 §6)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _integer(value: Scalar | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        return int(value)
    return None


# --- the next request -------------------------------------------------------------------------------------------------


def first_request(pagination: Pagination, request: JsonRequest) -> JsonRequest | None:
    """The listing's first request: a `param` pagination's value set to `start`, the others as bound. None when the
    body holds no value at the pagination's path."""
    if isinstance(pagination, ParamPagination):
        return _with_value(request, pagination.where, pagination.name, pagination.start)
    return request


def next_request(
    pagination: Pagination, request: JsonRequest, number: int, document: JsonValue, headers: HeaderList
) -> JsonRequest | None:
    """The request of page `number` (0 for the first) after `request`, whose response was `document` with the
    response `headers`; None when the response names no next page, or names one on another origin."""
    match pagination:
        case ParamPagination():
            return _with_value(request, pagination.where, pagination.name, pagination.start + number * pagination.step)
        case CursorPagination():
            cursor = _one_scalar(pagination.cursor, document)
            if cursor is None:
                return None
            return _with_value(request, pagination.where, pagination.name, cursor)
        case NextUrlPagination():
            target = _one_scalar(pagination.next, document)
            return None if not isinstance(target, str) else _same_origin(request, urljoin(request.url, target))
        case LinkPagination():
            target = next_link(headers, request.url)
            return None if target is None else _same_origin(request, target)
        case OnePagePagination():
            return None


def next_link(headers: HeaderList, base: str) -> str | None:
    """The target of the first link with relation type `next` in the `Link` fields of `headers` (RFC 8288 §3; the
    fields combine in order, RFC 9110 §5.3), resolved against `base` (RFC 3986 §5); None when there is none."""
    for name, value in headers:
        if name.lower() != "link":
            continue
        for found in _LINK_VALUE.finditer(value):
            if _NEXT in _relations(found[2]):
                return urljoin(base, str(found[1]).strip())
    return None


def _relations(params: str) -> list[str]:
    for found in _LINK_PARAM.finditer(params):
        if found[1].lower() == "rel":  # RFC 8288 §3: only the first `rel` parameter counts
            return [relation.lower() for relation in (found[2] or found[3] or "").split()]
    return []


def _one_scalar(path: str, document: JsonValue) -> Scalar | None:
    """The one non-empty scalar `path` selects in `document`; None otherwise (absent, null, empty or several)."""
    nodes = json_doc.select(path, document)
    if len(nodes) != 1 or not json_doc.is_scalar(value := nodes[0][1]) or value == "":
        return None
    return value


def _same_origin(request: JsonRequest, url: str) -> JsonRequest | None:
    """`request` sent to `url`, when it is on the request's origin (RFC 6454 §4: scheme, host and port): a recipe's
    scheme and host are fixed (`model`), so a response never sends it elsewhere."""
    old, new = urlsplit(request.url), urlsplit(url)
    if (old.scheme, old.netloc) != (new.scheme, new.netloc):
        return None
    return replace(request, url=new._replace(fragment="").geturl())


def _with_value(request: JsonRequest, where: PageWhere, name: str, value: Scalar) -> JsonRequest | None:
    if where == "query":
        return replace(request, url=with_query_value(request.url, name, _text(value) or ""))
    if request.body is None:
        return None
    body = with_body_value(request.body, name, value)
    return None if body is None else replace(request, body=body)


def with_body_value(body: str, path: str, value: Scalar) -> str | None:
    """`body` (JSON text) with the value at `path` (a normalized path) replaced by `value`, written as a JSON string
    where the old value was one and as the value's own JSON type otherwise; a missing last member is added. None when
    `body` is not JSON or `path` does not reach an object or array there."""
    try:
        document = json_text.parse(body)
    except ValueError:
        return None
    segments = json_doc.segments(path)
    if not segments:
        return None
    parent = _node(document, segments[:-1])
    key = _key(segments[-1])
    if isinstance(parent, dict) and isinstance(key, str):
        parent[key] = _typed(parent.get(key), value)
    elif isinstance(parent, list) and isinstance(key, int) and key < len(parent):
        parent[key] = _typed(parent[key], value)
    else:
        return None
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _typed(old: JsonValue, value: Scalar) -> Scalar:
    if isinstance(old, str):
        return _text(value) or ""
    if isinstance(value, str) and _INTEGER.fullmatch(value) and isinstance(old, int):
        return int(value)
    return value


def _node(document: JsonValue, segments: Sequence[str]) -> JsonValue:
    node: JsonValue = document
    for segment in segments:
        key = _key(segment)
        if isinstance(node, dict) and isinstance(key, str):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and key < len(node):
            node = node[key]
        else:
            return None
    return node


def _key(segment: str) -> str | int | None:
    """A segment of a normalized path as a member name or an index; None for a wildcard."""
    if segment.startswith("['"):
        return segment[2:-2]
    inner = segment[1:-1]
    return None if inner == "*" else int(inner)
