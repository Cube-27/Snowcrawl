"""The url and image_url normalizer: WHATWG-style parsing, absolutization against
the document base, RFC 3986 canonicalization, and declines on empty, relative-without-base and non-http(s) input."""

from datetime import UTC, datetime
from urllib.parse import urljoin

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.normalize import images, url
from snowcrawl.normalize.base import Context, Invalid, NormalizeResult, Ok
from snowcrawl.schemas.types import FieldSpec, FieldType

AT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = Context(captured_at=AT)
URL = FieldSpec(type=FieldType.URL)
IMAGE = FieldSpec(type=FieldType.IMAGE_URL)

_label = st.from_regex(r"[A-Za-z0-9]([A-Za-z0-9-]{0,8}[A-Za-z0-9])?", fullmatch=True)
_host = st.lists(_label, min_size=1, max_size=3).map(lambda labels: ".".join([*labels, "test"]))
_segment = st.text(max_size=8).filter(lambda s: "/" not in s and "?" not in s and "#" not in s)
_urls = st.builds(
    lambda scheme, host, port, segments, query, fragment: (
        f"{scheme}://{host}{port}/{'/'.join(segments)}{query}{fragment}"
    ),
    st.sampled_from(["http", "https", "HTTP", "Https"]),
    _host,
    st.sampled_from(["", ":80", ":443", ":8080"]),
    st.lists(_segment, max_size=4),
    st.sampled_from(["", "?", "?a=1&b=%7e", "?q=café x"]),
    st.sampled_from(["", "#", "#top", "#a?b"]),
)


@given(st.sampled_from([URL, IMAGE]), _urls)
def test_output_is_absolute_http_without_a_fragment_and_normalizes_to_itself(spec: FieldSpec, raw: str) -> None:
    result = url.normalize(raw, spec, CTX)

    assert isinstance(result, Ok)
    assert isinstance(result.value, str)
    assert result.value.startswith(("http://", "https://"))
    assert "#" not in result.value
    assert url.normalize(result.value, spec, CTX) == Ok(result.value)
    # The stated form is the reference as written, and it normalizes to the same value.
    assert _value(url.normalize(result.published, spec, CTX)) == result.value


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (" \x1f\t\n", "empty"),
        ("http://a.test/\ud800", "surrogate"),
        ("javascript:alert(1)", "not_http"),
        ("data:image/png;base64,iVBORw0KGgo=", "not_http"),
        ("ftp://a.test/p", "not_http"),
        ("http:///path", "no_host"),
        ("https:////cdn.example.test/i.png", "no_host"),  # an empty authority is declined, not repaired (C7)
        ("http://example.test:99999/", "unparsable"),
        ("http://a%FFb.example.test/", "unparsable"),
        ("http://a%20b.example.test/", "forbidden_host_code_point"),
        ("http://a<b.example.test/", "forbidden_host_code_point"),
    ],
)
def test_input_that_is_no_http_url_is_invalid(raw: str, reason: str) -> None:
    ctx = Context(captured_at=AT, base_url="https://base.test/")

    assert url.normalize(raw, URL, ctx) == Invalid(reason)
    assert url.normalize(raw, IMAGE, ctx) == Invalid(reason)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Shop.EXAMPLE.test:443/a", "https://shop.example.test/a"),
        ("http://example.test:8080/", "http://example.test:8080/"),
        ("http://example.test/%7euser/%2fkept?q=%e2%82%ac#frag", "http://example.test/~user/%2Fkept?q=%E2%82%AC"),
        ("http://example.test/a/./b/../c/%2E%2E/d", "http://example.test/a/d"),
        ("http://example.test/a b/é?x=é y", "http://example.test/a%20b/%C3%A9?x=%C3%A9%20y"),
        ("  http://exam\tple.test/a\nb\r  ", "http://example.test/ab"),
        ("http://EX%41MPLE.test/", "http://example.test/"),
        ("http://example.test/i.jpg?w=1&amp;h=2&reg=3", "http://example.test/i.jpg?w=1&h=2&reg=3"),
    ],
)
def test_absolute_urls_are_canonicalized(raw: str, expected: str) -> None:
    assert _value(url.normalize(raw, URL, CTX)) == (expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../c?x=1#f", "https://example.test/c?x=1"),
        ("/root", "https://example.test/root"),
        ("//cdn.example.test/i.png", "https://cdn.example.test/i.png"),
        ("?page=2", "https://example.test/a/b?page=2"),
    ],
)
def test_relative_urls_resolve_against_the_document_base_and_need_one(raw: str, expected: str) -> None:
    ctx = Context(captured_at=AT, base_url="https://example.test/a/b", page_url="https://example.test/other")

    result = url.normalize(raw, URL, ctx)
    assert isinstance(result, Ok)
    assert result.value == expected
    # It publishes absolute, in the page's spelling (a fragment the page writes stays).
    assert result.published == urljoin("https://example.test/a/b", raw)
    assert url.normalize(raw, URL, CTX) == Invalid("relative_without_base")


def test_an_image_url_that_is_the_page_url_is_invalid() -> None:
    ctx = Context(captured_at=AT, page_url="https://example.test/p?id=1#reviews")

    assert url.normalize("HTTPS://example.test:443/p?id=1", IMAGE, ctx) == Invalid("page_url")
    assert _value(url.normalize("HTTPS://example.test:443/p?id=1", URL, ctx)) == ("https://example.test/p?id=1")
    assert _value(url.normalize("https://example.test/p.png", IMAGE, ctx)) == ("https://example.test/p.png")


@pytest.mark.parametrize("path", ["/player/index.html?videoId=1", "/v/clip.mp4", "/app.js"])
def test_an_image_url_naming_a_file_of_another_media_type_is_invalid(path: str) -> None:
    raw = f"https://cdn.example.test{path}"

    assert url.normalize(raw, IMAGE, CTX) == Invalid("not_an_image")
    assert isinstance(url.normalize(raw, URL, CTX), Ok)
    # A path with no registered extension, or an image's, says nothing against it.
    assert isinstance(url.normalize("https://cdn.example.test/i/123/format:webp", IMAGE, CTX), Ok)


def test_the_resource_key_ignores_only_the_scheme_the_query_and_a_stated_size() -> None:
    key = images.resource_key("https://cdn.example.test/i/a-300x200.jpg?w=1")

    assert key is not None
    assert key == images.resource_key("http://cdn.example.test/i/a_64x64.jpg#zoom")
    assert key != images.resource_key("https://cdn.example.test/i/b-300x200.jpg")
    assert key != images.resource_key("https://img.example.test/i/a-300x200.jpg")
    assert images.resource_key("/i/a.jpg") is None
    # Transform-pair segments (one of them a number) state the rendition: one file at two crops and a pixel ratio.
    small = "https://cdn.example.test/t_img/c_fill,h_573,w_458/dpr_2.0/v1/a.png"
    assert images.resource_key(small) == images.resource_key("https://cdn.example.test/t_img/c_fill,w_2000/v1/a.png")
    # A pixel ratio is no size: neither rendition outranks the other by it.
    ratios = ["https://cdn.example.test/t_img/dpr_2.0/v1/a.png", "https://cdn.example.test/t_img/dpr_1.5/v1/a.png"]
    assert images.distinct_files(ratios) == ratios[:1]
    assert images.resource_key("https://cdn.example.test/men_x/a.png") != images.resource_key(
        "https://cdn.example.test/women_x/a.png"
    )


@pytest.mark.parametrize(
    ("rendition", "same"),
    [
        ("https://cdn.example.test/i/7/a.jpg?w=9", True),  # one resource_key
        ("https://cdn.example.test/image/v2/site/i/7/a.jpg", True),  # an image service's prefix
        ("https://cdn.example.test/i/7-800-auto/a.jpg", True),  # rendition parameters after a directory
        ("https://cdn.example.test/i/7/a.jpg/renditions/600.jpg", True),  # a rendition below the file
        ("https://cdn.example.test/i/77/a.jpg", False),  # another directory
        ("https://cdn.example.test/i/7/b.jpg", False),  # another file
        ("https://img.example.test/i/7/a.jpg", False),  # another authority
    ],
)
def test_renditions_an_image_server_writes_into_the_path_name_one_file(rendition: str, same: bool) -> None:
    file = "https://cdn.example.test/i/7/a.jpg"

    assert images.same_file(file, rendition) is same
    assert images.same_file(rendition, file) is same
    # Two renditions of one directory; parameters without a size are another directory.
    assert images.same_file(
        "https://cdn.example.test/i/7-800-auto/a.jpg", "https://cdn.example.test/i/7-600-auto/a.jpg"
    )
    assert not images.same_file("https://cdn.example.test/i/7-blue/a.jpg", "https://cdn.example.test/i/7-red/a.jpg")
    # A query written percent-encoded into the path still ends the file's name.
    assert images.same_file("https://cdn.example.test/i/7/a.jpg%3Fw%3D785", file)
    # A bare file name is no evidence of one file.
    assert not images.same_file("https://cdn.example.test/a.jpg", "https://cdn.example.test/i/a.jpg")


_SIDES = st.integers(min_value=1, max_value=4000)


@given(_SIDES, _SIDES, _SIDES, _SIDES)
def test_a_rendition_is_larger_only_when_it_is_at_least_as_large_in_every_size(w: int, h: int, x: int, y: int) -> None:
    file = "https://cdn.example.test/i/7/a.jpg?v=3&fit=crop"
    first, other = f"{file}&w={w}&h={h}", f"{file}&h={y}&w={x}"

    # Another crop (larger in one size, smaller in the other) never replaces the first rendition; a larger copy does.
    assert images.largest_rendition([first, other]) == (1 if x >= w and y >= h and (x, y) != (w, h) else 0)


@given(_SIDES, _SIDES, _SIDES, _SIDES)
def test_sizes_only_one_rendition_states_never_rank_it_by_themselves(w: int, h: int, resize: int, plain: int) -> None:
    file = "4f2a/widget_01.jpg"
    crop, copy = (
        f"https://cdn.example.test/i/c_fill,w_{w},h_{h}/w_{resize}/{file}",
        f"https://cdn.example.test/i/w_{plain}/{file}",
    )

    # A crop outranks a copy stating fewer sizes only when larger whichever of its sizes the copy's is; the copy
    # outranks it when at least as large as each of the crop's.
    expected = 1 if min(w, h, resize) > plain else 0
    assert images.largest_rendition([copy, crop]) == expected
    assert images.largest_rendition([crop, copy]) == (1 if plain >= max(w, h, resize) else 0)


def test_a_proxy_url_names_the_file_its_url_parameter_names() -> None:
    key = images.resource_key("https://cdn.example.test/i/a.jpg?w=940")
    proxy = "https://www.example.test/_img?url="

    assert key == images.resource_key(f"{proxy}https%3A%2F%2Fcdn.example.test%2Fi%2Fa.jpg&w=640")
    assert images.resource_key(f"{proxy}%2Fi%2Fa.jpg") == images.resource_key("https://www.example.test/i/a.jpg")
    assert images.resource_key(f"{proxy}%2Fi%2Fa.jpg") != images.resource_key(f"{proxy}%2Fi%2Fb.jpg")
    # Another parameter holding a URL (a fallback, a referrer) is no file the URL serves.
    other = "https://img.example.test/i/a.jpg?fallback=https%3A%2F%2Fcdn.example.test%2Fi%2Fz.jpg"
    assert images.resource_key(other) == ("resource", "img.example.test", "/i/a.jpg")


def _value(result: NormalizeResult) -> JsonValue:
    """The normalized value (the stated form aside, which only publishes)."""
    assert isinstance(result, Ok)
    return result.value
