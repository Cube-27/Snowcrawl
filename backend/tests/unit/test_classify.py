"""Block classification: HTTP status and structural body properties only."""

import pytest

from snowcrawl.acquire.classify import Redirect, classify, interstitial, offerless
from snowcrawl.acquire.types import BlockClass
from snowcrawl.db.models import PageKind

_HTML = (("content-type", "text/html; charset=utf-8"),)
_JSON = (("content-type", "application/json"),)


def _page(text_chars: int, links: int, extra: str = "") -> str:
    anchors = "".join(f'<a href="/p/{i}"></a>' for i in range(links))
    return f"<html><head><title>t</title></head><body><p>{'x' * text_chars}</p>{anchors}{extra}</body></html>"


_FULL = _page(400, 20)
_EMPTY_APP = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, "not_found"),
        (410, "not_found"),
        (401, "auth_wall"),
        (403, "blocked"),
        # The proxy asked for credentials (RFC 9110 §15.5.8): no refusal by the site.
        (407, "error"),
        (451, "blocked"),
        # 503 is a server error that asks the client to back off: blocked wins over error.
        (503, "blocked"),
        (400, "error"),
        (500, "error"),
        (200, "ok"),
        # The fetcher follows redirects; a 3xx reaching classify was left unfollowed and holds no page.
        (300, "error"),
        (301, "error"),
    ],
)
def test_status_decides_before_the_body(status: int, expected: BlockClass) -> None:
    assert classify(status, _HTML, _FULL) == expected
    # An empty page under a non-2xx status is classified by its status alone.
    assert classify(status, _HTML, _EMPTY_APP) == (expected if status >= 300 else "shell")


def test_a_page_without_hyperlinks_is_a_stand_in_unless_it_renders_an_article() -> None:
    assert classify(200, _HTML, _page(400, 0, "<script>void 0</script>")) == "blocked"
    # Ten times a shell's text: an article whose navigation a script drives.
    assert classify(200, _HTML, _page(2_000, 0, "<script>void 0</script>")) == "ok"
    # Text no reader sees until a script shows it does not count.
    concealed = f"<div hidden>{'y' * 2_000}</div><script>void 0</script>"
    assert classify(200, _HTML, _page(400, 0, concealed)) == "blocked"


# --- a refusal that stands in for the page until it navigates on ----------------------------------------------

_AT = "https://shop.example.test/item/1"
_SCRIPT = "<script>void 0</script>"
# A refusal with text and links enough to pass the shell rule, whose links all lead off the site or back to the page.
_OFF_SITE = (
    "<html><body><p>"
    + "Checking the connection. " * 20
    + "</p>"
    + "".join(f'<a href="https://help.example.org/{i}">Help {i}</a>' for i in range(6))
    + '<a href="/item/1">Retry</a><a href="#top">Top</a>'
    + "</body></html>"
)


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        pytest.param(403, _HTML, _EMPTY_APP, True, id="script-shell"),
        pytest.param(503, _HTML, _page(400, 0, _SCRIPT), True, id="script-linkless"),
        pytest.param(429, _HTML, _OFF_SITE.replace("</body>", _SCRIPT + "</body>"), True, id="no-site-link"),
        pytest.param(
            403, _HTML, _page(400, 20, '<meta http-equiv="Refresh" content="5">'), True, id="meta-refresh-with-links"
        ),
        pytest.param(403, (*_HTML, ("refresh", "3")), _page(10, 0), True, id="refresh-header"),
        pytest.param(403, _HTML, _page(10, 0), False, id="static-no-script"),
        pytest.param(403, _HTML, _OFF_SITE, False, id="static-off-site"),
        pytest.param(403, _HTML, _page(400, 20, _SCRIPT), False, id="site-page-with-script"),
        pytest.param(401, _HTML, _EMPTY_APP, False, id="asks-for-credentials"),
        pytest.param(451, _HTML, _EMPTY_APP, False, id="legal-refusal"),
        pytest.param(200, _HTML, _EMPTY_APP, False, id="not-a-refusal"),
        pytest.param(403, _JSON, _EMPTY_APP, False, id="not-html"),
    ],
)
def test_an_interstitial_is_a_refusal_that_can_navigate_and_is_no_page_of_the_site(
    status: int, headers: tuple[tuple[str, str], ...], body: str, expected: bool
) -> None:
    assert interstitial(status, headers, body, _AT) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_EMPTY_APP, True, id="script-shell"),
        pytest.param(_page(400, 0, _SCRIPT), True, id="script-linkless"),
        pytest.param(_page(10, 0), False, id="static-shell"),
        pytest.param(_page(400, 20, _SCRIPT), False, id="site-page-with-script"),
        pytest.param(_OFF_SITE.replace("</body>", _SCRIPT + "</body>"), False, id="off-site-links-are-a-page"),
    ],
)
def test_a_rendered_2xx_interstitial_can_navigate_and_is_a_shell_or_links_nowhere(body: str, expected: bool) -> None:
    assert interstitial(200, _HTML, body, _AT, rendered=True) is expected


# --- owner plan 2026-10-19 P3: usable content beats a refusal; where a redirect led ---------------------------------

_PRODUCT = (
    '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "Widget",'
    ' "offers": {"@type": "Offer", "price": "9.50", "priceCurrency": "EUR"}}</script>'
)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param(_PRODUCT, "ok", id="product-with-offers"),
        pytest.param('<meta property="product:price:amount" content="9.50">', "ok", id="ogp-price"),
        pytest.param(_PRODUCT.replace('"Product"', '"https://schema.org/Product"'), "ok", id="product-type-iri"),
        pytest.param(
            '<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">W</span></div>',
            "ok",
            id="microdata-product",
        ),
        pytest.param("", "blocked", id="no-structured-data"),
    ],
)
def test_a_403_that_carries_the_page_is_the_page(extra: str, expected: BlockClass) -> None:
    assert classify(403, _HTML, _page(400, 20, extra), types=_PRODUCT_TYPES) == expected
    # Structured data of a type the task does not read the page for is not the page.
    assert classify(403, _HTML, _page(400, 20, extra), types=("JobPosting",)) == "blocked"


_PRODUCT_TYPES = ("Offer", "Product", "ProductGroup")
_POSTING = (
    '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting", "title": "Welder",'
    f' "description": "{"d" * 3000}"}}</script>'
)


def test_a_document_stating_an_item_of_a_read_type_is_the_page_unless_a_detail_script_carries_it() -> None:
    # No links and no rendered text: the reader reads the structured data, not the rendered page.
    body = f"<html><head>{_POSTING}</head><body><div id='root'></div></body></html>"
    assert classify(200, _HTML, body, types=("JobPosting",)) == "ok"
    assert classify(200, _HTML, body) == "shell"
    # Fetched as a detail page, its scripts carry the rest of the item: the browser renders it; unless a redirect led
    # away from the page asked for.
    assert classify(200, _HTML, body, "detail", types=("JobPosting",)) == "shell"
    home = Redirect("https://jobs.example.test/job/7", "https://jobs.example.test/", "detail")
    assert classify(200, _HTML, body, "detail", home, types=("JobPosting",)) == "soft_404"


def _product(data: str) -> str:
    """A detail page with links and text enough not to be a shell, stating `data` as a JSON-LD block."""
    return _page(400, 20, f'<script type="application/ld+json">{data}</script>')


_STUB = '{"@context": "https://schema.org", "@type": "WebPage", "mainEntity": {"@type": "Product", "name": "Kettle"}}'


@pytest.mark.parametrize(
    ("body", "types", "expected"),
    [
        # A Product stub with no offer, or a page stating no structured data, read for an Offer: none stated.
        pytest.param(_product(_STUB), _PRODUCT_TYPES, True, id="stub"),
        pytest.param(_page(400, 20, '<meta property="og:title" content="Kettle">'), _PRODUCT_TYPES, True, id="og-only"),
        pytest.param(_product(_STUB), ("Product",), False, id="no-offer-read"),
        # An offer stated untyped, as an IRI member, as an Offer stating its item, in microdata, or as an OGP price.
        pytest.param(_product('{"@type": "Product", "name": "K", "offers": {"price": "9"}}'), _PRODUCT_TYPES, False),
        pytest.param(
            _product('{"@type": "Product", "http://schema.org/offers": {"price": "9"}}'), _PRODUCT_TYPES, False
        ),
        pytest.param(
            _product('{"@type": "Offer", "itemOffered": {"@type": "Product", "name": "K"}}'), _PRODUCT_TYPES, False
        ),
        pytest.param(
            _page(
                400,
                20,
                '<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">K</span>'
                '<div itemprop="offers" itemscope><link itemprop="availability" href="https://schema.org/InStock"></div>'
                "</div>",
            ),
            _PRODUCT_TYPES,
            False,
            id="microdata",
        ),
        pytest.param(
            _product(_STUB).replace("<head>", '<head><meta property="product:price:amount" content="9">'),
            _PRODUCT_TYPES,
            False,
            id="ogp-price",
        ),
        # Out of stock: an offer with its availability and no price is an offer stated.
        pytest.param(
            _product('{"@type": "Product", "name": "K", "offers": {"availability": "https://schema.org/OutOfStock"}}'),
            _PRODUCT_TYPES,
            False,
            id="out-of-stock",
        ),
        pytest.param(
            _product('{"@type": "Product", "name": "K", "offers": []}'), _PRODUCT_TYPES, True, id="empty-offers"
        ),
        # A context defining the `offers` term states no offer.
        pytest.param(
            _product('{"@context": {"offers": "https://schema.org/offers"}, "@type": "Product", "name": "K"}'),
            _PRODUCT_TYPES,
            True,
            id="context-term",
        ),
    ],
)
def test_a_document_read_for_an_offer_is_offerless_when_its_structured_data_states_none(
    body: str, types: tuple[str, ...], expected: bool
) -> None:
    assert offerless(body, types) is expected
    # Classification renders no `ok` page for it: the ladder reads it with the page's extraction (`rungs._offerless`).
    assert classify(200, _HTML, body, "detail", types=types) == "ok"


_ITEMS = '<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [{"@type": "ListItem"}]}</script>'


def _stating(title: str, heading: str, text_chars: int = 100) -> str:
    links = "".join(f'<a href="/p/{i}">p{i}</a>' for i in range(6))
    return f"<html><head><title>{title}</title></head><body>{heading}<p>{'x' * text_chars}</p>{links}</body></html>"


@pytest.mark.parametrize(
    ("body", "types", "expected"),
    [
        # The title or a shown h1 states the registry's description of 404, as whole words in any case.
        pytest.param(_stating("Shop", "<h1>Item  not\nFOUND</h1>"), (), "soft_404", id="heading"),
        pytest.param(_stating("Page Not Found | Shop", "<h1>Oops</h1>"), (), "soft_404", id="title"),
        pytest.param(_stating("Shop", "<div hidden><h1>Not found</h1></div>"), (), "ok", id="hidden-heading"),
        pytest.param(_stating("Shop", "<h1>Not foundations</h1>"), (), "ok", id="other-word"),
        # An article that names the status is a page.
        pytest.param(_stating("Not Found", "", 2_000), (), "ok", id="article"),
        # A page that states an item of a type the task reads it for is the page, whatever it is titled.
        pytest.param(_stating("Not Found", _POSTING), ("JobPosting",), "ok", id="described"),
        # Nor is a page whose structured data lists items.
        pytest.param(_stating("Not Found", _ITEMS), (), "ok", id="lists-items"),
    ],
)
def test_a_short_page_titled_with_the_404_description_stands_in_for_a_404(
    body: str, types: tuple[str, ...], expected: BlockClass
) -> None:
    assert classify(200, _HTML, body, types=types) == expected


_NAV = "<nav>" + "".join(f'<a href="/s/{i}">Section {i}</a>' for i in range(8)) + "</nav>"
_STATE = f'<script>window.__STATE__ = {{"text": "{"s" * 3000}"}}</script>'


@pytest.mark.parametrize(
    ("main", "expected"),
    [
        pytest.param("<div id='root'></div>", "shell", id="text-in-a-script"),
        pytest.param(f"<div hidden>{'h' * 3000}</div>", "shell", id="text-concealed"),
        pytest.param(f"<main><p>{'x' * 400}</p></main>", "ok", id="text-rendered"),
    ],
)
def test_a_content_fetch_of_a_page_whose_scripts_carry_its_text_is_a_shell(main: str, expected: BlockClass) -> None:
    body = f"<html><head>{_STATE}</head><body><header>{_NAV}</header>{main}</body></html>"
    assert classify(200, _HTML, body, content_only=True) == expected
    # A records fetch reads the inline state over HTTP: the site's links make it a page.
    assert classify(200, _HTML, body) == "ok"


_LISTING = "<ul>" + "".join(f'<li><a href="/p/{i}">Widget {i}</a><p>{"y" * 40}</p></li>' for i in range(8)) + "</ul>"


@pytest.mark.parametrize(
    ("asked", "answered", "kind", "body", "expected"),
    [
        pytest.param(_AT, _AT + "?ref=1", "detail", _FULL, "ok", id="same-page"),
        pytest.param(_AT, "https://shop.example.test/en-gb/item/1", "detail", _FULL, "ok", id="locale-added"),
        pytest.param(_AT, "https://shop.example.test/", "detail", _FULL, "soft_404", id="to-the-root"),
        pytest.param(_AT, "https://shop.example.test/", None, _FULL, "ok", id="no-kind-lands-anywhere"),
        pytest.param(
            "https://shop.example.test/en-us/item/1",
            "https://shop.example.test/de-de/",
            "detail",
            _FULL,
            "geo_redirect",
            id="another-region",
        ),
        pytest.param(_AT, "https://shop.example.test/fr-fr/", "listing", _FULL, "geo_redirect", id="region-root"),
        pytest.param(
            _AT,
            "https://shop.example.test/c/widgets",
            "detail",
            _page(400, 0, _LISTING),
            "redirected_to_listing",
            id="to-a-listing",
        ),
        pytest.param(
            _AT,
            "https://shop.example.test/item/1-new",
            "detail",
            _page(400, 0, _LISTING + _PRODUCT),
            "ok",
            id="moved-product",
        ),
        pytest.param(
            _AT,
            "https://shop.example.test/item/blue-widget/1",
            "detail",
            _page(400, 0, _LISTING),
            "ok",
            id="slug-added",
        ),
        pytest.param(
            _AT,
            "https://shop.example.test/c/123",
            "detail",
            _page(400, 0, _LISTING),
            "redirected_to_listing",
            id="digit",
        ),
        pytest.param(_AT, "https://shop.example.test/check?next=%2Fitem%2F1", "detail", _FULL, "blocked", id="returns"),
        pytest.param(
            _AT, "https://shop.example.test/check?u=L2l0ZW0vMQ", "detail", _page(400, 0, _LISTING), "blocked", id="b64"
        ),
    ],
)
def test_a_redirect_is_classed_by_where_it_led(
    asked: str, answered: str, kind: PageKind | None, body: str, expected: BlockClass
) -> None:
    assert classify(200, _HTML, body, redirect=Redirect(asked, answered, kind)) == expected


@pytest.mark.parametrize(("entries", "expected"), [("[]", "shell"), ('[{"@type": "ListItem", "position": 1}]', "ok")])
def test_an_empty_item_list_lists_nothing(entries: str, expected: BlockClass) -> None:
    item_list = f'<script type="application/ld+json">{{"@type": "ItemList", "itemListElement": {entries}}}</script>'
    assert classify(200, _HTML, _page(400, 2, item_list), page_kind="listing") == expected


_TILE = '<li class="c"><a href="/c/{i}">{shown}</a></li>'
_MENU = "<nav>" + "".join(f'<a href="/c/{i}">Category {i}</a>' for i in range(4)) + "</nav>"


@pytest.mark.parametrize(
    ("shown", "chrome", "expected"),
    [
        ('<img src="/t/{i}.jpg" alt=""> Item {i}', "", "ok"),
        ("Item {i}", "", "shell"),  # a tag cloud: a link and its label only
        ('<img src="/t/{i}.jpg" alt=""> Item {i}', _MENU, "shell"),  # the menu's sections again
    ],
)
def test_a_listing_lists_items_only_in_a_group_that_shows_more_and_copies_no_navigation(
    shown: str, chrome: str, expected: BlockClass
) -> None:
    tiles = "<ul>" + "".join(_TILE.format(i=i, shown=shown.format(i=i)) for i in range(4)) + "</ul>"
    body = f"<html><body>{chrome}<main><p>{'x' * 400}</p>{tiles}</main></body></html>"
    redirect = Redirect(_AT, "https://shop.example.test/c", "listing")

    assert classify(200, _HTML, body, page_kind="listing", redirect=redirect) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ((("content-type", "application/pdf"),), "unsupported_media"),
        (_JSON, "unsupported_media"),
        ((("Content-Type", "application/xhtml+xml"),), "ok"),
        ((), "ok"),  # no declared type: sniffed as HTML (WHATWG MIME Sniffing §7)
    ],
)
def test_a_2xx_answer_that_is_no_html_document_is_unsupported_media(
    headers: tuple[tuple[str, str], ...], expected: BlockClass
) -> None:
    assert classify(200, headers, _FULL) == expected
