"""The capture bundle: everything replay needs, in one zstd-compressed tar.

```
manifest.json      # CaptureManifest
body.html          # response body as received (transfer/content encoding removed); body.json for API captures;
                   # for browser captures, the rendered DOM
body.raw.html      # browser captures only: the navigation response body as the server sent it
network/0001.json  # JSON bodies in response order, as received: a render's captured calls, an HTTP page's data
                   # calls (`acquire.identity`), or a custom recipe's extra calls and page beside its main answer
```

`encode` and `decode` are pure and `decode(encode(b)) == b`. Replay reads nothing but the bundle.
"""

import hashlib
import io
import json
import re
import tarfile
from datetime import UTC, datetime
from email.message import Message
from typing import Annotated, Final, Literal, Self
from uuid import UUID

import webencodings
import zstandard
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from snowcrawl.core.capture import BlockClass, CaptureMethod
from snowcrawl.core.derived import held

SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
# A header list, not a mapping: HTTP allows a field name to repeat (RFC 9110 §5.3, e.g. Set-Cookie), and the
# bundle keeps headers as received.
HeaderList = tuple[tuple[str, str], ...]

# The `data_call` marks of what an `api` capture holds beside its main answer: the HTML page its recipe fetched
# (`RECIPE_PAGE`) and each extra call's answer, by the call's name (`recipe_call`).
RECIPE_PAGE: Final = "recipe_page"
_RECIPE_CALL: Final = "recipe_call:"


def recipe_call(name: str) -> str:
    """The mark of the answer of the recipe's extra call `name`."""
    return _RECIPE_CALL + name


def recipe_call_name(mark: str | None) -> str | None:
    """The extra call a body marked `mark` answers; None for any other body."""
    return mark.removeprefix(_RECIPE_CALL) if mark is not None and mark.startswith(_RECIPE_CALL) else None


def recipe_mark(mark: str | None) -> bool:
    """Whether a body marked `mark` is a recipe's: its page or an extra call's answer."""
    return mark == RECIPE_PAGE or recipe_call_name(mark) is not None


_MANIFEST = "manifest.json"
_RAW_BODY = "body.raw.html"


class CorruptBundle(Exception):
    """The bytes are not a valid bundle."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Headers(_Frozen):
    request: HeaderList
    response: HeaderList


class NetworkBody(_Frozen):
    """Manifest entry for one captured network body; the bytes sit at the same index of the bundle's `network`.

    `method`, `request_body` and `request_headers` describe the request the body answered, which API discovery groups
    and binds by and header minimization replays; bundles written before they were kept decode
    with None. `request_body` is None also for a request without a body or with one that is not UTF-8 text.
    `request_headers` are recorded as the page's are (header fields only: no credentials, no pseudo-headers), in the
    order sent.

    `response_headers` are recorded the same way, in the order received; `resource_type` is the request's Fetch
    destination as the browser names it (`fetch`, `xhr`, `document`); `action` is how many browser actions the page
    had performed when the response arrived (0: the page's own load). Bundles written before they were kept decode
    with None."""

    url: str
    status: int
    content_type: str | None
    sha256: Sha256
    file: str
    method: str | None = None
    request_body: str | None = None
    request_headers: HeaderList | None = None
    response_headers: HeaderList | None = None
    resource_type: str | None = None
    action: int | None = Field(default=None, ge=0)
    # The platform whose documented product JSON this body is, for a data call an HTTP fetch made whose answer passed
    # the identity check (`acquire.identity`); None for a render's captured call. Left out of the manifest when None,
    # so a bundle written before it was kept encodes as it did.
    data_call: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # Whether the call went to the page's own site (HTML Standard §7.1.1: the same registrable domain), and the
    # cross-site script inclusion guard its JSON answer opens with (`guard_prefix`); kept by a render, left out of the
    # manifest when None, as `data_call` is.
    same_site: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    guard_prefix: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # Why an engineer capture kept the call without its body (its file is empty): past a cap. Left out of the manifest
    # when None, as `data_call` is.
    omitted: str | None = Field(default=None, exclude_if=lambda value: value is None)


class CaptureManifest(_Frozen):
    bundle_version: Literal[1] = 1
    capture_id: UUID
    url: str
    final_url: str
    fetched_at: AwareDatetime
    method: CaptureMethod
    status_code: int
    block_class: BlockClass
    headers: Headers
    timings_ms: dict[str, int]
    html_sha256: Sha256
    # Absent from bundles written before raw bodies were kept: those decode with None.
    raw_html_sha256: Sha256 | None = None
    network_bodies: tuple[NetworkBody, ...]
    fetch_policy: dict[str, JsonValue]
    engine_version: str
    # Browser listing captures: the rendered area (CSS pixels) of each repeated item block, by its element path in the
    # body (`acquire.items.element_path`); None when not measured (the segment step then ranks by structure).
    item_areas: dict[str, float] | None = None
    # Browser captures: per Fetch resource type the browser named (`websocket`, `eventsource`, `script`...), how many
    # the page loaded, and per counted media type (`application/wasm`, `application/grpc-web`) how many responses
    # carried it; counts only, no URLs (`content.signals`). Left out of the manifest when None, as `data_call` is.
    resources: dict[str, int] | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def page_url(self) -> str:
        """The URL of the page the capture stands for: an `api` capture's `url`, the page its answer was requested for
        (its `final_url` is the endpoint's); else `final_url`, where the request ended (RFC 9110 §15.4)."""
        return self.url if self.method == "api" else self.final_url


class CaptureBundle(_Frozen):
    manifest: CaptureManifest
    body: bytes
    network: tuple[bytes, ...]
    raw_body: bytes | None = None

    @model_validator(mode="after")
    def _matches_manifest(self) -> Self:
        entries = self.manifest.network_bodies
        if sha256_hex(self.body) != self.manifest.html_sha256:
            raise ValueError("body does not match manifest html_sha256")
        if self.raw_body is not None and self.manifest.method != "browser":
            raise ValueError(f"a raw body is kept by the browser only, not by method {self.manifest.method}")
        raw_sha256 = None if self.raw_body is None else sha256_hex(self.raw_body)
        if raw_sha256 != self.manifest.raw_html_sha256:
            raise ValueError("raw_body does not match manifest raw_html_sha256")
        if self.manifest.method == "api" and not all(recipe_mark(entry.data_call) for entry in entries):
            raise ValueError(
                "network bodies are a page's: captured by the browser, data calls an HTTP fetch made, or a recipe's"
            )
        if len(self.network) != len(entries):
            raise ValueError(f"network has {len(self.network)} bodies but the manifest lists {len(entries)}")
        for i, (data, entry) in enumerate(zip(self.network, entries, strict=True)):
            if entry.file != network_file(i):
                raise ValueError(f"network_bodies[{i}].file is {entry.file!r}, expected {network_file(i)!r}")
            if sha256_hex(data) != entry.sha256:
                raise ValueError(f"network[{i}] does not match network_bodies[{i}].sha256")
        return self

    @property
    def text(self) -> str:
        """The body as text (`decode_html`), decoded once per bundle: every reader of the body reads this text."""
        return held(self, "_text", self._decoded_from(), lambda: decode_html(*self._decoded_from()))

    def decoded(self, text: str) -> Self:
        """This bundle, `text` its body as the rung decoded it (`decode_html` of the same body, response headers and
        method), so no reader decodes it again."""
        held(self, "_text", self._decoded_from(), lambda: text)
        return self

    def _decoded_from(self) -> tuple[bytes, HeaderList, CaptureMethod]:
        return self.body, self.manifest.headers.response, self.manifest.method


def encode(bundle: CaptureBundle) -> bytes:
    """Deterministic: equal bundles give equal bytes (tar headers carry no time, owner or mode of their own)."""
    members = [
        (_MANIFEST, bundle.manifest.model_dump_json().encode()),
        (_body_file(bundle.manifest.method), bundle.body),
        *(() if bundle.raw_body is None else ((_RAW_BODY, bundle.raw_body),)),
        *((entry.file, data) for entry, data in zip(bundle.manifest.network_bodies, bundle.network, strict=True)),
    ]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getbuffer())


def decode(blob: bytes) -> CaptureBundle:
    """Raise `CorruptBundle` unless `blob` holds exactly the members its manifest names, with matching hashes."""
    members: dict[str, bytes] = {}
    try:
        raw = zstandard.ZstdDecompressor().decompress(blob)
        # Members are read into memory, never extracted, so their names can't touch the filesystem.
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for info in tar.getmembers():
                file = tar.extractfile(info) if info.isfile() else None
                if file is None or info.name in members:
                    raise CorruptBundle(f"unexpected member {info.name!r}")
                members[info.name] = file.read()
    except (zstandard.ZstdError, tarfile.TarError) as e:
        raise CorruptBundle(f"not a zstd-compressed tar: {e}") from e

    if _MANIFEST not in members:
        raise CorruptBundle(f"missing {_MANIFEST}")
    try:
        manifest = CaptureManifest.model_validate_json(members.pop(_MANIFEST))
    except ValidationError as e:
        raise CorruptBundle(f"invalid {_MANIFEST}: {e}") from e

    body_file = _body_file(manifest.method)
    raw_file = () if manifest.raw_html_sha256 is None else (_RAW_BODY,)
    expected = {body_file, *raw_file, *(entry.file for entry in manifest.network_bodies)}
    if members.keys() != expected:
        raise CorruptBundle(f"unexpected members {sorted(members)}; the manifest names {sorted(expected)}")
    try:
        return CaptureBundle(
            manifest=manifest,
            body=members[body_file],
            network=tuple(members[entry.file] for entry in manifest.network_bodies),
            raw_body=members[_RAW_BODY] if raw_file else None,
        )
    except ValidationError as e:
        raise CorruptBundle(str(e)) from e


CAPTURES_PREFIX: Final = "captures/"


def capture_key(capture_id: UUID, fetched_at: datetime) -> str:
    """Production captures, which expire after 30 days. `fetched_at` must be
    timezone-aware."""
    month = fetched_at.astimezone(UTC)
    return f"{CAPTURES_PREFIX}{month:%Y}/{month:%m}/{capture_id}.bundle.zst"


# HTML Standard §13.2.3.2: the prescan reads at most the first 1024 bytes for a `meta` element's `charset` attribute,
# or the `charset` parameter of its `content` (§2.6.7 "extracting a character encoding from a meta element"): the
# value runs to a quote, whitespace, `;` or the tag's end.
_PRESCAN_BYTES: Final = 1024
_META_CHARSET: Final = re.compile(rb"""<meta[\s/][^>]*?charset\s*=\s*["']?\s*([^\s"';>/]+)""", re.IGNORECASE)
_UTF8: Final = webencodings.UTF8
_META_UTF16: Final = frozenset({"utf-16be", "utf-16le"})
_USER_DEFINED: Final = "x-user-defined"
_WINDOWS_1252: Final = "windows-1252"
# WHATWG MIME Sniffing §4.6: the JSON MIME types, `application/json`, `text/json` and any `+json` structured syntax
# suffix (RFC 6839 §3.1); and the HTML ones (HTML Standard §16.1, RFC 3236), which an API answer may be when refused.
_JSON_TYPES: Final = frozenset({"application/json", "text/json"})
_JSON_SUFFIX: Final = "+json"
_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
# React Server Components' Flight payload (React `ReactFlightClient.js`), as Next.js labels it
# (`app-router-headers.ts`).
FLIGHT_TYPE: Final = "text/x-component"
# The media types a browser capture counts among its resources (`CaptureManifest.resources`), by essence;
# `GRPC_WEB_TYPE` stands for every `application/grpc-web*` type.
WASM_TYPE: Final = "application/wasm"
GRPC_WEB_TYPE: Final = "application/grpc-web"


def is_flight(content_type: str | None) -> bool:
    """Whether a captured body's `Content-Type` labels a Flight payload: its essence, lower-cased and parameters
    dropped (WHATWG MIME Sniffing §2.1)."""
    return (content_type or "").split(";", 1)[0].strip().lower() == FLIGHT_TYPE


def is_html(content_type: str | None) -> bool:
    """Whether a captured body's `Content-Type` labels HTML (`text/html`, `application/xhtml+xml`): its essence,
    lower-cased and parameters dropped (WHATWG MIME Sniffing §2.1)."""
    return (content_type or "").split(";", 1)[0].strip().lower() in _HTML_TYPES


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_html(body: bytes, headers: HeaderList, method: CaptureMethod) -> str:
    """A response body as text; undecodable bytes become U+FFFD. Acquisition decodes a body once with it, and every
    reader of the body reads that text (`CaptureBundle.text`). A browser body is the rendered DOM, which the browser
    fetcher serializes as UTF-8, whatever the server declared for the bytes it sent. Any other body is decoded as HTML
    encoding sniffing says (HTML Standard §13.2.3.2): a byte order mark, else the charset its Content-Type names (RFC
    9110 §8.3.2), else a `meta` declaration in its first bytes, else UTF-8; a label names its encoding as the Encoding
    Standard §4.2 says (`iso-8859-1` is windows-1252), and an unknown label is skipped. A JSON body (`_is_json`) is
    UTF-8 (RFC 8259 §8.1, its media type has no charset parameter), whatever charset its label names; the UTF-16
    and UTF-32 forms are told from its first bytes as `core.json_text` reads them (RFC 4627 §3)."""
    if method == "browser":
        return body.decode(_UTF8.codec_info.name, errors="replace")
    if _is_json(headers, method):
        return body.decode(json.detect_encoding(body), errors="replace")
    encoding = _transport_encoding(headers) or _meta_encoding(body) or _UTF8
    text, _ = webencodings.decode(body, encoding, errors="replace")  # a byte order mark outranks `encoding`
    return text


def _is_json(headers: HeaderList, method: CaptureMethod) -> bool:
    """Whether a body is a JSON text: labelled with a JSON MIME type, or an API capture's (`body.json`) not labelled
    HTML. The essence is the last Content-Type's,
    lower-cased, parameters dropped (RFC 9110 §8.3.1)."""
    types = [value.split(";", 1)[0].strip().lower() for name, value in headers if name.lower() == "content-type"]
    essence = types[-1] if types else ""
    if essence in _JSON_TYPES or essence.endswith(_JSON_SUFFIX):
        return True
    return method == "api" and essence not in _HTML_TYPES


def _transport_encoding(headers: HeaderList) -> webencodings.Encoding | None:
    header = Message()
    for name, value in headers:
        if name.lower() == "content-type":
            header["content-type"] = value
            break
    label = header.get_content_charset()
    return None if label is None else webencodings.lookup(label)


def _meta_encoding(body: bytes) -> webencodings.Encoding | None:
    match = _META_CHARSET.search(body, 0, _PRESCAN_BYTES)
    # A label that is not ASCII names no encoding (Encoding Standard §4.2): `lookup` finds none, and it is skipped.
    label = None if match is None else match.group(1).decode("ascii", errors="replace")
    encoding = None if label is None else webencodings.lookup(label)
    if encoding is None:
        return None
    # HTML Standard §13.2.3.2 prescan: a `meta` declaration of UTF-16 means UTF-8 (the bytes it was read from are
    # ASCII-compatible), and of x-user-defined means windows-1252.
    if encoding.name in _META_UTF16:
        return _UTF8
    return webencodings.lookup(_WINDOWS_1252) if encoding.name == _USER_DEFINED else encoding


def _body_file(method: CaptureMethod) -> str:
    return "body.json" if method == "api" else "body.html"


# RFC 8259 §2: a JSON text is one value; the data a page loads is an object or an array, which open with these.
_OPENERS: Final = ("{", "[")
# A JSON body may open with a short prefix that makes it no script a page of another origin could run (a
# cross-site script inclusion guard: an endless loop or unbalanced brackets, then the JSON text). Such a prefix holds
# no object or array opener, so the JSON text starts at the first one; a prefix longer than this is not one.
_GUARD_PREFIX_MAX: Final = 16


def network_json(data: bytes) -> JsonValue:
    """A captured network body as JSON (RFC 8259): UTF-8 text (a byte order mark is ignored, as §8.1 allows),
    either one JSON text or a short guard prefix followed by an object or array (`_GUARD_PREFIX_MAX`). Raises
    `ValueError` when it is neither. The browser reads bodies to keep with it and every reader of the bundle's
    network bodies parses them with it, so the two agree."""
    return _guarded_json(data)[1]


def guard_prefix(data: bytes) -> str | None:
    """The guard prefix a JSON body opens with (`network_json`), None when it has none or is not JSON."""
    try:
        return _guarded_json(data)[0]
    except ValueError:
        return None


def _guarded_json(data: bytes) -> tuple[str | None, JsonValue]:
    """The body's guard prefix (None without one) and its JSON value (`network_json`)."""
    text = data.decode("utf-8-sig")  # UnicodeDecodeError is a ValueError
    try:
        return None, _loads(text)
    except ValueError:
        starts = [i for i in (text.find(opener) for opener in _OPENERS) if i > 0]
        if not starts or min(starts) > _GUARD_PREFIX_MAX:
            raise
    value = _loads(text[min(starts) :])
    if not isinstance(value, (dict, list)):
        raise ValueError("a guard prefix is followed by an object or an array")
    return text[: min(starts)], value


def _not_json(constant: str) -> JsonValue:
    raise ValueError(f"{constant} is not JSON (RFC 8259 §6)")


_DECODER: Final = json.JSONDecoder(parse_constant=_not_json)


def _loads(text: str) -> JsonValue:
    try:
        value: JsonValue = _DECODER.decode(text)
    except RecursionError:
        raise ValueError("nested too deeply") from None
    return value


def network_file(index: int) -> str:
    return f"network/{index + 1:04d}.json"
