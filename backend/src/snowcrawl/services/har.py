"""A capture as an HTTP Archive (HAR 1.2, W3C Web Performance WG draft "HTTP Archive (HAR) format"): the page's
document and each call the capture kept, in capture order, for the engineer's own tools.

The archive holds what the bundle holds and nothing else: headers as the capture recorded them, so without cookies
(`acquire.headers.recorded`) and with the values of the caller's own headers masked (`acquire.outcome`); no cookie
lists. What the bundle does not keep is written as HAR says an unknown is: `-1` for a size or a timing that may be
unknown, the empty string for the HTTP version, and 0 for the timings that must be known (`send`, `wait`, `receive`).
Every entry starts when the page's fetch started (`fetched_at`): a call's own start is not kept. The page's document is
its main frame's latest navigation: when a page step submitted a form, that is the kept `document` call to the page's
URL, so the document entry takes that call's request and the call is not written twice.
"""

import base64
from collections.abc import Sequence
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from pydantic import JsonValue

from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.storage.capture_bundle import CaptureBundle, HeaderList, NetworkBody

__all__ = ["HAR_VERSION", "har"]

HAR_VERSION: Final = "1.2"
_CREATOR: Final = "Snowcrawl"
_PAGE: Final = "page_1"
# HAR 1.2 `content.encoding`: a body that is not UTF-8 text is written in base64.
_BASE64: Final = "base64"


def har(bundle: CaptureBundle) -> dict[str, JsonValue]:
    """The capture `bundle` as a HAR 1.2 log (module docstring)."""
    manifest = bundle.manifest
    started = manifest.fetched_at.isoformat()
    navigated = _navigated(manifest.network_bodies, manifest.final_url)
    request = manifest.network_bodies[navigated] if navigated is not None else None
    document = _entry(
        started,
        method=(request.method if request else None) or "GET",
        url=manifest.final_url,
        request_headers=manifest.headers.request,
        request_body=request.request_body if request else None,
        status=manifest.status_code,
        response_headers=manifest.headers.response,
        content_type=_header(manifest.headers.response, "content-type"),
        answer=bundle.raw_body if bundle.raw_body is not None else bundle.body,
    )
    calls = [
        _entry(
            started,
            method=entry.method or "GET",
            url=entry.url,
            request_headers=entry.request_headers or (),
            request_body=entry.request_body,
            status=entry.status,
            response_headers=entry.response_headers or (),
            content_type=entry.content_type,
            answer=data,
            comment=entry.omitted,
        )
        for index, (entry, data) in enumerate(zip(manifest.network_bodies, bundle.network, strict=True))
        if index != navigated
    ]
    page: dict[str, JsonValue] = {
        "startedDateTime": started,
        "id": _PAGE,
        "title": manifest.url,
        "pageTimings": {"onContentLoad": -1, "onLoad": -1},
    }
    log: dict[str, JsonValue] = {
        "version": HAR_VERSION,
        "creator": {"name": _CREATOR, "version": ENGINE_VERSION},
        "pages": [page],
        "entries": [document, *calls],
    }
    return {"log": log}


def _navigated(entries: Sequence[NetworkBody], url: str) -> int | None:
    """The index of the page's own document among the kept calls: the last main-frame navigation (`document`), when it
    is to the page's URL; None when the page's document is its first load."""
    documents = [index for index, entry in enumerate(entries) if entry.resource_type == "document"]
    if documents and entries[documents[-1]].url == url:
        return documents[-1]
    return None


def _entry(
    started: str,
    *,
    method: str,
    url: str,
    request_headers: HeaderList,
    request_body: str | None,
    status: int,
    response_headers: HeaderList,
    content_type: str | None,
    answer: bytes,
    comment: str | None = None,
) -> JsonValue:
    request: dict[str, JsonValue] = {
        "method": method,
        "url": url,
        "httpVersion": "",
        "cookies": [],
        "headers": _headers(request_headers),
        "queryString": [{"name": name, "value": value} for name, value in parse_qsl(urlsplit(url).query, True)],
        "headersSize": -1,
        "bodySize": -1 if request_body is None else len(request_body.encode()),
    }
    if request_body is not None:
        mime = _header(request_headers, "content-type") or ""
        request["postData"] = {"mimeType": mime, "text": request_body}
    entry: dict[str, JsonValue] = {
        "pageref": _PAGE,
        "startedDateTime": started,
        "time": 0,
        "request": request,
        "response": {
            "status": status,
            "statusText": "",
            "httpVersion": "",
            "cookies": [],
            "headers": _headers(response_headers),
            "content": _content(answer, content_type),
            "redirectURL": _header(response_headers, "location") or "",
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": 0, "receive": 0},
    }
    if comment is not None:
        entry["comment"] = comment  # HAR 1.2 §entries: a comment provided by the user or the application
    return entry


def _content(answer: bytes, content_type: str | None) -> JsonValue:
    """HAR 1.2 `content`: the answer as text, or in base64 when it is not UTF-8."""
    content: dict[str, JsonValue] = {"size": len(answer), "mimeType": content_type or ""}
    try:
        content["text"] = answer.decode("utf-8")
    except UnicodeDecodeError:
        content["text"] = base64.b64encode(answer).decode("ascii")
        content["encoding"] = _BASE64
    return content


def _headers(headers: Sequence[tuple[str, str]]) -> list[JsonValue]:
    return [{"name": name, "value": value} for name, value in headers]


def _header(headers: Sequence[tuple[str, str]], name: str) -> str | None:
    """The last value of header `name` (RFC 9110 §5.1: names compare case-insensitively)."""
    values = [value for header, value in headers if header.lower() == name]
    return values[-1] if values else None
