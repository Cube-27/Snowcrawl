"""Header minimization by replay ("headers proven necessary by replay").

A recipe starts with `Accept: application/json` alone. When its replay does not reproduce every sample page, the
headers the browser sent with the endpoint request are added one at a time, in the order sent, and the first whose
addition makes the replay pass is kept; the others are dropped. So a kept header is one the replay was shown to need.

Some captured headers are never sent: the ones the client sets itself, credentials, values that look like tokens and
values that differ between sample pages (a recipe's header is a constant, or the page URL as `{page_url}`). After the
captured ones, each header the caller's request context supplies is tried as a reference (`ContextHeader`): the
replay sends the context's value, and the recipe keeps the name alone, never the value. When the replay needs anything
else, or more than one added header, the recipe is not learnable and the field stays on the browser (§2: cookie
handoff, tokens and signed requests are §22).

A custom recipe's draft (`proposed`) is not replayed: an engineer tests it. It keeps every captured header a recipe may
hold, minus those the HTTP rung sends itself.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from snowcrawl.acquire.api import FETCH_HEADER_ORDER
from snowcrawl.config_ir.api_recipe.binding import Sample
from snowcrawl.config_ir.api_recipe.model import (
    CLIENT_FIELDS,
    CREDENTIAL_FIELDS,
    PAGE_URL,
    Answer,
    ContextHeader,
    HeaderValue,
    RecipeHeader,
    RequestSpec,
    token_like,
)
from snowcrawl.config_ir.api_recipe.replay import Replay, ReplayPage, reproduces
from snowcrawl.schemas.types import SchemaDef

# RFC 8259 §11: the JSON media type, the one header a recipe starts with.
_ACCEPT: Final = HeaderValue(name="Accept", value="application/json")
_NO_SINGLE_HEADER: Final = "no single captured header makes the replay reproduce the sample values"
# The headers the HTTP rung sends itself as a script's `fetch()` does (its user agent, client hints, language, Fetch
# metadata and priority: one coherent fingerprint), which a recipe never holds; but `Accept` and a body's
# `Content-Type`, which are the request's own.
_RUNG_OWN: Final = frozenset(FETCH_HEADER_ORDER) - {"accept", "content-type"}


@dataclass(frozen=True)
class Proven:
    """The spec with the headers its replay was shown to need; `headers_tried` counts the captured headers replayed."""

    spec: RequestSpec
    headers_tried: int


@dataclass(frozen=True)
class NotLearnable:
    """No header set the recipe may keep makes the replay pass; `reason` is the learning job's diagnostic."""

    reason: str
    headers_tried: int


async def minimize(
    spec: RequestSpec, fields: Mapping[str, str], pages: Sequence[ReplayPage], schema: SchemaDef, replay: Replay
) -> Proven | NotLearnable:
    """The smallest header set with which replaying `spec` reproduces the `fields` map on every page of
    `pages`; their captured requests give the headers to try, in the order the first page's request sent them, then
    the request context of the pages (`ReplayPage.context`) its header names, each as a reference."""
    base = _with_headers(spec, [_ACCEPT])
    if base is not None and await _passes(base, fields, pages, schema, replay):
        return Proven(base, 0)
    candidates, withheld = _candidates(pages)
    for tried, header in enumerate(candidates, start=1):
        # RFC 9110 §5.1: field names are case-insensitive; a captured Accept replaces the recipe's.
        trial = _with_headers(spec, [header] if header.name.lower() == "accept" else [_ACCEPT, header])
        if trial is not None and await _passes(trial, fields, pages, schema, replay):
            return Proven(trial, tried)
    if withheld:
        return NotLearnable(
            "the replay needs a header a recipe never keeps (a credential, a token or a per-page value): "
            + ", ".join(withheld),
            len(candidates),
        )
    return NotLearnable(_NO_SINGLE_HEADER, len(candidates))


async def _passes(
    spec: RequestSpec, fields: Mapping[str, str], pages: Sequence[ReplayPage], schema: SchemaDef, replay: Replay
) -> bool:
    for page in pages:
        if not await reproduces(spec, fields, page, schema, replay):
            return False
    return True


def _with_headers(spec: RequestSpec, headers: list[RecipeHeader]) -> RequestSpec | None:
    """`spec` with `headers`; `None` when a captured value is not one a recipe can hold (a brace, RFC 6570)."""
    try:
        return RequestSpec.model_validate({**spec.model_dump(), "headers": [h.model_dump() for h in headers]})
    except ValidationError:
        return None


def proposed(samples: Sequence[Sample], answer: Answer) -> tuple[list[RecipeHeader], list[str]]:
    """The headers a custom recipe's draft sends, from its sample requests and without a replay: `Accept:
    application/json` for a JSON answer, and each captured header a recipe may hold whose value is the same on every
    sample, except those the HTTP rung sends itself (`_RUNG_OWN`). A captured `Accept` replaces the default; a body's
    `Content-Type` is the request's own (Fetch §5.2 sends `text/plain` without one). Also the names of the captured
    headers never sent (`_captured`)."""
    captured, withheld = _captured(samples, skip=_RUNG_OWN)
    headers: list[RecipeHeader] = [_ACCEPT] if answer == "json" else []
    # RFC 9110 §5.1: field names are case-insensitive; a captured Accept replaces the recipe's.
    if any(header.name.lower() == "accept" for header in captured):
        headers = []
    return [*headers, *captured], withheld


def _candidates(pages: Sequence[ReplayPage]) -> tuple[list[RecipeHeader], list[str]]:
    """The headers to try: the captured ones as recipe values, then the request context's as references; and the
    names of the captured ones never sent."""
    context = pages[0].context if pages else None
    supplied = context.header_names() if context is not None else frozenset()
    # A field the request context supplies is a reference, never the captured value (the caller's secret); one the HTTP
    # rung sends itself is never a recipe's.
    candidates, withheld = _captured([page.sample for page in pages], skip=supplied | _RUNG_OWN)
    names = sorted(supplied - CLIENT_FIELDS)
    return [*candidates, *(ContextHeader(name=name, from_context=True) for name in names)], withheld


def _captured(samples: Sequence[Sample], *, skip: frozenset[str]) -> tuple[list[RecipeHeader], list[str]]:
    """The headers the first sample's request sent, in order, as recipe values, but for those named in `skip` and those
    the client sets itself; and the names of those never sent: credentials, values that look like tokens and values
    that differ between the samples, and values a recipe cannot hold."""
    candidates: list[RecipeHeader] = []
    withheld: list[str] = []
    seen: set[str] = set(skip)
    for name, _ in samples[0].request.headers if samples else ():
        key = name.lower()
        # RFC 9113 §8.3: a name starting with `:` is an HTTP/2 pseudo-header, not a field.
        if key in seen or key in CLIENT_FIELDS or key.startswith(":"):
            continue
        seen.add(key)
        value = _constant(key, samples)
        header = None if value is None or key in CREDENTIAL_FIELDS or token_like(value) else _header(name, value)
        if header is None:
            withheld.append(name)
        else:
            candidates.append(header)
    return candidates, withheld


def _header(name: str, value: str) -> HeaderValue | None:
    """The header as a recipe holds it; None for a value a recipe cannot hold (a brace, RFC 6570)."""
    try:
        return HeaderValue(name=name, value=value)
    except ValidationError:
        return None


def _constant(key: str, samples: Sequence[Sample]) -> str | None:
    """The field's value on every sample, with the page's own URL written `{page_url}`; `None` when it differs."""
    values = {_recipe_value(key, sample) for sample in samples}
    return values.pop() if len(values) == 1 else None


def _recipe_value(key: str, sample: Sample) -> str | None:
    value = next((v for name, v in sample.request.headers if name.lower() == key), None)
    return f"{{{PAGE_URL}}}" if value == sample.page_url else value
