"""A custom recipe's draft from two to five engineer captures (`POST /v1/recipes/propose`): the calls of the main
endpoint the engineer chose, made into the recipe that reproduces them. Deterministic, and it persists and sends
nothing: the draft is a function of the samples and the choice alone, and every edit after it is the engineer's.

The main call is an endpoint of the samples' call list (`services.recipe_calls`), named by its key or by one of its
calls. A detail page's request is each sample's first answered call of the endpoint; a listing's is the first call of
the series it belongs to (`network_match.row_series`), with the pagination the series follow (`pagination.infer`) set
to where the listing starts. The request is bound from the sample pages as learning binds it (`binding.propose`): a
value that varies and that nothing on the page states is left as the first sample's literal and reported, and a
token-like query value no page states is left out (`binding.withhold_tokens`). Its headers are the captured ones a
recipe may hold (`headers.proposed`); its map takes, per field, the path the call list read the published value at on
the most samples, relative to a row for a listing; its URL patterns generalize the sample URLs
(`url_pattern.generalize`). The answer names the fields left without a path, the values left unbound and the headers
left out.
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, NonNegativeInt, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.collect.network_match import SUCCESS, DataCall, EndpointKey, endpoint_key
from snowcrawl.config_ir.api_recipe.binding import (
    Ambiguous,
    CapturedRequest,
    Sample,
    page_data,
    propose,
    withhold_tokens,
)
from snowcrawl.config_ir.api_recipe.headers import proposed
from snowcrawl.config_ir.api_recipe.listing import ListingPage, first_page_request, page_series, series_pagination
from snowcrawl.config_ir.api_recipe.model import ApiRecipe, Pagination, with_pagination
from snowcrawl.config_ir.custom_recipe import CustomRecipe, InvalidRecipe
from snowcrawl.config_ir.url_pattern import generalize
from snowcrawl.config_ir.validation import HeldOutPage
from snowcrawl.core.validation import field_error
from snowcrawl.db.models import CaptureId, SchemaId
from snowcrawl.services.recipe_calls import INDEX as _INDEX
from snowcrawl.services.recipe_calls import Endpoint, SampleIds, Samples, listed, load_samples
from snowcrawl.services.scrape_request import Input
from snowcrawl.storage.base import ObjectStore
from snowcrawl.storage.capture_bundle import NetworkBody, is_html

__all__ = ["CallRef", "EndpointRef", "Proposal", "ProposeRequest", "propose_recipe"]


class CallRef(Input):
    """One call of a sample: its capture and its index among the capture's network bodies."""

    capture_id: CaptureId
    index: NonNegativeInt


class EndpointRef(Input):
    """An endpoint of the call list, by its key: request method, URL shape and request body shape (`Endpoint`)."""

    method: str | None = None
    origin: str
    has_path: bool
    segments: NonNegativeInt
    query: list[str]
    body_shape: list[str] | None = None


class ProposeRequest(Input):
    """The body of `POST /v1/recipes/propose`: the sample captures, the main call and the schema the recipe serves."""

    capture_ids: SampleIds
    main: CallRef | EndpointRef
    schema_id: SchemaId


class Proposal(BaseModel):
    """A custom recipe's draft, with the schema fields its map leaves unfilled, where a value of the request varies
    between the samples and nothing binds it (left as the first sample's literal) or looks like a token (left out),
    and the captured headers left out (a credential, a token, a value that varies)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    recipe: CustomRecipe
    unfilled: list[str]
    unbound: list[str]
    withheld_headers: list[str]


async def propose_recipe(engine: AsyncEngine, store: ObjectStore, request: ProposeRequest) -> Proposal:
    """The draft (module docstring). Raises as the call list (`services.recipe_calls`) does for the captures, and
    `InvalidRecipe` when the main call is not one of the samples', or the samples' calls of it make no recipe."""
    samples = await load_samples(engine, store, request.capture_ids, request.schema_id)
    # Matching every leaf of every body is CPU work: a thread keeps the event loop serving meanwhile.
    return await asyncio.to_thread(drafted, samples, request.main)


def drafted(samples: Samples, main: CallRef | EndpointRef) -> Proposal:
    """The draft of `samples` for the `main` call; raises `InvalidRecipe` as `propose_recipe` does."""
    key, chosen = _main_key(samples, main)
    endpoint = next((found for found in listed(samples.pages, samples.schema).endpoints if _key(found) == key), None)
    if endpoint is None:
        raise InvalidRecipe([("main", "no sample made a call of the main endpoint")])
    calls = _sample_calls(endpoint, chosen)
    if samples.schema.page_kind == "listing":
        if endpoint.rows is None:
            raise InvalidRecipe([("main", "the main endpoint's answers hold no rows of the listing")])
        requests, pagination = _listing_requests(samples, calls, endpoint.rows)
        fields = _row_map(endpoint, endpoint.rows)
    else:
        requests, pagination = _detail_requests(samples, calls), None
        fields = _map(endpoint)
    if not fields:
        reason = "the main call's answers hold no value the samples published, so no field has a path"
        raise InvalidRecipe([("main.map", reason)])
    answer = _answer(samples, calls)
    request, unbound, withheld = _request(requests, pagination, answer)
    recipe = _recipe(samples, {**request, "answer": answer, "map": fields})
    unfilled = [name for name in samples.schema.fields if name not in fields]
    return Proposal(recipe=recipe, unfilled=unfilled, unbound=unbound, withheld_headers=withheld)


def _main_key(samples: Samples, main: CallRef | EndpointRef) -> tuple[EndpointKey, CallRef | None]:
    """The endpoint `main` names, and the call it names when it names one."""
    if isinstance(main, EndpointRef):
        return _key(main), None
    page = next((page for page in samples.pages if page.capture_id == main.capture_id), None)
    if page is None:
        raise InvalidRecipe([("main.capture_id", "not one of the sample captures")])
    if main.index >= len(page.page.network_entries):
        raise InvalidRecipe([("main.index", f"capture {main.capture_id} has no call {main.index}")])
    entry = page.page.network_entries[main.index]
    return endpoint_key(entry.method, entry.url, entry.request_body), main


def _key(endpoint: Endpoint | EndpointRef) -> EndpointKey:
    body = None if endpoint.body_shape is None else tuple(endpoint.body_shape)
    shape = (endpoint.origin, endpoint.has_path, endpoint.segments, tuple(endpoint.query))
    return EndpointKey(endpoint.method, shape, body)


def _sample_calls(endpoint: Endpoint, chosen: CallRef | None) -> dict[CaptureId, int]:
    """Each sample's call of the endpoint, by index: the one chosen, else the sample's first answered one."""
    calls: dict[CaptureId, int] = {}
    for call in endpoint.calls:
        if call.status in SUCCESS:
            calls.setdefault(call.capture_id, call.index)
    if chosen is not None:
        calls[chosen.capture_id] = chosen.index
    return calls


def _detail_requests(samples: Samples, calls: Mapping[CaptureId, int]) -> list[Sample]:
    found = []
    for page, url in zip(samples.pages, samples.urls, strict=True):
        if (index := calls.get(page.capture_id)) is not None:
            found.append(Sample(url, _captured(page.page.network_entries[index]), page_data(page.page)))
    return found


def _captured(entry: NetworkBody) -> CapturedRequest:
    return CapturedRequest(str(entry.method).upper(), entry.url, entry.request_body, entry.request_headers or ())


def _listing_requests(samples: Samples, calls: Mapping[CaptureId, int], rows: str) -> tuple[list[Sample], Pagination]:
    """Each sample's first request of the series its call of the endpoint belongs to, at the listing's start, and the
    pagination the series follow."""
    series: list[tuple[HeldOutPage, str, tuple[DataCall, ...]]] = []
    for page, url in zip(samples.pages, samples.urls, strict=True):
        found = _series(_listing_page(page, url), calls.get(page.capture_id))
        if found:
            series.append((page, url, found))
    pagination = series_pagination([calls for _, _, calls in series], rows)
    if pagination is None:
        raise InvalidRecipe([("main", "no sample's calls of the main endpoint show how the next page is asked")])
    requests = []
    for page, url, calls_of in series:
        if (request := first_page_request(calls_of[0], pagination)) is not None:
            requests.append(Sample(url, request, page_data(page.page)))
    return requests, pagination


def _listing_page(page: HeldOutPage, url: str) -> ListingPage:
    rows = tuple(row.published for row in page.rows)
    return ListingPage(page.capture_id, url, rows, page.ctx, page_data(page.page), (page.page,))


def _series(page: ListingPage, index: int | None) -> tuple[DataCall, ...]:
    """The page's series holding its call `index`; empty when there is none."""
    if index is None:
        return ()
    return next((calls for calls in page_series(page).values() if any(d.call.index == index for d in calls)), ())


def _answer(samples: Samples, calls: Mapping[CaptureId, int]) -> Literal["json", "html"]:
    """`html` when the first sample's call of the endpoint answered HTML (HTML Standard §1.10.1), else `json`."""
    page = next((page for page in samples.pages if page.capture_id in calls), None)
    if page is None:
        raise InvalidRecipe([("main", "no sample has an answered call of the main endpoint")])
    return "html" if is_html(page.page.network_entries[calls[page.capture_id]].content_type) else "json"


def _request(
    requests: Sequence[Sample], pagination: Pagination | None, answer: Literal["json", "html"]
) -> tuple[dict[str, object], list[str], list[str]]:
    """The bound request as a recipe's members, where values were left unbound, and the headers left out."""
    stripped, tokens = withhold_tokens(requests)
    bound = propose(stripped)
    if isinstance(bound, Ambiguous):
        raise InvalidRecipe([("main", f"the samples' calls bind no request: {bound.reason}")])
    spec = with_pagination(bound.spec, pagination)
    if spec is None:
        raise InvalidRecipe([("main", "the pagination does not fit the bound request")])
    headers, withheld = proposed(stripped, answer)
    unbound = [*bound.literal, *(f"query value {name!r} (token-like, left out)" for name in tokens)]
    members = {**spec.model_dump(), "headers": [header.model_dump() for header in headers]}
    return members, unbound, withheld


def _map(endpoint: Endpoint) -> dict[str, str]:
    """Per field, the path read the published value on the most samples (`FieldMatch.paths` comes most first)."""
    return {match.field: match.paths[0].path for match in endpoint.fields if match.published and match.paths}


def _row_map(endpoint: Endpoint, rows: str) -> dict[str, str]:
    """`_map` with each path relative to a row (RFC 9535 §2.2: `$` is the row), for the paths inside the rows."""
    # The call list writes an array's items as one (`[*]`), its row query too.
    prefix = _INDEX.sub("[*]", rows).removesuffix("[*]")
    relative: dict[str, str] = {}
    for name, path in _map(endpoint).items():
        if path.startswith(prefix + "[*]"):
            relative[name] = "$" + path.removeprefix(prefix + "[*]")
    return relative


def _recipe(samples: Samples, main: Mapping[str, object]) -> CustomRecipe:
    schema = samples.schema
    try:
        api = ApiRecipe.model_validate(main)
    except ValidationError as e:
        raise _invalid(e, "main") from None
    try:
        return CustomRecipe(
            schema_id=schema.id, page_kind=schema.page_kind, patterns=generalize(samples.urls), main=api
        )
    except ValidationError as e:
        raise _invalid(e) from None


def _invalid(error: ValidationError, *within: str) -> InvalidRecipe:
    return InvalidRecipe([field_error(e | {"loc": (*within, *e["loc"])}) for e in error.errors()])
