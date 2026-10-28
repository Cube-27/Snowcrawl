"""Block classification from HTTP status and document structure, and from standard text in two cases.

Every rule reads the body as text, decoded once at acquisition by HTML encoding sniffing
(`storage.capture_bundle.decode_html`): a parser handed the raw bytes of a page in a legacy encoding would read its
attributes as UTF-8 and fail on them.

This module compares no body text with a string of its own: vendor strings are site data (C3/C4), so a vendor's
stand-in marker on a 200 page is read in `acquire.challenge` from the adapter package's signature data, one exception
to the structure-only rule. The other is the HTTP Status Code Registry's description of 404 (RFC 9110 §15.5.5,
versioned IANA data): a 2xx document whose structured data neither states an item of a type the task reads it for
nor lists items, that renders less than an article and whose title or top-level heading states that description stands
in for a 404 page (`soft_404`, `_states_not_found`).

Other challenge pages served with 200 mostly present as shells and get a browser retry. An error or challenge page
served with 200 that has text enough not to be a shell is told by its shape instead: it links nowhere. A
site's pages are connected by hyperlinks (HTML Standard §4.6), so a page without one, whatever scripts, images or forms
it holds, is not one of them, unless it renders an order of magnitude more text than a shell (`_CARRIED_MIN_CHARS`):
a stand-in states a line or two, and a page whose navigation a script drives still shows its article. Nor is a
document that declares a refresh (it navigates itself on, HTML Standard §4.2.5.3) and whose hyperlinks all lead to no
web page (`mailto:`, `tel:`, RFC 6068, RFC 3966): a stand-in for the page while the site is busy or checking the client.

A 2xx document whose structured data states an item of a type the task reads the page for (the policy's
`target_types`: a JSON-LD node or a microdata item typed so, or an OGP product price for a Product or Offer) is the
page, however little it shows or links: the reader reads that data (`_describes`). Fetched as a detail page while its
body carries its content unrendered (`carries_unrendered`, below), it is still a shell for the browser to render: the
rest of the item is only in the rendered page (`_script_state`); without a browser outcome it is kept as it is.
Whether a document's structured data states an offer at all, for a task whose schema reads fields from one (`Offer`
among its types), is `offerless`: no schema.org `offers`, no node typed Offer or stating an `itemOffered`, no OGP
price. The ladder reads it with what the page's collectors publish (`rungs._offerless`); classification does not.

A page fetched for content formats alone (`content_only`) is read for its rendered text, so it is a shell when that text
is near-empty while its scripts and concealed subtrees carry an order of magnitude more (`carries_unrendered`): the
document holds its content in a form no reader renders (HTML Standard §4.12.1: script data is not rendered; §6.1:
`hidden`), and the browser runs the scripts that put it in place. Its links do not make it a page: the site's chrome
(`in_chrome`) is not counted as its text.

A page fetched as a listing is a shell too when nothing on it lists items: no group of repeated items that shows more
than links and copies no navigation (`lists_items`, the item layer's own test of a group) and no structured data that
lists them (`_structured_rows`). A tag cloud, a pager or the site's category tiles repeated in the content are what a
listing whose grid a script fills still shows. Its grid loads by script after the page (an XHR), so the
browser may render what HTTP did not. Only the HTTP rung passes the page kind: the
browser is the last rung, and its page is extracted whatever its links.

A 2xx answer whose media type is not HTML (a PDF, an image, a feed) is `unsupported_media`: no page reader reads it, and
the browser would render the same bytes, so it gets no browser attempt. A 451 is a refusal for legal reasons (RFC 7725
§3): `blocked`, which `acquire.challenge` labels `legal`.

A 401 asks for credentials (RFC 9110 §15.5.2): an `auth_wall`, which no rung passes, never a block. A block needs
corroboration, and usable content beats it (`usable_content`): a 403 document that carries the page's structured data
(an item of a type the task reads it for, `_describes`; on a listing, the items it lists) is the page, served with a
refusal status. A 2xx page reached through a redirect (`redirect`: the URL asked for and the one that answered) is
classed by where the redirect led (`_moved`): to another country's site and away from the page (`geo_redirect`), to a
site's root (`soft_404`: RFC 9110 §15.5.5 asks a server to answer 404 for what it does not have, and a redirect to the
home page stands in for one), or, for a detail page, to a page that lists items and states no item of a type the task
reads it for (`redirected_to_listing`). None of these is a block, so none is fetched again in the browser, whose
navigation would follow the same redirect. A redirect to a page whose query carries the asked page's path is another
thing: an interstitial that sends the client back once passed (`_returns_to`), a block the browser may pass.
"""

import csv
import re
from base64 import urlsafe_b64decode
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from functools import cache
from pathlib import Path
from typing import Final, NamedTuple
from urllib.parse import SplitResult, parse_qsl, unquote, urldefrag, urljoin, urlsplit

from lxml.html import HtmlElement
from selectolax.lexbor import LexborHTMLParser, LexborNode

from snowcrawl.acquire.items import (
    MIN_ITEMS,
    copies_navigation,
    hyperlinks,
    in_chrome,
    item_groups,
    lxml_of,
    lxml_root,
    shows_items,
)
from snowcrawl.acquire.types import BlockClass
from snowcrawl.core import json_text
from snowcrawl.core.engine_version import engine_data_dir
from snowcrawl.db.models import PageKind
from snowcrawl.storage.capture_bundle import HeaderList

# RFC 9110 §15.5.5 (404 Not Found), §15.5.11 (410 Gone).
_NOT_FOUND = frozenset({404, 410})
# RFC 9110 §15.5.2: 401 asks for the user's credentials, which no rung has.
_AUTH_WALL = 401
# RFC 9110 §15.5.4 (403), §15.6.4 (503); RFC 6585 §4 (429); RFC 7725 §3 (451). Not 407 (RFC 9110 §15.5.8): the
# proxy the request went through asked for its credentials, which says nothing of the site; it is an `error`.
_BLOCKED = frozenset({403, 429, 451, 503})
# RFC 9110 §15.5.4: a server MAY answer 403 with a representation; when it holds the page's own data, it is the page.
_FORBIDDEN = 403
# HTML Standard: these elements' contents are never rendered as page text (script data, style sheets, fallback
# for script-disabled user agents, inert template contents).
_INVISIBLE = ["script", "style", "noscript", "template"]
# HTML media types (HTML Standard §16.1 text/html; RFC 3236 application/xhtml+xml). A response without a declared
# type is treated as HTML, as browsers sniff it (WHATWG MIME Sniffing §7).
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# Shell thresholds: unmeasured starting points, to be confirmed on dev.
_SHELL_MAX_TEXT_CHARS = 200
_SHELL_MAX_LINKS = 5
# A document carries its content unrendered when its scripts and concealed subtrees hold at least an order of magnitude
# more text than a shell renders (`carries_unrendered`); a document that renders as much is the page, however it links
# (`_no_site_page`).
_CARRIED_MIN_CHARS = 10 * _SHELL_MAX_TEXT_CHARS
# HTML Standard §6.1: an element with the `hidden` attribute is not rendered, nor one whose inline style sets
# `display: none` (CSS Display 3 §2.5); a script may show either later.
_HIDDEN = "hidden"
_DISPLAY_NONE = re.compile(r"(?:^|;)\s*display\s*:\s*none\s*(?:!\s*important\s*)?(?:;|$)", re.IGNORECASE)
_CONCEALED = "[hidden], [style]"
# HTML Standard §4.6.1: `a` and `area` elements with an `href` are hyperlinks.
_HYPERLINKS = "a[href], area[href]"
# Structured data a listing's rows are read from without its grid: as many microdata items (HTML
# Standard §5.2) as a grid's items (`MIN_ITEMS`), or a schema.org ItemList in a JSON-LD script data block (W3C JSON-LD
# 1.1 §7.3) whose media type, before parameters and in any case (RFC 9110 §8.3.1), is `_JSON_LD`.
# Top-level microdata items (HTML Standard §5.2.2): an item that is no property of another (a breadcrumb's entries,
# a product's offer are).
_MICRODATA = "[itemscope]:not([itemprop])"
_JSON_LD = "application/ld+json"
_ITEM_LIST = "ItemList"
_ENTRIES = "itemListElement"  # schema.org ItemList: the list's entries
_SCHEMA_ORG = re.compile(r"^(?:https?://schema\.org/|schema:)")
# HTML Standard §5.2.2: a microdata item's `itemtype`, a set of space-separated absolute URLs naming its types.
_MICRODATA_TYPED = "[itemscope][itemtype]"
# The Open Graph protocol's product object price (`product:price:amount`), in RDFa's `property` or HTML's `name`: it
# states the price of the page's one product, a schema.org Product's Offer.
_OGP_PRICE_AMOUNT = "product:price:amount"
_OGP_PRICED = frozenset({"Product", "Offer"})
# schema.org: an Offer, the type a schema's offer fields (price, availability) are read from; `offers`, "an offer to
# provide this item", whose value is an Offer typed or not (schema.org's range; `collect.jsonld` types an untyped one);
# `itemOffered`, "the item being offered", stated by an Offer. A JSON-LD member may name either as a schema.org IRI.
_OFFER = "Offer"
_OFFER_MEMBERS = frozenset({"offers", "itemOffered"})
_CONTEXT = "@context"  # JSON-LD 1.1 §3.1: a node's context, which defines terms
# HTML Standard §5.2.2: a microdata property's `itemprop`, a set of space-separated names.
_ITEMPROP = "[itemprop]"
# BCP 47 (RFC 5646 §2.1): a language subtag of 2 or 3 letters, with a region subtag of 2 letters (ISO 3166-1) or none,
# as a path segment names a site's edition (`en`, `en-us`, `de_DE`).
_BREAKS = r"/\-_."
_LOCALE_SEGMENT = re.compile(r"[a-z]{2,3}(?:[-_]([a-z]{2}))?", re.ASCII | re.IGNORECASE)


class Redirect(NamedTuple):
    """Where a fetch of `asked`, as a page of `kind`, was answered from: `answered`, past the redirects it followed."""

    asked: str
    answered: str
    kind: PageKind | None


_TYPE = "@type"  # JSON-LD 1.1 §3.5: a node's type, a string or an array of them
# A refusal a page's own script may lift: 403 (RFC 9110 §15.5.4), 429 (RFC 6585 §4), 503 (RFC 9110 §15.6.4);
# not 401/407, which ask for credentials (RFC 9110 §11.6), nor 451, a legal refusal (RFC 7725).
INTERSTITIAL_STATUS = frozenset({403, 429, 503})
# HTML Standard §4.2.5.3: the `refresh` pragma (`meta http-equiv`, an ASCII case-insensitive keyword), and §7.11.1:
# the `Refresh` header, which the same declarative refresh steps read. Either makes the document navigate on.
_REFRESH = "refresh"
# RFC 9110 §4.2: the URI schemes of resources served over HTTP, which a site's pages are.
_WEB_SCHEMES = ("http", "https")
# HTML Standard §4.12.1: `script` elements; script is the only other way a document navigates itself (the `Location`
# interface, or a form it submits).
_SCRIPT = "script"
# The IANA HTTP Status Code Registry (`backend/data/manifest.json`): each status code's description (RFC 9110 §15), the
# reason phrase a server states for it; 404 (RFC 9110 §15.5.5) is the status a server answers for what it does not have.
STATUS_REGISTRY: Final = Path("iana") / "http-status-codes-1.csv"
_NOT_FOUND_CODE = "404"
# HTML Standard §4.2.2: the document's title; §4.3.6: `h1`, the top-ranked heading, which names the page's content.
_TITLES = "title, h1"


def classify(
    status: int,
    headers: HeaderList,
    text: str,
    page_kind: PageKind | None = None,
    redirect: Redirect | None = None,
    *,
    types: tuple[str, ...] = (),
    content_only: bool = False,
) -> BlockClass:
    """The response's block class from its decoded `text`; `page_kind` is the kind the page is fetched as, when its
    task names one and the listing rule applies; `redirect` where the fetch was answered from; `types` the schema.org
    types the task reads the page for and `content_only` a fetch for content formats alone (`FetchPolicy`), when the
    rules that read them apply."""
    if status in _NOT_FOUND:
        return "not_found"
    if status == _AUTH_WALL:
        return "auth_wall"
    if status in _BLOCKED:
        kind = fetched_as(page_kind, redirect)
        usable = status == _FORBIDDEN and is_html(headers) and usable_content(text, kind, types)
        return "ok" if usable else "blocked"
    if status >= 400:
        return "error"
    if unread_media(status, headers):
        return "unsupported_media"
    if not 200 <= status < 300:
        # RFC 9110 §15.4: a 3xx asks the client to act further (follow its `Location`, or choose, a 300); one the
        # fetch left unfollowed (no `Location`, a 300, the hop limit) holds no page; nor does a 1xx (§15.2).
        return "error"
    return _document_class(text, headers, (page_kind, redirect), types, content_only=content_only)


def fetched_as(page_kind: PageKind | None, redirect: Redirect | None) -> PageKind | None:
    """The kind the page is fetched as: `page_kind`, else the redirect's (the browser rung passes no `page_kind`, so
    the listing shell rule spares its page, but its listing's structured data is still usable content)."""
    return page_kind if page_kind is not None or redirect is None else redirect.kind


def _document_class(
    text: str,
    headers: HeaderList,
    fetch: tuple[PageKind | None, Redirect | None],
    types: tuple[str, ...],
    *,
    content_only: bool,
) -> BlockClass:
    """The class of a 2xx HTML document (`classify`), fetched as `fetch` (its page kind and redirect): its structure,
    then where a redirect led."""
    tree = LexborHTMLParser(text)
    # Read before the shell rule strips script data blocks from the tree.
    documents = _json_ld(tree)
    structured = _structured_rows(tree, documents)
    # A content-only fetch reads the rendered text, whatever structured data the page states.
    described = not content_only and _describes(tree, documents, types)
    unstructured = None if described else _page_shell(tree, content_only=content_only)
    # After the shell rule strips the invisible elements, before its return: a short page that says it is not found,
    # and whose structured data neither states an item nor lists any, is answered for good, not rendered again.
    if not (described or structured) and _states_not_found(tree):
        return "soft_404"
    # Before the shell return: a short document that refreshes and links only to mail or phone is a stand-in too.
    if unstructured == "blocked" or (_refreshes(tree, headers) and _offers_no_web_page(tree)):
        return "blocked"
    if unstructured is not None:
        return unstructured
    return _structured_class(tree, text, fetch, structured=structured, described=described)


def _structured_class(
    tree: LexborHTMLParser,
    text: str,
    fetch: tuple[PageKind | None, Redirect | None],
    *,
    structured: bool,
    described: bool,
) -> BlockClass:
    """The class of a document that is no shell (`_document_class`): a listing that lists nothing, then where a
    redirect led, then its script state."""
    page_kind, redirect = fetch
    base = None if redirect is None else redirect.answered
    if page_kind == "listing" and not structured and not lists_items(text, base):
        return "shell"
    moved = None
    if redirect is not None and redirect.kind is not None:
        moved = _moved(redirect, described=described, lists=lambda: structured or lists_items(text, base))
    # After the redirect rules: a detail page that a redirect led elsewhere is not the page, however it renders.
    return moved or (_script_state(tree, page_kind) if described else None) or "ok"


def _states_not_found(tree: LexborHTMLParser) -> bool:
    """Whether a document stands in for a 404 page (module docstring): it renders less than an article
    (`_CARRIED_MIN_CHARS`, as `_no_site_page` counts it) and its title or a shown `h1` (`_TITLES`) holds the registry's
    description of 404 as whole words, compared caselessly with its white space collapsed (HTML rendering collapses
    it). Without the registry file nothing is read as one."""
    phrase = _not_found_phrase()
    if phrase is None or _unconcealed_chars(tree) >= _CARRIED_MIN_CHARS:
        return False
    words = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
    return any(
        words.search(" ".join(node.text().split()).casefold()) is not None
        for node in tree.css(_TITLES)
        if not any(_concealed(n.attributes) for n in (node, *_ancestors(node)))
    )


@cache
def _not_found_phrase() -> str | None:
    """The registry's description of 404, casefolded and its white space collapsed; None without the registry file."""
    data = engine_data_dir()
    path = None if data is None else data / STATUS_REGISTRY
    if path is None or not path.is_file():
        return None
    with path.open(encoding="utf-8", newline="") as registry:
        for row in csv.DictReader(registry):
            if row.get("Value") == _NOT_FOUND_CODE:
                return " ".join((row.get("Description") or "").split()).casefold() or None
    return None


def _page_shell(tree: LexborHTMLParser, *, content_only: bool) -> BlockClass | None:
    """`_no_site_page`, except that for a fetch for content formats alone, a document that carries its content
    unrendered (`carries_unrendered`, read before the shell rule strips the scripts) is a shell whatever its links."""
    if content_only and _carries_unrendered(tree):
        return "shell"
    return _no_site_page(tree)


def _script_state(tree: LexborHTMLParser, page_kind: PageKind | None) -> BlockClass | None:
    """A detail page whose structured data describes its item but whose body carries the rest of it unrendered
    (`carries_unrendered`) is a shell: what the structured data leaves out (an apply link, a full location list)
    exists only once the browser runs the scripts. Without a browser outcome, the page is kept for what its data
    states (`rungs._as_last_page`, which passes no page kind)."""
    return "shell" if page_kind == "detail" and _carries_unrendered(tree) else None


def offerless(text: str, types: tuple[str, ...]) -> bool:
    """Whether `types` name an Offer and a decoded HTML document's structured data states none (`_lacks_offer`)."""
    tree = LexborHTMLParser(text)
    return _lacks_offer(tree, _json_ld(tree), types)


def _lacks_offer(tree: LexborHTMLParser, documents: list[object], types: tuple[str, ...]) -> bool:
    """Whether the task reads an Offer (`_OFFER` among `types`) and the document's structured data states none: no
    JSON-LD node typed Offer or holding an `offers` or `itemOffered` value, no microdata item typed Offer or property
    named so, and no OGP product price (module docstring)."""
    if _OFFER not in types or any(_typed(documents, frozenset({_OFFER}))):
        return False
    if any(_OFFER in _item_types(item) for item in tree.css(_MICRODATA_TYPED)):
        return False
    named = (set((node.attributes.get("itemprop") or "").split()) for node in tree.css(_ITEMPROP))
    if any(not _OFFER_MEMBERS.isdisjoint(names) for names in named):
        return False
    return not _offer_member(documents) and not _ogp_priced(tree)


def _offer_member(documents: list[object]) -> bool:
    """Whether a JSON-LD object at any depth holds a value under `offers` or `itemOffered` (`_OFFER_MEMBERS`), named
    as a term or a schema.org IRI (walked with a stack, not recursion). A `@context` defines terms and states no data
    (JSON-LD 1.1 §3.1), so it is not walked."""
    stack: list[object] = list(documents)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            members = {k: v for k, v in value.items() if k != _CONTEXT}
            if any(
                _SCHEMA_ORG.sub("", str(k)) in _OFFER_MEMBERS and v not in (None, [], {}) for k, v in members.items()
            ):
                return True
            stack.extend(members.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def usable_content(text: str, page_kind: PageKind | None = None, types: tuple[str, ...] = ()) -> bool:
    """Whether an HTML document carries the page's structured data, whatever its status or a vendor's marker says:
    an item of one of the `types` the task reads it for (`_describes`), or, fetched as a listing, the items its
    structured data lists (`_structured_rows`). Usable content beats vendor noise."""
    tree = LexborHTMLParser(text)
    documents = _json_ld(tree)
    return _describes(tree, documents, types) or (page_kind == "listing" and _structured_rows(tree, documents))


def _describes(tree: LexborHTMLParser, documents: list[object], types: tuple[str, ...]) -> bool:
    """Whether the document's structured data states an item of one of `types` (schema.org type names, subtypes
    included by the policy): a JSON-LD node at any depth (JSON-LD 1.1 §3.5 `@type`) or a microdata item (HTML Standard
    §5.2.2 `itemtype`) typed so, or, for a Product or an Offer, an OGP product price with content."""
    wanted = frozenset(types)
    if not wanted:
        return False
    if any(_typed(documents, wanted)):
        return True
    if any(not wanted.isdisjoint(_item_types(item)) for item in tree.css(_MICRODATA_TYPED)):
        return True
    return not wanted.isdisjoint(_OGP_PRICED) and _ogp_priced(tree)


def _ogp_priced(tree: LexborHTMLParser) -> bool:
    """Whether the document states an OGP product price (`_OGP_PRICE_AMOUNT`) with content."""
    return any(
        (meta.attributes.get("property") or meta.attributes.get("name")) == _OGP_PRICE_AMOUNT
        and meta.attributes.get("content")
        for meta in tree.css("meta")
    )


def _item_types(item: LexborNode) -> set[str]:
    """The schema.org type names of a microdata item's `itemtype` URLs (a URL of another vocabulary kept whole)."""
    return {_SCHEMA_ORG.sub("", url) for url in (item.attributes.get("itemtype") or "").split()}


def carries_unrendered(text: str) -> bool:
    """Whether a decoded HTML document carries its content unrendered (`_carries_unrendered`)."""
    return _carries_unrendered(LexborHTMLParser(text))


def _carries_unrendered(tree: LexborHTMLParser) -> bool:
    """Whether the document's scripts and concealed subtrees (`_HIDDEN`, `_DISPLAY_NONE`) hold at least
    `_CARRIED_MIN_CHARS` of text while what it renders outside the site's chrome is under `_SHELL_MAX_TEXT_CHARS`:
    content a script puts in place (module docstring). Counted in non-whitespace characters, as the shell rule counts.
    The tree is read, not changed."""
    scripts = sum(_chars(script.text()) for script in tree.css(_SCRIPT))
    concealed = sum(
        _chars(node.text()) for node in _outermost(n for n in tree.css(_CONCEALED) if _concealed(n.attributes))
    )
    if scripts + concealed < _CARRIED_MIN_CHARS:
        return False
    root = lxml_of(tree)
    body = root.find("body")
    shown = root if body is None else body
    for element in [e for e in shown.iter() if _unshown(e)]:
        if element.getparent() is not None:
            element.drop_tree()
    return _chars(shown.text_content()) < _SHELL_MAX_TEXT_CHARS


def _outermost(nodes: Iterable[LexborNode]) -> Iterator[LexborNode]:
    """The `nodes` (in document order) inside none of the others, so that a subtree's text is counted once. Nodes are
    told apart by identity (`mem_id`): comparing two with `==` serializes them."""
    kept: set[int] = set()
    for node in nodes:
        if not any(ancestor.mem_id in kept for ancestor in _ancestors(node)):
            kept.add(node.mem_id)
            yield node


def _ancestors(node: LexborNode) -> Iterator[LexborNode]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _unshown(element: HtmlElement) -> bool:
    """Whether an element of the lxml copy shows none of the page's own text: never rendered (`_INVISIBLE`), concealed,
    or the site's chrome. A comment or processing instruction is no element."""
    if not isinstance(element.tag, str):
        return False
    return element.tag in _INVISIBLE or _concealed(dict(element.attrib)) or in_chrome(element)


def _concealed(attributes: Mapping[str, str | None]) -> bool:
    return _HIDDEN in attributes or _DISPLAY_NONE.search(attributes.get("style") or "") is not None


def _chars(text: str) -> int:
    return sum(not character.isspace() for character in text)


def _moved(redirect: Redirect, *, described: bool, lists: Callable[[], bool]) -> BlockClass | None:
    """The class of a 2xx page fetched as a page of a kind that a redirect led elsewhere (module docstring), or None
    when it is the page asked for: the same host and path past any locale segment (RFC 3986 §6.2.2.1: the host
    case-insensitively; a query or fragment is no other page). A fetch of no kind (a crawl's page) may land anywhere."""
    a, b = urlsplit(redirect.asked), urlsplit(redirect.answered)
    if (a.hostname, _rest(a)) == (b.hostname, _rest(b)):
        return None
    if _returns_to(a, b):
        return "blocked"
    if (region := _region(b)) is not None and region != _region(a) and _rest(a) != _rest(b):
        return "geo_redirect"
    if not _rest(b) and _rest(a):
        return "soft_404"
    # RFC 3986 §3.3: the last segment names the resource within its hierarchy; a path that keeps it is the same page
    # under another name (a slug added), not a listing in its place.
    if redirect.kind == "detail" and not _keeps_name(a, b) and not described and lists():
        return "redirected_to_listing"
    return None


def _returns_to(asked: SplitResult, answered: SplitResult) -> bool:
    """Whether the page a redirect led to carries the asked page's path in its query (RFC 3986 §3.4), as given or
    base64-encoded (RFC 4648 §4): an interstitial, a check or a sign-in, that sends the client back once passed."""
    path = asked.path.rstrip("/")
    if not path:
        return False
    for _, value in parse_qsl(answered.query):
        decoded = value
        with suppress(ValueError):
            decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
        if path in unquote(value) or path in decoded:
            return True
    return False


def _keeps_name(asked: SplitResult, answered: SplitResult) -> bool:
    """Whether the answered path holds the asked path's last segment as a whole word of one of its segments (between
    `/`, `-`, `_` or `.`): an id kept with a slug added, not a digit that only appears in another number."""
    name = next((segment for segment in reversed(asked.path.split("/")) if segment), None)
    word = rf"(?<![^{_BREAKS}]){re.escape(name or '')}(?![^{_BREAKS}])"
    return name is not None and re.search(word, answered.path) is not None


def _region(url: SplitResult) -> str | None:
    """The region a URL names a site's edition for: a BCP 47 region subtag in its first path segment, else its
    country-code top-level domain (RFC 1591: the two-letter TLDs are ISO 3166-1 codes), lowercased; None for neither."""
    segments = url.path.split("/")
    if len(segments) > 1 and (match := _LOCALE_SEGMENT.fullmatch(segments[1])) is not None and match.group(1):
        return match.group(1).lower()
    tld = (url.hostname or "").rsplit(".", 1)[-1]
    return tld if len(tld) == 2 and tld.isascii() and tld.isalpha() else None


def _rest(url: SplitResult) -> str:
    """The path past a leading BCP 47 locale segment, without its trailing slash: what names the page within an
    edition; empty for a site's (or an edition's) root."""
    segments = [segment for segment in url.path.split("/") if segment]
    if segments and _LOCALE_SEGMENT.fullmatch(segments[0]) is not None:
        segments = segments[1:]
    return "/".join(segments)


def interstitial(status: int, headers: HeaderList, text: str, url: str, *, rendered: bool = False) -> bool:
    """Whether a document at `url` stands in for the page until it navigates on: it can navigate
    itself (a refresh, or a script), and either its status is a refusal a page's own script may lift
    (`INTERSTITIAL_STATUS`) and it is no page of the site by structure (it declares a refresh, it is a shell or links
    nowhere, the rules `classify` applies to a 2xx page, or no hyperlink leads to another page of its own host), or,
    `rendered` by the browser, it is a 2xx document that is a shell or links nowhere: a check served with success that
    reloads once passed. Over HTTP no script runs, so a 2xx document never navigates on there. A document with neither
    a refresh nor a script can never replace itself, so waiting on it is time lost."""
    success = rendered and 200 <= status < 300
    if not (success or status in INTERSTITIAL_STATUS) or not is_html(headers):
        return False
    tree = LexborHTMLParser(text)
    refresh = _refreshes(tree, headers)
    if not refresh and tree.css_first(_SCRIPT) is None:
        return False
    if success:
        return _no_site_page(tree) is not None
    return refresh or _no_site_page(tree) is not None or not _links_within(tree, url)


def _refreshes(tree: LexborHTMLParser, headers: HeaderList) -> bool:
    if any(name.lower() == _REFRESH for name, _ in headers):
        return True
    return any((meta.attributes.get("http-equiv") or "").strip().lower() == _REFRESH for meta in tree.css("meta"))


def _links_to_web(tree: LexborHTMLParser) -> bool:
    """Whether a hyperlink leads to a web page: a relative reference, or an absolute one whose scheme is http or https
    (RFC 3986 §4.2, the scheme compared case-insensitively). A `mailto:` or `tel:` link starts a message or a
    call, not a page of any site."""
    for link in tree.css(_HYPERLINKS):
        try:
            scheme = urlsplit(link.attributes.get("href") or "").scheme.lower()
        except ValueError:  # an authority no URL parser reads (RFC 3986 §3.2.2) leads nowhere
            continue
        if scheme in ("", *_WEB_SCHEMES):
            return True
    return False


def _offers_no_web_page(tree: LexborHTMLParser) -> bool:
    """Whether the document has hyperlinks and none leads to a web page (`_links_to_web`). Read after the shell rule
    strips the invisible elements: a `template`'s links are not in the document."""
    return tree.css_first(_HYPERLINKS) is not None and not _links_to_web(tree)


def _links_within(tree: LexborHTMLParser, url: str) -> bool:
    """Whether a hyperlink leads to another page of `url`'s host (RFC 3986 §5 reference resolution, the host compared
    case-insensitively): a site's pages link to each other (HTML Standard §4.6), a page standing in for one
    links nowhere on the site, or only to itself."""
    here = urlsplit(url)
    for link in tree.css(_HYPERLINKS):
        try:
            target = urlsplit(urljoin(url, link.attributes.get("href") or ""))
        except ValueError:  # an authority no URL parser reads (RFC 3986 §3.2.2) leads nowhere
            continue
        elsewhere = (target.path or "/", target.query) != (here.path or "/", here.query)
        if target.scheme in _WEB_SCHEMES and target.hostname == here.hostname and elsewhere:
            return True
    return False


def _no_site_page(tree: LexborHTMLParser) -> BlockClass | None:
    if _is_shell(tree):
        return "shell"
    # After the shell rule: hyperlinks inside a `template` are not in the document, and the rule strips them. A
    # document that renders an order of magnitude more text than a shell (`_CARRIED_MIN_CHARS`) is the page however it
    # links on (a script, a form): a stand-in states a line or two, not an article.
    if tree.css_first(_HYPERLINKS) is None and _unconcealed_chars(tree) < _CARRIED_MIN_CHARS:
        return "blocked"
    return None


def _unconcealed_chars(tree: LexborHTMLParser) -> int:
    """`_shown_chars` without the text of concealed subtrees (`_HIDDEN`, `_DISPLAY_NONE`), which no reader sees until a
    script shows it: a stand-in's hidden text does not make it an article."""
    concealed = _outermost(n for n in tree.css(_CONCEALED) if _concealed(n.attributes))
    return _shown_chars(tree) - sum(_chars(node.text()) for node in concealed)


def unread_media(status: int, headers: HeaderList) -> bool:
    """Whether a response is a 2xx answer (RFC 9110 §15.3) whose media type no page reader reads (`unsupported_media`),
    told by its status and headers alone, before any of its body."""
    return 200 <= status < 300 and not is_html(headers)


def is_html(headers: HeaderList) -> bool:
    # RFC 9110 §8.3: the media type is the Content-Type value before any parameters, case-insensitive.
    types = [value.split(";", 1)[0].strip().lower() for name, value in headers if name.lower() == "content-type"]
    return not types or types[-1] in _HTML_TYPES


def _is_shell(tree: LexborHTMLParser) -> bool:
    tree.strip_tags(_INVISIBLE)
    # Links first: most pages have enough of them, and then the body text need not be built.
    if len(tree.css("a[href]")) >= _SHELL_MAX_LINKS:
        return False
    return _shown_chars(tree) < _SHELL_MAX_TEXT_CHARS


def _shown_chars(tree: LexborHTMLParser) -> int:
    """The body's text in non-whitespace characters, once the shell rule stripped what is never rendered: HTML
    rendering collapses whitespace, and counting only non-whitespace makes the count independent of how text nodes
    are joined."""
    return _chars(tree.body.text()) if tree.body is not None else 0


def _structured_rows(tree: LexborHTMLParser, documents: list[object]) -> bool:
    return len(tree.css(_MICRODATA)) >= MIN_ITEMS or any(
        True for _ in _typed(documents, frozenset({_ITEM_LIST}), _ENTRIES)
    )


def _json_ld(tree: LexborHTMLParser) -> list[object]:
    """The page's JSON-LD script data blocks, parsed (a block that is not JSON states nothing)."""
    blocks = (s.text() for s in tree.css("script[type]") if _media_type(s.attributes.get("type")) == _JSON_LD)
    return [_json(block) for block in blocks]


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _json(text: str) -> object:
    # Read leniently, as the JSON-LD collector reads blocks (RFC 8259 §9, `core.json_text.parse`).
    try:
        return json_text.parse(text, lenient=True)
    except ValueError:  # a block that is not JSON states nothing
        return None


def _typed(documents: list[object], types: frozenset[str], member: str | None = None) -> Iterator[dict[str, object]]:
    """Each node of the JSON-LD documents typed as one of `types` (`@type`, a string or an array of them), at any
    depth (walked with a stack, not recursion); with `member`, only those whose `member` holds a value."""
    stack: list[object] = list(documents)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if not types.isdisjoint(_type_names(value.get(_TYPE))) and (
                member is None or value.get(member) not in (None, [], {})
            ):
                yield value
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _type_names(found: object) -> Iterator[str]:
    """The names of a JSON-LD `@type` (a string or an array of them), each also as a full schema.org IRI or compact
    `schema:` name (JSON-LD 1.1 §3.2: a type is an IRI; schema.org's are its terms under its namespace)."""
    if isinstance(found, str):
        found = [found]
    elif not isinstance(found, list):
        return
    for name in found:
        if isinstance(name, str):
            yield _SCHEMA_ORG.sub("", name)


def lists_items(text: str, base: str | None) -> bool:
    """Whether the page lists items: a group of its repeated blocks (`acquire.items.item_groups`, the walk the item
    layer's segment step ranks; lxml reads it, as the segment step does) that shows them and copies no navigation, as
    the item layer requires of a group (`shows_items`, `copies_navigation`), each block leading to its first
    hyperlink's document (`_documents`)."""
    root = lxml_root(text)
    groups = item_groups(root)
    chrome = [(anchor, url) for anchor in hyperlinks(root) if in_chrome(anchor) for url in _documents(anchor, base)]

    def documents(block: HtmlElement) -> list[str]:
        return [url for anchor in hyperlinks(block) for url in _documents(anchor, base)]

    return any(
        shows_items(group)
        and not copies_navigation(group, (next(iter(documents(b)), None) for b in group), groups, chrome, documents)
        for group in groups
    )


def _documents(anchor: HtmlElement, base: str | None) -> list[str]:
    """The document a hyperlink leads to, resolved against `base`, the URL that served the page (RFC 3986 §5), without
    its fragment, which names a part of the document; none for a reference that `base` cannot resolve."""
    url = urldefrag(urljoin(base or "", (anchor.get("href") or "").strip())).url
    return [url] if urlsplit(url).scheme in _WEB_SCHEMES else []
