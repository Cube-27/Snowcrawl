"""The call list of engineer captures (`services.captures.capture`): the calls their pages made, grouped into endpoints,
each with where it carries the schema's fields, which an engineer chooses a custom recipe's calls from. Deterministic:
no LLM is asked.

Calls group by endpoint as API discovery groups them (`network_match.endpoint_key`: request method, URL shape, request
body shape), across the samples given. Each group lists its calls, and per schema field the map paths that read the
value the sample published, compared by the field type's normalizer (`api_recipe.discovery.body_readings`), with how
many samples one of its calls read it on; a field no sample published lists the paths whose value normalizes as its
type instead. A listing row's values are matched at any item of an array, so its paths are written with `[*]`
(RFC 9535 §2.5.1.2). An HTML answer has no paths: the engineer writes its XPath, so the list says on how many samples
the published value's text occurs in it. A group whose calls carry the rows a listing shows proposes their row query
(`data_calls` in `collect/network_match.py`).
"""

import asyncio
import html
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from snowcrawl.collect import json_doc
from snowcrawl.collect.network_match import SUCCESS, EndpointKey, data_calls, endpoint_key
from snowcrawl.config_ir.api_recipe.discovery import Normalized, body_readings, field_hits
from snowcrawl.config_ir.held_out import load_captures, read_bundle
from snowcrawl.config_ir.validation import Facts, HeldOutPage
from snowcrawl.content.page import answer_text
from snowcrawl.core.errors import Refusal
from snowcrawl.db.models import Capture, CaptureId, Extraction, Job, Record, SchemaId, TaskRecord, UrlTask
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.captures import CaptureNotFound, bundle_of
from snowcrawl.storage.base import ObjectStore
from snowcrawl.storage.capture_bundle import NetworkBody, is_html, network_json

__all__ = [
    "INDEX",
    "MAX_SAMPLES",
    "CallList",
    "CallSummary",
    "Endpoint",
    "FieldMatch",
    "NoSamples",
    "PathCount",
    "SampleIds",
    "Samples",
    "call_list",
    "listed",
    "load_samples",
]

MAX_SAMPLES: Final = 5
# The paths of a type a field no sample published lists at most, in document order: a list for an engineer to read,
# where a text field's type takes any string.
_TYPED_PATHS_MAX: Final = 20
# RFC 9535 §2.5.1.2: an array index selector, which a row's paths write as the wildcard `[*]`.
INDEX: Final = re.compile(r"\[\d+\]")
_WHITESPACE: Final = re.compile(r"\s+")


class NoSamples(Refusal, LookupError):
    """A capture was read for no schema to match its calls with, or the captures were read for different schemas."""


class CallSummary(BaseModel):
    """One call of a sample: its index among the capture's network bodies (`GET /v1/captures/{id}/calls/{n}`), its
    answer's status, content type and size, the request's Fetch destination, the page step it followed (0: the page's
    load), and whether it went to the page's own site."""

    capture_id: CaptureId
    index: int
    status: int
    content_type: str | None
    resource_type: str | None
    step: int | None
    same_site: bool | None
    size: int


class PathCount(BaseModel):
    """A map path, and on how many of the samples a call of the endpoint read it."""

    path: str
    samples: int


class FieldMatch(BaseModel):
    """Where an endpoint carries one schema field: `published` paths read the value a sample published, else `paths`
    read a value of the field's type; `in_text` counts the samples whose HTML answer holds the published value's
    text."""

    field: str
    published: bool
    paths: list[PathCount]
    in_text: int


class Endpoint(BaseModel):
    """One endpoint group: its request method, URL shape (origin, whether there is a path, its segment count, its last
    segment's format suffix such as `.json`, "" for none, and the query names in order) and request body shape, the
    samples it was called on, its calls, the fields it carries and the row query of the rows it lists."""

    method: str | None
    origin: str
    has_path: bool
    segments: int
    suffix: str
    query: list[str]
    body_shape: list[str] | None
    samples: list[CaptureId]
    calls: list[CallSummary]
    fields: list[FieldMatch]
    rows: str | None


class CallList(BaseModel):
    """The calls of the samples, by endpoint in the order they were first called (the samples' order, then each
    capture's: its own site's calls first)."""

    schema_id: SchemaId
    samples: list[CaptureId]
    endpoints: list[Endpoint]


@dataclass
class _Group:
    calls: list[CallSummary] = field(default_factory=list)
    samples: dict[CaptureId, None] = field(default_factory=dict)
    published: dict[str, dict[str, set[int]]] = field(default_factory=dict)  # field -> path -> sample numbers
    typed: dict[str, dict[str, set[int]]] = field(default_factory=dict)
    in_text: dict[str, set[int]] = field(default_factory=dict)
    rows: str | None = None


def _distinct(capture_ids: list[CaptureId]) -> list[CaptureId]:
    if len(set(capture_ids)) != len(capture_ids):
        raise ValueError("a capture is named twice")
    return capture_ids


# The sample captures a custom recipe is built from: two to five, all distinct.
SampleIds = Annotated[list[CaptureId], Field(min_length=2, max_length=MAX_SAMPLES), AfterValidator(_distinct)]


@dataclass(frozen=True)
class Samples:
    """Sample captures as a call list reads them: the schema matched, each capture as a held-out page with the values
    of its latest record of the schema, and the URL each was requested at, in the order named."""

    schema: SchemaDef
    pages: list[HeldOutPage]
    urls: list[str]


async def call_list(engine: AsyncEngine, store: ObjectStore, captures: Sequence[CaptureId]) -> CallList:
    """The call list of `captures`, matched against their schema (`_schemas`) and the values of their latest records:
    a capture the engine read no record from has none published, so each field lists the paths of its type. Raises
    `CaptureNotFound` for an id no capture has, or a capture whose bundle is no longer stored, and `NoSamples` when one
    has no schema, or theirs differ."""
    samples = await load_samples(engine, store, captures)
    # Matching every leaf of every body is CPU work: a thread keeps the event loop serving meanwhile.
    return await asyncio.to_thread(listed, samples.pages, samples.schema)


async def load_samples(
    engine: AsyncEngine, store: ObjectStore, captures: Sequence[CaptureId], schema_id: SchemaId | None = None
) -> Samples:
    """The `captures` matched against `schema_id`, else against their own schema (`_schemas`); raises as `call_list`
    does."""
    async with engine.connect() as conn:
        query = select(Capture.id, Capture.url).where(Capture.id.in_(captures))
        urls = {row.id: row.url for row in await conn.execute(query)}
        if unknown := [str(capture) for capture in captures if capture not in urls]:
            raise CaptureNotFound(f"no capture {', '.join(unknown)}")
        if schema_id is None:
            schema_id = _one_schema(captures, await _schemas(conn, captures))
        schema = await registry.get_by_id(conn, schema_id)
        recorded = {page.capture_id: page for page in await load_captures(conn, store, schema, list(captures))}
    unrecorded = [capture for capture in captures if capture not in recorded]
    read = await asyncio.gather(*(_unrecorded(engine, store, capture) for capture in unrecorded))
    pages = {**recorded, **dict(zip(unrecorded, read, strict=True))}
    return Samples(schema, [pages[capture] for capture in captures], [urls[capture] for capture in captures])


async def _schemas(conn: AsyncConnection, captures: Sequence[CaptureId]) -> dict[CaptureId, SchemaId]:
    """Each capture's schema: that of a record its latest extraction stood for, else that of the job whose page it is
    (an engineer capture of a page the engine read no record from)."""
    recorded = (
        select(Extraction.capture_id, Record.schema_id)
        .join(TaskRecord, TaskRecord.extraction_id == Extraction.id)
        .join(Record, Record.id == TaskRecord.record_id)
        .where(Extraction.capture_id.in_(captures))
        .order_by(Extraction.capture_id, Extraction.id.desc())  # ids are UUIDv7: the latest extraction first
    )
    asked = (
        select(UrlTask.capture_id, Job.schema_id)
        .join(Job, Job.id == UrlTask.job_id)
        .where(UrlTask.capture_id.in_(captures), Job.schema_id.is_not(None))
    )
    schemas: dict[CaptureId, SchemaId] = {}
    for query in (recorded, asked):
        for capture_id, schema_id in await conn.execute(query):
            if capture_id is not None and schema_id is not None:  # the `in_` and `is_not` above hold none
                schemas.setdefault(capture_id, schema_id)
    return schemas


async def _unrecorded(engine: AsyncEngine, store: ObjectStore, capture_id: CaptureId) -> HeldOutPage:
    """The capture as a sample with no value published."""
    bundle = await bundle_of(engine, store, capture_id)
    page, ctx = await asyncio.to_thread(read_bundle, bundle)
    return HeldOutPage(capture_id, page, ctx, Facts())


def _one_schema(captures: Sequence[CaptureId], schemas: Mapping[CaptureId, SchemaId]) -> SchemaId:
    missing = [str(capture) for capture in captures if capture not in schemas]
    if missing:
        raise NoSamples(f"capture {', '.join(missing)} was read for no schema, so none says what to match")
    if len(set(schemas.values())) > 1:
        raise NoSamples("the captures were read for different schemas")
    return schemas[captures[0]]


def listed(pages: Sequence[HeldOutPage], schema: SchemaDef) -> CallList:
    """The call list of the sample `pages` (module docstring)."""
    groups: dict[EndpointKey, _Group] = {}
    for number, page in enumerate(pages):
        rows = {data.call.index: data.rows for data in data_calls(page.page) if data.rows is not None}
        for index, entry, data in _calls(page):
            key = endpoint_key(entry.method, entry.url, entry.request_body, entry.request_headers)
            group = groups.setdefault(key, _Group())
            group.calls.append(_summary(page.capture_id, index, entry, data))
            group.samples[page.capture_id] = None
            group.rows = group.rows or rows.get(index)
            _match(group, number, entry, data, page, schema)
    endpoints = [_endpoint(key, group, schema) for key, group in groups.items()]
    return CallList(schema_id=schema.id, samples=[page.capture_id for page in pages], endpoints=endpoints)


def _calls(page: HeldOutPage) -> Iterator[tuple[int, NetworkBody, bytes]]:
    entries, bodies = page.page.network_entries, page.page.network
    return ((index, entry, data) for index, (entry, data) in enumerate(zip(entries, bodies, strict=True)))


def _summary(capture_id: CaptureId, index: int, entry: NetworkBody, data: bytes) -> CallSummary:
    return CallSummary(
        capture_id=capture_id,
        index=index,
        status=entry.status,
        content_type=entry.content_type,
        resource_type=entry.resource_type,
        step=entry.action,
        same_site=entry.same_site,
        size=len(data),
    )


def _match(group: _Group, number: int, entry: NetworkBody, data: bytes, page: HeldOutPage, schema: SchemaDef) -> None:
    """Add what the call `data` of sample `number` carries of each field to `group`."""
    if entry.status not in SUCCESS:
        return
    if is_html(entry.content_type):
        for name in _in_text(answer_text(entry, data), page.facts.published):
            group.in_text.setdefault(name, set()).add(number)
        return
    try:
        document = network_json(data)
    except ValueError:
        return
    published = page.facts.published
    typed, hits = body_readings(document, published, page.ctx, schema)
    for row in page.rows:
        for name, paths in field_hits(typed, document, row.published, page.ctx, schema).items():
            hits.setdefault(name, {}).update(dict.fromkeys(INDEX.sub("[*]", path) for path in paths))
    _add(group.published, hits, number)
    stated = hits.keys() | published.keys() | {name for row in page.rows for name in row.published}
    _add(group.typed, _typed_paths(typed, stated), number)


def _typed_paths(typed: Mapping[str, Normalized], stated: set[str]) -> dict[str, dict[str, None]]:
    """The paths of each field no page published whose value normalizes as its type, in document order, an array's
    items as one (`[*]`)."""
    return {
        name: dict.fromkeys(INDEX.sub("[*]", reading.path) for reading, _ in readings)
        for name, readings in typed.items()
        if name not in stated and readings
    }


def _add(counts: dict[str, dict[str, set[int]]], hits: Mapping[str, Mapping[str, None]], number: int) -> None:
    for name, paths in hits.items():
        for path in paths:
            counts.setdefault(name, {}).setdefault(path, set()).add(number)


def _in_text(answer: str, published: Mapping[str, JsonValue]) -> Iterator[str]:
    """The fields whose published value's text occurs in the HTML `answer` (`content.page.answer_text`):
    each of its strings and numbers, as written, in the answer with its character references resolved (HTML Standard
    §13.2.5.72) and its whitespace collapsed."""
    text = _collapsed(html.unescape(answer))
    for name, value in published.items():
        leaves = [_collapsed(str(leaf)) for _, leaf in json_doc.scalars(value)]
        if leaves and all(leaf and leaf in text for leaf in leaves):
            yield name


def _collapsed(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _endpoint(key: EndpointKey, group: _Group, schema: SchemaDef) -> Endpoint:
    origin, has_path, segments, suffix, query = key.url
    return Endpoint(
        method=key.method,
        origin=origin,
        has_path=has_path,
        segments=segments,
        suffix=suffix,
        query=list(query),
        body_shape=None if key.body is None else list(key.body),
        samples=list(group.samples),
        calls=group.calls,
        fields=[match for match in (_field(name, group) for name in schema.fields) if match is not None],
        rows=group.rows,
    )


def _field(name: str, group: _Group) -> FieldMatch | None:
    """Where the group carries field `name`; None when it carries it nowhere."""
    published = group.published.get(name, {})
    typed = group.typed.get(name, {}) if not published else {}
    in_text = len(group.in_text.get(name, ()))
    if not published and not typed and not in_text:
        return None
    # The paths read on the most samples first; ties keep the order they were first read in.
    counted = sorted(
        (PathCount(path=path, samples=len(numbers)) for path, numbers in (published or typed).items()), key=_most
    )
    paths = counted if published else counted[:_TYPED_PATHS_MAX]
    return FieldMatch(field=name, published=bool(published), paths=paths, in_text=in_text)


def _most(count: PathCount) -> int:
    return -count.samples
