"""URL and image_url normalizer: an absolute, canonical http(s) URL.

Accepts a string, from the DOM, an LLM or a structured source (JSON-LD `url` and `image` values are strings). Any
other JSON value is `Invalid("not_a_string")`. A relative reference resolves against `ctx.base_url`. The reference as
the page writes it (WHATWG URL Standard: without the leading and trailing C0 controls and spaces and the tabs and
newlines the parser removes), resolved against the base when relative, is the value's stated form (`Ok.stated`), the
one that publishes: always absolute, in the page's spelling; the canonical form is for equality and identity only.
"""

import mimetypes
import re
import string
from collections.abc import Hashable
from dataclasses import replace
from functools import lru_cache
from urllib.parse import SplitResult, quote, unquote, urljoin, urlsplit

from pydantic import JsonValue

from snowcrawl.normalize.base import (
    Context,
    Invalid,
    NormalizeResult,
    Ok,
    Raw,
    decoded_references,
    has_surrogate,
    raw_key,
)
from snowcrawl.schemas.types import FieldSpec, FieldType

# WHATWG URL Standard, basic URL parser: strip leading and trailing C0 controls and spaces, and remove every ASCII
# tab or newline.
_C0_AND_SPACE = "".join(chr(c) for c in range(0x21))
_TAB_OR_NEWLINE = re.compile(r"[\t\n\r]")
# A reference of C0 controls, spaces and Unicode White_Space (`\s`) only carries no URL. `\s` already
# covers U+0009-000D, U+001C-001F and the space, so the class lists only the other C0 controls.
_BLANK = re.compile(r"[\x00-\x08\x0e-\x1b\s]*")
# RFC 3986 §2.1 and §6.2.2.1: percent-encoding triplets; hex digits compare case-insensitively, uppercase is
# canonical.
_PERCENT_TRIPLET = re.compile(r"%([0-9A-Fa-f]{2})")
# RFC 3986 §2.3: unreserved characters. Percent-encoded, they are equivalent to the character.
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# RFC 3986 §3.3 `pchar` without the unreserved set (which `quote` never encodes), plus `/`; §3.4 adds `?` for the
# query. `%` stays, so existing triplets and a bare `%` (a WHATWG validation error, not a failure) are kept.
_PATH_SAFE = "!$&'()*+,;=:@/%"
_QUERY_SAFE = _PATH_SAFE + "?"
# WHATWG URL Standard, forbidden domain code points: the forbidden host code points (U+0000, tab, LF, CR, space, `#`,
# `/`, `:`, `<`, `>`, `?`, `@`, `[`, `\`, `]`, `^`, `|`), the other C0 controls, `%` and U+007F DELETE.
_FORBIDDEN_IN_DOMAIN = frozenset(" #/:<>?@[\\]^|%\x7f" + "".join(chr(c) for c in range(0x20)))
# RFC 9110 §4.2.1 and §4.2.2: the default ports of http and https, the only schemes of the type.
_DEFAULT_PORT = {"http": 80, "https": 443}
# A relative reference with no base to resolve it against; `resolve.derive` absolutizes these.
RELATIVE_WITHOUT_BASE = Invalid("relative_without_base")
# The registered media types of a file name's extension (IANA media types, as Python's built-in `mimetypes` table holds
# them; `MimeTypes()` reads that table only, never the host's files, so the answer is the same on every machine).
_MEDIA_TYPES = mimetypes.MimeTypes()
# RFC 6838 §4.2.2: the top-level type of image media.
_IMAGE_TYPE = "image"


def normalize(raw: Raw, spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """Normalize a url or image_url field."""
    if spec.type not in (FieldType.URL, FieldType.IMAGE_URL):
        raise TypeError(f"the url normalizer does not handle type {spec.type}")
    if not isinstance(raw, str):
        return Invalid("not_a_string")
    result = canonical(raw, ctx.base_url)
    if (
        spec.type is FieldType.IMAGE_URL
        and isinstance(result, Ok)
        and ctx.page_url is not None
        and canonical(ctx.page_url, None) == result
    ):
        # Without a fetch there is no content-type evidence, so the structural checks are that an
        # image is not the page it appears on, and that its path names no file of another media type (`_other_media`).
        return Invalid("page_url")
    if spec.type is FieldType.IMAGE_URL and isinstance(result, Ok) and _other_media(result.value):
        return Invalid("not_an_image")
    if isinstance(result, Ok) and (written := _written(raw, ctx.base_url)) != result.value:
        return replace(result, stated=written)
    return result


def _written(raw: str, base: str | None) -> str:
    """The reference as the page writes it, resolved against the document base when relative (RFC 3986 §5.2): a
    published URL is absolute, so a reader outside the page can follow it, and otherwise keeps the page's spelling."""
    written = _reference(raw)
    return written if base is None or urlsplit(written).scheme else urljoin(base, written)


def media_type(url: str) -> str | None:
    """The registered media type the URL's path names by its file extension (RFC 6838), or `None` when it names none."""
    return name_media_type(urlsplit(url).path)


def name_media_type(name: str) -> str | None:
    """The registered media type a file name or path names by its extension (RFC 6838), or `None`."""
    return _MEDIA_TYPES.guess_type(name, strict=True)[0]


def is_image_type(media: str | None) -> bool:
    """Whether a media type is of the top-level type `image` (RFC 6838 §4.2.2)."""
    return media is not None and media.partition("/")[0] == _IMAGE_TYPE


def _other_media(value: JsonValue) -> bool:
    """Whether an image value's path names a file of a registered media type other than an image (RFC 6838 §4.2.2: an
    HTML document, a script, a video): a page or a player a source lists among its images is none of them. A path with
    no registered extension says nothing."""
    found = media_type(value) if isinstance(value, str) else None
    return found is not None and found.partition("/")[0] != _IMAGE_TYPE


def key(value: JsonValue, _spec: FieldSpec) -> Hashable:
    """Equality as a key: equal after canonicalization. A relative URL has no canonical form
    without its base."""
    url = canonical(value, None) if isinstance(value, str) else None
    return ("url", url.value, url.reasons) if isinstance(url, Ok) else raw_key(value)


def is_absolute(raw: Raw) -> bool:
    """Whether `raw` is an absolute http(s) URL (RFC 3986 §4.3) that canonicalizes with no base to resolve against."""
    return isinstance(raw, str) and isinstance(canonical(raw, None), Ok)


def canonical_url(raw: str, base: str | None) -> str | None:
    """`raw` as a canonical absolute http(s) URL, or `None` when it is not one: `canonical` for callers outside a field
    (the content formats), which drop what declines."""
    result = canonical(raw, base)
    return result.value if isinstance(result, Ok) and isinstance(result.value, str) else None


def _reference(raw: str) -> str:
    """The reference as the page writes it: a URL written into HTML or a script HTML-escaped (`?a=1&amp;b=2`) read as
    the page's parser reads an attribute value, its character references decoded (HTML Standard §13.1.4), so the query
    keeps its delimiters; then, as the WHATWG URL Standard's basic URL parser reads input, without its leading and
    trailing C0 controls and spaces and without its ASCII tabs and newlines."""
    return _TAB_OR_NEWLINE.sub("", decoded_references(raw).strip(_C0_AND_SPACE))


# A pure function of its two strings, called for one URL by every reader of it (normalization, equality, image files),
# so its results are kept; the results are frozen dataclasses, safe to share.
@lru_cache(maxsize=8192)
def canonical(raw: str, base: str | None) -> NormalizeResult:
    """The url type's canonical form of `raw`, resolved against `base` when relative. Other modules
    that canonicalize URLs outside a field (the `links` content format) call this, so there is one set of rules."""
    # A URL is percent-encoded as UTF-8 (WHATWG URL Standard), and a lone surrogate has no UTF-8 encoding.
    if has_surrogate(raw):
        return Invalid("surrogate")
    reference = _reference(raw)
    # A blank reference is empty whatever the base (it would otherwise resolve to the base).
    if _BLANK.fullmatch(reference):
        return Invalid("empty")
    try:
        parts = urlsplit(reference)
        if not parts.scheme:
            if base is None:
                return RELATIVE_WITHOUT_BASE
            if has_surrogate(base):
                return Invalid("surrogate")
            # RFC 3986 §5.2: reference resolution against the base URI.
            reference = urljoin(base, reference)
            parts = urlsplit(reference)
        port = parts.port
    except ValueError:
        return Invalid("unparsable")
    # Only http(s) URLs are values of the type. `urlsplit` lowercases the scheme
    # (RFC 3986 §3.1: schemes are case-insensitive).
    if parts.scheme not in _DEFAULT_PORT:
        return Invalid("not_http")
    # RFC 9110 §4.2.1: a recipient rejects an http(s) URI with an empty host, as `https:////host/path` has (RFC 3986
    # §3.2: the authority between `//` and the next `/` is empty). It is not repaired by skipping slashes as the WHATWG
    # parser does: the next segment is the host only when the slashes were a mistake (C7).
    if not parts.hostname:
        return Invalid("no_host")
    host = _host(parts.hostname)
    if isinstance(host, Invalid):
        return host
    # RFC 3986 §6.2.3: an empty path with an authority is "/". Dot segments are removed after percent-normalization,
    # so `%2E` counts as a dot (WHATWG URL Standard, single- and double-dot segments).
    path = _remove_dot_segments(quote(_normalize_triplets(parts.path) or "/", safe=_PATH_SAFE))
    # The fragment is dropped: it names a part of the document, not a different resource (RFC 3986 §3.5).
    # An empty query keeps its `?`: RFC 3986 §6.2.3 removes an empty component's delimiter only where the scheme
    # allows it, and http does not.
    query = (
        f"?{quote(_normalize_triplets(parts.query), safe=_QUERY_SAFE)}" if "?" in reference.partition("#")[0] else ""
    )
    return Ok(f"{parts.scheme}://{_authority(parts, host, port)}{path}{query}")


def _host(hostname: str) -> str | Invalid:
    """The host, as the WHATWG host parser reads a domain: percent-decoded as UTF-8, and refused when it then holds
    a forbidden domain code point. An IPv6 literal (`urlsplit` checks its brackets) is not a domain."""
    if ":" in hostname:
        return hostname
    try:
        domain = unquote(hostname, errors="strict")
    except UnicodeDecodeError:
        return Invalid("unparsable")
    if not _FORBIDDEN_IN_DOMAIN.isdisjoint(domain):
        return Invalid("forbidden_host_code_point")
    # RFC 3986 §3.2.2: the host is case-insensitive, and a decoded triplet may be uppercase.
    return domain.lower()


def _authority(parts: SplitResult, hostname: str, port: int | None) -> str:
    # `hostname` is lowercased and has no IPv6 brackets.
    host = f"[{hostname}]" if ":" in hostname else hostname
    userinfo, at, _ = parts.netloc.rpartition("@")
    # RFC 9110 §4.2: the scheme's default port is the same as no port.
    shown_port = f":{port}" if port is not None and port != _DEFAULT_PORT[parts.scheme] else ""
    return f"{userinfo}{at}{host}{shown_port}"


def _normalize_triplets(component: str) -> str:
    return _PERCENT_TRIPLET.sub(_normalize_triplet, component)


def _normalize_triplet(match: re.Match[str]) -> str:
    char = chr(int(match.group(1), 16))
    return char if char in _UNRESERVED else f"%{match.group(1).upper()}"


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4, for an absolute path."""
    segments = path.split("/")
    out: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(segment)
    if segments[-1] in (".", ".."):
        out.append("")
    return "/".join(out)
