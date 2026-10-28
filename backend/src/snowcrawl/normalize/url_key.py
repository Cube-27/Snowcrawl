"""URL canonicalization: the form `map` and the crawl frontier dedup URLs by, and the form every job
kind keys its `url_task` by, so a page's entity key does not depend on the job that fetched it.

The url type's canonical form (WHATWG parsing, the RFC 3986 §6.2 normalizations, the fragment
removed), with the query's name-value pairs in the order WHATWG `URLSearchParams.sort()` gives them. No parameter is
dropped or rewritten by name: there is no list of parameter names (C4).

The row keys of one listing page drop its session parameters (`session_parameters`): a parameter that every row link
carries with one same value, and that the page's own URLs do not carry, names the visit, not the item. It
is read from the page, never from a list, and only dropped, never invented (C7).

Whether two URLs name one document or one item is read by one pair of keys, used for comparison only and never
published (a URL publishes as the page writes it): `document_key` is the canonical form's scheme, authority and path,
one trailing slash of the path aside (RFC 3986 §3 names the resource by them; §6.2.4: `/a/` and `/a` may name one
resource, while `/a//` is another path), so a query (§3.4: it selects within the resource, a variant or a view) and a
fragment name no other document. `item_key` adds the query as a multiset of its decoded name-value pairs, in
any order (a repeated pair counts twice): two URLs name one item when their pairs are the same.
"""

from collections import Counter
from collections.abc import Collection, Iterable
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from snowcrawl.normalize.url import canonical_url


def canonicalize(url: str, base: str | None = None) -> str | None:
    """`url`, resolved against `base` when relative, in canonical form; `None` when it is not an http(s) URL."""
    canonical = canonical_url(url, base)
    if canonical is None:
        return None
    head, mark, query = canonical.partition("?")
    return f"{head}?{_sorted(query)}" if mark else canonical


def _sorted(query: str) -> str:
    # WHATWG URL Standard, application/x-www-form-urlencoded parsing: the query splits on `&`, and empty sequences
    # are skipped. `sort()` orders the pairs by name, stably, so pairs of one name keep their order.
    return "&".join(sorted((pair for pair in query.split("&") if pair), key=_name))


def _name(pair: str) -> bytes:
    # The name as the form parser decodes it (`+` is a space, then percent-decoding as UTF-8), compared by UTF-16
    # code units as `sort()` compares names. The pair itself keeps the url type's bytes.
    return unquote_plus(pair.partition("=")[0]).encode("utf-16-be")


DocumentKey = tuple[str, str, str]
ItemKey = tuple[DocumentKey, tuple[tuple[str, str], ...]]


def document_key(url: str, base: str | None = None) -> DocumentKey | None:
    """The document `url`, resolved against `base` when relative, names (module docstring): scheme, authority and path
    of its canonical form, one trailing slash of the path aside; `None` when it is not an http(s) URL."""
    key = item_key(url, base)
    return None if key is None else key[0]


def item_key(url: str, base: str | None = None) -> ItemKey | None:
    """The item `url`, resolved against `base` when relative, names (module docstring): its `document_key` and its
    query's decoded name-value pairs, sorted; `None` when it is not an http(s) URL."""
    canonical = canonical_url(url, base)
    if canonical is None:
        return None
    parts = urlsplit(canonical)
    pairs = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return (parts.scheme, parts.netloc, parts.path.removesuffix("/")), pairs


def refined_by(url: str, other: str) -> bool:
    """Whether `other` names the item `url` names, or that item refined (module docstring's keys): one document, and
    `url`'s query pairs all among `other`'s, as a multiset (RFC 3986 §3.4: a query selects within the resource; WHATWG
    URL Standard, application/x-www-form-urlencoded parsing). A pair `other` adds, whatever its name, narrows or
    annotates the same item; a `url` without a query names the document, not one item in it, so it is refined by no
    URL with a query. Neither URL resolves against a base."""
    mine, theirs = item_key(url), item_key(other)
    if mine is None or theirs is None or mine[0] != theirs[0]:
        return False
    return mine[1] == theirs[1] or (bool(mine[1]) and Counter(mine[1]) <= Counter(theirs[1]))


def session_parameters(row_urls: Collection[str], page_urls: Iterable[str]) -> frozenset[str]:
    """The names of the query parameters that carry no item identity on a listing page: each of two or more row URLs
    carries the parameter once and with one same value, and none of the page's own URLs (`collect.run`'s `page_urls`)
    carries it. One distinct value per row is identity and is kept; a single row shows nothing to compare."""
    if len(row_urls) < 2:
        return frozenset()
    shared: Counter[tuple[str, str]] = Counter()
    for url in row_urls:
        pairs = _pairs(url)
        names = [name for name, _ in pairs]
        shared.update(pair for pair in set(pairs) if names.count(pair[0]) == 1)
    on_page = {name for url in page_urls for name, _ in _pairs(url)}
    return frozenset(name for (name, _), n in shared.items() if n == len(row_urls) and name not in on_page)


def without_parameters(url: str, names: Collection[str]) -> str:
    """`url` with the query pairs named in `names` removed; a query left empty loses its `?`."""
    head, mark, query = url.partition("?")
    if not mark or not names:
        return url
    kept = [pair for pair in query.split("&") if pair and _decoded(pair) not in names]
    return f"{head}?{'&'.join(kept)}" if kept else head


def _pairs(url: str) -> list[tuple[str, str]]:
    """The query's (decoded name, pair as written) list, empty sequences skipped as the form parser skips them."""
    _, mark, query = url.partition("?")
    return [(_decoded(pair), pair) for pair in query.split("&") if pair] if mark else []


def _decoded(pair: str) -> str:
    return unquote_plus(pair.partition("=")[0])
