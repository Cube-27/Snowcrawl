"""What a browser render captures besides its document: the JSON and Flight
bodies the page loads and the HTML fragments it fetches from its own site, the network data-source layer's input
(Appendix A #59) and API discovery's (§7.2 step 1); the resources it loaded, counted; the rendered area of a listing's
item blocks; and the screenshot or PDF the policy asks for.

An engineer capture (`FetchPolicy.calls` `all`) keeps every call instead, which an engineer chooses a custom recipe's
calls from: each fetch and XMLHttpRequest answer on any site, and each document the main frame navigates to after a
page step (a form submit), unless its answer is an event stream or binary (`_UNKEPT`), within larger caps
(`CAPS`); its own site's calls first.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Final, cast
from urllib.parse import urlsplit

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import Frame, Page, Request, Response

from snowcrawl.acquire.headers import recorded
from snowcrawl.acquire.items import element_path, item_groups, lxml_root
from snowcrawl.acquire.public_suffix import default_suffixes
from snowcrawl.acquire.types import CallsMode, FetchPolicy
from snowcrawl.storage.artifacts import PDF, SCREENSHOT
from snowcrawl.storage.capture_bundle import (
    FLIGHT_TYPE,
    GRPC_WEB_TYPE,
    WASM_TYPE,
    HeaderList,
    NetworkBody,
    guard_prefix,
    is_html,
    network_file,
    network_json,
    sha256_hex,
)

__all__ = [
    "AREAS_JS",
    "ARTIFACT_TIMEOUT_S",
    "CAPS",
    "DATA_REQUESTS",
    "JSON_BODIES_MAX",
    "JSON_PAGE_MAX_BYTES",
    "Answer",
    "Caps",
    "DataCalls",
    "Resources",
    "capture_artifacts",
    "header_list",
    "item_areas",
    "json_bodies",
]

# Data bodies (JSON and Flight) and same-site HTML fragments are captured up
# to 2 MB each and 50 per page; at most 10 MB of them per page in all.
_JSON_BODY_MAX_BYTES = 2_000_000
JSON_BODIES_MAX = 50
JSON_PAGE_MAX_BYTES = 10_000_000


@dataclass(frozen=True)
class Caps:
    """How many bodies a capture keeps, and how many bytes of them in all."""

    bodies: int
    page_bytes: int


# An engineer capture keeps every call (`all`), so more of them: 500 bodies, 50 MB per page; a body's cap stays 2 MB.
CAPS: Final[dict[CallsMode, Caps]] = {
    "data": Caps(JSON_BODIES_MAX, JSON_PAGE_MAX_BYTES),
    "all": Caps(500, 50_000_000),
}
# The answers an engineer capture leaves out, by media type essence: an event stream (HTML Standard §9.2
# `text/event-stream`, which never ends), and binary media (RFC 2046 §4: the `image`, `audio` and `video` top-level
# types, and `font`, RFC 8081; `application/octet-stream`, §4.5.1; archives and PDF documents, whose subtypes name
# `zip` or `pdf`). A WebSocket is no response at all.
_UNKEPT_TOPS: Final = frozenset({"image", "audio", "video", "font"})
_UNKEPT_TYPES: Final = frozenset({"text/event-stream", "application/octet-stream"})
_UNKEPT_PARTS: Final = ("zip", "pdf")
# The reads an engineer capture starts early (`DataCalls.early`) per body it keeps: room for the other sites' calls
# its own site's go before (`json_bodies`), bounded on a page that calls without end.
_EARLY_READS_PER_BODY = 2
# The bodies read from the browser at once: each read is a round trip to it, and at most this many bodies of at most
# `_JSON_BODY_MAX_BYTES` are held before the page cap is applied, so the reads stay bounded.
_READS_AT_ONCE = 5
# Fetch Standard §2.2.5: requests whose destination is the empty string (fetch() and XMLHttpRequest) or `document`
# carry data or a new document; Playwright names them `fetch`, `xhr` and `document`. Scripts, styles, images and fonts
# are the page's code and media, not data it loads.
DATA_REQUESTS = frozenset({"fetch", "xhr", "document"})
# WHATWG MIME Sniffing §4.6: the JSON MIME types are `application/json`, `text/json` and any type with the `+json`
# structured syntax suffix (RFC 6839 §3.1). A body labelled `text/plain`, or not labelled, may still be JSON: it is
# kept only when its bytes are (`network_json`, RFC 8259).
_SNIFFED_TYPES = frozenset({"text/plain", ""})
# A data call is a fetch or XMLHttpRequest call (Fetch Standard §2.2.5 destinations) that can carry the page's data: one
# to the page's own site (HTML Standard §7.1.1, its registrable domain), or one that asks for JSON (an `Accept` naming
# a JSON MIME type, RFC 9110 §12.5.1). Calls to other sites that ask for no JSON (analytics beacons, ad and tag-manager
# pings) are always in flight on some pages and carry nothing the page shows.
_CALLS = frozenset({"fetch", "xhr"})
# HTML Standard §4.6.7.12 `<link rel=preload as=fetch>`: a fetch the page starts before its script asks for it (Fetch
# Standard §2.2.5: destination the empty string, as fetch()'s), whose answer the script's own fetch() is then served
# from without a request of its own. Playwright names its resource type `other`; it is a data call when its answer is
# labelled data (`_data_label`).
_PRELOAD = "other"
_WEBSOCKET = "websocket"
# A screenshot or PDF past this size is dropped rather than stored: the capture is kept without it.
_ARTIFACT_MAX_BYTES = 25_000_000
# Seconds the files of a page may take beyond the fetch's own budget (Playwright's default per-operation timeout): a
# slow file is left out, it never turns a page that loaded into a timeout.
ARTIFACT_TIMEOUT_S = 30
# The rendered area of each named element in CSS pixels (CSSOM View `getBoundingClientRect`); 0 when the element is not
# rendered visible (CSS 2.1 §11.2 `visibility`, `display: none` has no box), -1 when the path names no element of that
# tag (the live document no longer is the one the path was read from).
AREAS_JS: Final = """(elements) => Array.from(elements, ([path, tag]) => {
  const node = document.evaluate(path, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  if (node === null || node.nodeType !== Node.ELEMENT_NODE || node.localName !== tag) return -1;
  if (getComputedStyle(node).visibility !== 'visible') return 0;
  const box = node.getBoundingClientRect();
  return box.width * box.height;
})"""
# The most item blocks a listing capture measures (`_item_areas`): a page's groups seldom hold more, and the walk
# stays bounded on one that does.
_MEASURED_BLOCKS_MAX = 2_000


@dataclass(frozen=True)
class Answer:
    """A kept answer as read from the browser: its body, and the headers of its request and its response as a capture
    records them (`_headers`); `omitted` says why an engineer capture keeps the call without its body (`_OVER_CAP`)."""

    data: bytes
    request_headers: HeaderList | None
    response_headers: HeaderList | None
    omitted: str | None = None


# An engineer capture keeps a call whose answer is past a cap as a row without its body, saying so
# (`NetworkBody.omitted`), so that the call list shows it was made.
_OVER_CAP: Final = f"body not kept: over {_JSON_BODY_MAX_BYTES // 1_000_000} MB"
_OVER_PAGE_CAP: Final = f"body not kept: the capture holds {CAPS['all'].page_bytes // 1_000_000} MB of bodies"


class _OverCap(Exception):
    """The answer is past the per-body cap."""


@dataclass
class DataCalls:
    """The data responses a page has received so far (when the policy captures them), in arrival order, each with how
    many browser actions the page had performed when it arrived; `actions` is that count now, which a listing's
    browser actions (`browser_tab`) and the policy's page steps (`browser.stepped`) raise. `mode` is the calls the
    capture keeps (`FetchPolicy.calls`)."""

    responses: list[tuple[Response, int]] = field(default_factory=list)
    actions: int = 0
    mode: CallsMode = "data"
    in_flight: set[Request] = field(default_factory=set)  # the data calls (`_CALLS`) not answered yet
    answered: int = 0  # the data calls that have ended, answered or failed
    idle_since: float = 0.0  # on `time.monotonic`: when the last data call in flight ended
    # The data requests whose body arrived in full or failed (`requestfinished`, `requestfailed`). A body is read only
    # once its request is done: reading one still arriving waits for its end, which a body that never ends (a stream,
    # a keepalive beacon held open) never reaches, and the read would outlast the fetch's budget.
    done: set[Request] = field(default_factory=set)
    # In `all` mode each kept answer is read once its request is done (`_read`), not when the page is: a page step that
    # navigates the main frame discards the bodies of the document it leaves (Chromium keeps a response's body with
    # the document that loaded it). The reads are bounded as `json_bodies`' are: `_READS_AT_ONCE` at a time, and at
    # most `_EARLY_READS_PER_BODY` times the capture's caps (`CAPS`) in reads started and bytes read.
    early: dict[Request, asyncio.Task[Answer | None]] = field(default_factory=dict)
    kept: dict[Request, Response] = field(default_factory=dict)
    early_bytes: int = 0
    reads: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_READS_AT_ONCE))

    def on_request(self, request: Request, page_url: str) -> None:
        """Count `request` in flight when it is a data call of the page at `page_url` (`_CALLS`)."""
        if request.resource_type in _CALLS and (_same_site(request.url, page_url) or _asks_json(request)):
            self.in_flight.add(request)

    def on_done(self, request: Request) -> None:
        if request in self.in_flight:
            self.in_flight.discard(request)
            self.answered += 1
            if not self.in_flight:
                self.idle_since = time.monotonic()
        if request.resource_type in DATA_REQUESTS or request.resource_type == _PRELOAD:
            self.done.add(request)
        response = self.kept.pop(request, None)
        if response is not None and len(self.early) < _EARLY_READS_PER_BODY * CAPS[self.mode].bodies:
            self.early[request] = asyncio.get_running_loop().create_task(self._read_early(response))

    async def _read_early(self, response: Response) -> Answer | None:
        async with self.reads:
            if self.early_bytes >= _EARLY_READS_PER_BODY * CAPS[self.mode].page_bytes:
                return None
            answer = await _read(response, self.mode)
        self.early_bytes += 0 if answer is None else len(answer.data)
        return answer

    def cancel(self) -> None:
        """Stop the early reads not done: the page is no longer read."""
        for task in self.early.values():
            task.cancel()

    def finished(self, response: Response) -> bool:
        """Whether `response`'s body can be read without waiting: its request is done."""
        return response.request in self.done

    def on_response(self, response: Response, main_frame: Frame) -> None:
        request = response.request
        navigation = request.is_navigation_request() and request.frame == main_frame
        if self.mode == "all":
            kept = _any_call(response, navigation, self.actions)
        else:
            # The main frame's navigation responses are the page itself, not data it loaded.
            data = request.resource_type in DATA_REQUESTS and _data_label(response) is not None
            kept = not navigation and (data or _preloaded(response) or _fragment(response, main_frame.url))
        if kept:
            self.responses.append((response, self.actions))
            if self.mode == "all":
                self.kept[request] = response


@dataclass
class Resources:
    """The resources a page has loaded, counted (`CaptureManifest.resources`): per Fetch resource type, and per media
    type the signals read (`content.signals`). A WebSocket is counted from the page's `websocket` event, its own."""

    counts: Counter[str] = field(default_factory=Counter)

    def on_request(self, request: Request) -> None:
        if request.resource_type != _WEBSOCKET:
            self.counts[request.resource_type] += 1

    def on_websocket(self) -> None:
        self.counts[_WEBSOCKET] += 1

    def on_response(self, response: Response) -> None:
        media_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media_type == WASM_TYPE:
            self.counts[WASM_TYPE] += 1
        elif media_type.startswith(GRPC_WEB_TYPE):
            self.counts[GRPC_WEB_TYPE] += 1


async def capture_artifacts(page: Page, policy: FetchPolicy) -> dict[str, bytes]:
    """The files `policy` asks of the rendered page, by artifact name: a PNG of the viewport or the whole page, and a
    PDF print (HTML Standard §6.6 print media). A PNG has one pixel per CSS pixel (CSSOM View), whatever the screen's
    `devicePixelRatio` (`acquire.desktop_screen`). Reading changes nothing in the page. A file the browser
    could not make in `ARTIFACT_TIMEOUT_S`, or one past `_ARTIFACT_MAX_BYTES`, is left out: the fetch still reached
    its page."""
    asked: list[tuple[str, Callable[[], Awaitable[bytes]]]] = []
    if (screenshot := policy.screenshot) is not None:
        full_page = screenshot == "full_page"
        asked.append((SCREENSHOT, lambda: page.screenshot(type="png", full_page=full_page, scale="css")))
    if policy.pdf:
        asked.append((PDF, lambda: page.pdf(print_background=True)))
    # One budget for all the files; each is made on its own, so one the browser fails to make leaves out only itself.
    deadline = asyncio.get_running_loop().time() + ARTIFACT_TIMEOUT_S
    files: dict[str, bytes] = {}
    for name, make in asked:
        data = await _made(make, deadline)
        if data is not None and len(data) <= _ARTIFACT_MAX_BYTES:
            files[name] = data
    return files


async def _made(make: Callable[[], Awaitable[bytes]], deadline: float) -> bytes | None:
    try:
        async with asyncio.timeout_at(deadline):
            return await make()
    except (PlaywrightError, TimeoutError):
        return None


async def item_areas(page: Page, body: bytes) -> dict[str, float] | None:
    """The rendered area of each block of the document's repeated item groups (`acquire.items.item_groups`), by
    element path: the segment step ranks a browser capture's groups by it (`content.segment`). The blocks are found in
    the serialized document the capture keeps, so their paths name the same elements when it is parsed again; None
    when the document has no group or the page can no longer be read."""
    # Parsing is CPU work: a thread keeps the event loop serving other pages meanwhile.
    refs = await asyncio.to_thread(_block_refs, body)
    if not refs:
        return None
    try:
        areas = cast(list[float], await page.evaluate(AREAS_JS, [list(ref) for ref in refs]))
    except PlaywrightError:
        return None
    if any(area < 0 for area in areas):  # the live document changed since it was kept: rank by structure instead
        return None
    return {path: round(area, 1) for (path, _), area in zip(refs, areas, strict=True)}


def _block_refs(body: bytes) -> list[tuple[str, str]]:
    """The element path and tag of each block of the document's repeated item groups, at most `_MEASURED_BLOCKS_MAX`
    (the browser serializes the DOM as UTF-8)."""
    blocks = [block for group in item_groups(lxml_root(body.decode("utf-8", errors="replace"))) for block in group]
    return [(element_path(block), str(block.tag)) for block in blocks[:_MEASURED_BLOCKS_MAX]]


def _any_call(response: Response, navigation: bool, actions: int) -> bool:
    """Whether an engineer capture keeps `response`: a fetch or XMLHttpRequest answer, or a document the main frame
    navigated to after a page step (`actions` > 0: the first is the page itself), unless it is unkept (`_unkept`)."""
    request = response.request
    called = request.resource_type in _CALLS or _preloaded(response) or (navigation and actions > 0)
    return called and not _unkept(response.headers.get("content-type", ""))


def _preloaded(response: Response) -> bool:
    """Whether `response` answers a preload the page made of data (`_PRELOAD`)."""
    return response.request.resource_type == _PRELOAD and _data_label(response) is True


def _unkept(content_type: str) -> bool:
    """Whether an answer of `content_type` is one an engineer capture leaves out (`_UNKEPT_TOPS`)."""
    essence = content_type.split(";", 1)[0].strip().lower()
    top, _, subtype = essence.partition("/")
    return top in _UNKEPT_TOPS or essence in _UNKEPT_TYPES or any(part in subtype for part in _UNKEPT_PARTS)


def _same_site(url: str, page_url: str) -> bool:
    """Whether `url` is on the same site as `page_url` (HTML Standard §7.1.1: the same registrable domain)."""
    suffixes = default_suffixes()
    host, page_host = urlsplit(url).hostname, urlsplit(page_url).hostname
    return host is not None and page_host is not None and suffixes.site(host) == suffixes.site(page_host)


def _asks_json(request: Request) -> bool:
    """Whether the request's `Accept` names a JSON MIME type or a Flight payload (`_data_label`'s rule: RFC 6839 §3.1
    `+json` included)."""
    accept = request.headers.get("accept", "")
    return any(_data_essence(part.split(";", 1)[0].strip().lower()) for part in accept.split(","))


async def _json_size(response: Response) -> int:
    """A JSON body's size before it is read: its `Content-Length` (RFC 9110 §8.6), else its size on the wire;
    0 when the browser no longer holds the request, whose body then can't be read either."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit():
        return int(declared)
    try:
        return (await response.request.sizes())["responseBodySize"]
    except PlaywrightError:
        return 0


def _data_label(response: Response) -> bool | None:
    """True when the response is labelled with a JSON MIME type or as a Flight payload, False when its label leaves its
    body to be sniffed, None when it names another kind of body (`_SNIFFED_TYPES`). The essence is compared,
    lower-cased, parameters dropped (WHATWG MIME Sniffing §2.1)."""
    essence = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if _data_essence(essence):
        return True
    return False if essence in _SNIFFED_TYPES else None


def _fragment(response: Response, page_url: str) -> bool:
    """Whether `response` is a success (RFC 9110 §15.3) answering a fetch or XMLHttpRequest call to the page's own site
    (`_same_site`) with HTML (`_html_label`): an HTML fragment the page fetched and shows."""
    request = response.request
    success = 200 <= response.status < 300
    return success and request.resource_type in _CALLS and _html_label(response) and _same_site(response.url, page_url)


def _data_essence(essence: str) -> bool:
    """Whether `essence` names a JSON MIME type or a Flight payload (`FLIGHT_TYPE`), the bodies a capture keeps."""
    return essence in ("application/json", "text/json", FLIGHT_TYPE) or essence.endswith("+json")


async def json_bodies(
    responses: list[tuple[Response, int]],
    page_url: str,
    mode: CallsMode = "data",
    early: Mapping[Request, asyncio.Task[Answer | None]] | None = None,
) -> tuple[tuple[NetworkBody, bytes], ...]:
    """The bodies of `responses` (each with the action count it arrived at; `DataCalls.on_response` admitted them) a
    capture of the page at `page_url` keeps under `mode`, within its caps (`CAPS`), in response order; in `all`, the
    page's own site's first. An answer read `early` (`DataCalls.early`) is not read again. A call whose request body is
    past the per-body cap is left out before its answer is read; the answers are read `_READS_AT_ONCE` at a time, and
    none after the capture is full."""
    read_early = early or {}
    caps = CAPS[mode]
    if mode == "all":
        # A stable sort: each site's calls stay in response order.
        responses = sorted(responses, key=lambda entry: not _same_site(entry[0].url, page_url))
    sent = ((response, action) for response, action in responses if _request_size(response) <= _JSON_BODY_MAX_BYTES)
    captured: list[tuple[NetworkBody, bytes]] = []
    total = 0
    # No more answers are read than the capture has room for.
    while len(captured) < caps.bodies and (
        batch := list(islice(sent, min(_READS_AT_ONCE, caps.bodies - len(captured))))
    ):
        answers = await asyncio.gather(
            *(read_early.get(response.request) or _read(response, mode) for response, _ in batch)
        )
        for (response, action), read in zip(batch, answers, strict=True):
            answer = _within(read, total, caps, mode)
            if answer is None:
                continue
            captured.append((_entry(response, action, answer, len(captured), page_url, mode), answer.data))
            total += len(answer.data)
    return tuple(captured)


def _within(answer: Answer | None, total: int, caps: Caps, mode: CallsMode) -> Answer | None:
    """`answer` when the capture, holding `total` bytes, has room for it; past its cap, in `all` mode the answer
    without its body (`_OVER_PAGE_CAP`), else None."""
    if answer is None or total + len(answer.data) <= caps.page_bytes:
        return answer
    return replace(answer, data=b"", omitted=_OVER_PAGE_CAP) if mode == "all" else None


def _request_size(response: Response) -> int:
    return len(response.request.post_data_buffer or b"")


async def _read(response: Response, mode: CallsMode) -> Answer | None:
    """The answer to `response`'s request a capture under `mode` keeps (`_json_body`), None when it keeps none; in
    `all` mode one past the per-body cap is kept without its body (`_OVER_CAP`)."""
    omitted = None
    try:
        data = await _json_body(response, mode)
    except _OverCap:
        if mode != "all":
            return None
        data, omitted = b"", _OVER_CAP
    if data is None:
        return None
    request_headers, response_headers = await asyncio.gather(
        _headers(response.request.headers_array()), _headers(response.headers_array())
    )
    return Answer(data, request_headers, response_headers, omitted)


def _entry(response: Response, action: int, answer: Answer, index: int, page_url: str, mode: CallsMode) -> NetworkBody:
    """The manifest entry of the `index`th body a capture of the page at `page_url` keeps under `mode`, `answer`,
    answering `response`'s request; its guard prefix is read for an engineer capture (`all`), which shows it."""
    request, data = response.request, answer.data
    return NetworkBody(
        url=response.url,
        status=response.status,
        content_type=response.headers.get("content-type"),
        sha256=sha256_hex(data),
        file=network_file(index),
        method=request.method,
        request_body=_text_or_none(request.post_data_buffer),
        request_headers=answer.request_headers,
        response_headers=answer.response_headers,
        resource_type=request.resource_type,
        action=action,
        same_site=_same_site(response.url, page_url),
        guard_prefix=guard_prefix(data) if mode == "all" else None,
        omitted=answer.omitted,
    )


async def _json_body(response: Response, mode: CallsMode) -> bytes | None:
    """The response's body when, in `data` mode, it is not empty and data: labelled JSON, Flight or HTML (an admitted
    fragment, `_fragment`), or sniffed as JSON text holding an object or an array (`network_json`). In `all` mode any
    answer `DataCalls.on_response` admitted is kept, an empty one too. Raises `_OverCap` for a body past the per-body
    cap."""
    if await _json_size(response) > _JSON_BODY_MAX_BYTES:
        raise _OverCap
    try:
        data = await response.body()
    except PlaywrightError:
        return None  # no body to read: a redirect, or a request that failed or was evicted
    if len(data) > _JSON_BODY_MAX_BYTES:
        raise _OverCap
    if mode == "all":
        return data
    if not data:
        return None  # an empty body (a 204, RFC 9110 §15.3.5) holds no JSON text
    return data if _data_label(response) or _html_label(response) or _sniffed(data) else None


def _html_label(response: Response) -> bool:
    """Whether the response is labelled HTML (`is_html`): a fetch or XMLHttpRequest call's HTML answer is a fragment
    the page shows (Turbo frames, htmx-style partials)."""
    return is_html(response.headers.get("content-type"))


def _sniffed(data: bytes) -> bool:
    try:
        return isinstance(network_json(data), (dict, list))
    except ValueError:
        return False


async def _headers(read: Awaitable[Sequence[Mapping[str, object]]]) -> HeaderList | None:
    """A request's or response's headers as a capture records them (no credentials); None when the browser no longer
    holds them."""
    try:
        return recorded(header_list(await read))
    except PlaywrightError:
        return None


def _text_or_none(data: bytes | None) -> str | None:
    try:
        return None if data is None else data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def header_list(headers: Sequence[Mapping[str, object]]) -> HeaderList:
    # Playwright's header arrays keep repeated names in order (RFC 9110 §5.3), as the bundle does.
    return tuple((str(h["name"]), str(h["value"])) for h in headers)


# The rendered document with its open shadow roots (HTML Standard, `Element.getHTML` with `serializableShadowRoots` and
# `shadowRoots`): every open root reachable from the document by walking `shadowRoot` is serialized as a declarative
# `<template shadowrootmode="open">`, which the HTML reader flattens. Run in Patchright's isolated world with `evaluate`
# (no init script); a closed root is not reachable and stays out. The doctype and the root element's own tag
# are written around `getHTML`, which serializes the children; a browser without it gives `outerHTML`.
# What the page removed once the scroll pass had scrolled past it (`browser_scroll`, `__scrolledPast`) is serialized
# back where it left, latest removal first so that each lands among the siblings it left (at the parent's start when
# none is left: it lay above what the parent shows): each node not back in the document whose text the document does
# not hold (its run of text nodes outside scripts, styles and templates, each trimmed and its whitespace collapsed,
# matched whole: "item 1" is not held by "item 10"). The nodes are put back, the document serialized and the nodes
# removed again in one task, so the page's scripts never run on a document holding them.
_PUT_BACK_JS: Final = """() => {
  const placed = [];
  const past = globalThis.__scrolledPast;
  if (past === undefined || past.length === 0) return placed;
  const SKIPPED = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const { SHOW_ELEMENT, SHOW_TEXT, FILTER_REJECT, FILTER_ACCEPT } = NodeFilter;
  // Each text node's text between NUL separators, which no text node holds (HTML Standard §13.2.5: the tokenizer
  // replaces U+0000 in text).
  const text = (node) => {
    const parts = [];
    const add = (data) => {
      const part = data.replace(/\\s+/g, ' ').trim();
      if (part !== '') parts.push(part);
    };
    if (node.nodeType === Node.TEXT_NODE) add(node.nodeValue);
    const walker = document.createTreeWalker(node, SHOW_ELEMENT | SHOW_TEXT, (child) => (
      child.nodeType === Node.ELEMENT_NODE && SKIPPED.has(child.tagName) ? FILTER_REJECT : FILTER_ACCEPT));
    for (let child = walker.nextNode(); child !== null; child = walker.nextNode()) {
      if (child.nodeType === Node.TEXT_NODE) add(child.nodeValue);
    }
    return parts.length === 0 ? '' : `\\0${parts.join('\\0')}\\0`;
  };
  let held = text(document.documentElement);
  for (const { nodes, parent, prev, next } of past.slice().reverse()) {
    if (!parent.isConnected) continue;
    const back = nodes.filter((node) => {
      const said = text(node);
      if (node.isConnected || said === '' || held.includes(said)) return false;
      held += said;
      return true;
    });
    if (back.length === 0) continue;
    if (prev && prev.parentNode === parent) prev.after(...back);
    else if (next && next.parentNode === parent) next.before(...back);
    else parent.prepend(...back);
    placed.push(...back);
  }
  return placed;
}"""
_SERIALIZE_JS: Final = """() => {
  const roots = [];
  const walk = (root) => {
    for (const element of root.querySelectorAll('*')) {
      if (element.shadowRoot) { roots.push(element.shadowRoot); walk(element.shadowRoot); }
    }
  };
  walk(document);
  const html = document.documentElement;
  const { doctype: type } = document;
  const doctype = type ? `<!DOCTYPE ${type['name']}>` : '';
  if (html === null) return doctype;
  if (typeof html.getHTML !== 'function') return doctype + html.outerHTML;
  const quoted = (value) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const attributes = Array.from(html.attributes, ({ name, value }) => ` ${name}="${quoted(value)}"`).join('');
  const inner = html.getHTML({ serializableShadowRoots: true, shadowRoots: roots });
  return `${doctype}<html${attributes}>${inner}</html>`;
}"""
DOCUMENT_HTML_JS: Final = f"""() => {{
  const placed = ({_PUT_BACK_JS})();
  try {{
    return ({_SERIALIZE_JS})();
  }} finally {{
    for (const node of placed) node.remove();
  }}
}}"""


async def document_html(page: Page) -> str:
    """The rendered document with its open shadow roots (`DOCUMENT_HTML_JS`), read in the isolated world."""
    return cast(str, await page.evaluate(DOCUMENT_HTML_JS, isolated_context=True))
