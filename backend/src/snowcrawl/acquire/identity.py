"""Identity-checked data calls: a JSON (or HTML) answer fetched for one product, kept only when
it states that product.

A data call is a request for a product's JSON that the fetch makes beside the product's page: the product JSON a
platform documents for a detail page (`adapters.platforms.product_json`), or an API recipe's request
(`config_ir.api_recipe.execute`). Before its answer is used it must state the requested product (the identity check):
a string or number equal to a key the request was made with (a handle, a slug, a product id), or a URL whose path is the
page's path; an HTML answer also by its canonical link, its `og:url` or a link of it, relative ones resolved against its
document base URL (`page_refs`). An answer that does not is dropped and recorded on its attempt as
`NOT_THE_PRODUCT`: an endpoint that ignores its key answers one product for every page.

A documented platform answer that passes joins the page's HTTP capture as one more network body, marked with its
platform (the network body's `data_call`), which the extraction reads as that platform documents it
(`collect.platform_json`).
"""

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import Final
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html
from pydantic import JsonValue

from snowcrawl.acquire.outcome import as_error
from snowcrawl.acquire.page_refs import page_refs
from snowcrawl.acquire.types import FetchResult
from snowcrawl.core import json_text
from snowcrawl.core.ids import uuid7
from snowcrawl.storage.capture_bundle import CaptureBundle, NetworkBody, network_file, network_json

__all__ = ["MIN_KEY", "NOT_THE_PRODUCT", "identified", "identifies", "joined", "joined_call", "with_call"]

# The reason of a data call's attempt whose answer does not state the requested product.
NOT_THE_PRODUCT: Final = "not_the_product"
# A key shorter than this names no product (a locale, a two-letter path prefix): it is no evidence of identity.
MIN_KEY: Final = 4
# The most scalars of an answer the identity check reads: a product's JSON holds its key near the top.
_MAX_SCALARS: Final = 20_000


def identifies(answer: JsonValue, page_url: str, keys: Iterable[str]) -> bool:
    """Whether the JSON `answer` states the product of the page at `page_url` requested by `keys` (module docstring);
    a key shorter than `MIN_KEY` is not compared."""
    return _identifies(_scalars(answer), page_url, keys)


def _identifies(scalars: Iterable[str | int | float], page_url: str, keys: Iterable[str]) -> bool:
    wanted = {unquote(key).casefold() for key in keys if len(key) >= MIN_KEY}
    path = urlsplit(page_url).path.rstrip("/")
    for value in scalars:
        text = str(value)
        if text.casefold() in wanted or (len(path) > 1 and _path_of(page_url, text) == path):
            return True
    return False


def _path_of(page_url: str, text: str) -> str | None:
    """The path of `text` as a URL reference resolved against the page (RFC 3986 §5), when it is an absolute URL or an
    absolute path."""
    if not text.startswith(("http://", "https://", "/")):
        return None
    try:
        return urlsplit(urljoin(page_url, text)).path.rstrip("/")
    except ValueError:
        return None


def _scalars(answer: JsonValue) -> Iterator[str | int | float]:
    """The strings and numbers of `answer` (a boolean or null is no key), walked with a stack, up to `_MAX_SCALARS`."""
    stack, seen = [answer], 0
    while stack and seen < _MAX_SCALARS:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            seen += 1
            yield value


def _html_scalars(text: str) -> Iterator[str]:
    """The text nodes and attribute values of an HTML answer's elements (HTML Standard §13.2), stripped, up to
    `_MAX_SCALARS`: where an HTML document states a key (a text, a `data-*` value, a link's `href`)."""
    try:
        root = lxml_html.document_fromstring(text)
    except (etree.ParserError, ValueError):
        return
    values = (
        value.strip()
        for element in root.iter(etree.Element)
        for value in (*element.attrib.values(), element.text or "", element.tail or "")
    )
    yield from islice(filter(None, values), _MAX_SCALARS)


def _names_page(answer: FetchResult, page_url: str) -> bool:
    """Whether the HTML `answer` names the page at `page_url`: its canonical link (RFC 6596), its `og:url` (Open Graph
    protocol: the object's canonical URL) or one of its links (HTML Standard §4.6 `a href`), each resolved against its
    document base URL (RFC 3986 §5; HTML Standard §2.4.1), has the page's path. The answer's own URL names nothing:
    it is the request's."""
    refs = page_refs(answer)
    return _identifies((*refs.own[1:], *refs.links), page_url, ())


def joined(page: FetchResult, call: FetchResult, key: str, platform: str) -> tuple[FetchResult | None, FetchResult]:
    """The HTTP `page` with the `ok` documented answer `call` of `platform` as one more network body, marked with the
    platform, as a capture of its own (a new id), when the answer states the product (`identifies`), and the call's
    result; else None, and the call as an error, `NOT_THE_PRODUCT`. The page keeps its
    attempts; the caller adds the call's."""
    if call.block_class != "ok":
        return None, call
    try:
        answer = network_json(call.bundle.body)
    except ValueError:
        return None, as_error(call, NOT_THE_PRODUCT)
    if not identifies(answer, page.bundle.manifest.final_url, (key,)):
        return None, as_error(call, NOT_THE_PRODUCT)
    return with_call(page, call, call.bundle.manifest.final_url, platform), call


def with_call(
    page: FetchResult,
    call: FetchResult,
    url: str,
    platform: str | None = None,
    *,
    method: str = "GET",
    request_body: str | None = None,
) -> FetchResult:
    """The HTTP `page` with the answer of the companion `call` it made (a `method` request with `request_body`) as one
    more network body at `url`, marked with the `platform` whose documented JSON it is (None for an eager fragment,
    `content.fragments`, or a located field source's call), as a capture of its own (a new id)."""
    bundle = joined_call(page.bundle, call.bundle, url, platform, method=method, request_body=request_body)
    return page.model_copy(update={"bundle": bundle})


def joined_call(
    page: CaptureBundle,
    call: CaptureBundle,
    url: str,
    platform: str | None = None,
    *,
    method: str = "GET",
    request_body: str | None = None,
) -> CaptureBundle:
    """`with_call`'s capture: `page` with `call`'s answer as one more network body, under a new capture id."""
    sent = call.manifest
    content_type = next((value for name, value in sent.headers.response if name.lower() == "content-type"), None)
    manifest = page.manifest
    entry = NetworkBody(
        url=url,
        status=sent.status_code,
        content_type=content_type,
        sha256=sent.html_sha256,
        file=network_file(len(page.network)),
        method=method,
        request_body=request_body,
        request_headers=sent.headers.request,
        response_headers=sent.headers.response,
        resource_type="fetch",
        action=0,
        data_call=platform,
    )
    return CaptureBundle(
        # Another capture than the HTTP page's: a new id, so no parse of the page alone stands for it.
        manifest=manifest.model_copy(
            update={"capture_id": uuid7(), "network_bodies": (*manifest.network_bodies, entry)}
        ),
        body=page.body,
        network=(*page.network, call.body),
        raw_body=page.raw_body,
    ).decoded(page.text)  # the page's body, read as the page's


def identified(result: FetchResult, url: str, keys: tuple[str, ...], *, html: bool = False) -> FetchResult:
    """A data call's `ok` answer when it states the product of the page at `url` it was requested with `keys` for
    (`identifies`: a parameter value, or a URL with the page's path); else an `error`, `NOT_THE_PRODUCT`, which is not
    used: a recipe's page is then served by its configured fetch, a companion call joins nothing. An `html` answer
    states it in a text node or an attribute value (`_html_scalars`), or names the page by a URL it states about itself
    or links to (`_names_page`). An answer that is not JSON is the collector's to report."""
    if result.block_class != "ok":
        return result
    if html:
        found = _identifies(_html_scalars(result.bundle.text), url, keys) or _names_page(result, url)
        return result if found else as_error(result, NOT_THE_PRODUCT)
    try:
        answer = json_text.parse(result.bundle.body)
    except ValueError:
        return result
    return result if identifies(answer, url, keys) else as_error(result, NOT_THE_PRODUCT)
