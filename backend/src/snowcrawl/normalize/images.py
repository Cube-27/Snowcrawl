"""Image policy (normalization and resolution): which values name one image file, and which of its renditions
publishes.

- The file: an absolute URL's canonical authority and path, scheme, query and stated size aside (`resource_key`), or a
  rendition an image server writes into the path (`same_file`); its family, the images a server names after it
  (`same_family`).
- The rendition: of renditions of one file, the largest one states (`largest_rendition`), of those over `https` if any
  is (`preferred_rendition`: RFC 9110 §4.2.2, the same file reached over TLS).
- A list of images names each file once (`distinct_files`); values that agree publish the secure one
  (`preferred_value`), each file at the largest rendition any of them states (`largest_of_each`).
- A list whose files another list holds, with more, is that list's representative part (`representative_part`).
"""

import posixpath
import re
from collections.abc import Hashable, Sequence
from functools import lru_cache
from typing import Final
from urllib.parse import parse_qsl, urljoin, urlsplit

from pydantic import JsonValue

from snowcrawl.normalize.base import Ok
from snowcrawl.normalize.url import canonical, is_image_type, name_media_type

# RFC 9110 §4.2.1 and §4.2.2: http and https, the schemes of the url type.
_SCHEMES = frozenset({"http", "https"})
_HTTPS = "https"  # RFC 9110 §4.2.2


def resource_key(value: JsonValue) -> tuple[str, str, str] | None:
    """The stored resource an absolute URL names: its canonical authority and path (RFC 3986 §3.2, §3.3: the path
    identifies the resource within the authority), without the scheme, which only says how the authority is reached
    (http or https), and without the query, which may vary only how it is served, such as an image's size or
    crop. Not an equality (two queries can name two resources): it only tells which of one entity's own values
    another source names, and which values are renditions of one file. `None` for anything that is no
    absolute URL. An image proxy's URL, whose query names the file it serves in one `url` parameter (an image
    optimizer's `?url=<file>&w=640`), names that file's resource, resolved against the proxy URL (RFC 3986 §5)."""
    return _resource(value) if isinstance(value, str) else None


# Resolution compares every image of a field with every other one's, so a URL's file is read once.
@lru_cache(maxsize=8192)
def _resource(value: str) -> tuple[str, str, str] | None:
    # A loop, not recursion: a proxy URL may name a proxy URL in turn, as deep as its length allows.
    while True:
        url = canonical(value, None)
        if not isinstance(url, Ok) or not isinstance(url.value, str):
            return None
        parts = urlsplit(url.value)
        served = [v for name, v in parse_qsl(parts.query) if name == _PROXIED]
        if not (len(served) == 1 and served[0]):
            return ("resource", parts.netloc, _without_dimensions(parts.path))
        value = urljoin(url.value, served[0])  # a file named in the query is shorter than the URL, so this ends
        if urlsplit(value).scheme not in _SCHEMES:
            return None


def same_file(a: JsonValue, b: JsonValue) -> bool:
    """Whether two image URLs name one file (`resource_key`), or two renditions of it that an image server writes into
    the path rather than the query: on one authority, both paths hold the same file name (the first segment naming an
    image file by its extension, RFC 6838), anything after it being a rendition below the file
    (`<file>.jpg/renditions/600.jpg`), and the directories before it, read back from the file, are the same, a segment
    with rendition parameters after a separator standing for the bare one (`/1234-800-auto/` for `/1234/` or
    `/1234-600-auto/`); further
    leading segments of the longer path are the image service's prefix (`/image/v2/<file's path>`). At least one
    directory must be read alike: a bare file name is no evidence of one file."""
    first, second = resource_key(a), resource_key(b)
    if first is None or second is None:
        return False
    if first == second:
        return True
    one, other = _file_path(first), _file_path(second)
    if one is None or other is None or one[0] != other[0] or one[-1] != other[-1]:
        return False
    directories = list(zip(reversed(one[1:-1]), reversed(other[1:-1]), strict=False))
    return bool(directories) and all(_rendition_segment(x, y) for x, y in directories)


def same_family(file: JsonValue, other: JsonValue) -> bool:
    """Whether `other` names `file` (`same_file`) or another image an image server names after it: on one authority
    and in one directory (`resource_key`), a file name that is `file`'s, its extension aside, continued after a
    separator (no letter or digit) with more (`name_m`, `name_d1.jpg` beside `name.jpg`). A structural property of how
    a server names the views and renditions of one file; it says the images are related, not which one is a view."""
    if same_file(file, other):
        return True
    first, second = resource_key(file), resource_key(other)
    if first is None or second is None or first[1] != second[1]:
        return False
    directory, _, name = first[2].rpartition("/")
    other_directory, _, other_name = second[2].rpartition("/")
    stem, other_stem = posixpath.splitext(name)[0], posixpath.splitext(other_name)[0]
    return (
        directory == other_directory
        and bool(stem)
        and len(other_stem) > len(stem) + 1
        and other_stem.startswith(stem)
        and not other_stem[len(stem)].isalnum()
    )


@lru_cache(maxsize=8192)
def _file_path(key: tuple[str, str, str]) -> tuple[str, ...] | None:
    """The authority, then the path's segments up to the first that names an image file (`same_file`)."""
    segments = [segment for segment in key[2].split("/") if segment]
    for index, segment in enumerate(segments):
        name = _ENCODED_QUERY.split(segment, maxsplit=1)[0]
        if is_image_type(name_media_type(name)):
            return (key[1], *segments[:index], name)
    return None


def _rendition_segment(x: str, y: str) -> bool:
    """Whether two path segments are one, or one base each continues with rendition parameters (after a separator, no
    letter or digit, and holding a digit: a size): `1234-800-auto`, `1234-600-auto` and `1234` name one directory."""
    if x == y:
        return True
    base = min(len(x), len(y))
    while base and x[:base] != y[:base]:
        base -= 1
    while base and not all(len(s) == base or not s[base].isalnum() for s in (x, y)):
        base -= 1
    return base > 0 and all(len(s) == base or any(c.isdigit() for c in s[base:]) for s in (x, y))


# A query delimiter percent-encoded into the path (`photo.png%3Fwidth%3D785`): the URL was percent-encoded once more
# than written (RFC 3986 §2.4: a string is encoded once), so the file's name ends before it.
_ENCODED_QUERY = re.compile(r"%3F", re.IGNORECASE)
# The query parameter by which an image proxy or optimizer names the file it serves (`/_next/image?url=`, `?url=`).
_PROXIED: Final = "url"
# What an image path states about the rendition it serves, not the file: a whole segment `<width>x<height>` or a list
# of `<letters>_<value>` transform pairs one of which is a number, decimal or not (`w_300,h_200`, `c_fill,w_458`,
# `dpr_2.0`); a size closing the file's name before its extension
# (`name-300x200.ext`, `name_1024x.ext`, `name@2x.ext`); a format conversion appended as a second extension
# (`name.jpg.webp`); a trailing `=<options>` on the last segment.
_SIZE_SEGMENT = re.compile(r"\d+x\d+")
_PAIR = re.compile(r"[a-z]+_[\w.:]+", re.IGNORECASE)
_NUMERIC_PAIR = re.compile(r"[a-z]+_\d+(?:\.\d+)?", re.IGNORECASE)
_SIZE_SUFFIX = re.compile(r"(?<=[^/])[-_@](?:\d+x\d*|x\d+)(?=\.[^./]+$)", re.IGNORECASE)
_CONVERSION = re.compile(r"(\.[a-z0-9]{2,5})\.[a-z0-9]{2,5}$", re.IGNORECASE)
_OPTIONS = re.compile(r"(?<=[^/=])=[\w,-]+$")


def _rendition(segment: str) -> bool:
    """Whether a whole path segment states a rendition (`_PAIR`): `<width>x<height>`, or pairs one of them a number."""
    if _SIZE_SEGMENT.fullmatch(segment):
        return True
    pairs = segment.split(",")
    return all(_PAIR.fullmatch(p) for p in pairs) and any(_NUMERIC_PAIR.fullmatch(p) for p in pairs)


def _without_dimensions(path: str) -> str:
    kept = [segment for segment in path.split("/") if not _rendition(segment)]
    stripped = _OPTIONS.sub("", "/".join(kept))
    return _CONVERSION.sub(r"\1", _SIZE_SUFFIX.sub("", stripped))


# A size has a few digits; a longer run (an id or a stamp) is no size, and stays clear of `int`'s digit limit. A
# decimal (a pixel ratio, `dpr_1.5`) is a scale factor, no size: neither of its digit runs is read.
_NUMBER = re.compile(r"(?<![\d.])\d{1,9}(?![\d.])")


def largest_rendition(values: Sequence[str]) -> int:
    """The index of the largest of renditions of one file (`resource_key`), or 0 when none is shown larger.

    A rendition states its size in what `resource_key` sets aside: the path's dimension segments, size suffix and
    options, and query parameters (RFC 3986 §3.4) every rendition states with a decimal value that differ between them
    (a parameter equal in all, such as a version stamp, says nothing of size, and one only some state, such as a scale
    factor, compares with no other). The largest is one at least as large as every other in each size they share: a
    rendition larger in one size and smaller in another (`1200x630` beside `1000x1000`) is another crop of the file,
    such as the card an Open Graph `og:image` is cut to (Open Graph protocol, "Structured Properties":
    `og:image:width`, `og:image:height`), not a larger copy, so none is largest then. Of equal sizes the first wins. A
    rendition stating none may be the unresized file or a default one, so nothing is ranked then."""
    sizes = [_stated_size(value, values) for value in values]
    if not sizes or not all(sizes):
        return 0
    return next((i for i, size in enumerate(sizes) if all(_covers(size, other) for other in sizes)), 0)


def _covers(size: tuple[int, ...], other: tuple[int, ...]) -> bool:
    """Whether a rendition stating `size` is at least as large as one stating `other` in each size they share.
    Renditions stating as many sizes read them alike (the path's in order, the query's by name). Else which of one's
    sizes is which of the other's is not stated, so it is at least as large under every pairing (its smallest size is
    at least the other's largest), and a rendition stating sizes the other does not (a crop, `1200x630` before a
    `500` resize) must be larger: on a tie only those sizes would rank it, and they compare with nothing the other
    states."""
    if len(size) == len(other):
        return all(mine >= theirs for mine, theirs in zip(size, other, strict=True))
    return min(size) > max(other) if len(size) > len(other) else min(size) >= max(other)


def _stated_size(value: str, renditions: Sequence[str]) -> tuple[int, ...]:
    """The sizes a rendition states (`largest_rendition`): the path's in order, then the varying query's by name."""
    parts = urlsplit(value)
    path = parts.path
    stated = [m.group() for m in (_SIZE_SUFFIX.search(path), _OPTIONS.search(path)) if m is not None]
    stated += [segment for segment in path.split("/") if _rendition(segment)]
    query = _numeric_query(parts.query)
    others = [_numeric_query(urlsplit(r).query) for r in renditions]
    varying = [
        v for name, v in sorted(query.items()) if all(name in o for o in others) and any(o[name] != v for o in others)
    ]
    return (*(int(n) for text in stated for n in _NUMBER.findall(text)), *varying)


def _numeric_query(query: str) -> dict[str, int]:
    return {name: int(v) for name, v in parse_qsl(query) if _NUMBER.fullmatch(v)}


def distinct_files(values: Sequence[JsonValue]) -> list[JsonValue]:
    """Image values with each file once (`resource_key`), at the place of the file's first rendition: of its
    renditions the largest (`largest_rendition`) of those over `https` if any is (RFC 9110 §4.2.2: the same file
    reached over TLS). Values that are no absolute URL are kept as they are."""
    groups: dict[Hashable, list[str]] = {}
    for value in values:
        key = resource_key(value)
        if key is not None and isinstance(value, str):
            groups.setdefault(key, []).append(value)
    kept: list[JsonValue] = []
    for value in values:
        key = resource_key(value)
        group = groups.pop(key, None) if key is not None else None
        if group is not None:
            kept.append(preferred_rendition(group))
        elif key is None:
            kept.append(value)
    return kept


def preferred_rendition(renditions: Sequence[str]) -> str:
    """Of renditions of one file, the largest (`largest_rendition`) of those over `https`, else of all."""
    secure = [r for r in renditions if urlsplit(r).scheme == _HTTPS] or list(renditions)
    return secure[largest_rendition(secure)]


def secure(value: JsonValue) -> bool:
    """Whether every URL of an image value (a URL or a list of them) uses `https` (RFC 9110 §4.2.2)."""
    items = value if isinstance(value, list) else [value]
    return all(isinstance(item, str) and urlsplit(item).scheme == _HTTPS for item in items)


def preferred_value(values: Sequence[JsonValue], *, single: bool) -> int:
    """The index of the value that publishes of image values that agree or are renditions of one file: of those whose
    URLs all use `https` if any, else of all (a rendition over `http` beside its `https` twin names the same file, and
    `https` reaches it over TLS, RFC 9110 §4.2.2), the largest rendition of a `single` image (`largest_rendition`),
    else the first."""
    places = [i for i, value in enumerate(values) if secure(value)] or list(range(len(values)))
    scalars = [value for i in places if isinstance(value := values[i], str)]
    if single and len(scalars) == len(places):
        return places[largest_rendition(scalars)]
    return places[0]


def image_list(value: JsonValue) -> list[str] | None:
    """An image value as its URLs, a scalar as one; None when an item is no URL string."""
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, str)] if all(isinstance(i, str) for i in items) else None


def largest_of_each(images: list[str], others: Sequence[list[str]]) -> list[str]:
    """Each of `images`, in its order, at its largest rendition among `others`' renditions of its file (`same_file`),
    whatever position they list it at."""
    renditions = [[item, *(r for listed in others for r in listed if same_file(item, r))] for item in images]
    return [preferred_rendition(found) for found in renditions]


def representative_part(own: Sequence[str], other: Sequence[str], *, first_only: bool) -> bool:
    """Whether the list `own` is the representative part of `other`: `other` names a file `own` does not, and every
    file of `own` (`first_only`: its first, for an Open Graph list, whose first `og:image` represents the object, Open
    Graph protocol "Basic Metadata" and "Arrays") is among `other`'s (`same_file`)."""
    named = own[:1] if first_only else own
    extra = any(not any(same_file(image, file) for file in own) for image in other)
    return extra and all(any(same_file(file, image) for image in other) for file in named)
