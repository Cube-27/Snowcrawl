"""The API recipe: how to fetch the JSON document a page's values come
from, with its parameters bound from the page, and where each field sits in that document.

A recipe is data that our interpreters run, never code. A document that validates here names a binder for every
placeholder and uses every binder, keeps the request's scheme and host fixed, holds no credential value (a credential
header is a reference to the caller's request context, sent with the value the context holds), and writes its
JSONPaths in the subset the engine reads (`select` of `json_doc`). Site strings (the endpoint URL, a binder regex)
live in recipe documents only (C3); this module holds none.
"""

import re
from collections.abc import Callable
from typing import Annotated, Final, Literal, NamedTuple, Self, get_args
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from snowcrawl.collect import json_doc
from snowcrawl.collect.api import map_paths
from snowcrawl.collect.network_match import LearnedCalls
from snowcrawl.config_ir.portable_regex import PCRE_FLAGS, check_pattern
from snowcrawl.config_ir.xpath1 import check_xpath1
from snowcrawl.db.models import CaptureId
from snowcrawl.schemas.types import NAME_PATTERN, Name

# Where a binder reads a parameter's value: a path segment, a query value or a regex group of the page URL, or a
# scalar in one of the page's JSON documents (the two need the HTML page fetched first). A custom recipe's binders also
# read a parameter of the URL pattern the page matched (`pattern`), its warm-up's answer by JSONPath, XPath 1.0 or a
# regex group (`warm_up`, `warm_up_xpath`, `warm_up_regex`), and, in an extra call, the main call's answer (`main`).
BinderKind = Literal[
    "path", "query", "regex", "jsonld", "js_state", "pattern", "warm_up", "warm_up_xpath", "warm_up_regex", "main"
]
PageDataKind = Literal["jsonld", "js_state"]
PAGE_DATA_KINDS: Final = frozenset(get_args(PageDataKind))
# The binders a custom recipe's serving reads, not the page.
CUSTOM_KINDS: Final[frozenset[BinderKind]] = frozenset({"pattern", "warm_up", "warm_up_xpath", "warm_up_regex", "main"})
WARM_UP_KINDS: Final[frozenset[BinderKind]] = frozenset({"warm_up", "warm_up_xpath", "warm_up_regex"})
# What a call answers: a JSON document (RFC 8259) its map reads by JSONPath, or an HTML document read by XPath 1.0.
Answer = Literal["json", "html"]
# What produced a recipe's request: deterministic binding or role D.
BoundBy = Literal["deterministic", "role_d"]

# `{name}`: a parameter's place in the URL, the body or a header value (RFC 6570 level 1 expression syntax).
PLACEHOLDER: Final = re.compile(rf"\{{({NAME_PATTERN})\}}")
# The page's own URL, the one value a header may template (§4.6: `Referer`); never a parameter name.
PAGE_URL: Final = "page_url"
# Path segment indexes count from 0 and have one spelling: no leading zero.
_INDEX: Final = re.compile(r"0|[1-9]\d*", re.ASCII)
# RFC 9110 §5.6.2: a field name is a token.
_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# A value looks like a token when one of its items (RFC 9110 §5.6.1, §5.6.6: items split at commas, parameters at
# semicolons, words at whitespace; RFC 7515 §7.1: the parts of a JWS compact serialization split at dots) is 20 or more
# characters of one base64 alphabet (RFC 4648 §4 standard, §5 URL- and filename-safe, hex, is in both), padding
# allowed. A recipe never stores one: header minimization never sends such a header, and binding leaves such a query
# value out unless the page states it (`binding.withhold_tokens`).
_ITEM_SEPARATORS: Final = re.compile(r"[\s,;.]+")
_BASE64_TOKEN: Final = re.compile(r"(?:[A-Za-z0-9+/]{20,}|[A-Za-z0-9_-]{20,})={0,2}")


def token_like(value: str) -> bool:
    return any(_BASE64_TOKEN.fullmatch(item) for item in _ITEM_SEPARATORS.split(value))


# Fields that carry credentials: RFC 9110 §11.6.2 and §11.7.2, RFC 6265 §5.4. A recipe never holds a value of one: one
# it needs is a reference to the header the caller's request context supplies (`ContextHeader`), and without such a
# context the recipe is not learnable.
CREDENTIAL_FIELDS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})
# Fields the HTTP client sets for the connection it makes, never part of a recipe: Host (RFC 9110 §7.2), the content
# length and transfer coding of the body (RFC 9112 §6.1), the connection options (RFC 9110 §7.6.1: Connection,
# Keep-Alive, TE, Upgrade, Proxy-Connection) and the content codings the client decodes (RFC 9110 §12.5.3).
CLIENT_FIELDS: Final = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "upgrade",
        "proxy-connection",
        "accept-encoding",
    }
)


class Binder(NamedTuple):
    kind: BinderKind
    arg: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.arg}"


def parse_binder(text: str) -> Binder:
    """`path:<index>`, `query:<name>`, `regex:<pattern with one group>`, `jsonld:<query>`, `js_state:<query>`,
    `pattern:<name>`, `warm_up:<query>`, `warm_up_xpath:<XPath 1.0>`, `warm_up_regex:<pattern with one group>` or
    `main:<query>`; raises `ValueError` for anything else."""
    name, _, arg = text.partition(":")
    for kind, check in _BINDER_CHECKS.items():
        if kind == name:
            check(arg)
            return Binder(kind, arg)
    raise ValueError(f"not a binder kind: {name!r}")


def _path_index(arg: str) -> None:
    if _INDEX.fullmatch(arg) is None:
        raise ValueError(f"not a path segment index: {arg!r}")


def _query_name(arg: str) -> None:
    if not arg:
        raise ValueError("a query binder names a query parameter")


def _one_group_regex(arg: str) -> None:
    check_pattern(arg)
    if re.compile(arg, PCRE_FLAGS).groups != 1:
        raise ValueError(f"a regex binder has exactly one capturing group: {arg!r}")


def _engine_jsonpath(arg: str) -> None:
    json_doc.select(arg, None)  # raises for a query outside the subset the engine reads


def _pattern_name(arg: str) -> None:
    if re.fullmatch(NAME_PATTERN, arg) is None:
        raise ValueError(f"not a URL pattern parameter: {arg!r}")


def _xpath1(arg: str) -> None:
    check_xpath1(arg)


_BINDER_CHECKS: Final[dict[BinderKind, Callable[[str], None]]] = {
    "path": _path_index,
    "query": _query_name,
    "regex": _one_group_regex,
    "jsonld": _engine_jsonpath,
    "js_state": _engine_jsonpath,
    "pattern": _pattern_name,
    "warm_up": _engine_jsonpath,
    "warm_up_xpath": _xpath1,
    "warm_up_regex": _one_group_regex,
    "main": _engine_jsonpath,
}


def _check_binder(text: str) -> str:
    parse_binder(text)
    return text


def check_jsonpath(path: str) -> str:
    """`path`, when it is a query the engine reads (the `select` of `json_doc`); raises `ValueError` otherwise."""
    _engine_jsonpath(path)
    return path


def check_map_path(path: str, answer: "Answer" = "json") -> str:
    """`path` when it reads the answer: JSONPaths the engine reads joined by `+` for JSON, XPath 1.0 for HTML."""
    if answer == "html":
        return check_xpath1(path)
    for part in map_paths(path):
        _engine_jsonpath(part)
    return path


def _check_url_template(url: str) -> str:
    outside = PLACEHOLDER.sub("", url)
    if "{" in outside or "}" in outside:
        raise ValueError("a brace outside a {name} placeholder")  # RFC 3986 §2: not a URL character
    raw = urlsplit(url)
    if "{" in raw.scheme + raw.netloc:
        raise ValueError("the scheme and host are fixed; placeholders go in the path or query")
    filled = urlsplit(PLACEHOLDER.sub("x", url))
    if filled.scheme not in ("http", "https") or not filled.hostname or filled.fragment:
        raise ValueError("an absolute http(s) URL without a fragment")
    return url


def _check_header_value(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("a field value holds no line break")  # RFC 9110 §5.5
    if any(name != PAGE_URL for name in PLACEHOLDER.findall(value)):
        raise ValueError(f"{{{PAGE_URL}}} is the only placeholder a header value takes")
    return value


BinderText = Annotated[str, AfterValidator(_check_binder)]
UrlTemplate = Annotated[str, AfterValidator(_check_url_template)]
HeaderName = Annotated[str, StringConstraints(pattern=rf"^{_TOKEN.pattern}$")]
HeaderText = Annotated[str, AfterValidator(_check_header_value)]
EngineJsonPath = Annotated[str, AfterValidator(check_jsonpath)]


def _not_page_url(name: str) -> str:
    if name == PAGE_URL:
        raise ValueError(f"{PAGE_URL} is the page URL, not a parameter")
    return name


ParamName = Annotated[Name, AfterValidator(_not_page_url)]


class _Model(BaseModel):
    # The API sends every member, defaulted ones too, so its document marks them all `required` (OpenAPI 3.1).
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)


class HeaderValue(_Model):
    """A header the recipe sends with a constant value (`{page_url}` the one placeholder it takes)."""

    name: HeaderName
    value: HeaderText


class ContextHeader(_Model):
    """A header the recipe sends with the value the caller's request context holds for `name`: the recipe
    stores the name alone, never a value."""

    name: HeaderName
    from_context: Literal[True]


RecipeHeader = HeaderValue | ContextHeader


# How a request body writes its values: JSON text, or application/x-www-form-urlencoded (`RequestBody`).
BodyEncoding = Literal["json", "form"]


class RequestBody(_Model):
    """A request body with `{name}` placeholders: JSON, where a value is written JSON-escaped (RFC 8259 §7), or
    `form`, application/x-www-form-urlencoded (HTML §4.10.21.7), where a value is written percent-encoded."""

    template: str = Field(min_length=1)
    encoding: BodyEncoding = "json"


# Where a listing's next request carries its page value: a query parameter (RFC 3986 §3.4), named; or a value of the
# JSON request body (RFC 8259), at a normalized path (RFC 9535 §2.7), which reaches GraphQL `variables` too.
PageWhere = Literal["query", "body"]


def _check_body_path(path: str) -> str:
    _engine_jsonpath(path)
    if "[*]" in path:
        raise ValueError(f"a body value's path names one value, without wildcards: {path!r}")
    return path


class ParamPagination(_Model):
    """§4.8: the listing's requests set `name` to `start`, `start + step`, ... (a page number or an item offset) until
    `rows` selects nothing."""

    kind: Literal["param"]
    where: PageWhere
    name: str = Field(min_length=1)
    start: int = Field(ge=0)
    step: int = Field(ge=1)
    rows: EngineJsonPath


class CursorPagination(_Model):
    """The next request sets `name` to the value the previous response holds at `cursor`, until it holds none."""

    kind: Literal["cursor"]
    where: PageWhere
    name: str = Field(min_length=1)
    cursor: EngineJsonPath
    rows: EngineJsonPath


class NextUrlPagination(_Model):
    """The next request goes to the URL the previous response holds at `next` (resolved against the request's URL,
    RFC 3986 §5), on the recipe's own origin, until it holds none."""

    kind: Literal["next_url"]
    next: EngineJsonPath
    rows: EngineJsonPath


class LinkPagination(_Model):
    """The next request goes to the target of the previous response's `Link` field with relation type `next`
    (RFC 8288 §3), on the recipe's own origin, until it names none."""

    kind: Literal["link"]
    rows: EngineJsonPath


Pagination = Annotated[
    ParamPagination | CursorPagination | NextUrlPagination | LinkPagination, Field(discriminator="kind")
]


def body_pagination_path(pagination: "Pagination | None") -> str | None:
    """The request body path a pagination sets, if it sets one."""
    if isinstance(pagination, (ParamPagination, CursorPagination)) and pagination.where == "body":
        return pagination.name
    return None


class RequestSpec(_Model):
    """The request half of a recipe: what deterministic binding and role D produce."""

    method: Literal["GET", "POST"]
    url: UrlTemplate
    params_from_url: dict[ParamName, BinderText] = {}
    headers: list[RecipeHeader] = []
    body: RequestBody | None = None
    pagination: Pagination | None = None
    requires_page_fetch: bool = False
    answer: Answer = "json"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.body is not None and self.method != "POST":
            raise ValueError("only a POST carries a body")  # RFC 9110 §9.3.1: a GET body has no defined meaning
        used = set(PLACEHOLDER.findall(self.url + (self.body.template if self.body else "")))
        if used != set(self.params_from_url):
            raise ValueError(f"placeholders {sorted(used)} and binders {sorted(self.params_from_url)} differ")
        names = [header.name.lower() for header in self.headers]
        if len(set(names)) != len(names):
            raise ValueError("a header named twice")  # RFC 9110 §5.1: field names are case-insensitive
        valued = {header.name.lower() for header in self.headers if isinstance(header, HeaderValue)}
        if CREDENTIAL_FIELDS.intersection(valued):
            raise ValueError("a recipe holds no credential value: a credential is a reference to the request context")
        if CLIENT_FIELDS.intersection(names):
            raise ValueError("a recipe carries no field the HTTP client sets itself")
        if self.requires_page_fetch != self.reads_page_data:
            raise ValueError("requires_page_fetch is true exactly when a binder reads the page's JSON documents")
        return self

    @model_validator(mode="after")
    def _pagination_fits(self) -> Self:
        if (path := body_pagination_path(self.pagination)) is not None:
            _check_body_path(path)
            if self.body is None or self.body.encoding != "json":
                raise ValueError("a pagination value in the body needs a JSON request body")
        return self

    @property
    def context_headers(self) -> frozenset[str]:
        """The names, lowercased, of the headers the caller's request context must supply."""
        return frozenset(header.name.lower() for header in self.headers if isinstance(header, ContextHeader))

    @property
    def binders(self) -> dict[str, Binder]:
        return {name: parse_binder(text) for name, text in self.params_from_url.items()}

    @property
    def reads_page_data(self) -> bool:
        return any(binder.kind in PAGE_DATA_KINDS for binder in self.binders.values())

    def binder_args(self, kinds: frozenset[BinderKind]) -> set[str]:
        """The arguments of the binders of `kinds`."""
        return {binder.arg for binder in self.binders.values() if binder.kind in kinds}


def with_pagination(spec: RequestSpec, pagination: Pagination | None) -> RequestSpec | None:
    """`spec` with `pagination` (as it is without one); None when the two do not make a valid request (a body value
    the spec has no body for)."""
    if pagination is None:
        return spec
    try:
        return RequestSpec.model_validate({**spec.model_dump(), "pagination": pagination.model_dump()})
    except ValidationError:
        return None


class Proof(_Model):
    """How the recipe was accepted: the held-out captures it reproduced, the headers replay tried and
    what bound it."""

    validated_on: list[CaptureId] = Field(min_length=1)
    headers_tried: int = Field(ge=0)
    bound_by: BoundBy


class ApiRecipe(RequestSpec):
    """A request and where each field sits in its answer. A learned recipe holds its `proof`; an operator's custom
    recipe may hold none: the engineer's test proved it, not held-out pages."""

    map: dict[Name, str] = Field(min_length=1)
    proof: Proof | None = None

    @model_validator(mode="after")
    def _map_reads_the_answer(self) -> Self:
        for path in self.map.values():
            check_map_path(path, self.answer)
        if self.answer == "html" and self.pagination is not None:
            raise ValueError("a pagination's rows are JSONPaths: an HTML answer has none")
        return self

    @property
    def learned_calls(self) -> LearnedCalls | None:
        """A listing recipe's endpoint as the calls a browser page makes show it (`network_match.LearnedCalls`); None
        for a recipe without pagination."""
        if self.pagination is None:
            return None
        parts = urlsplit(self.url)
        segments = parts.path[1:].split("/") if parts.path else []
        return LearnedCalls(
            method=self.method,
            origin=f"{parts.scheme}://{parts.netloc}",
            segments=tuple(None if PLACEHOLDER.search(segment) else segment for segment in segments),
            rows=self.pagination.rows,
            map=self.map,
        )
