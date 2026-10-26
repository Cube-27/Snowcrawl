"""Recipe learning (integration into config learning): the API recipe a learning run
gives its config, from the run's sample and held-out pages.

Discovery reads the sample pages' browser captures; the best endpoint candidate is the one a recipe is bound
from (one endpoint per config). Its request is bound deterministically, or by role D when that is
ambiguous, its headers are proven by replay, and it is validated on the held-out pages whose captures
hold an answer of the same endpoint (§4.7: held-out browser captures with network bodies). A page's values are what
the engine published from it (`record.data`), as everywhere in learning.

A listing's recipe (Appendix A #59) is learned the same way from the series of calls that gave its rows
(`listing.py`): the endpoint and row map, the pagination its calls follow, binding and header minimization, then
validation on the held-out pages whose calls of the endpoint it reproduces for the first two pages.

Network bodies come with browser captures only. With `render`, a page whose capture holds none (for a
listing: whose calls show no pagination) is rendered in the browser with network capture, a listing with its browser
actions, at most `MAX_RENDERS` pages per run, sample pages first; its published values stay those of its capture.

A repair run (`repair.py` in `config_ir`) learns the same way from the pages a custom recipe serves, preferring the
recipe's own endpoint (`prefer`): the candidates of its method, URL shape and body shape come first.

A run always ends with a recipe or the reason it has none, so that the config's validation report says which:
no endpoint carrying a required field on every sample page (with whether the sample had network bodies at
all), a listing whose calls show no pagination or come from one listing URL only, or the step a recipe failed. Replays
go through `replay`, which sends them through the SSRF guard.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Final

from snowcrawl.collect.network_match import FORM_TYPE, EndpointKey, endpoint_key, url_shape
from snowcrawl.config_ir.api_recipe.binding import (
    Ambiguous,
    CapturedRequest,
    Sample,
    bind,
    page_data,
    withhold_tokens,
)
from snowcrawl.config_ir.api_recipe.discovery import (
    DiscoverySample,
    EndpointCandidate,
    discover,
    endpoint_request,
)
from snowcrawl.config_ir.api_recipe.headers import NotLearnable, Proven, minimize
from snowcrawl.config_ir.api_recipe.listing import (
    ListingEndpoint,
    ListingPage,
    discover_listing,
    listing_pagination,
    page_series,
    replay_page,
)
from snowcrawl.config_ir.api_recipe.model import BoundBy, Pagination, RequestSpec, with_pagination
from snowcrawl.config_ir.api_recipe.replay import Render, Replay, ReplayPage
from snowcrawl.config_ir.api_recipe.role_d import Rejected, bind_by_role_d
from snowcrawl.config_ir.api_recipe.validate import HeldOutReplay, RecipeValidated, refused, validate_recipe
from snowcrawl.config_ir.held_out import StoredPage
from snowcrawl.config_ir.validation import MIN_HELD_OUT
from snowcrawl.db.models import JobId, PageKind
from snowcrawl.llm.client import LLMClient
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.storage.capture_bundle import CaptureBundle

# A run renders at most this many sample pages in the browser, one at a time: two listing URLs bind a request and a
# third confirms it; and at most `MIN_HELD_OUT` held-out pages, the fewest validation accepts.
SAMPLE_RENDERS: Final = 3
MAX_RENDERS: Final = SAMPLE_RENDERS + MIN_HELD_OUT
_NO_LISTING_ENDPOINT: Final = (
    "no_endpoint: no series of calls carries a required row field's published values on every sample page"
)


async def learn_recipe(
    client: LLMClient,
    sample: Sequence[StoredPage],
    held_out: Sequence[StoredPage],
    schema: SchemaDef,
    replay: Replay,
    *,
    job_id: JobId,
    render: Render | None = None,
    prefer: RequestSpec | None = None,
) -> RecipeValidated:
    """The recipe learned from `sample` and validated on `held_out`, or why there is none; pages without the calls
    to learn from are rendered with `render`, and the endpoint of `prefer`'s request comes first when the sample shows
    it. An LLM failure of role D propagates, as role A's does."""
    renders = _Renders(render)
    if schema.page_kind == "listing":
        return await learn_listing(client, sample, held_out, schema, replay, renders, job_id=job_id, prefer=prefer)
    sample = await renders.detail(sample, SAMPLE_RENDERS)
    # A capture without network bodies says nothing of the endpoint; binding needs two pages that have some.
    with_calls = [page for page in sample if page.bundle.network]
    sample = with_calls if len(with_calls) >= 2 else sample
    # Parsing the network bodies and the pages is CPU work: a thread keeps the event loop serving meanwhile.
    found = await asyncio.to_thread(_discover, sample, schema, prefer)
    if found is None:
        return refused(_no_endpoint(sample))
    endpoint, pages = found
    bound = await prove(client, endpoint.map, pages, schema, replay, job_id)
    if isinstance(bound, str):
        return refused(bound)
    proven, bound_by = bound
    held = await asyncio.to_thread(_held_out, await renders.detail(held_out, MIN_HELD_OUT), endpoint)
    return await validate_recipe(proven, bound_by, endpoint.map, held, schema, replay)


class _Renders:
    """The browser renders of one run, at most `MAX_RENDERS`; none without `render`."""

    def __init__(self, render: Render | None) -> None:
        self.render = render
        self.left = MAX_RENDERS if render is not None else 0
        self._listings: set[str] = set()  # the listing URLs rendered: a second render shows what the first did

    async def bundles(self, url: str, page_kind: PageKind) -> tuple[CaptureBundle, ...]:
        if self.render is None or self.left <= 0:
            return ()
        self.left -= 1
        return tuple(await self.render(url, page_kind))

    async def detail(self, pages: Sequence[StoredPage], at_most: int) -> list[StoredPage]:
        """`pages` with a render's capture for at most `at_most` of those whose capture holds no network bodies."""
        found: list[StoredPage] = []
        for page in pages:
            bundles: tuple[CaptureBundle, ...] = ()
            if not page.bundle.network and at_most > 0:
                at_most -= 1
                bundles = await self.bundles(page.bundle.manifest.url, "detail")
            found.append(replace(page, bundle=bundles[0]) if bundles and bundles[0].network else page)
        return found

    async def listing(
        self, pages: Sequence[ListingPage], needs: Callable[[ListingPage], bool], at_most: int
    ) -> list[ListingPage]:
        """`pages` with a render's states for at most `at_most` of those that `needs` one."""
        found: list[ListingPage] = []
        for page in pages:
            if needs(page) and at_most > 0 and page.url not in self._listings:
                at_most -= 1
                self._listings.add(page.url)
                page = page.rendered(await self.bundles(page.url, "listing"))
            found.append(page)
        return found


async def learn_listing(
    client: LLMClient,
    sample: Sequence[StoredPage],
    held_out: Sequence[StoredPage],
    schema: SchemaDef,
    replay: Replay,
    renders: _Renders,
    *,
    job_id: JobId,
    prefer: RequestSpec | None = None,
) -> RecipeValidated:
    """A listing's recipe (module docstring), or why there is none."""
    found = await _listing_endpoint(sample, schema, renders, prefer)
    if isinstance(found, str):
        return refused(found)
    endpoint, pagination, replayed = found
    bound = await prove(client, endpoint.map, replayed, schema, replay, job_id, pagination)
    if isinstance(bound, str):
        return refused(bound)
    proven, bound_by = bound
    pages = [ListingPage.of(page) for page in held_out]
    # A held-out page is validated on its first two pages: one whose capture shows fewer is rendered, renders left.
    pages = await renders.listing(pages, lambda page: len(page_series(page).get(endpoint.key, ())) < 2, MIN_HELD_OUT)
    held = [
        HeldOutReplay(page.capture_id, held_page)
        for page in pages
        if (held_page := replay_page(page, endpoint.key, pagination)) is not None
    ]
    return await validate_recipe(proven, bound_by, endpoint.map, held, schema, replay)


async def _listing_endpoint(
    sample: Sequence[StoredPage], schema: SchemaDef, renders: _Renders, prefer: RequestSpec | None
) -> tuple[ListingEndpoint, Pagination, list[ReplayPage]] | str:
    """The sample's listing endpoint, its pagination and the sample pages of distinct URLs as replay reads them; else
    why there is none. Only sample pages with calls are read: a capture without network bodies says nothing."""
    pages = [ListingPage.of(page) for page in sample]
    before = renders.left
    pages = await renders.listing(pages, lambda page: not page.has_calls, SAMPLE_RENDERS)
    with_calls = [page for page in pages if page.has_calls]
    if not with_calls:
        return _no_endpoint(sample)
    endpoints = await asyncio.to_thread(discover_listing, with_calls, schema)
    if not endpoints:
        return _NO_LISTING_ENDPOINT
    endpoint = _preferred(endpoints, _series_endpoint, prefer)
    pagination = await asyncio.to_thread(listing_pagination, with_calls, endpoint.key)
    if pagination is None:
        # The captures show one call each: the listing's browser actions show how it asks for the next page.
        # The two passes share the sample's renders: the rest are the
        # held-out pages'.
        unpaged = await renders.listing(
            with_calls,
            lambda page: len(page_series(page).get(endpoint.key, ())) < 2,
            SAMPLE_RENDERS - (before - renders.left),
        )
        pagination = await asyncio.to_thread(listing_pagination, unpaged, endpoint.key)
        with_calls = unpaged
    if pagination is None:
        return "no_pagination: no sample page's calls of the listing endpoint show how its next page is asked"
    distinct = {page.url: page for page in with_calls}
    replayed = [found for page in distinct.values() if (found := replay_page(page, endpoint.key, pagination))]
    if len(replayed) < 2:
        return "one_listing: binding needs the listing endpoint's calls on two listing URLs"
    return endpoint, pagination, replayed


def _discover(
    sample: Sequence[StoredPage], schema: SchemaDef, prefer: RequestSpec | None
) -> tuple[EndpointCandidate, list[ReplayPage]] | None:
    """The best endpoint of the sample, `prefer`'s first, with each sample page as replay reads it."""
    samples = [DiscoverySample(page.bundle, page.held_out.facts.published, page.held_out.ctx) for page in sample]
    candidates = discover(samples, schema)
    if not candidates:
        return None
    endpoint = _preferred(candidates, _request_endpoint, prefer)
    return endpoint, [_replay_page(page, request) for page, request in zip(sample, endpoint.requests, strict=True)]


def _preferred[T](found: Sequence[T], endpoint: Callable[[T], EndpointKey], prefer: RequestSpec | None) -> T:
    """The first of `found` (best first) whose endpoint is `prefer`'s, else the best. Endpoints compare without their
    query names: a listing's series key reads the URL without its query (`network_match.series_key`)."""
    if prefer is not None:
        body = None if prefer.body is None else prefer.body.template
        form = prefer.body is not None and prefer.body.encoding == "form"
        wanted = _query_free(
            endpoint_key(prefer.method, prefer.url, body, (("content-type", FORM_TYPE),) if form else ())
        )
        for item in found:
            if _query_free(endpoint(item)) == wanted:
                return item
    return found[0]


def _query_free(key: EndpointKey) -> EndpointKey:
    origin, has_path, segments, suffix, _ = key.url
    return replace(key, url=(origin, has_path, segments, suffix, ()))


def _request_endpoint(candidate: EndpointCandidate) -> EndpointKey:
    request = candidate.requests[0]
    return endpoint_key(request.method, request.url, request.body, request.headers)


def _series_endpoint(endpoint: ListingEndpoint) -> EndpointKey:
    method, url, body, _ = endpoint.key
    return EndpointKey(method, url_shape(url), body)


def _no_endpoint(sample: Sequence[StoredPage]) -> str:
    if not any(page.bundle.network for page in sample):
        # Network bodies come with browser captures only; an HTTP capture has none to discover from.
        return "no_network_bodies: no sample page has network responses to discover an endpoint from"
    return "no_endpoint: no JSON response carries a required field's published value on every sample page"


def _held_out(held_out: Sequence[StoredPage], endpoint: EndpointCandidate) -> list[HeldOutReplay]:
    return [
        HeldOutReplay(page.held_out.capture_id, _replay_page(page, request))
        for page in held_out
        if (request := endpoint_request(page.bundle, endpoint)) is not None
    ]


def _replay_page(page: StoredPage, request: CapturedRequest) -> ReplayPage:
    # The page URL is the one the page was requested at, which serving binds from.
    sample = Sample(page.bundle.manifest.url, request, page_data(page.held_out.page))
    return ReplayPage(sample, page.held_out.facts.published, page.held_out.ctx)


async def prove(
    client: LLMClient,
    fields: Mapping[str, str],
    pages: Sequence[ReplayPage],
    schema: SchemaDef,
    replay: Replay,
    job_id: JobId,
    pagination: Pagination | None = None,
) -> tuple[Proven, BoundBy] | str:
    """The bound request, with `pagination` when it is a listing's, and the headers its replay proved, and what bound
    it; else why there is none. Token-like query values the page does not state are left out first
    (`withhold_tokens`)."""
    samples, withheld = withhold_tokens([page.sample for page in pages])
    pages = [replace(page, sample=sample) for page, sample in zip(pages, samples, strict=True)]
    spec = bind(samples)
    if isinstance(spec, Ambiguous):
        proven = await bind_by_role_d(client, spec, fields, pages, schema, replay, job_id=job_id, pagination=pagination)
        bound_by: BoundBy = "role_d"
    else:
        paged = with_pagination(spec, pagination)
        if paged is None:
            return "not_learnable: the pagination does not fit the bound request"
        proven = await minimize(paged, fields, pages, schema, replay)
        bound_by = "deterministic"
    if isinstance(proven, NotLearnable):
        left_out = f" (token-like query values left out: {', '.join(withheld)})" if withheld else ""
        return f"not_learnable: {proven.reason}{left_out}"
    if isinstance(proven, Rejected):
        return "role_d_rejected: " + "; ".join(proven.diagnostics)
    return proven, bound_by
