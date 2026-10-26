"""Recipe execution (the adapter rung): serve a site's pages with its
active config's API recipe over `fetch_json`, with no browser.

`RecipeRung` is the recipe bound to its fetchers, the rung `acquire` takes (`acquire.rungs`): it binds the
parameters from the page URL (fetching the HTML page first only when a binder reads the page's JSON documents,
`requires_page_fetch`), sends the recipe's request through the SSRF guard and answers a
`FetchResult(method='api')` whose body is the JSON. A detail page's answer serves it only when it states the requested
product (`acquire.identity.identifies`): an endpoint that answers one product for every
page is an `error`, `not_the_product`. A page the recipe cannot serve is an `error` result, and `acquire`
fetches it the configured way; when the rung fetched the HTML page to bind and reached it, that page is the
result instead, so it is not fetched twice. A listing's pages come from `pages`, which follows the recipe's pagination
(`pagination.py`: a page number or offset, a cursor, a next URL or an RFC 8288 `next` link, in the query or the JSON
body) until a response's `rows` select nothing or the caller's records target is reached: this is how infinite scroll
is served. `recipe_collector` reads what the rung fetched.

A custom recipe (`Custom`, a config with URL patterns) serves in this order, as one fetch whose requests share one
cookie jar (`acquire.cookies.shared`): its warm-up (a fixed URL, sent raw; or the page itself, the HTML page fetch),
then the HTML page when a binder or a page field reads it, then the main call, then each extra call. A call's
parameters are bound from the page URL, the URL pattern's parameters, the warm-up's answer and, for an extra call, the
main call's (`binding.Served`). A detail answer of any call is kept only when it identifies the item (as above); a call
that binds nothing is not sent, and an extra call's failure leaves its fields to the page. What the recipe fetched
beside the main answer joins the `api` capture as marked network bodies (`storage.capture_bundle.recipe_call`,
`RECIPE_PAGE`), and every attempt is marked with the recipe's config id (`AttemptRecord.recipe`).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue

from snowcrawl.acquire.api import JsonFetcher, JsonRequest
from snowcrawl.acquire.cookies import FetchCookies, shared
from snowcrawl.acquire.identity import identified, with_call
from snowcrawl.acquire.outcome import Content, fetch_result
from snowcrawl.acquire.rungs import Rung, slow_down, with_attempts
from snowcrawl.acquire.types import BlockClass, FetchPolicy, FetchResult
from snowcrawl.collect import json_doc
from snowcrawl.collect.api import ApiCollector
from snowcrawl.collect.config import ConfigExecutor, Executed
from snowcrawl.collect.network_match import LearnedCalls
from snowcrawl.config_ir.api_recipe.binding import (
    CallAnswer,
    PageData,
    Served,
    page_data,
    render_url,
    resolve_params,
)
from snowcrawl.config_ir.api_recipe.model import PLACEHOLDER, ApiRecipe
from snowcrawl.config_ir.api_recipe.pagination import first_request, next_request
from snowcrawl.config_ir.api_recipe.replay import ANY_TYPE, request_from
from snowcrawl.config_ir.model import SentBy, WarmUp
from snowcrawl.config_ir.url_pattern import match, most_specific
from snowcrawl.content.page import ParsedPage
from snowcrawl.core import json_text
from snowcrawl.db.models import FieldStatus
from snowcrawl.normalize.base import Context
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.storage.capture_bundle import RECIPE_PAGE, NetworkBody, decode_html, recipe_call, recipe_call_name

# A listing served by its recipe sends at most this many page requests: the records target ends it
# sooner, and a pagination that never ends (a cursor that comes back) is cut here.
MAX_PAGES: Final = 50


def recipe_collector(recipe: ApiRecipe, recipe_id: str) -> ApiCollector:
    """The collector of the captures `recipe` fetches; `recipe_id` names it in candidate locators."""
    rows = None if recipe.pagination is None else recipe.pagination.rows
    return ApiCollector(recipe_id, recipe.map, rows, recipe.answer)


@dataclass(frozen=True, slots=True)
class Custom:
    """What a custom recipe serves beyond its main call (module docstring): its config id (`recipe_id`), its URL
    patterns, its extra calls by name, its warm-up, whether a field is read from the HTTP page (`page_fields`), the
    least interval between its requests, in milliseconds (`rate_ms`), and what sends its calls (`sent_by`: the JSON
    rung, or the browser from the site's warm page, `acquire.browser_send`)."""

    recipe_id: str
    patterns: tuple[str, ...]
    calls: Mapping[str, ApiRecipe] = field(default_factory=dict)
    warm_up: WarmUp | None = None
    page_fields: bool = False
    rate_ms: int | None = None
    sent_by: SentBy = "http"

    @property
    def page_warm_up(self) -> bool:
        """Whether the warm-up is the page itself."""
        return self.warm_up is not None and self.warm_up.url is None

    def fetches_page(self, main: ApiRecipe) -> bool:
        """Whether serving fetches the HTML page: it is the warm-up, a field is read from it, or a binder reads its
        JSON documents."""
        reads = main.requires_page_fetch or any(call.requires_page_fetch for call in self.calls.values())
        return self.page_warm_up or self.page_fields or reads

    def warm_url(self, url: str) -> str:
        """The URL the site's warm page opens at for the page at `url` (`sent_by = browser`): the fixed warm-up's, when
        the pattern binds each of its parameters, else the page itself."""
        return self.warm_up_url(self.pattern_params(url)) or url

    def warm_up_url(self, params: Mapping[str, str]) -> str | None:
        """The fixed warm-up's URL with the pattern's `params`; None without one, or when they leave a placeholder of
        it unbound."""
        warm_up = None if self.warm_up is None else self.warm_up.url
        if warm_up is None or not set(PLACEHOLDER.findall(warm_up)) <= set(params):
            return None
        return render_url(warm_up, params)

    def pattern_params(self, url: str) -> dict[str, str]:
        """The parameters the most specific of the patterns binds from `url` (none when no pattern matches it)."""
        chosen = most_specific(sorted(self.patterns), url)
        return {} if chosen is None else match(chosen, url) or {}


@dataclass(frozen=True, slots=True)
class RecipeExecutor:
    """The `ConfigExecutor` of the captures a config's recipe fetched: the recipe's collector, with the status of the
    config's fields the recipe maps, which role C is then not asked for. A custom recipe's capture also holds its
    extra calls' answers, each read by its call's collector (`calls`), and the HTML page its page fields read
    (`page`, the config's executor of those fields); `custom` is what it serves."""

    recipe: ApiRecipe
    collector: ApiCollector
    field_status: Mapping[str, FieldStatus]
    custom: Custom | None = None
    calls: Mapping[str, ApiCollector] = field(default_factory=dict)
    page: ConfigExecutor | None = None

    @classmethod
    def of(
        cls,
        recipe: ApiRecipe,
        recipe_id: str,
        config_status: Mapping[str, FieldStatus],
        custom: Custom | None = None,
        page: ConfigExecutor | None = None,
    ) -> "RecipeExecutor":
        calls = {} if custom is None else custom.calls
        # An extra call's locators name the config and the call: `.` is neither `:` nor whitespace.
        collectors = {name: recipe_collector(call, f"{recipe_id}.{name}") for name, call in calls.items()}
        # An absent field is no recipe's either, and role C is not asked for it on a recipe's pages. A custom recipe
        # serves every field of its config.
        status = {
            name: s for name, s in config_status.items() if custom is not None or name in recipe.map or s == "absent"
        }
        return cls(recipe, recipe_collector(recipe, recipe_id), status, custom, collectors, page)

    @property
    def browser_fields(self) -> frozenset[str]:
        return frozenset()  # a recipe's endpoint answers over HTTP

    @property
    def learned_calls(self) -> LearnedCalls | None:
        return None  # it reads the recipe's own `api` captures, whose bodies are its own calls' answers

    def execute(self, page: ParsedPage, schema: SchemaDef, ctx: Context) -> Executed:
        parts = [self.collector.collect(page, schema, ctx)]
        for body, entry in zip(page.network, page.network_entries, strict=False):
            name = recipe_call_name(entry.data_call)
            if name is not None and (collector := self.calls.get(name)) is not None:
                json_body = collector.answer == "json"
                answer = ParsedPage(_text(body, entry, html=not json_body), json_body=json_body, url=entry.url)
                parts.append(collector.collect(answer, schema, ctx))
            elif entry.data_call == RECIPE_PAGE and self.page is not None:
                parts.append(self.page.execute(ParsedPage(_text(body, entry, html=True), url=entry.url), schema, ctx))
        if len(parts) == 1:
            return parts[0]
        return Executed(
            rows=tuple(row for part in parts for row in part.rows),
            candidates=tuple(found for part in parts for found in part.candidates),
            refusals=tuple(refusal for part in parts for refusal in part.refusals),
            notes=tuple(note for part in parts for note in part.notes),
        )


@dataclass(frozen=True, slots=True)
class RecipeRung:
    """`recipe` bound to `fetch_json` (the job's guard and engine version) and to `fetch_page`, the HTTP rung that
    fetches the HTML page when the recipe reads the page's JSON documents; `custom`, what a custom recipe serves beyond
    its main call (module docstring)."""

    recipe: ApiRecipe
    fetch_json: JsonFetcher
    fetch_page: Rung
    engine_version: str
    custom: Custom | None = None

    async def __call__(self, url: str, policy: FetchPolicy) -> FetchResult:
        """The answer the recipe's endpoint gives for the page at `url`, with a custom recipe's extra calls."""
        jar = await self._jar()
        bound = await self._request(url, policy, jar)
        if isinstance(bound, FetchResult):
            return self._marked(bound)
        result = (
            self._unserved(url, policy, "unbound_parameter")
            if bound.request is None
            else await _in(jar, self.fetch_json(bound.request, policy))
        )
        if self.recipe.pagination is None:
            result = identified(result, url, bound.keys, html=self.recipe.answer == "html")
        result = await self._with_calls(url, policy, jar, bound, result)
        # A fetch whose policy names the browser falls back to the browser, not to the HTML page fetched to bind; a
        # custom recipe's to the adapter and the page rungs after it (`acquire`), not to its own page fetch.
        fallback = policy.method_hint != "browser" and self.custom is None
        return self._marked(bound.after(result, fallback=fallback))

    async def pages(
        self,
        url: str,
        policy: FetchPolicy,
        *,
        records: int | None,
        limit: int = MAX_PAGES,
        deadline: float | None = None,
    ) -> AsyncIterator[FetchResult]:
        """The listing at `url`, one response per page its pagination names (`next_request` of `pagination.py`), until a
        response's `rows` select no object, the pagination names no next page (or one already requested), `records`
        rows were yielded (None: no row target), `limit` requests were sent, or `deadline` (`time.monotonic()`) has
        passed. Each request waits for its host's rate in `fetch_json` (`services.fetching.recipe_rung`). A response
        that is not `ok` is yielded last; so is one with no rows, as an unserved `error` result that carries its
        attempts: every request sent counts as usage, and a first page without rows is one the recipe did not serve.
        A custom recipe's extra calls join the first page."""
        pagination = self.recipe.pagination
        if pagination is None:
            raise ValueError("a recipe without pagination serves one response per page")
        jar = await self._jar()
        first = await self._first(url, policy, jar)
        if isinstance(first, FetchResult):
            yield self._marked(first)
            return
        bound, request = first
        sent: set[tuple[str, str | None]] = set()
        rows = 0
        for number in range(limit):
            sent.add((request.url, request.body))
            result = await _in(jar, self.fetch_json(request, policy))
            # What serving fetched before the first request is counted with the first response.
            result = bound.after(result) if number == 0 else result
            if result.block_class != "ok":
                yield self._marked(result)
                return
            # Parsing is CPU work: a thread keeps the event loop serving other tasks meanwhile.
            document, count = await asyncio.to_thread(_rows, result, pagination.rows)
            if not count:
                unserved = self._unserved(url, policy, "no_rows")
                yield self._marked(with_attempts(unserved, result.attempts, result.duration_ms))
                return
            if number == 0:
                result = await self._with_calls(url, policy, jar, bound, result)
            yield self._marked(result)
            rows += count
            headers = result.bundle.manifest.headers.response
            following = next_request(pagination, request, number + 1, document, headers)
            if following is None or (following.url, following.body) in sent or _done(rows, records, deadline):
                return
            request = following

    async def _first(
        self, url: str, policy: FetchPolicy, jar: FetchCookies | None
    ) -> "tuple[_Bound, JsonRequest] | FetchResult":
        """A listing's first request and what serving fetched before it; else the result that ends the listing."""
        pagination = self.recipe.pagination
        bound = await self._request(url, policy, jar)
        if isinstance(bound, FetchResult):
            return bound
        request = None if bound.request is None or pagination is None else first_request(pagination, bound.request)
        if request is None:
            return bound.after(self._unserved(url, policy, "unbound_parameter"))
        return bound, request

    async def _jar(self) -> FetchCookies | None:
        """A custom recipe's one jar for its requests (`acquire.cookies.shared`); None: each request has its own."""
        return None if self.custom is None else await FetchCookies.new()

    async def _request(self, url: str, policy: FetchPolicy, jar: FetchCookies | None) -> "_Bound | FetchResult":
        """The recipe's request for the page, with what serving fetched before it (`_Bound`); when a fetch before it
        did not reach its answer, an `error` result, or that fetch itself when it asked for fewer requests."""
        custom = self.custom
        served = Served() if custom is None else Served(pattern=custom.pattern_params(url))
        warm = await self._warm_up(url, policy, jar, served)
        if warm is not None and warm.block_class != "ok":
            return warm if slow_down(warm) else _preceded(warm, self._unserved(url, policy, "warm_up_failed"))
        if warm is not None:
            served = replace(served, warm_up=CallAnswer(warm.bundle.text))
        fetched = await self._page(url, policy, jar)
        if isinstance(fetched, FetchResult):
            return _preceded(warm, fetched)
        page, parsed, data = fetched
        if page is not None and custom is not None and custom.page_warm_up:
            served = replace(served, warm_up=CallAnswer(page.bundle.text, parsed))  # the page is the warm-up
        # A header the recipe references is the caller's (`ContextHeader`): sent with the value the policy's context
        # holds, and without one the parameter is unbound. Binding may parse an answer: CPU work, on a thread.
        request, keys = await asyncio.to_thread(_bind, self.recipe, url, data, policy, served)
        return _Bound(request, keys, data, served, page, warm)

    async def _page(
        self, url: str, policy: FetchPolicy, jar: FetchCookies | None
    ) -> tuple[FetchResult | None, ParsedPage | None, PageData] | FetchResult:
        """The HTML page, its parse and its JSON documents when serving reads it (none otherwise); when that fetch did
        not reach the page, an `error` result, or the page fetch itself when it asked for fewer requests."""
        custom = self.custom
        if not (self.recipe.requires_page_fetch if custom is None else custom.fetches_page(self.recipe)):
            return None, None, {}
        page = await _in(jar, self.fetch_page(url, policy))
        if slow_down(page):
            return page  # a 429 or 503 asks for fewer requests: `acquire` keeps it, with no fallback request
        if page.block_class != "ok" and not self._read_as_is(page):
            return _after(page, self._unserved(url, policy, f"page_{page.block_class}"))
        # Parsing is CPU work: a thread keeps the event loop serving other tasks meanwhile.
        return (page, *await asyncio.to_thread(_page_data, page))

    def _read_as_is(self, page: FetchResult) -> bool:
        """Whether a page the HTTP rung classed `shell` is still read for a custom recipe's page fields: a shell says
        a render would state more (its script state, its unrendered text), not that the document states nothing, and
        a step that finds nothing in it publishes nothing (C7). One a challenge labels is a stand-in for the page."""
        return (
            self.custom is not None
            and self.custom.page_fields
            and page.block_class == "shell"
            and page.attempts[-1].challenge is None
        )

    async def _warm_up(
        self, url: str, policy: FetchPolicy, jar: FetchCookies | None, served: Served
    ) -> FetchResult | None:
        """The answer of a custom recipe's warm-up at a fixed URL, sent as the page's script would send it and read
        whatever it is (raw); None without one (the page itself is fetched as the HTML page). A URL whose parameters
        the pattern did not bind is not sent: an `error` result."""
        custom = self.custom
        if custom is None or custom.warm_up is None or custom.warm_up.url is None:
            return None
        target = custom.warm_up_url(served.pattern)
        if target is None:
            return self._unserved(url, policy, "unbound_parameter")
        request = JsonRequest(target, url, headers=(("Accept", ANY_TYPE),), raw=True)
        return await _in(jar, self.fetch_json(request, policy))

    async def _with_calls(
        self, url: str, policy: FetchPolicy, jar: FetchCookies | None, bound: "_Bound", main: FetchResult
    ) -> FetchResult:
        """The `ok` main answer with each extra call's `ok` answer that identifies the item (a detail page's) and the
        HTML page a page field reads, as marked network bodies, and every attempt. A call that binds nothing is not
        sent; after one that asks for fewer requests none is."""
        custom = self.custom
        if custom is None or main.block_class != "ok":
            return main
        served = replace(bound.served, main=CallAnswer(main.bundle.text))
        result = main
        for name, call in custom.calls.items():
            request, keys = await asyncio.to_thread(_bind, call, url, bound.data, policy, served)
            if request is None:
                continue
            sent = await _in(jar, self.fetch_json(request, policy))
            if self.recipe.pagination is None:
                sent = identified(sent, url, keys, html=call.answer == "html")
            attempts, duration_ms = result.attempts + sent.attempts, result.duration_ms + sent.duration_ms
            if sent.block_class == "ok":
                mark = recipe_call(name)
                result = with_call(result, sent, request.url, mark, method=request.method, request_body=request.body)
            result = with_attempts(result, attempts, duration_ms)
            if slow_down(sent):
                break
        if custom.page_fields and bound.page is not None:
            result = with_call(result, bound.page, bound.page.bundle.manifest.final_url, RECIPE_PAGE)
        return result

    def _marked(self, result: FetchResult) -> FetchResult:
        """`result` with every attempt marked with the custom recipe's config id (`AttemptRecord.recipe`)."""
        if self.custom is None:
            return result
        recipe = self.custom.recipe_id
        marked = tuple(attempt.model_copy(update={"recipe": recipe}) for attempt in result.attempts)
        return with_attempts(result, marked, result.duration_ms)

    def _unserved(self, url: str, policy: FetchPolicy, reason: str) -> FetchResult:
        return unserved(url, policy, self.engine_version, "error", reason)


@dataclass(frozen=True)
class _Bound:
    """A recipe's request for the page (None when a parameter cannot be bound) and the values it was bound with; the
    page's JSON documents and what a custom recipe's binders read; the HTML `page` and the warm-up answer (`warm`)
    serving fetched before it."""

    request: JsonRequest | None
    keys: tuple[str, ...]
    data: PageData
    served: Served
    page: FetchResult | None = None
    warm: FetchResult | None = None

    def after(self, result: FetchResult, *, fallback: bool = False) -> FetchResult:
        """`result` with the fetches before it among its attempts (`_after`)."""
        return _preceded(self.warm, _after(self.page, result, fallback=fallback))


async def _in(jar: FetchCookies | None, request: Awaitable[FetchResult]) -> FetchResult:
    """`request` sent with `jar`, when there is one (`acquire.cookies.shared`)."""
    with nullcontext() if jar is None else shared(jar):
        return await request


def _preceded(earlier: FetchResult | None, result: FetchResult) -> FetchResult:
    """`result` with the attempts of the `earlier` fetch first."""
    if earlier is None:
        return result
    return with_attempts(result, earlier.attempts + result.attempts, earlier.duration_ms + result.duration_ms)


def _done(rows: int, records: int | None, deadline: float | None) -> bool:
    """Whether the rows yielded reach the row target, or the deadline has passed."""
    return (records is not None and rows >= records) or (deadline is not None and time.monotonic() >= deadline)


def unserved(url: str, policy: FetchPolicy, engine_version: str, block_class: BlockClass, reason: str) -> FetchResult:
    """The recipe's result for the page at `url` when it sent no request: `reason` says why."""
    return fetch_result(
        url=url,
        method="api",
        policy=policy,
        engine_version=engine_version,
        fetched_at=datetime.now(UTC),
        started=time.monotonic(),
        exchange=None,
        content=Content(body=b""),
        block_class=block_class,
        error=reason,
    )


def _after(page: FetchResult | None, result: FetchResult, *, fallback: bool = False) -> FetchResult:
    """`result` with the HTML page fetch before it among its attempts. With `fallback`, a `result` that did not serve
    the page, and asked for no fewer requests, gives way to the HTML page when that fetch reached it: it is the page
    `acquire` would fetch again over HTTP, one more request."""
    if page is None:
        return result
    unserved = result.block_class != "ok" and not slow_down(result)
    kept = page if fallback and unserved and page.block_class == "ok" else result
    return with_attempts(kept, page.attempts + result.attempts, page.duration_ms + result.duration_ms)


def _page_data(page: FetchResult) -> tuple[ParsedPage, PageData]:
    parsed = ParsedPage.of(page.bundle)
    return parsed, page_data(parsed)


def _bind(
    spec: ApiRecipe, url: str, data: PageData, policy: FetchPolicy, served: Served
) -> tuple[JsonRequest | None, tuple[str, ...]]:
    """`spec`'s request for the page (None when a parameter, or a header it takes from the policy's context, has no
    value), and the values its parameters were bound with (the identity check's keys)."""
    params = resolve_params(spec, url, data, served)
    if params is None:
        return None, ()
    return request_from(spec, url, params, policy.context), tuple(params.values())


def _text(body: bytes, entry: NetworkBody, *, html: bool) -> str:
    """A marked body's text, decoded by its own headers; an HTML one (the page, an HTML answer) as a page is, so a
    `meta` charset counts when its Content-Type names none (HTML Standard §13.2.3.2)."""
    return decode_html(body, entry.response_headers or (), "http" if html else "api")


def _rows(result: FetchResult, query: str) -> tuple[JsonValue, int]:
    """The response's JSON and how many rows `query` selects in it."""
    try:
        document = json_text.parse(result.bundle.body)
    except ValueError:  # not UTF-8 JSON (RFC 8259 §8.1): the collector reads no rows from it either
        return None, 0
    return document, row_count(query, document)


def row_count(query: str, document: JsonValue) -> int:
    """How many rows `query` selects in `document`: the collector reads only rows that are JSON objects (RFC 8259 §4,
    `collect.api`), so other items are no rows."""
    return sum(isinstance(row, dict) for _, row in json_doc.select(query, document))
