"""The API recipe collector: the JSON an API recipe fetched, read through the
recipe's `map`.

An `api` capture's body is the JSON the recipe's endpoint answered for the page (`FetchResult(method='api')`). Each map
entry is a query into that JSON (the `select` subset of `json_doc`), or for atomic money queries joined with `+`;
an entry reads a value only where each of its queries selects exactly one scalar. Values are `api` candidates
located as `api:<recipe id>:<normalized path>`; nothing here interprets them, the field's normalizer is the only
filter (C7).

The recipe lives in the config IR, which sits above `collect` (`.importlinter`), so the caller binds this collector to
the recipe's map and rows, as it binds the config executor (`collect.config`), and the result has the same shape: a
detail page's values belong to the page; a listing's row-scoped values belong to the rows its `rows` query selects,
read with paths relative to each row, and its page-scoped values to the page.

A custom recipe's call may answer an HTML document instead (`answer` `html`): each map entry is then an XPath 1.0
expression over the answer's parse (`content.page`), which reads a value only where it gives exactly one non-empty
string or number, located as `api:<recipe id>:<expression>`.

The row reader is shared: the `xhr` collector reads a listing's rows from a captured call with a map it proved on the
page (`collect.xhr`), so its rows are read exactly as a recipe's are (C8).
"""

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from lxml import etree
from pydantic import JsonValue

from snowcrawl.collect import json_doc
from snowcrawl.collect.base import ARRAY_ROW, PAGE, Candidate, Entity
from snowcrawl.collect.config import ROW_KIND, Executed, Refusal
from snowcrawl.collect.locator import format_locator
from snowcrawl.collect.schema_org_map import candidate, fragment_read
from snowcrawl.content.page import ParsedPage
from snowcrawl.core import json_text
from snowcrawl.normalize.base import Context
from snowcrawl.schemas.types import FieldSpec, SchemaDef

Scalar = str | int | float

# The source kinds whose JSON is read through a map: an API recipe's capture and a captured network call.
MappedKind = Literal["api", "xhr"]
# The type of a row each reads: a recipe's rows are a list's items (schema.org ListItem, as a config's are); a call's
# are the object items of one of its arrays (`collect.base.ARRAY_ROW`), whose being the page's rows is resolution's
# question, asked only where structured data and the config give none.
_ROW_TYPES: Final[dict[MappedKind, str]] = {"api": ROW_KIND, "xhr": ARRAY_ROW}

# §4.2: a money field joins the amount and currency paths with `+`; each part starts at the root `$` (RFC 9535 §2.2).
_JOIN: Final = re.compile(r"\+(?=\$)")


def map_paths(path: str) -> list[str]:
    """The JSONPaths a map entry joins with `+`, in order."""
    return _JOIN.split(path)


def joined_raw(parts: Sequence[Scalar]) -> str:
    """The raw value of a map entry that joins its parts with `+`: their texts separated by a space, a number as its
    JSON text (RFC 8259 §6), so the money normalizer reads an amount and a currency as one value."""
    return " ".join(part if isinstance(part, str) else json.dumps(part) for part in parts)


def read_map(path: str, document: JsonValue) -> Scalar | None:
    """The raw value a map entry reads in `document`: the one scalar its path selects, or its joined parts'
    `joined_raw`; `None` when a part selects anything but exactly one scalar."""
    found = _read(path, document, json_doc.ROOT)
    return None if found is None else found[0]


def _read(path: str, document: JsonValue, at: str) -> tuple[Scalar, str] | None:
    """`read_map` of `document`, whose own path is `at`, with the normalized paths of the parts joined by `+`."""
    parts: list[Scalar] = []
    paths: list[str] = []
    for part in map_paths(path):
        nodes = json_doc.select(part, document, at)
        if len(nodes) != 1 or not json_doc.is_scalar(value := nodes[0][1]):
            return None
        parts.append(value)
        paths.append(nodes[0][0])
    return (parts[0] if len(parts) == 1 else joined_raw(parts)), "+".join(paths)


def read_xpath(path: str, page: ParsedPage) -> Scalar | None:
    """The raw value an HTML answer's map entry reads in `page`: the one non-empty string or number its XPath 1.0
    expression gives (a node set of one node gives its string-value, XPath 1.0 §5); `None` otherwise."""
    try:
        found = page.lxml_tree.xpath(path)
    except etree.XPathError:  # not an expression lxml evaluates: it reads nothing, as an absent value
        return None
    if isinstance(found, list):
        if len(found) != 1:
            return None
        found = _STRING_VALUE(found[0]) if isinstance(found[0], etree._Element) else found[0]
    if isinstance(found, bool):
        return None  # a test, not a value
    if isinstance(found, float):
        return None if math.isnan(found) else found  # NaN is no number a page states (XPath 1.0 §3.5)
    text = str(found).strip()
    return text or None


# XPath 1.0 §5: an element's string-value.
_STRING_VALUE: Final = etree.XPath("string()")


@dataclass(frozen=True)
class ApiCollector:
    """The collector of the captures one recipe fetched. `recipe_id` names the recipe in locators (no `:` and no
    whitespace); `map` is its field -> path map, and `rows` its listing's row query (`pagination.rows`), if any;
    `answer` what its answer is (module docstring)."""

    recipe_id: str
    map: Mapping[str, str]
    rows: str | None = None
    answer: Literal["json", "html"] = "json"

    def collect(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> Executed:
        if self.answer == "html":
            return self._html(page, schema, ctx)
        try:
            document = json_text.parse(page.html)
        except ValueError as e:
            refusal = Refusal(kind="document", source=f"api:{self.recipe_id}", reason=f"not JSON ({e})")
            return Executed(rows=(), candidates=(), refusals=(refusal,))
        reading = _Reading("api", self.recipe_id, schema, ctx, recipe=True)
        known = {name: path for name, path in self.map.items() if name in schema.fields}
        refusals = tuple(
            Refusal(kind="field", source=f"api:{self.recipe_id}", field=name, reason="not a field of the schema")
            for name in self.map
            if name not in known
        )
        rows = self.rows if schema.page_kind == "listing" else None
        # On a detail page every field describes the page's one entity (detail fields are row-scoped).
        page_fields = {n: p for n, p in known.items() if rows is None or schema.fields[n].scope == "page"}
        candidates = reading.candidates(page_fields, document, json_doc.ROOT, PAGE)
        if rows is None:
            return Executed(rows=(), candidates=tuple(candidates), refusals=refusals, notes=tuple(reading.notes))
        row_fields = {n: p for n, p in known.items() if n not in page_fields}
        entities, listed = reading.rows(rows, row_fields, document)
        found = (*candidates, *listed)
        return Executed(rows=tuple(entities), candidates=found, refusals=refusals, notes=tuple(reading.notes))

    def _html(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> Executed:
        """An HTML answer's values, every field the page's (an HTML answer has no rows query)."""
        notes: list[str] = []
        found: list[Candidate] = []
        for name, path in self.map.items():
            spec = schema.fields.get(name)
            if spec is None or (raw := read_xpath(path, page)) is None:
                continue
            locator = format_locator("api", self.recipe_id, path)
            if (made := candidate(name, spec, PAGE, "api", raw, locator, ctx, notes)) is not None:
                found.append(made)
        refusals = tuple(
            Refusal(kind="field", source=f"api:{self.recipe_id}", field=name, reason="not a field of the schema")
            for name in self.map
            if name not in schema.fields
        )
        return Executed(rows=(), candidates=tuple(found), refusals=refusals, notes=tuple(notes))


def read_rows(
    kind: MappedKind,
    address: str,
    query: str,
    fields: Mapping[str, str],
    document: JsonValue,
    schema: SchemaDef,
    ctx: Context,
) -> Executed:
    """The rows `query` selects in `document`, each a root entity `<kind>:<address>:<row path>` (`_ROW_TYPES`), with
    the `fields` of the map (paths relative to the row) read from it; values are located as
    `<kind>:<address>:<path>`."""
    reading = _Reading(kind, address, schema, ctx)
    entities, candidates = reading.rows(query, fields, document)
    return Executed(rows=tuple(entities), candidates=tuple(candidates), notes=tuple(reading.notes))


@dataclass
class _Reading:
    """One JSON document read for one schema: an API recipe's capture (`address` the recipe id) or a captured call."""

    kind: MappedKind
    address: str
    schema: SchemaDef
    ctx: Context
    notes: list[str] = field(default_factory=list)
    # A recipe's reading (`ApiCollector`) gives a list field every match of its path and reads a text value holding
    # markup as its rendered text, as the config executor does (`config_ir.executor`); a captured call's does not.
    recipe: bool = False

    def candidates(self, fields: Mapping[str, str], document: JsonValue, at: str, owner: str) -> list[Candidate]:
        found: list[Candidate] = []
        for name, path in fields.items():
            spec = self.schema.fields[name]
            read = self._read(path, spec, document, at)
            if read is None:
                continue  # the value is absent or not one scalar here: no candidate, as for any source
            raw, located = read
            locator = format_locator(self.kind, self.address, located)
            structured = fragment_read(raw, spec) if self.recipe else None
            made = candidate(name, spec, owner, self.kind, raw, locator, self.ctx, self.notes, read=structured)
            if made is not None:
                found.append(made)
        return found

    def _read(self, path: str, spec: FieldSpec, document: JsonValue, at: str) -> tuple[JsonValue, str] | None:
        """What a field's path reads in `document`: for a recipe's list field with one path, the scalars it selects in
        document order (RFC 9535 §2.5.1.2), none being no value; otherwise `_read`."""
        if not (self.recipe and spec.item is not None and len(map_paths(path)) == 1):
            return _read(path, document, at)
        nodes = json_doc.select(path, document, at)
        scalars = [value for _, value in nodes if json_doc.is_scalar(value)]
        return (scalars, nodes[0][0]) if scalars else None  # located at its first item

    def rows(self, query: str, fields: Mapping[str, str], document: JsonValue) -> tuple[list[Entity], list[Candidate]]:
        """The rows `query` selects, each with its `fields` read relative to it."""
        entities: list[Entity] = []
        candidates: list[Candidate] = []
        for path, row in json_doc.select(query, document):
            # A row is a JSON object (RFC 8259 §4); an item of another kind holds no fields.
            if isinstance(row, dict):
                owner = f"{self.kind}:{self.address}:{path}"
                entities.append(Entity(id=owner, types=(_ROW_TYPES[self.kind],), urls=(), parent=None))
                candidates.extend(self.candidates(fields, row, path, owner))
        return entities, candidates
