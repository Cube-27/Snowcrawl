"""The structural DOM collector's image pass (`collect.dom`), and the rules it
shares with the other DOM passes: the elements set apart from the main region (`content.regions`) and where its
hyperlinks lead.

- **The main image.** The first image of the page's main region (the `main` element, else the one `role="main"`
landmark, WAI-ARIA 1.2, else the document's one `article`), outside the region's tangential content
  (`nav`, `header`, `footer`, `aside`), its dialogs (`dialog`, roles `dialog` and `alertdialog`) and the
  fallback content of media elements, shown by the cleaned text's rules, whose source is not inline `data:`
  (RFC 2397). A decorative image (an empty `alt`, roles `none` and `presentation`, WAI-ARIA 1.2) is not
  it, and neither is a link card's: an image whose nearest block holding a hyperlink holds no other image, and whose
  hyperlinks lead to one other document, illustrates that document (a teaser of another product, a promotion). It
  fills an `image_url` field whose path names schema.org `image`. Before it, the first such image (in the main
  region, else in the body) of the file (`images.same_file`) the page's structured data or Open Graph states as its
  main image (`stated_images`): independent evidence of the product's image, which a page without a main landmark (a
  client-rendered storefront) needs to have a main image at all; without one, the first image of that file outside
  dialogs and media fallback only, shown or not, decorative or not, its tangential section (not the region's others)
  then holding the product's images.
- **The gallery.** The main image's slides fill a list of images whose path names schema.org `image`: from the main
  image up, the first block with siblings of its tag holding an image; each such block's image at the main image's tag
  path, else its first image named after the main image's file (`images.same_family`), a source several blocks give (a
  placeholder) left out; none when a block links to another document (cards) or fewer than two images remain
  (`_gallery`). A slide need not be shown: a carousel hides its slides but the current one.
- **The largest copy.** The main image and each slide publish at the largest rendition of their file
  (`images.same_file`, `images.largest_rendition`) any `img` or `picture` `source` of the region states (`Copies`): a
  thumbnail strip, the slides, a zoom layer and a grid state one image at their own sizes, and the first read may be
  the smallest.
- **An image's source.** Its `src`, unless its `srcset` leaves `src` out of its source set (HTML Standard §4.8.4.3:
  width descriptors, or a candidate of pixel density 1, which a candidate without a descriptor is): then the widest
  candidate, else the densest (a `2x` candidate is a larger rendition of the image a `1x` one shows). A lazy loader
  keeps an image's sources in the custom data attributes named after the standard ones (`data-srcset`, `data-src`,
  HTML Standard §3.2.6.6) and a placeholder in `src` and `srcset` until a script moves them in: they are its
  sources.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from pydantic import JsonValue
from selectolax.lexbor import LexborNode

from snowcrawl.collect.base import Candidate
from snowcrawl.collect.labels import properties
from snowcrawl.content.dom import tokens
from snowcrawl.content.page import ParsedPage, node_xpath
from snowcrawl.content.regions import DIALOG, DIALOG_ROLES, TANGENTIAL, main_region
from snowcrawl.content.repeats import MIN_REPEATS
from snowcrawl.content.text import shown
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.images import preferred_rendition, same_family, same_file
from snowcrawl.normalize.url import canonical_url, media_type
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# HTML Standard §4.8.7 (`object`), §4.8.9 (`video`, `audio`), §4.12.5 (`canvas`): their children are fallback content,
# shown only where the element itself cannot be.
_MEDIA = frozenset({"video", "audio", "object", "canvas"})
# WAI-ARIA 1.2 §5.4: roles `none` and `presentation` remove an element's meaning; for an image, it is decorative.
_PRESENTATIONAL = frozenset({"none", "presentation"})
# schema.org `Thing.image`: the property an image field's path names.
_IMAGE = "image"
# RFC 2397: a `data:` URL carries its content inline; it addresses no image resource.
_INLINE = "data:"
# HTML Standard §4.8.4.2: an image candidate string's width descriptor, a valid non-negative integer and `w`, and its
# pixel density descriptor, a valid floating-point number and `x`. A width has a few digits; a longer run is no width a
# page lays out, and stays clear of `int`'s digit limit.
_WIDTH_DESCRIPTOR = re.compile(r"\d{1,9}w")
_DENSITY_DESCRIPTOR = re.compile(r"\d{0,9}(?:\.\d{1,9})?x")
# HTML Standard §4.8.3 `srcset` and `src`, and the custom data attributes a lazy loader keeps them in.
_SRCSET, _SRC = "srcset", "src"
_LAZY = "data-"
# A hyperlink (HTML Standard §4.6.1: an `a` element with an `href` attribute).
A_HREF = "a[href]"

# Whether an image source is named after the main image's file (`images.same_family`).
_Named = Callable[[str], bool]

# A field, the raw value, the XPath of the element it came from, and what the field reads of it when that is not the
# raw value (a list field's items).
Found = tuple[str, JsonValue, str, JsonValue]


def apart_ids(region: LexborNode, *, tangential: bool = True) -> frozenset[int]:
    """The elements in the region's tangential content (unless not `tangential`), in a dialog (`content.regions`: a
    window apart, whose images are its own) or a media element's fallback."""
    roles = (f'[role~="{role}"]' for role in DIALOG_ROLES)
    selector = ",".join((*(TANGENTIAL if tangential else ()), *_MEDIA, DIALOG, *roles))
    return frozenset(inside.mem_id for root in region.css(selector) for inside in (root, *root.css("*")))


def children(node: LexborNode) -> list[LexborNode]:
    return [child for child in node.iter(include_text=False) if child.is_element_node]


def up_to(node: LexborNode, top: LexborNode) -> Iterator[LexborNode]:
    """The ancestors of `node` below `top`."""
    parent = node.parent if node != top else None
    while parent is not None and parent != top:
        yield parent
        parent = parent.parent


def image_found(
    page: ParsedPage, schema: SchemaDef, ctx: Context, outside: frozenset[int], primary: Sequence[str]
) -> Iterator[Found]:
    """The main image for each image field whose path names schema.org `image`, and its gallery for each such list of
    images, outside the product container's other blocks (`collect.product_scope.product_apart`). The main image is the
    shown image of a `primary` file (`_anchor`), else, in a main region, that region's first image (`_main_image`)."""
    named = _image_fields(schema)
    fields = [n for n, spec in named.items() if spec.type is FieldType.IMAGE_URL]
    lists = [n for n, spec in named.items() if spec.type is FieldType.LIST]
    landmark = main_region(page)
    region = landmark or page.tree.body
    if not (fields or lists) or region is None:
        return
    links, base = Links.of(region, ctx), ctx.base_url or ctx.page_url
    found = _main(region, landmark is not None, links, outside, primary, base)
    if found is None:
        return
    image, source, apart = found
    copies = Copies.of(region, base)
    shown_at, source = copies.largest(image, source)
    yield from ((name, source, node_xpath(shown_at), None) for name in fields)
    gallery = _gallery(image, region, links, apart, base) if lists else None
    if gallery is not None:
        views: list[JsonValue] = list(dict.fromkeys(copies.largest(image, view)[1] for view in gallery[1]))
        yield from ((name, views, node_xpath(gallery[0]), None) for name in lists)


@dataclass(frozen=True, slots=True)
class Copies:
    """Every image source a region states, by element: an `img`'s (`image_source`) and its `picture`'s `source`
    elements' (HTML Standard §4.8.2: alternative sources of the one image). The same file at another rendition
    (`images.same_file`) is the same image: a thumbnail strip, the slides, a zoom layer and a grid each state it at
    their own size."""

    stated: tuple[tuple[LexborNode, str, str], ...]

    @classmethod
    def of(cls, region: LexborNode, base: str | None) -> "Copies":
        found = ((node, image_source(node.attributes)) for node in region.css(_COPIES))
        return cls(tuple((n, s, a) for n, s in found if s is not None and (a := canonical_url(s, base)) is not None))

    def largest(self, element: LexborNode, source: str) -> tuple[LexborNode, str]:
        """The element stating the source's file at its largest rendition (`images.preferred_rendition`), and that
        source as written; the element and source themselves when no other states it larger."""
        own = next(((n, s, a) for n, s, a in self.stated if s == source), None)
        if own is None:
            return element, source
        found = [own, *(c for c in self.stated if c[2] != own[2] and same_file(c[2], own[2]))]
        chosen = preferred_rendition([a for _, _, a in found])
        node, written, _ = next(c for c in found if c[2] == chosen)
        return (element, source) if chosen == own[2] else (node, written)


# An `img`, and the `source` elements of a `picture` (HTML Standard §4.8.1, §4.8.2).
_COPIES = "img, picture > source"


def _image_fields(schema: SchemaDef) -> dict[str, FieldSpec]:
    """The image fields and lists of images whose path names schema.org `image`."""
    return {
        name: spec
        for name, spec in schema.fields.items()
        if _IMAGE in properties(spec)
        and FieldType.IMAGE_URL in (spec.type, spec.item.type if spec.item is not None else None)
    }


def stated_images(schema: SchemaDef, stated: Sequence[Candidate]) -> tuple[str, ...]:
    """The main image the page's own structured data and Open Graph state for its product (`stated`): per image field
    or list of images, entity and source kind, the first value, the representative image a source names first
    (schema.org `image` written as several values or a list; Open Graph's first `og:image`). An image field holds a
    candidate per stated value, so only the first one counts."""
    named = _image_fields(schema)
    firsts: dict[tuple[str, str, str], JsonValue] = {}
    for c in stated:
        if c.field in named and isinstance(c.normalized, Ok):
            firsts.setdefault((c.field, c.entity_id, c.source_kind), c.normalized.value)
    values = (value[0] if isinstance(value, list) and value else value for value in firsts.values())
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))


def _main(
    region: LexborNode,
    landmark: bool,
    links: "Links",
    outside: frozenset[int],
    primary: Sequence[str],
    base: str | None,
) -> tuple[LexborNode, str, frozenset[int]] | None:
    """The main image, its source and the elements set apart from its gallery (module docstring): the shown image of a
    `primary` file outside the region's tangential content (`_anchor`); else the first image of such a file outside
    dialogs and fallback content only, shown or not and decorative or not: the stated file is the evidence, so the
    tangential section holding it holds the product's images (a carousel hides its slides, a `noscript` fallback holds
    a lazy one, an empty `alt` says the text tells what the image shows, HTML Standard §4.8.4.4); else, in a main
    landmark, its first image (`_main_image`)."""
    apart = apart_ids(region) | outside
    found = _anchor(region, links, primary, base, lambda image: _shown_source(image, apart))
    if found is not None:
        return (*found, apart)
    near = apart_ids(region, tangential=False) | outside
    found = _anchor(region, links, primary, base, lambda image: _stated_source(image, near))
    if found is not None:
        return (*found, near | (apart - _sections(found[0], region)))
    found = _main_image(region, links, apart) if landmark else None
    return None if found is None else (*found, apart)


def _sections(image: LexborNode, region: LexborNode) -> frozenset[int]:
    """The elements of the tangential sections holding the image: they hold its slides, the region's others do not."""
    roots = [node for node in up_to(image, region) if node.tag in TANGENTIAL]
    return frozenset(inside.mem_id for root in roots for inside in (root, *root.css("*")))


def _stated_source(image: LexborNode, apart: frozenset[int]) -> str | None:
    return None if image.mem_id in apart else image_source(image.attributes)


def _anchor(
    region: LexborNode,
    links: "Links",
    primary: Sequence[str],
    base: str | None,
    read: Callable[[LexborNode], str | None],
) -> tuple[LexborNode, str] | None:
    """The region's first image of a `primary` file (`images.same_file`: the file, or a rendition of it) whose source
    `read` gives, not a link card's: the product's main image as structured data states it, found by its file
    (independent evidence of which image is the product's), so a page with no main landmark has one too, and its
    gallery is read from it."""
    if not primary:
        return None
    for image in region.css("img"):
        source = read(image)
        if source is None or (absolute := canonical_url(source, base)) is None:
            continue
        if any(same_file(absolute, file) for file in primary) and not _link_card(image, region, links):
            return image, source
    return None


def _main_image(region: LexborNode, links: "Links", apart: frozenset[int]) -> tuple[LexborNode, str] | None:
    for image in region.css("img"):
        source = _shown_source(image, apart)
        if source is not None and not _link_card(image, region, links):
            return image, source
    return None


def _shown_source(image: LexborNode, apart: frozenset[int]) -> str | None:
    """A shown image's source (`_source`)."""
    return _source(image, apart) if shown(image) else None


def _source(image: LexborNode, apart: frozenset[int]) -> str | None:
    """A non-decorative image's source, outside the elements set `apart`."""
    if image.mem_id in apart or decorative(image.attributes):
        return None
    return image_source(image.attributes)


def _gallery(
    image: LexborNode, region: LexborNode, links: "Links", apart: frozenset[int], base: str | None
) -> tuple[LexborNode, list[str]] | None:
    """The main image's gallery: the holder and the image sources of the blocks of one repeated structure around it.

    From the main image up, the first block that has siblings of its tag (two or more blocks of one tag, the repetition
    `collect.dom` reads sibling pairs from) and whose other blocks hold an image (else they are parts of one slide, the
    image beside its caption) is a slide among slides: each block holding an image at the main image's
    tag path within its block gives that image, in document order, shown or not (a carousel hides the slides but the
    current one, WAI-ARIA Authoring Practices, carousel pattern); a block without one there gives its first image named
    after the main image's file (`images.same_family`: a view or rendition the image server names after it, whatever
    its place in the block, shown or not, decorative or not). A block linking to another document makes the blocks
    cards of other items, not views of this one, so they are no gallery. A source several blocks give is the
    template's, as the align step reads a place every block repeats (a lazy loader's placeholder), not a view. Fewer
    than two distinct sources are no gallery."""
    main = canonical_url(image_source(image.attributes) or "", base)

    def named(source: str) -> bool:
        absolute = canonical_url(source, base)
        return main is not None and absolute is not None and same_family(main, absolute)

    node = image
    while node is not region and (parent := node.parent) is not None:
        blocks = [child for child in children(parent) if child.tag == node.tag]
        if len(blocks) >= MIN_REPEATS and not _parts(blocks, node):
            views = _views(blocks, _tag_path(image, node), links, (apart, named))
            return (parent, views) if len(views) >= MIN_REPEATS else None
        node = parent
    return None


def _parts(blocks: Sequence[LexborNode], own: LexborNode) -> bool:
    """Whether the blocks beside `own` hold no image: they are the parts of one slide (its image and its caption), not
    slides."""
    return not any(
        block.tag == "img" or block.css_first("img") is not None for block in blocks if block.mem_id != own.mem_id
    )


def _views(
    blocks: Sequence[LexborNode], path: tuple[str, ...], links: "Links", rule: tuple[frozenset[int], "_Named"]
) -> list[str]:
    """The sources the blocks show once each (`_gallery`); none when a block links to another document."""
    if any(links.of_block(block) for block in blocks):
        return []
    sources = [s for block in blocks if (s := _block_source(block, path, *rule)) is not None]
    counts = Counter(sources)
    return [source for source in dict.fromkeys(sources) if counts[source] == 1]


def _tag_path(element: LexborNode, block: LexborNode) -> tuple[str, ...]:
    """The tags from a block (excluded) down to an element inside it (included)."""
    tags = [element.tag or ""]
    tags.extend(ancestor.tag or "" for ancestor in up_to(element, block))
    return tuple(reversed(tags))


def _block_source(block: LexborNode, path: tuple[str, ...], apart: frozenset[int], named: "_Named") -> str | None:
    """The source of a block's first image at the tag path `path` from the block, else of its first image outside
    `apart` whose source is `named` after the main image's file."""
    images = [block] if block.tag == "img" else block.css("img")
    at_path = (s for image in images if _tag_path(image, block) == path and (s := _source(image, apart)) is not None)
    elsewhere = (s for image in images if (s := _stated_source(image, apart)) is not None and named(s))
    return next(at_path, None) or next(elsewhere, None)


def image_source(attributes: Mapping[str, str | None]) -> str | None:
    """An `img` element's source (module docstring): its `src`, else the URL of its first `srcset` candidate (HTML
    Standard §4.8.4.3: a URL, then optional descriptors, candidates separated by commas); an inline `data:` URL is
    none. A `srcset` with width descriptors, or with a candidate of pixel density 1, leaves `src` out of the image's
    source set ("update the source set"): the image never shows it (a lazy loader's placeholder), so its source is the
    widest candidate, else the densest (§4.8.4.2: a pixel density descriptor states how many image pixels a CSS pixel
    holds, so a `2x` candidate is a larger rendition of what a `1x` one shows), the first of equals. A lazy loader's
    `data-srcset` and `data-src` stand for `srcset` and `src`."""
    lazy = attributes.get(_LAZY + _SRCSET) or attributes.get(_LAZY + _SRC)
    srcset = attributes.get(_LAZY + _SRCSET if lazy else _SRCSET) or ""
    src = attributes.get(_LAZY + _SRC if lazy else _SRC) or ""
    candidates = _srcset(srcset)
    widths = [width for _, width, _ in candidates]
    densities = [density for _, _, density in candidates]
    if any(width is not None for width in widths):
        widest = max(range(len(candidates)), key=lambda i: widths[i] or 0)
        sources = [candidates[widest][0]]
    elif 1 in densities:
        densest = max(range(len(candidates)), key=lambda i: densities[i] or 0)
        sources = [candidates[densest][0]]
    else:
        sources = [src, *(url for url, _, _ in candidates[:1])]
    for source in (s.strip() for s in sources):
        if source and not source.lower().startswith(_INLINE):
            return source
    return None


def _srcset(srcset: str) -> list[tuple[str, int | None, float | None]]:
    """A `srcset`'s image candidate strings (HTML Standard §4.8.4.3, "parse a srcset attribute"): each URL, with its
    width descriptor's value when it has one, and its pixel density (1 for a candidate without a descriptor), none
    with a width descriptor or a descriptor it does not read. A URL runs to white space, trailing commas ending its
    candidate; its descriptors run to the next comma, which may follow the last one without white space
    (`a.jpg 600w,b.jpg 800w`)."""
    candidates: list[tuple[str, int | None, float | None]] = []
    position = 0
    while (start := _next_candidate(srcset, position)) < len(srcset):
        end = _token_end(srcset, start)
        url, position = srcset[start:end], end
        descriptors: list[str] = []
        if not url.endswith(","):
            comma = srcset.find(",", position)
            position = len(srcset) if comma < 0 else comma
            descriptors = srcset[end:position].split()
        candidate: tuple[str, int | None, float | None] = (url.rstrip(","), None, 1.0)
        for descriptor in descriptors:
            candidate = _described(candidate, descriptor)
        candidates.append(candidate)
    return candidates


def _next_candidate(srcset: str, position: int) -> int:
    """The position of the next candidate's URL: past white space and commas."""
    while position < len(srcset) and (srcset[position].isspace() or srcset[position] == ","):
        position += 1
    return position


def _token_end(srcset: str, position: int) -> int:
    while position < len(srcset) and not srcset[position].isspace():
        position += 1
    return position


def _described(
    candidate: tuple[str, int | None, float | None], descriptor: str
) -> tuple[str, int | None, float | None]:
    url, width, _ = candidate
    if _WIDTH_DESCRIPTOR.fullmatch(descriptor):
        return url, int(descriptor[:-1]), None
    if _DENSITY_DESCRIPTOR.fullmatch(descriptor) and descriptor != "x":
        return url, width, float(descriptor[:-1])
    return url, width, None


def decorative(attributes: Mapping[str, str | None]) -> bool:
    """Whether an `img` is decorative: an empty `alt` (HTML Standard §4.8.4.4), or roles `none`/`presentation`."""
    return attributes.get("alt") == "" or not _PRESENTATIONAL.isdisjoint(tokens(attributes.get("role")))


def _link_card(image: LexborNode, region: LexborNode, links: "Links") -> bool:
    """Whether the nearest block around the image that holds a hyperlink to another document holds no other image and
    leads to one document only: the card of that document, whose image is the document's, not this page's entity."""
    node: LexborNode | None = image.parent
    while node is not None and node.mem_id != region.mem_id:  # identity: `!=` compares serialized markup
        if len(node.css("img")) > 1:
            return False
        targets = links.of_block(node)
        if targets:
            return len(targets) == 1
        node = node.parent
    return False


@dataclass(frozen=True, slots=True)
class Links:
    """The other document each hyperlink of a region leads to (HTML Standard §4.6.1: an `a` with `href`), by element:
    canonical (`canonical_url`: http(s), without the fragment), other than the page itself and other than images (a
    larger copy) or a document about one: a query (RFC 3986 §3.4) whose parameter names an image file addresses that
    image (its zoom, its share, the items it shows), not another item."""

    targets: Mapping[int, str]

    @classmethod
    def of(cls, region: LexborNode, ctx: Context) -> "Links":
        here = canonical_url(ctx.page_url, None) if ctx.page_url is not None else None
        base = ctx.base_url or ctx.page_url
        found = {a.mem_id: canonical_url(a.attributes.get("href") or "", base) for a in region.css(A_HREF)}
        return cls({key: t for key, t in found.items() if t is not None and t != here and not _about_image(t)})

    def of_block(self, block: LexborNode) -> set[str]:
        """The other documents the block's hyperlinks lead to, the block's own included when it is one."""
        return {self.targets[a.mem_id] for a in (block, *block.css(A_HREF)) if a.mem_id in self.targets}


def _about_image(url: str) -> bool:
    """Whether the URL names an image (`is_image_url`) or its query names one in a parameter (`Links`)."""
    return is_image_url(url) or any(is_image_url(value) for _, value in parse_qsl(urlsplit(url).query))


def is_image_url(url: str) -> bool:
    """Whether the URL's path names an image file (RFC 6838: the top-level type `image`, by its extension)."""
    found = media_type(url)
    return found is not None and found.startswith("image/")
