"""`normalize.url_key.document_key`, `item_key` and `refined_by`: the one rule by which two URLs name one document,
one item, or one item and that item refined."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from snowcrawl.normalize.url_key import document_key, item_key, refined_by


@pytest.mark.parametrize(
    ("a", "b", "same_document", "same_item"),
    [
        ("https://example.com/p", "https://example.com/p/", True, True),
        ("https://example.com/p", "https://example.com/p//", False, False),
        ("https://example.com/p", "https://example.com/p?v=1", True, False),
        ("https://example.com/p?a=1&b=2", "https://example.com/p/?b=2&a=1", True, True),
        ("https://example.com/p?a=1&a=1", "https://example.com/p?a=1", True, False),
        ("https://example.com/p#top", "https://example.com/p", True, True),
        ("https://EXAMPLE.com:443/p", "https://example.com/p", True, True),
        ("http://example.com/p", "https://example.com/p", False, False),
        ("https://example.com/p", "https://example.org/p", False, False),
        ("https://example.com:8443/p", "https://example.com/p", False, False),
        ("https://example.com/P", "https://example.com/p", False, False),
    ],
)
def test_keys(a: str, b: str, same_document: bool, same_item: bool) -> None:
    assert (document_key(a) == document_key(b)) is same_document
    assert (item_key(a) == item_key(b)) is same_item


def test_a_relative_url_resolves_against_the_base_and_no_http_url_has_a_key() -> None:
    assert item_key("p/?b=2&a=1", "https://example.com/c/") == item_key("https://example.com/c/p?a=1&b=2")
    assert document_key("mailto:a@example.com") is None
    assert item_key("/p") is None


_segments = st.lists(st.text("abcz09-", min_size=1, max_size=4), min_size=1, max_size=3)
_pairs = st.lists(st.tuples(st.sampled_from("abc"), st.text("xyz09", max_size=2)), max_size=4)


def _url(path: str, pairs: list[tuple[str, str]]) -> str:
    query = "&".join(f"{name}={value}" for name, value in pairs)
    return f"https://example.com{path}" + (f"?{query}" if query else "")


@given(_segments, _pairs.flatmap(lambda pairs: st.tuples(st.just(pairs), st.permutations(pairs))), st.booleans())
def test_the_keys_hold_under_one_trailing_slash_and_query_order(
    segments: list[str], pairs: tuple[list[tuple[str, str]], list[tuple[str, str]]], slash: bool
) -> None:
    path = "/" + "/".join(segments)
    written, reordered = pairs
    assert item_key(_url(path, written)) == item_key(_url(path + "/" * slash, reordered))
    assert document_key(_url(path, written)) == document_key(_url(path + "/" * slash, []))


@given(_segments, _pairs, _pairs, st.booleans())
def test_a_url_is_refined_by_one_adding_pairs_only_when_it_has_a_query(
    segments: list[str], own: list[tuple[str, str]], added: list[tuple[str, str]], slash: bool
) -> None:
    path = "/" + "/".join(segments)
    url, request = _url(path, own), _url(path + "/" * slash, [*added, *own])
    assert refined_by(url, request) is (bool(own) or not added)
    assert refined_by(url, _url(path + "x", [*own, *added])) is False  # another document
    if added:  # a pair `url` lacks: the request names another item than `url`, never a refinement of it
        assert refined_by(request, url) is False
