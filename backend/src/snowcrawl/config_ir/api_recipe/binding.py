"""Parameter binding: read a recipe's parameters from a page, fill its templates with them, and find
the templates of an endpoint request from sample pages deterministically.

Values are compared and passed around percent-decoded (RFC 3986 §2.1) and encoded again where a placeholder stands,
so a value read from the page URL and one read from a JSON document mean the same thing. A binding is accepted only
when its templates reproduce the captured request of every sample page exactly; anything less is `Ambiguous`, the
case role D takes.

A query value that looks like a token (`model.token_like`: an API key) and is the same on every sample is never stored
as a literal: it is bound from the page when a page value holds it on every sample, and left out of the endpoint URL
otherwise (`withhold_tokens`, before binding and role D), so the replay shows whether the endpoint needs it; one it
needs leaves the site without a recipe, as a token header does.
"""

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from itertools import count
from typing import Final, NamedTuple
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit

from pydantic import JsonValue, ValidationError

from snowcrawl.collect import json_doc
from snowcrawl.collect.api import read_xpath
from snowcrawl.collect.network_match import FORM_TYPE, media_type, path_suffix, url_shape
from snowcrawl.config_ir.api_recipe.model import (
    CUSTOM_KINDS,
    PAGE_DATA_KINDS,
    PLACEHOLDER,
    Binder,
    BodyEncoding,
    PageDataKind,
    RequestSpec,
    parse_binder,
    token_like,
)
from snowcrawl.config_ir.executor import documents
from snowcrawl.config_ir.portable_regex import PCRE_FLAGS
from snowcrawl.config_ir.url_pattern import segment_param
from snowcrawl.content.page import ParsedPage
from snowcrawl.core import json_text
from snowcrawl.storage.capture_bundle import HeaderList

# The page's JSON documents a `jsonld:` or `js_state:` binder reads, by kind.
PageData = Mapping[PageDataKind, JsonValue]

# RFC 3986 §3.3: a path segment holds pchar; `quote` keeps the unreserved characters, these are the rest of it.
PATH_SAFE: Final = "!$&'()*+,;=:@"
# RFC 3986 §3.4: a query holds pchar, `/` and `?`; `&`, `=` and `+` are encoded in a value because the
# application/x-www-form-urlencoded reading (WHATWG URL §5) takes them as separators or a space.
QUERY_SAFE: Final = "!$'()*,;:@/?"
_DATA_KINDS: Final[dict[str, PageDataKind]] = {"jsonld": "jsonld", "js_state": "js_state"}


@dataclass(frozen=True)
class CapturedRequest:
    method: str
    url: str
    body: str | None = None
    headers: HeaderList = ()  # as the browser sent them, in order; header minimization replays them


@dataclass(frozen=True)
class Sample:
    """One sample page: its URL, the endpoint request the browser made for it, and its JSON documents."""

    page_url: str
    request: CapturedRequest
    page_data: PageData = field(default_factory=dict)


@dataclass(frozen=True)
class Ambiguous:
    """No deterministic binding reproduces every sample; `reason` is the learning job's diagnostic."""

    reason: str


@dataclass(frozen=True)
class CallAnswer:
    """A call's answer as a custom recipe's binders read it: its text, its JSON document (None when it is not JSON,
    RFC 8259) and its HTML parse (`content.page`; `parsed` when the caller has it already)."""

    text: str
    parsed: ParsedPage | None = None

    @cached_property
    def document(self) -> JsonValue:
        try:
            return json_text.parse(self.text.encode())
        except ValueError:
            return None

    @cached_property
    def page(self) -> ParsedPage:
        return self.parsed if self.parsed is not None else ParsedPage(self.text)


@dataclass(frozen=True)
class Served:
    """What serving a custom recipe fetched before a call, which the recipe's own binders read (`CUSTOM_KINDS`): the
    parameters of the URL pattern the page matched (decoded, `url_pattern.match`), the warm-up's answer and the main
    call's."""

    pattern: Mapping[str, str] = field(default_factory=dict)
    warm_up: CallAnswer | None = None
    main: CallAnswer | None = None

    def value(self, binder: Binder) -> str | None:
        """The value `binder` reads: a JSONPath selecting one scalar of a JSON answer, an XPath 1.0 expression giving
        one value of an HTML answer, or a regex's first group in an answer's text."""
        if binder.kind == "pattern":
            return self.pattern.get(binder.arg)
        answer = self.main if binder.kind == "main" else self.warm_up
        if answer is None:
            return None
        if binder.kind == "warm_up_xpath":
            found = read_xpath(binder.arg, answer.page)
            # XPath 1.0 §4.2 `string()`: an integral number is written without a decimal point.
            if found is None:
                return None
            return _scalar_text(int(found) if _integral(found) else found)
        if binder.kind == "warm_up_regex":
            matched = re.compile(binder.arg, PCRE_FLAGS).search(answer.text)
            return None if matched is None else matched[1]
        return _one_scalar(binder.arg, answer.document)


def _integral(value: JsonValue) -> bool:
    return isinstance(value, float) and value.is_integer()


# --- reading and filling ---------------------------------------------------------------------------------------------


def resolve(binder: Binder, page_url: str, page_data: PageData, served: Served | None = None) -> str | None:
    """The value `binder` reads from the page, decoded; `None` when the page has no non-empty value there. A custom
    recipe's binder reads what serving the recipe fetched (`served`), not the page: `None` without it."""
    if binder.kind in CUSTOM_KINDS:
        return None if served is None else served.value(binder) or None
    kind = _DATA_KINDS.get(binder.kind)
    value = _url_value(binder, _Url.split(page_url)) if kind is None else _data_value(kind, binder.arg, page_data)
    return value or None


def resolve_params(
    spec: RequestSpec, page_url: str, page_data: PageData, served: Served | None = None
) -> dict[str, str] | None:
    """Every parameter of `spec` read from the page (and what serving a custom recipe fetched, `served`); `None` when
    one has no value there."""
    params = _params_or_missing(spec, page_url, page_data, served)
    return params if isinstance(params, dict) else None


def _params_or_missing(
    spec: RequestSpec, page_url: str, page_data: PageData, served: Served | None = None
) -> dict[str, str] | str:
    """Every parameter of `spec` read from the page, or the name of the first one without a value there."""
    params: dict[str, str] = {}
    for name, binder in spec.binders.items():
        value = resolve(binder, page_url, page_data, served)
        if value is None:
            return name
        params[name] = value
    return params


def request_spec(
    method: str, url: str, params: Mapping[str, str], body: str | None, encoding: BodyEncoding = "json"
) -> RequestSpec:
    """The spec with these templates and binders and no headers; it fetches the page first exactly when a binder
    reads the page's JSON documents. Raises `ValidationError` when they are not a valid request."""
    reads_page = any(parse_binder(text).kind in _DATA_KINDS for text in params.values())
    return RequestSpec.model_validate(
        {
            "method": method,
            "url": url,
            "params_from_url": params,
            "body": None if body is None else {"template": body, "encoding": encoding},
            "requires_page_fetch": reads_page,
        }
    )


def render_url(template: str, params: Mapping[str, str]) -> str:
    """`template` with each placeholder replaced by its value, encoded for the path or the query it stands in."""
    path, question, query = template.partition("?")
    return _fill(path, params, PATH_SAFE) + question + _fill(query, params, QUERY_SAFE)


def with_query_value(url: str, name: str, value: str) -> str:
    """`url` with its query items named `name` replaced by one `name=value` at the end, both encoded as a query value
    is (`render_url`)."""
    path, _, query = url.partition("?")
    items = [item for item in query.split("&") if item and unquote(item.partition("=")[0]) != name]
    items.append(f"{quote(name, safe=QUERY_SAFE)}={quote(value, safe=QUERY_SAFE)}")
    return f"{path}?{'&'.join(items)}"


def without_query(url: str, items: Iterable[tuple[str, str]]) -> str:
    """`url` without its query items that are one of the `(name, value)` `items` (compared percent-decoded)."""
    path, question, query = url.partition("?")
    left = set(items)
    kept = [item for item in query.split("&") if item and _decoded_item(item) not in left]
    return path + (question + "&".join(kept) if kept else "")


def _decoded_item(item: str) -> tuple[str, str] | None:
    name, equals, value = item.partition("=")
    return (unquote(name), unquote(value)) if equals else None


def page_data(page: ParsedPage) -> PageData:
    """The page's JSON documents as `jsonld:` and `js_state:` binders read them: a list per kind, in page order,
    unreadable ones skipped (their collectors report them)."""
    return {kind: documents(kind, page) for kind in PAGE_DATA_KINDS}


def render_body(template: str, params: Mapping[str, str], encoding: str = "json") -> str:
    """`template` with each placeholder replaced by its value, escaped as JSON string content (RFC 8259 §7), or for a
    `form` body percent-encoded as application/x-www-form-urlencoded writes a value (HTML §4.10.21.7, WHATWG URL §5.2:
    every byte but the unreserved ones, a space as `+`)."""
    if encoding == "form":
        return PLACEHOLDER.sub(lambda found: quote_plus(params[found[1]], safe="*"), template)
    return PLACEHOLDER.sub(lambda found: json.dumps(params[found[1]], ensure_ascii=False)[1:-1], template)


def _fill(template: str, params: Mapping[str, str], safe: str) -> str:
    return PLACEHOLDER.sub(lambda found: quote(params[found[1]], safe=safe), template)


def _url_value(binder: Binder, url: "_Url") -> str | None:
    if binder.kind == "path":
        index = int(binder.arg)
        return unquote(url.segments[index]) if index < len(url.segments) else None
    if binder.kind == "query":
        return next((unquote(value) for name, value in url.query if value and unquote(name) == binder.arg), None)
    found = re.compile(binder.arg, PCRE_FLAGS).search(url.path)
    return unquote(found[1]) if found is not None and found[1] is not None else None


def _data_value(kind: PageDataKind, query: str, page_data: PageData) -> str | None:
    return None if kind not in page_data else _one_scalar(query, page_data[kind])


def _one_scalar(query: str, document: JsonValue) -> str | None:
    """The text of the one scalar `query` selects in `document`; None when it selects anything else."""
    nodes = json_doc.select(query, document)
    return _scalar_text(nodes[0][1]) if len(nodes) == 1 else None


def _scalar_text(value: JsonValue) -> str | None:
    """A string as is and a number as its JSON text (RFC 8259 §6); other values are not parameters."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    return None


class _Url(NamedTuple):
    """A URL as binding reads it: the origin, the raw path and its segments after the leading `/`, and the raw query
    items (a value is `None` for an item without `=`)."""

    origin: str
    path: str
    segments: list[str]
    query: list[tuple[str, str | None]]

    @classmethod
    def split(cls, url: str) -> "_Url":
        parts = urlsplit(url)
        items = [item.partition("=") for item in parts.query.split("&")] if parts.query else []
        query = [(name, value if equals else None) for name, equals, value in items]
        segments = parts.path[1:].split("/") if parts.path else []
        return cls(f"{parts.scheme}://{parts.netloc}", parts.path, segments, query)


# --- deterministic binding -------------------------------------------------------------------------------------------


def bind(samples: Sequence[Sample]) -> RequestSpec | Ambiguous:
    """The request spec whose binders reproduce the endpoint request of every sample page, with no headers
    yet; `Ambiguous` when there is none.

    A value of the endpoint request that equals a path segment or query value of the page URL on every sample becomes
    a `path:`/`query:` parameter. A value that varies and appears nowhere in the page URL becomes a `jsonld:`/
    `js_state:` parameter when the same path of the page's JSON documents holds it on every sample. Any other value
    that stays the same is a literal; one that varies makes the binding ambiguous. Telling the two apart needs two
    sample pages at least.
    """
    try:
        return _Binding.checked(samples).spec()
    except _Unbound as unbound:
        return Ambiguous(str(unbound))


@dataclass(frozen=True)
class Proposed:
    """A binding with literals left where nothing binds: the spec, and where each value that varies between the
    samples was left as the first sample's literal (`path segment 1`, `query value 'q'`, `body value $['x']`)."""

    spec: RequestSpec
    literal: tuple[str, ...]


def propose(samples: Sequence[Sample]) -> Proposed | Ambiguous:
    """`bind`, except that a value that varies and that no page value binds stays the first sample's literal and is
    reported in `Proposed.literal`, for a person to bind; the templates are checked against every sample only when
    none was left. `Ambiguous` for what no literal mends: fewer than two samples, their methods, or their endpoint URL
    shapes."""
    try:
        binding = _Binding.checked(samples, leave_literals=True)
        return Proposed(binding.spec(), tuple(binding.literal))
    except _Unbound as unbound:
        return Ambiguous(str(unbound))


def withhold_tokens(samples: Sequence[Sample]) -> tuple[list[Sample], tuple[str, ...]]:
    """The samples with each token-like query value that is the same on every sample and that no page value binds
    (module docstring) left out of their endpoint URLs, and the names of the query items left out. Items are matched
    by their percent-decoded name and value together, wherever they stand in each query, so an ordinary item of the
    same name stays and samples whose other query names differ are read too; a single sample's tokens are left out as
    well, since nothing there tells a constant from a parameter."""
    tokens = _shared_tokens(samples)
    if tokens:
        stated = _Binding(samples)
        tokens = [(name, value) for name, value in tokens if not stated.binds([value] * len(samples))]
    if not tokens:
        return list(samples), ()
    stripped = [replace(s, request=replace(s.request, url=without_query(s.request.url, tokens))) for s in samples]
    return stripped, tuple(name for name, _ in tokens)


def _shared_tokens(samples: Sequence[Sample]) -> list[tuple[str, str]]:
    """The percent-decoded token-like `(name, value)` query items every sample's endpoint URL holds, in the first
    sample's order."""
    if not samples:
        return []
    decoded = [_decoded_query(sample.request.url) for sample in samples]
    shared = set(decoded[0]).intersection(*decoded[1:])
    return [item for item in dict.fromkeys(decoded[0]) if item in shared and token_like(item[1])]


def _decoded_query(url: str) -> list[tuple[str, str]]:
    return [(unquote(name), unquote(value)) for name, value in _Url.split(url).query if value is not None]


class _Unbound(Exception):
    pass


class _Binding:
    def __init__(self, samples: Sequence[Sample], *, leave_literals: bool = False) -> None:
        """The page values of `samples`, which `binds` reads; a request is bound only from a `checked` set. With
        `leave_literals`, a varying value nothing binds stays a literal, its place kept in `literal` (`propose`)."""
        self.samples = samples
        self.method = samples[0].request.method.upper()
        self.params: dict[str, str] = {}  # binder text -> parameter name
        self.leave_literals = leave_literals
        self.literal: list[str] = []

    @classmethod
    def checked(cls, samples: Sequence[Sample], *, leave_literals: bool = False) -> "_Binding":
        if len(samples) < 2:
            raise _Unbound("binding needs two sample pages to tell parameters from constants")
        methods = {sample.request.method.upper() for sample in samples}
        if len(methods) != 1 or not methods <= {"GET", "POST"}:
            raise _Unbound(f"the sample requests use methods {sorted(methods)}, not one of GET and POST")
        return cls(samples, leave_literals=leave_literals)

    def spec(self) -> RequestSpec:
        url = self._url_template()
        form = self._form_body()
        body = self._form_template() if form else self._body_template()
        params = {name: text for text, name in self.params.items()}
        try:
            spec = request_spec(self.method, url, params, body, "form" if form else "json")
        except ValidationError as error:
            raise _Unbound(f"the bound templates are not a valid request: {error.errors()[0]['msg']}") from None
        # A literal left in place reproduces the first sample's request only.
        for number, sample in enumerate(self.samples if not self.literal else ()):
            if (problem := mismatch(spec, sample)) is not None:
                raise _Unbound(f"the bound templates do not reproduce the request of sample page {number}: {problem}")
        return spec

    def _url_template(self) -> str:
        urls = [_Url.split(sample.request.url) for sample in self.samples]
        first = urls[0]
        # The endpoint URL shape (`network_match.url_shape`) is what binding templates: only its parts may vary. A
        # value may end in what reads as a format suffix (`a.b`), so the suffix may vary with it.
        if len({_template_shape(sample.request.url) for sample in self.samples}) > 1:
            raise _Unbound("the endpoint URL's host, path length or query names differ between sample pages")
        segments = [self._segment(urls, i, literal) for i, literal in enumerate(first.segments)]
        query = [self._query_item(urls, i, name, value) for i, (name, value) in enumerate(first.query)]
        path = "/" + "/".join(segments) if first.path else ""
        return first.origin + path + ("?" + "&".join(query) if query else "")

    def _segment(self, urls: list[_Url], index: int, literal: str) -> str:
        """Path segment `index` as a template: its values bound whole; else, in the last segment, the stem before the
        format suffix every sample's ends with (`network_match.path_suffix`, which the URL shape holds), the suffix
        kept as a literal: `/items/{p1}.json` for a page `/items/<handle>`."""
        values = [unquote(url.segments[index]) for url in urls]
        where = f"path segment {index}"
        suffix = path_suffix(literal) if index == len(urls[0].segments) - 1 else ""
        stems = [value.removesuffix(suffix) for value in values]
        ends = all(value.endswith(suffix) and value != suffix for value in values)
        if suffix and ends and not self.binds(values) and self.binds(stems):
            return _placeholder_or(self._param(stems, where), literal.removesuffix(suffix)) + suffix
        return _placeholder_or(self._param(values, where), literal)

    def _query_item(self, urls: list[_Url], index: int, name: str, value: str | None) -> str:
        if value is None:
            return name
        values = [unquote(url.query[index][1] or "") for url in urls]
        return f"{name}={_placeholder_or(self._param(values, f'query value {name!r}'), value)}"

    def _body_template(self) -> str | None:
        bodies = [sample.request.body for sample in self.samples]
        if all(body is None for body in bodies):
            return None
        if any(body is None for body in bodies):
            raise _Unbound("some sample requests have a body and some do not")
        documents = [_parse_body(body) for body in bodies if body is not None]
        markers: dict[str, str] = {}
        for path, leaf in json_doc.scalars(documents[0]):
            name = self._param([_leaf_text(path, document) for document in documents], f"body value {path}")
            if name is not None:
                markers[path] = _marker(name, number=not isinstance(leaf, str))
        return _body_with_placeholders(documents[0], markers)

    def _form_body(self) -> bool:
        """Whether every sample sends a form body (HTML §4.10.21.7: its request's media type says so) that is not
        JSON text, which `_body_template` binds."""
        for sample in self.samples:
            body = sample.request.body
            if body is None or media_type(sample.request.headers) != FORM_TYPE:
                return False
            try:
                json_text.parse(body)
            except ValueError:
                continue
            return False
        return True

    def _form_template(self) -> str:
        """The form body as a template: each item's name as sent, its value bound as a query value is (a form body is
        a query's `name=value` items, WHATWG URL §5.1, values decoded with `+` as a space)."""
        forms = [[item.partition("=") for item in str(s.request.body).split("&") if item] for s in self.samples]
        if len({tuple(name for name, _, _ in form) for form in forms}) > 1:
            raise _Unbound("the form body's names differ between sample pages")
        items = []
        for index, (name, equals, value) in enumerate(forms[0]):
            values = [unquote_plus(form[index][2]) for form in forms]
            bound = self._param(values, f"body value {unquote_plus(name)!r}")
            items.append(name + equals + _placeholder_or(bound, value))
        return "&".join(items)

    def _param(self, values: list[str], where: str) -> str | None:
        """The parameter that binds `values` (one per sample page), or `None` when they are one literal; raises when
        they vary and nothing binds them."""
        varies = len(set(values)) > 1
        text = next(self._url_binders(values), None)
        # A constant token is bound from the page's data too: the page states the key it sends (`withhold_tokens`).
        if text is None and (varies or token_like(values[0])):
            text = next(self._data_binders(values), None)
        if text is not None:
            return self.params.setdefault(text, self._name(text))
        if varies and not self.leave_literals:
            raise _Unbound(f"{where} varies between sample pages and no page value binds it")
        if varies:
            self.literal.append(where)
        return None

    def _name(self, text: str) -> str:
        """A new parameter's name: a path segment's is the one a generalized URL pattern gives that segment
        (`url_pattern.segment_param`); any other binder's is `v<n>`, a name no pattern generalizes to."""
        binder = parse_binder(text)
        if binder.kind == "path":
            return segment_param(int(binder.arg))
        taken = set(self.params.values())
        return next(name for n in count(1) if (name := f"v{n}") not in taken)

    def binds(self, values: list[str]) -> bool:
        """Whether a page value binds `values` (one per sample page)."""
        return next(self._url_binders(values), None) is not None or next(self._data_binders(values), None) is not None

    def _url_binders(self, values: list[str]) -> Iterator[str]:
        first = _Url.split(self.samples[0].page_url)
        candidates = [Binder("path", str(i)) for i in range(len(first.segments))]
        candidates += [Binder("query", unquote(name)) for name, _ in first.query if name]
        yield from (str(binder) for binder in candidates if self._binds(binder, values))

    def _data_binders(self, values: list[str]) -> Iterator[str]:
        for kind, document in self.samples[0].page_data.items():
            for path, leaf in json_doc.scalars(document):
                binder = _binder_or_none(f"{kind}:{path}")
                if _scalar_text(leaf) == values[0] and binder is not None and self._binds(binder, values):
                    yield str(binder)

    def _binds(self, binder: Binder, values: list[str]) -> bool:
        return all(
            resolve(binder, sample.page_url, sample.page_data) == value
            for sample, value in zip(self.samples, values, strict=True)
        )


def _template_shape(url: str) -> tuple[object, ...]:
    """The URL shape without its last segment's format suffix."""
    origin, has_path, segments, _, query = url_shape(url)
    return origin, has_path, segments, query


def _placeholder_or(name: str | None, literal: str) -> str:
    return literal if name is None else "{" + name + "}"


def mismatch(spec: RequestSpec, sample: Sample) -> str | None:
    """Why `spec` does not reproduce the endpoint request of `sample` (its method, URL and body), or `None` when
    it does: `method_mismatch(got, want)`, `no_value(<param>)`, `url_mismatch(got, want)` or `body_mismatch(...)`."""
    if spec.method != sample.request.method.upper():
        return f"method_mismatch({spec.method}, {sample.request.method})"
    params = _params_or_missing(spec, sample.page_url, sample.page_data)
    if isinstance(params, str):
        return f"no_value({params})"
    if (url := render_url(spec.url, params)) != sample.request.url:
        return f"url_mismatch({url}, {sample.request.url})"
    return _body_mismatch(spec, params, sample.request.body)


def _body_mismatch(spec: RequestSpec, params: Mapping[str, str], captured: str | None) -> str | None:
    if spec.body is None or captured is None:
        return None if spec.body is None and captured is None else "body_mismatch(a body on one side only)"
    rendered = render_body(spec.body.template, params, spec.body.encoding)
    if spec.body.encoding == "form":
        # WHATWG URL §5.1: two form bodies say the same when their decoded items are the same, in order.
        same = parse_qsl(rendered, keep_blank_values=True) == parse_qsl(captured, keep_blank_values=True)
        return None if same else f"body_mismatch({rendered}, {captured})"
    try:
        same = _canonical(json_text.parse(rendered)) == _canonical(json_text.parse(captured))
    except ValueError:  # a value that is not a JSON number filled a number's place
        return "body_mismatch(a body is not JSON)"
    return None if same else f"body_mismatch({rendered}, {captured})"


def _canonical(document: JsonValue) -> str:
    # Python compares `true == 1` and `1 == 1.0`; their JSON texts differ (RFC 8259 §3).
    return json.dumps(document, sort_keys=True)


def _binder_or_none(text: str) -> Binder | None:
    """A normalized path whose names hold a quote or backslash is outside the subset `select` reads."""
    try:
        return parse_binder(text)
    except ValueError:
        return None


def _parse_body(body: str) -> JsonValue:
    try:
        return json_text.parse(body)
    except ValueError:
        raise _Unbound("a sample request body is not JSON") from None


def _leaf_text(path: str, document: JsonValue) -> str:
    try:
        nodes = json_doc.select(path, document)
    except ValueError:
        nodes = []
    text = _scalar_text(nodes[0][1]) if len(nodes) == 1 else None
    if text is None:
        raise _Unbound(f"the request body has no value at {path} on every sample page")
    return text


def _marker(name: str, *, number: bool) -> str:
    # NUL is not text a JSON body carries unescaped (RFC 8259 §7), so a marker never meets a real value.
    return f"\x00{'N' if number else 'S'}{name}\x00"


def _body_with_placeholders(document: JsonValue, markers: Mapping[str, str]) -> str:
    """`document` as JSON text with the value at each marked path replaced by its placeholder: `"{name}"` for a
    string, a bare `{name}` for a number."""
    text = json.dumps(_marked(document, json_doc.ROOT, markers), ensure_ascii=False, separators=(",", ":"))
    for marker in markers.values():
        number, name = marker[1] == "N", marker[2:-1]
        text = text.replace(json.dumps(marker), "{" + name + "}" if number else '"{' + name + '}"')
    return text


def _marked(value: JsonValue, path: str, markers: Mapping[str, str]) -> JsonValue:
    if isinstance(value, dict):
        return {key: _marked(child, json_doc.member(path, key), markers) for key, child in value.items()}
    if isinstance(value, list):
        return [_marked(child, f"{path}[{i}]", markers) for i, child in enumerate(value)]
    return markers.get(path, value)
