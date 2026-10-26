"""The engine executor: runs an extraction config IR over a `ParsedPage` and
proposes `source_kind='config'` candidates (`snowcrawl.collect.config`).

For each field the steps run in order, and the first value that passes its post ops and the field's normalizer is
the candidate, with the locator `config:<field>:<step>:<selector>`. When no step gives a valid value, the first value
that matched is still proposed with its decline, so resolution and drift see a match that is not valid. Steps read
the page's parsed trees and script blocks: `xpath`, `css` and `attr` the lxml tree, `regex` the body text,
`jsonld` the schema.org nodes of the JSON-LD blocks, `jsonpath` the JSON documents of its source. Selectors are data
interpreted by lxml and our JSONPath reader, never code.
"""

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urljoin

from lxml import etree
from lxml.html import HtmlElement
from pydantic import JsonValue, ValidationError

from snowcrawl.collect import js_state, jsonld, xhr
from snowcrawl.collect.base import PAGE, Candidate, Entity
from snowcrawl.collect.config import ROW_KIND, Executed, Refusal
from snowcrawl.collect.json_doc import select
from snowcrawl.collect.locator import format_locator
from snowcrawl.collect.network_match import LearnedCalls
from snowcrawl.collect.schema_org_map import fragment_read
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.config_ir.model import (
    NEXT_PAGE_FIELD,
    AbsolutizeUrlOp,
    AttrStep,
    CssStep,
    ExtractionConfigIR,
    FieldSteps,
    JsonLdStep,
    JsonPathStep,
    JsonSource,
    ListingSpec,
    NormalizeOp,
    PostOp,
    RegexReplaceOp,
    RegexStep,
    Step,
    TrimOp,
    XPathStep,
    selector_key,
)
from snowcrawl.config_ir.portable_regex import PCRE_FLAGS
from snowcrawl.content.page import ParsedPage
from snowcrawl.db.models import FieldStatus
from snowcrawl.normalize.base import Context, Invalid, NormalizeResult, Ok, Raw
from snowcrawl.normalize.dispatch import normalize
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef


def row_id(n: int) -> str:
    """The entity id of a listing's `n`-th row (document order)."""
    return f"config:row:{n}"


class ConfigMismatch(ValueError):
    """The config was learned for another schema or page kind than the page is extracted with."""


class _Refused(Exception):
    """A step the engine cannot run here; its message becomes a refusal and the next step runs."""


@dataclass(frozen=True, slots=True)
class _Scope:
    """Where a step runs: the page's document element, or one listing row: an element, or for a row a `jsonpath` row
    step selects, the JSON value (`item`), which its fields' `jsonpath` steps read from (`$` is the row)."""

    node: HtmlElement
    in_row: bool
    item: JsonValue = None
    json_row: bool = False


@dataclass(frozen=True, slots=True)
class EngineExecutor:
    """A `ConfigExecutor` for one IR. `vocabulary` is the JSON-LD collector's, so `when_type` matches a node exactly
    when that collector would type it so (subtypes included)."""

    ir: ExtractionConfigIR
    vocabulary: Vocabulary

    @property
    def field_status(self) -> Mapping[str, FieldStatus]:
        return {name: status for name, status in self.ir.field_status().items() if status != "suspended"}

    @property
    def browser_fields(self) -> frozenset[str]:
        # The running fields whose located source is the browser's render (`FieldSteps.needs`).
        listed = self.ir.listing.fields if self.ir.listing is not None else {}
        return frozenset(
            name
            for name, steps in (*self.ir.fields.items(), *listed.items())
            if steps.needs == "browser" and steps.status != "suspended"
        )

    @property
    def learned_calls(self) -> LearnedCalls | None:
        return None if self.ir.api_recipe is None else self.ir.api_recipe.learned_calls

    def execute(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> Executed:
        self._check(schema)
        run = _Run(page, schema, ctx, self.vocabulary)
        document = _Scope(page.lxml_tree, in_row=False)
        candidates = list(run.fields(self.ir.fields, document, PAGE))
        rows: list[Entity] = []
        if self.ir.listing is not None:
            rows, listed = run.listing(self.ir.listing, document)
            candidates.extend(listed)
        return Executed(rows=tuple(rows), candidates=tuple(candidates), refusals=tuple(run.refusals))

    def _check(self, schema: SchemaDef) -> None:
        # Configs are per schema: a config never runs against another one.
        ref = self.ir.schema_
        if (ref.name, self.ir.page_kind) != (schema.name, schema.page_kind):
            raise ConfigMismatch(
                f"config for {ref.name} ({self.ir.page_kind}) run with {schema.name} ({schema.page_kind})"
            )


@dataclass
class _Run:
    page: ParsedPage
    schema: SchemaDef
    ctx: Context
    vocabulary: Vocabulary
    refusals: list[Refusal] = field(default_factory=list)
    # Each JSON source's documents and the JSON-LD nodes, read once per page however many steps read them.
    _documents: dict[JsonSource, list[JsonValue]] = field(default_factory=dict)
    _nodes: list[tuple[tuple[str, ...], JsonValue]] | None = None

    def documents(self, source: JsonSource) -> list[JsonValue]:
        if source not in self._documents:
            self._documents[source] = documents(source, self.page)
        return self._documents[source]

    def nodes(self) -> list[tuple[tuple[str, ...], JsonValue]]:
        if self._nodes is None:
            found = jsonld.JsonLdCollector(self.vocabulary).nodes(self.page)
            self._nodes = [(types, dict(data)) for _, types, data in found]
        return self._nodes

    def fields(self, fields: Mapping[str, FieldSteps], scope: _Scope, owner: str) -> Iterator[Candidate]:
        for name, steps in fields.items():
            spec = self.schema.fields.get(name)
            if spec is None:
                self.refusals.append(
                    Refusal(kind="field", source="config", field=name, reason="not a field of the schema")
                )
            # §8.3: a suspended field falls back to cold-mode resolution
            elif steps.status != "suspended" and (found := self.first_value(name, steps.steps, spec, scope, owner)):
                yield found

    def first_value(
        self, name: str, steps: Sequence[Step], spec: FieldSpec, scope: _Scope, owner: str
    ) -> Candidate | None:
        """The first valid value of `steps`, else the first value that matched, else none."""
        declined: Candidate | None = None
        for index, step in enumerate(steps):
            try:
                values = _matches(step, scope, self)
            except _Refused as refused:
                self.refusals.append(Refusal(kind="step", source="config", field=name, step=index, reason=str(refused)))
                continue
            if not values:
                continue
            if isinstance(step, (JsonLdStep, JsonPathStep)):
                values = [_structured(value, spec) for value in values]
            found = self.candidate(name, index, step, values, spec, owner)
            if isinstance(found.normalized, Ok):
                return found
            declined = declined or found
        return declined

    def candidate(self, name: str, index: int, step: Step, values: list[Raw], spec: FieldSpec, owner: str) -> Candidate:
        raw, normalized = self.value(step, values, spec)
        return Candidate(
            field=name,
            entity_id=owner,
            source_kind="config",
            raw=raw,
            normalized=normalized,
            locator=format_locator("config", name, str(index), _selector(step)),
            # §8.1, §10: a config is learned from the page's other sources, and a `jsonld` step reads the very node the
            # JSON-LD collector reads, so a config value never counts toward `agreed`.
            independent=False,
        )

    def value(self, step: Step, values: list[Raw], spec: FieldSpec) -> tuple[Raw, NormalizeResult]:
        """The raw value after the step's post ops, and the field's normalizer's result on it. A list field takes every
        match, each through the post ops; any other field takes exactly one match (§4.3: `max_matches` 1)."""
        if spec.item is not None:  # a list field (FieldSpec: only a list has an item)
            items = [_post(value, step.post, spec.item, self.ctx) for value in values]
            failed = next((item for item in items if isinstance(item, Invalid)), None)
            if failed is not None:
                return values, failed
            processed = cast(list[Raw], items)
            return processed, normalize(processed, spec, self.ctx)
        if len(values) > 1:
            return values, Invalid("multi_match")
        one = _post(values[0], step.post, spec, self.ctx)
        if isinstance(one, Invalid):
            return values[0], one
        return one, normalize(one, spec, self.ctx)

    def listing(self, listing: ListingSpec, document: _Scope) -> tuple[list[Entity], list[Candidate]]:
        """One entity per row that `row` selects, the row fields evaluated relative to it, and the page's next-page
        candidate from `next`."""
        rows: list[Entity] = []
        candidates: list[Candidate] = []
        for n, scope in enumerate(self.rows(listing)):
            owner = row_id(n)
            rows.append(Entity(id=owner, types=(ROW_KIND,), urls=(), parent=None))
            candidates.extend(self.fields(listing.fields, scope, owner))
        next_spec = self.schema.fields.get(NEXT_PAGE_FIELD)
        if listing.next is not None and next_spec is not None:
            found = self.first_value(NEXT_PAGE_FIELD, [listing.next], next_spec, document, PAGE)
            if found is not None:
                candidates.append(found)
        return rows, candidates

    def rows(self, listing: ListingSpec) -> list[_Scope]:
        step = listing.row
        if isinstance(step, JsonPathStep):
            return self.json_rows(step)
        if not isinstance(step, (XPathStep, CssStep)):
            self.refuse_row("a row step selects elements or JSON objects: xpath, css or jsonpath")
            return []
        try:
            found = _evaluate(step.xpath, self.page.lxml_tree)
        except _Refused as refused:
            self.refuse_row(str(refused))
            return []
        if not isinstance(found, list):
            return []
        return [_Scope(node, in_row=True) for node in found if isinstance(node, HtmlElement)]

    def refuse_row(self, reason: str) -> None:
        self.refusals.append(Refusal(kind="row", source="config", reason=reason))

    def json_rows(self, step: JsonPathStep) -> list[_Scope]:
        """One row per JSON object the step selects in its source's documents."""
        try:
            found = [value for document in self.documents(step.source) for _, value in select(step.jsonpath, document)]
        except (ValueError, _Refused) as error:
            self.refuse_row(str(error))
            return []
        root = self.page.lxml_tree
        return [_Scope(root, in_row=True, item=item, json_row=True) for item in found if isinstance(item, dict)]


def _matches(step: Step, scope: _Scope, run: _Run) -> list[Raw]:
    """The values `step` selects in `scope`, in document order."""
    match step:
        case _ if scope.json_row and not isinstance(step, JsonPathStep):
            raise _Refused("a JSON row is read by jsonpath steps only")
        case JsonPathStep() if scope.json_row:
            return _select_all(step.jsonpath, [scope.item])
        case XPathStep() | CssStep():
            return _xpath_values(step.xpath, scope.node)
        case AttrStep():
            value = scope.node.get(step.attr)
            return [] if value is None else [str(value)]
        case RegexStep():
            _page_only(scope)
            return _regex_values(step.regex, run.page.html)
        case JsonLdStep():
            _page_only(scope)
            return _jsonld_values(step, run)
        case JsonPathStep():
            _page_only(scope)
            return _select_all(step.jsonpath, run.documents(step.source))


def _structured(value: Raw, spec: FieldSpec) -> Raw:
    """A structured-data value as the structured collectors read it for the field (`fragment_read`: a text field reads
    a value holding element markup as its rendered text), so a step on a JSON document gives the value the engine
    published from it."""
    read = fragment_read(value, spec)
    return value if read is None else read


def _page_only(scope: _Scope) -> None:
    # §4.3: `regex` reads the body text and `jsonld`/`jsonpath` the page's documents; none has a row to be relative to.
    if scope.in_row:
        raise _Refused("not relative to a row: regex, jsonld and jsonpath steps read the whole page")


# XPath 1.0 §1: an expression evaluates to a node-set (lxml: a list), a boolean, a number or a string.
_XPathResult = list[object] | bool | float | str


def _evaluate(xpath: str, node: HtmlElement) -> _XPathResult:
    try:
        result: _XPathResult = node.xpath(xpath)  # lxml's stubs type it `Any`
    except etree.XPathError as error:  # lxml compiles at run time (an unbound variable, a wrong argument count)
        raise _Refused(f"invalid_xpath: {error}") from None
    return result


def _xpath_values(xpath: str, node: HtmlElement) -> list[Raw]:
    """XPath 1.0 §1: a result is a node-set, a string, a number or a boolean. A node's value is its string-value;
    an empty string or NaN is no match; a boolean is no value of any field type."""
    result = _evaluate(xpath, node)
    if isinstance(result, bool):
        raise _Refused("a boolean XPath result is not a value")
    if isinstance(result, float):
        return [] if math.isnan(result) else [result]
    if isinstance(result, str):
        return [str(result)] if result else []
    if not isinstance(result, list):
        raise _Refused(f"unexpected XPath result {type(result).__name__}")
    return [_string_value(item) for item in result]


# XPath 1.0 §4.2: `string()` is the context node's string-value.
_STRING_VALUE = etree.XPath("string()")


def _string_value(item: object) -> str:
    if isinstance(item, etree._Element):
        return str(_STRING_VALUE(item))
    return str(item)  # an attribute or text node, which lxml returns as its string


def _regex_values(pattern: str, text: str) -> list[Raw]:
    """Each match's first group, or the whole match when the pattern has no group; the IR's regexes are compiled with
    the flags both Python and PCRE give them (`portable_regex`)."""
    compiled = re.compile(pattern, PCRE_FLAGS)
    group = 1 if compiled.groups else 0
    return [found for m in compiled.finditer(text) if (found := m.group(group)) is not None]


def _jsonld_values(step: JsonLdStep, run: _Run) -> list[Raw]:
    when = step.when_type
    nodes = [data for types, data in run.nodes() if when is None or any(run.vocabulary.is_a(t, when) for t in types)]
    return _select_all(step.jsonld, nodes)


def documents(source: JsonSource, page: ParsedPage) -> list[JsonValue]:
    """The page's JSON documents of `source`, in page order."""
    # Unreadable documents are skipped here; the jsonld, js_state and xhr collectors report them for the page.
    match source:
        case "jsonld":
            return [document for _, document in jsonld.blocks(page, [])]
        case "js_state":
            return [document.data for document in js_state.script_documents(page, []) if document.scheme is None]
        case "rsc":
            flight = js_state.flight_document(page, [])
            return [] if flight is None or not isinstance(flight.data, dict) else list(flight.data.values())
        case "attr":
            return [document.data for document in js_state.attribute_documents(page)]
        case "xhr":
            return [document.data for document in xhr.network_documents(page, [])]
        case "api":
            raise _Refused("an api source is read through the api recipe executor")


def _select_all(query: str, documents: Sequence[JsonValue]) -> list[Raw]:
    try:
        return [value for document in documents for _, value in select(query, document)]
    except ValueError as error:  # outside the RFC 9535 subset the engine reads (the `select` of `json_doc`)
        raise _Refused(f"unsupported_jsonpath: {error}") from None


def _post(value: Raw, ops: Sequence[PostOp], spec: FieldSpec, ctx: Context) -> Raw | Invalid:
    """`value` through `ops` in order. A normalizer op checks that the value is valid for its type and passes it on
    unchanged: the field's normalizer makes the candidate's value (C8)."""
    for op in ops:
        result = _apply(op, value, spec, ctx)
        if isinstance(result, Invalid):
            return result
        value = result
    return value


def _apply(op: PostOp, value: Raw, spec: FieldSpec, ctx: Context) -> Raw | Invalid:
    if isinstance(op, NormalizeOp):
        return _check(op.type, value, spec, ctx)
    if not isinstance(value, str):
        return Invalid("post_op_needs_text")
    match op:
        case TrimOp():
            return value.strip()
        case RegexReplaceOp():
            return re.sub(op.regex_replace.pattern, op.regex_replace.repl, value, flags=PCRE_FLAGS)
        case AbsolutizeUrlOp():
            return _absolutize(value, ctx)
    raise AssertionError(f"unknown post op {op!r}")


def _check(type_: FieldType, value: Raw, spec: FieldSpec, ctx: Context) -> Raw | Invalid:
    try:
        checked = spec if spec.type is type_ else FieldSpec(type=type_)
    except ValidationError:
        return Invalid(f"{type_}_needs_parameters")  # an enum or identifier check needs the field's own vocab or scheme
    result = normalize(value, checked, ctx)
    return value if isinstance(result, Ok) else Invalid(f"{type_}: {result.reason}")


def _absolutize(value: str, ctx: Context) -> Raw | Invalid:
    # RFC 3986 §5: a reference resolves against the document base URL; without one it stays unresolved (C7).
    if ctx.base_url is None:
        return Invalid("no_base_url")
    try:
        return urljoin(ctx.base_url, value)
    except ValueError:
        return Invalid("invalid_url")


def _selector(step: Step) -> str:
    return str(getattr(step, selector_key(step) or ""))
