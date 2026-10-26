"""URL patterns: the URLs a custom recipe serves, as a host and a path template.

A pattern is text, `host/segment/...[?key={name}&...]`. The path is split into segments (RFC 3986 §3.3); a segment is
a literal, `{name}` (exactly one non-empty segment, whose value feeds the parameter `name`: RFC 6570 level 1 expression
syntax, as a recipe's `PLACEHOLDER` writes it), `*` (exactly one non-empty segment) or, last only, `**` (the rest of
the path, any number of segments). A query item `key={name}` binds the value of the URL's query parameter `key`
(RFC 3986 §3.4, read as application/x-www-form-urlencoded, WHATWG URL §5), which the URL must state; its other query
parameters are ignored. Hosts compare case-insensitively (RFC 3986 §3.2.2), segments and values percent-decoded
(RFC 3986 §6.2.2.2), so the matched values are the ones a recipe's binders read.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Final
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import AfterValidator

from snowcrawl.config_ir.api_recipe.model import PAGE_URL
from snowcrawl.schemas.types import NAME_PATTERN

WILDCARD: Final = "*"
REST: Final = "**"
# A placeholder in the shape: what two patterns with the same shape differ in is only their parameter names.
_SHAPE_PLACEHOLDER: Final = "{}"
_NAMED: Final = re.compile(rf"\{{({NAME_PATTERN})\}}")
# RFC 3986 §3.2.2: a reg-name host (unreserved and sub-delims, percent-encoded octets) or an IP literal; lowercase,
# the case-insensitive comparison's one spelling.
_HOST: Final = re.compile(r"(?:[a-z0-9\-._~!$&'()*+,;=]|%[0-9a-f]{2})+|\[[0-9a-f:.]+\]")
# RFC 3986 §3.3 pchar, without the braces of a placeholder.
# RFC 3986 §3.3: the pchar characters outside unreserved, which a literal need not percent-encode.
_PCHAR_SAFE: Final = "!$&'()*+,;=:@"
_LITERAL: Final = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})+")


@dataclass(frozen=True, slots=True)
class Pattern:
    """A parsed pattern: the host, the path segments as written (`**` only last) and the query keys, sorted, with the
    parameter each binds."""

    host: str
    segments: tuple[str, ...]
    query: tuple[tuple[str, str], ...]

    @property
    def rest(self) -> bool:
        return bool(self.segments) and self.segments[-1] == REST

    @property
    def path(self) -> tuple[str, ...]:
        """The segments before a trailing `**`."""
        return self.segments[:-1] if self.rest else self.segments

    @property
    def literals(self) -> int:
        return sum(1 for segment in self.path if _kind(segment) == "literal")

    @property
    def names(self) -> list[str]:
        return [s[1:-1] for s in self.path if _kind(s) == "name"] + [name for _, name in self.query]


def _kind(segment: str) -> str:
    if segment == WILDCARD:
        return "wildcard"
    if segment == REST:
        return "rest"
    return "name" if _NAMED.fullmatch(segment) else "literal"


@lru_cache(maxsize=1024)
def parse(text: str) -> Pattern:
    """The pattern `text` writes; raises `ValueError` for text that is not one."""
    head, question, query = text.partition("?")
    host, _, path = head.partition("/")
    if _HOST.fullmatch(host) is None:
        raise ValueError(f"not a lowercase host: {host!r}")
    segments = tuple(path.split("/")) if path else ()
    for index, segment in enumerate(segments):
        _check_segment(segment, last=index == len(segments) - 1)
    pattern = Pattern(host, segments, _query(query) if question else ())
    names = pattern.names
    if len(set(names)) != len(names):
        raise ValueError("a parameter named twice")
    if PAGE_URL in names:
        raise ValueError(f"{PAGE_URL} is the page URL, not a parameter")
    return pattern


def _check_segment(segment: str, *, last: bool) -> None:
    kind = _kind(segment)
    if kind == "rest" and not last:
        raise ValueError(f"{REST} stands only for the rest of the path, last")
    if kind == "literal" and _LITERAL.fullmatch(segment) is None:
        raise ValueError(f"a segment is a literal, {{name}}, {WILDCARD} or {REST}: {segment!r}")


def _query(query: str) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for item in query.split("&"):
        key, _, value = item.partition("=")
        named = _NAMED.fullmatch(value)
        if not key or named is None or _LITERAL.fullmatch(key) is None:
            raise ValueError(f"a query item is key={{name}}: {item!r}")
        items.append((unquote(key), named[1]))
    if len({key for key, _ in items}) != len(items):
        raise ValueError("a query key named twice")
    return tuple(sorted(items))


def check_pattern(text: str) -> str:
    parse(text)
    return text


UrlPattern = Annotated[str, AfterValidator(check_pattern)]


def params(text: str) -> set[str]:
    """The parameters the pattern binds."""
    return set(parse(text).names)


def match(text: str, url: str) -> dict[str, str] | None:
    """The parameters the pattern binds from `url`, decoded; None when `url` does not match it."""
    pattern = parse(text)
    parts = urlsplit(url)
    if _host(parts.hostname) != pattern.host:
        return None
    segments = [unquote(segment) for segment in _segments(parts.path)]
    bound = _match_path(pattern, segments)
    if bound is None:
        return None
    stated = {key: value for key, value in parse_qsl(parts.query) if value}
    for key, name in pattern.query:
        if key not in stated:
            return None
        bound[name] = stated[key]
    return bound


def _host(hostname: str | None) -> str:
    # RFC 3986 §3.2.2: an IP literal is written in brackets, which `hostname` leaves out.
    host = hostname or ""
    return f"[{host}]" if ":" in host else host


def _segments(path: str) -> list[str]:
    # RFC 3986 §6.2.3: an empty path and `/` are the same resource of an http(s) URL, with no segment. A pattern
    # holds no empty segment, so the empty one a trailing `/` leaves is not matched either.
    segments = path[1:].split("/") if path not in ("", "/") else []
    return segments[:-1] if segments and not segments[-1] else segments


def _match_path(pattern: Pattern, segments: list[str]) -> dict[str, str] | None:
    path = pattern.path
    if len(segments) < len(path) or (len(segments) > len(path) and not pattern.rest):
        return None
    bound: dict[str, str] = {}
    for template, segment in zip(path, segments, strict=False):
        kind = _kind(template)
        if kind == "literal" and unquote(template) != segment:
            return None
        if kind != "literal" and not segment:
            return None
        if kind == "name":
            bound[template[1:-1]] = segment
    return bound


def shape(text: str) -> str:
    """The pattern with every `{name}` and `*` written `{}` (both match one non-empty segment), its literals in one
    percent-encoding (RFC 3986 §6.2.2.2) and its query keys sorted: two patterns of one shape match the same URLs, so
    no two active recipes of a scope hold one shape."""
    pattern = parse(text)
    path = "/".join(_shape_segment(s) for s in pattern.segments)
    query = "&".join(f"{key}={_SHAPE_PLACEHOLDER}" for key, _ in pattern.query)
    return pattern.host + (f"/{path}" if pattern.segments else "") + (f"?{query}" if query else "")


def _shape_segment(segment: str) -> str:
    kind = _kind(segment)
    if kind in ("name", "wildcard"):
        return _SHAPE_PLACEHOLDER
    return segment if kind == "rest" else quote(unquote(segment), safe=_PCHAR_SAFE)


def patterns_shape(texts: Iterable[str]) -> str:
    """The shape of a pattern set: its patterns' shapes, sorted and deduplicated, one per line (no shape holds a line
    break); `''` for none, the learned config's."""
    return "\n".join(sorted({shape(text) for text in texts}))


def _specificity(text: str) -> tuple[int, int, int, str]:
    pattern = parse(text)
    # More literal segments, then a longer path, then more query keys; the shape breaks a tie, the same way each time.
    return (-pattern.literals, -len(pattern.path), -len(pattern.query), shape(text))


def most_specific(texts: Iterable[str], url: str) -> str | None:
    """The most specific of the patterns that match `url`; None when none does."""
    matching = [text for text in texts if match(text, url) is not None]
    return min(matching, key=_specificity, default=None)


def generalize(urls: Sequence[str]) -> list[str]:
    """Patterns for sample URLs: the samples of one host and one path length make one pattern whose segments equal on
    every sample stay literal and the rest become `{p<i>}`, `i` the segment's index (`segment_param`). Query
    parameters are left out."""
    groups: dict[tuple[str, int], list[list[str]]] = {}
    for url in urls:
        parts = urlsplit(url)
        segments = _segments(parts.path)
        groups.setdefault((_host(parts.hostname), len(segments)), []).append(segments)
    patterns = []
    for (host, _), samples in sorted(groups.items()):
        path = [_general(index, column) for index, column in enumerate(zip(*samples, strict=True))]
        patterns.append(check_pattern(host + "".join(f"/{segment}" for segment in path)))
    return patterns


def segment_param(index: int) -> str:
    """The name of the parameter a generalized pattern gives path segment `index` (from 0): `p<index>`. A recipe's
    binder of that segment (`path:<index>`) has the same name, so one name is one value."""
    return f"p{index}"


def _general(index: int, column: tuple[str, ...]) -> str:
    first = column[0]
    if all(segment == first for segment in column) and _LITERAL.fullmatch(first):
        return first
    return f"{{{segment_param(index)}}}"
