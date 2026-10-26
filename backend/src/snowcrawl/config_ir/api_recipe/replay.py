"""Recipe replay: send a recipe's request for a captured page over `fetch_json` (so through the
SSRF guard) and check that the response reproduces the page's published values through the recipe's map.

A detail recipe's answer must also state the page's product (the identity check), as
serving requires (`execute`). Header minimization replays the sample pages; held-out validation replays the held-out
ones the same way. A listing
recipe (one with `pagination`) is replayed for its first two pages, the second built by its pagination as serving
builds it (`next_request` of `pagination.py`), and each must give the rows the page's captured calls gave: the same
rows in the same order, each mapped field's raw value equal (Appendix A #59).
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from pydantic import JsonValue

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.acquire.identity import identifies
from snowcrawl.acquire.types import FetchResult, RequestContext
from snowcrawl.collect import json_doc
from snowcrawl.collect.api import read_map
from snowcrawl.collect.network_match import FORM_TYPE
from snowcrawl.config_ir.api_recipe.binding import PageData, Sample, Served, render_body, render_url, resolve_params
from snowcrawl.config_ir.api_recipe.model import PAGE_URL, ContextHeader, Pagination, RequestSpec
from snowcrawl.config_ir.api_recipe.pagination import first_request, next_request
from snowcrawl.core import json_text
from snowcrawl.db.models import JobId, PageKind
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import equal, normalize
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.storage.capture_bundle import CaptureBundle, HeaderList

# Sends one request: `fetch_json` with the job's policy and engine version.
Replay = Callable[[JsonRequest], Awaitable[FetchResult]]
Scalar = str | int | float
# Renders a page in the browser with network capture, a listing with its browser actions:
# the document states kept, none when the page was not reached or may not be fetched. Learning renders the pages whose
# captures hold no calls to learn from.
Render = Callable[[str, PageKind], Awaitable[Sequence[CaptureBundle]]]
# A learning run's `Replay`, for its job, whose usage the requests count as.
Replays = Callable[[JobId], Replay]
# Fetch §4.1 (fetch, step 12): the `Accept` a script's request sends when it names none, as a call answering HTML does.
ANY_TYPE: Final = "*/*"
# Appendix A #59: a listing recipe is validated on its first two pages, the one the page loaded and the next.
REPLAYED_PAGES: Final = 2


@dataclass(frozen=True)
class ReplayPage:
    """A captured page: its URL, endpoint request and JSON documents, its published values (`record.data`) and the
    normalize context of the page; for a listing, the responses of its first captured calls of the endpoint (pages 1
    and 2, when the page made a second call), which a listing recipe's replay must reproduce; and the caller's request
    context a replay takes referenced headers from (`ContextHeader`), when the run has one."""

    sample: Sample
    values: Mapping[str, JsonValue]
    ctx: Context
    listing: tuple[JsonValue, ...] = ()
    context: RequestContext | None = None


def request_for(
    spec: RequestSpec,
    page_url: str,
    page_data: PageData,
    context: RequestContext | None = None,
    served: Served | None = None,
) -> JsonRequest | None:
    """The request `spec` makes for the page, a header it takes from the caller's request context with the value
    `context` holds; `None` when a parameter, or such a header, has no value there. The context goes only to the page's
    own host, so a recipe that references it on another host makes no request. A custom recipe's binders read what
    serving it fetched (`served`); a call that answers HTML is sent raw (`JsonRequest.raw`)."""
    params = resolve_params(spec, page_url, page_data, served)
    return None if params is None else request_from(spec, page_url, params, context)


def request_from(
    spec: RequestSpec, page_url: str, params: Mapping[str, str], context: RequestContext | None = None
) -> JsonRequest | None:
    """`request_for` with the parameters already read (`resolve_params`)."""
    url = render_url(spec.url, params)
    own_host = urlsplit(url).hostname == urlsplit(page_url).hostname
    supplied = {} if context is None or not own_host else {name.lower(): value for name, value in context.headers}
    headers: list[tuple[str, str]] = []
    for header in spec.headers:
        if isinstance(header, ContextHeader):
            if (secret := supplied.get(header.name.lower())) is None:
                return None
            headers.append((header.name, secret.get_secret_value()))
        else:  # `{page_url}` is the one placeholder a header value takes
            headers.append((header.name, header.value.replace(f"{{{PAGE_URL}}}", page_url)))
    body = None if spec.body is None else render_body(spec.body.template, params, spec.body.encoding)
    return JsonRequest(url, page_url, spec.method, _with_defaults(spec, headers), body, raw=spec.answer == "html")


def _with_defaults(spec: RequestSpec, headers: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """`headers` with the `Content-Type` of a form body and the `Accept` of a call answering HTML, where the recipe
    names neither."""
    named = {name.lower() for name, _ in headers}
    defaults = []
    if spec.body is not None and spec.body.encoding == "form" and "content-type" not in named:
        defaults.append(("Content-Type", FORM_TYPE))
    if spec.answer == "html" and "accept" not in named:
        defaults.append(("Accept", ANY_TYPE))
    return (*headers, *defaults)


async def reproduces(
    spec: RequestSpec, fields: Mapping[str, str], page: ReplayPage, schema: SchemaDef, replay: Replay
) -> bool:
    """Whether replaying `spec` for `page` reproduces every field `compare` compares there, and at least one."""
    compared = await compare(spec, fields, page, schema, replay)
    return bool(compared) and all(compared.values())


async def compare(
    spec: RequestSpec, fields: Mapping[str, str], page: ReplayPage, schema: SchemaDef, replay: Replay
) -> dict[str, bool]:
    """For each `fields` entry the page has a published value for, whether replaying `spec` for `page` answers JSON
    whose value at its path is `equal` to that value; every entry is false when the request cannot be made, or its
    answer is not JSON or does not state the page's product (`acquire.identity.identifies`: a parameter value, or a
    URL with the page's path; serving refuses such an answer, `execute`), and nothing is sent when no field is
    compared."""
    if spec.pagination is not None:
        return await _compare_listing(spec, spec.pagination, fields, page, replay)
    compared = {name: path for name, path in fields.items() if page.values.get(name) is not None}
    request = request_for(spec, page.sample.page_url, page.sample.page_data, page.context)
    if not compared or request is None:
        return dict.fromkeys(compared, False)
    result = await replay(request)
    if result.block_class != "ok":
        return dict.fromkeys(compared, False)
    try:
        document = json_text.parse(result.bundle.body)
    except ValueError:  # UnicodeDecodeError is a ValueError
        return dict.fromkeys(compared, False)
    params = resolve_params(spec, page.sample.page_url, page.sample.page_data) or {}
    if not identifies(document, page.sample.page_url, params.values()):
        return dict.fromkeys(compared, False)
    return {name: _equal(read_map(path, document), name, page, schema) for name, path in compared.items()}


async def _compare_listing(
    spec: RequestSpec, pagination: Pagination, fields: Mapping[str, str], page: ReplayPage, replay: Replay
) -> dict[str, bool]:
    """For each `fields` entry any captured page has a value for, whether replaying the listing's first pages gives
    every captured page's rows with the same raw values there."""
    captured = page.listing[:REPLAYED_PAGES]
    wanted = [row_values(pagination.rows, fields, document) for document in captured]
    compared = dict.fromkeys(_valued(fields, wanted), True)
    bound = request_for(spec, page.sample.page_url, page.sample.page_data, page.context)
    request = None if bound is None else first_request(pagination, bound)
    for number, want in enumerate(wanted):
        document = None if request is None else await _replayed(request, replay)
        if request is None or document is None:
            return dict.fromkeys(compared, False)
        got = row_values(pagination.rows, fields, document[0])
        for name in compared:
            compared[name] = compared[name] and got[name] == want[name]
        request = next_request(pagination, request, number + 1, document[0], document[1])
    return compared


def _valued(fields: Mapping[str, str], wanted: Sequence[Mapping[str, list[Scalar | None]]]) -> list[str]:
    """The `fields` some captured page's rows have a value for."""
    return [name for name in fields if any(value is not None for want in wanted for value in want[name])]


async def _replayed(request: JsonRequest, replay: Replay) -> tuple[JsonValue, HeaderList] | None:
    """The replayed response's JSON with its headers; None when it failed or is not JSON."""
    result = await replay(request)
    if result.block_class != "ok":
        return None
    try:
        return json_text.parse(result.bundle.body), result.bundle.manifest.headers.response
    except ValueError:  # UnicodeDecodeError is a ValueError
        return None


def row_values(rows: str, fields: Mapping[str, str], document: JsonValue) -> dict[str, list[Scalar | None]]:
    """Each field's raw value in every row `rows` selects in `document` (an object row, read relative to it), in row
    order."""
    found: dict[str, list[Scalar | None]] = {name: [] for name in fields}
    for _, row in json_doc.select(rows, document):
        if isinstance(row, dict):
            for name, path in fields.items():
                found[name].append(read_map(path, row))
    return found


def _equal(raw: str | int | float | None, name: str, page: ReplayPage, schema: SchemaDef) -> bool:
    if raw is None:
        return False
    spec = schema.fields[name]
    result = normalize(raw, spec, page.ctx)
    return isinstance(result, Ok) and equal(result.value, page.values[name], spec)
