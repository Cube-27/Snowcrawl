"""Commerce platform fingerprints: site data, which names platforms and so
lives in the adapter package (C3).

A platform is told from what every one of its sites serves: the paths of its own static assets and scripts, the
global its storefront script defines, the response headers its servers send. These are platform-level signals, never a
table of sites (C4). A page shows a platform's signal in its markup or its response headers; the first platform whose
signal it shows is its platform, the storefront platforms before the Next.js framework, which may front any of them.

Where a platform documents how a URL selects one variant of a product page (a query parameter on the product's own
URL), `variant_params` names the parameters, so that resolution knows a link to the page's own resource with one of
them for a variant of the page, not a link to a thing described elsewhere.

Where a platform documents a public, tokenless product JSON endpoint on the shop's own host, `product_json` names the
request for a product page and `answer_shape` the schema.org node its answer holds, as the platform documents its
members (`Shape`, C2), amounts in the minor unit the documentation states; a platform whose product API
needs a credential (SFCC's OCAPI client id, BigCommerce's storefront token, Magento's REST token) or that has none
(Next.js) is recorded only.

Where a platform documents a script global that states the page's currency (the presentment currency the shop shows
prices in), `currency_members` names it; it is read only on a page that shows the platform's signal, as a page-level
declaration (`collect.currencies`).

Shopify's product answer states one Offer per variant, each with the variant's own identifiers and price, as a page's
per-variant Offers do: resolution weighs them as it weighs those (price), and they fill the `variants`
rows when the page states none. Each Offer's `url` is the variant's page (Liquid `variant.url`), so the request URL
that selects a variant names its row (`resolve.rows.with_variants`). Of them, the page shows one variant (Liquid
`product.selected_or_first_available_variant`), whose `sku` and `barcode` are read as the product's own, so resolution
reads its Offer as the variant the page shows; held from the variant's node, they stay that variant's,
and publish for the product only where the page states none of its own (Variant fill). An image a variant
features (its `featured_image`) is that variant's, so the page's images are the shown variant's, first, and those no
variant features. Its `price_min` and `price_max` are the range its variants' prices span (an AggregateOffer's
`lowPrice` and `highPrice`): a page amount equal to one of them is that bound, not the shown variant's price, where the
page states another for it (price). WooCommerce's answer states one price for the product: a product
whose variations are priced differently has none for the page, which shows the selected variation's, so its answer is
read without its Offer and never stands against the price the page shows (merge, do not
override).
"""

import re
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from pydantic import JsonValue

from snowcrawl.adapters.base import ALL, Members, Shape
from snowcrawl.storage.capture_bundle import HeaderList

__all__ = [
    "SIGNATURES",
    "CurrencyMember",
    "PlatformSignature",
    "ProductJson",
    "answer_shape",
    "platform_of",
    "product_json",
    "signature_of",
]


@dataclass(frozen=True)
class CurrencyMember:
    """A documented script assignment that states the page's currency: the reference assigned to (`Shopify.currency`)
    and the members from the assigned value to the ISO 4217 code (none: the value is the code)."""

    assigned: str
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformSignature:
    platform: str
    markers: tuple[str, ...] = ()  # in the page's markup: the platform's asset paths and script globals
    header_names: frozenset[str] = frozenset()  # lowercase response header names only the platform's servers send
    # The query parameters (names, or name prefixes ending in `_`) by which the platform's documented product URLs
    # select one variant of the product.
    variant_params: tuple[str, ...] = ()
    currency_members: tuple[CurrencyMember, ...] = ()


# The global the Online Store's theme layout defines (`content_for_header`); a headless storefront has none, nor the
# Online Store's Ajax API.
_ONLINE_STORE: Final = "Shopify.shop"
# shopify.dev Liquid `variant.url`: the product URL with `?variant=<id>` selects that variant.
_SHOPIFY_VARIANT_PARAM: Final = "variant"

SIGNATURES: Final[tuple[PlatformSignature, ...]] = (
    # Shopify: the Online Store's `Shopify.shop` global (`content_for_header`) and the platform's asset CDN, which a
    # headless storefront (Hydrogen) also serves from; its edge sends `x-shopid` and `x-shopify-stage`.
    PlatformSignature(
        "shopify",
        markers=(_ONLINE_STORE, "cdn.shopify.com/", "shopify-features"),
        header_names=frozenset({"x-shopid", "x-shopify-stage"}),
        variant_params=(_SHOPIFY_VARIANT_PARAM,),
        # shopify.dev Liquid / `content_for_header`: `Shopify.currency = {"active": <presentment currency>, "rate": …}`.
        currency_members=(CurrencyMember("Shopify.currency", ("active",)),),
    ),
    # Salesforce Commerce Cloud (Demandware): its controller path and static asset path (SFCC B2C developer docs,
    # "URL syntax": `/on/demandware.store/Sites-{site}-Site/`, `/on/demandware.static/`).
    # SFCC developer docs, "Product variation URLs": `dwvar_<product id>_<attribute>=<value>` selects a variation value.
    PlatformSignature("sfcc", markers=("/on/demandware.store/", "/on/demandware.static/"), variant_params=("dwvar_",)),
    # Magento / Adobe Commerce: its RequireJS module namespace and init script (Adobe Commerce frontend docs,
    # "JavaScript init"); its full-page cache sends `x-magento-tags`.
    PlatformSignature(
        "magento",
        markers=("Magento_Ui/js/core/app", '"Magento_', "mage/requirejs/"),
        header_names=frozenset({"x-magento-tags", "x-magento-cache-debug"}),
    ),
    # BigCommerce: the Stencil theme's asset CDN and bootstrap (BigCommerce dev docs, "Stencil":
    # `cdn11.bigcommerce.com`, `stencilBootstrap`).
    PlatformSignature("bigcommerce", markers=("cdn11.bigcommerce.com/s-", "stencilBootstrap(")),
    # WooCommerce: the plugin's asset path and the class its script swaps on the body (WooCommerce developer docs).
    # Its variation links carry `attribute_<attribute slug>=<term>` for each attribute (WooCommerce developer docs,
    # `WC_Product_Variation::get_permalink`).
    PlatformSignature(
        "woocommerce", markers=("/plugins/woocommerce/", "woocommerce-no-js"), variant_params=("attribute_",)
    ),
    # Shopware 6: the storefront's bundle asset path and the globals its layout's meta template defines, among them
    # `window.currencyIsoCode`, the sales channel context's currency.
    PlatformSignature(
        "shopware",
        markers=("/bundles/storefront/", "window.salesChannelId"),
        currency_members=(CurrencyMember("window.currencyIsoCode"),),
    ),
    # Next.js (commerce or any): the framework's data script and static path (Next.js docs, "Pages Router": the
    # `__NEXT_DATA__` script; the `/_next/static/` build output).
    PlatformSignature("nextjs", markers=('id="__NEXT_DATA__"', "/_next/static/")),
)

# The most markup scanned for a marker: a platform's script and asset references sit in the document's head and the
# start of its body; a longer scan reads a product description's text.
_SCAN_CHARS: Final = 500_000


def signature_of(markup: str, headers: HeaderList) -> PlatformSignature | None:
    """The signature of the platform whose signal the page shows (module docstring), or None."""
    names = {name.lower() for name, _ in headers}
    head = markup[:_SCAN_CHARS]
    for signature in SIGNATURES:
        if names & signature.header_names or any(marker in head for marker in signature.markers):
            return signature
    return None


def platform_of(markup: str, headers: HeaderList) -> str | None:
    """The platform whose signal the page shows (module docstring), or None."""
    signature = signature_of(markup, headers)
    return None if signature is None else signature.platform


@dataclass(frozen=True)
class ProductJson:
    """A platform's public JSON of the product a page shows: the platform, the endpoint, and the product's key the page
    URL names (a handle or slug), which the answer must state (`acquire.identity.identifies`)."""

    platform: str
    url: str
    key: str


# Shopify's product route (shopify.dev Ajax API, "Product": `GET /{locale}/products/{product-handle}.js`, which "returns
# the JSON of a product"; the route also appears under a collection, `/collections/{handle}/products/{handle}`).
_SHOPIFY_PRODUCT: Final = re.compile(
    r"(?P<prefix>(?:/[a-z]{2,3}(?:-[a-z]{2})?)?)(?:/collections/[\w-]+)?/products/(?P<handle>[\w-]+)/?",
    re.ASCII | re.IGNORECASE,
)
# WordPress REST API discovery (developer.wordpress.org/rest-api/using-the-rest-api/discovery): the API root is the
# `link rel="https://api.w.org/"` of every page.
_API_ROOT: Final = re.compile(r"""<link[^>]+rel=["']https://api\.w\.org/["'][^>]*>""", re.IGNORECASE)
_HREF: Final = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
# A WordPress slug: lowercase letters, digits and hyphens (`sanitize_title`); a percent-encoded one is left alone.
_SLUG: Final = re.compile(r"[a-z0-9-]+", re.ASCII)


def product_json(platform: str | None, url: str, markup: str) -> ProductJson | None:
    """The documented public product JSON request for the product page at `url` of `platform`, or None: Shopify's Ajax
    API product (`/products/{handle}.js`) of an Online Store page (`_ONLINE_STORE`), or WooCommerce's Store API
    products by slug (developer.woocommerce.com, "Store API, Products": `GET /wp-json/wc/store/v1/products?slug=`), at
    the API root the page links to, on its own origin."""
    parts = urlsplit(url)
    route = _SHOPIFY_PRODUCT.fullmatch(parts.path)
    if platform == "shopify" and route is not None and _ONLINE_STORE in markup[:_SCAN_CHARS]:
        handle = route["handle"]
        return ProductJson("shopify", f"{parts.scheme}://{parts.netloc}{route['prefix']}/products/{handle}.js", handle)
    if platform == "woocommerce":
        return _store_api(parts.scheme, parts.netloc, parts.path, markup)
    return None


def _store_api(scheme: str, netloc: str, path: str, markup: str) -> ProductJson | None:
    slug = path.rstrip("/").rpartition("/")[2]
    link = _API_ROOT.search(markup[:_SCAN_CHARS])
    href = None if link is None else _HREF.search(link.group(0))
    if not _SLUG.fullmatch(slug) or href is None:
        return None
    root = urljoin(f"{scheme}://{netloc}/", href.group(1).replace("&amp;", "&"))
    # Same origin as the page: a root on another scheme (an `http` link on an `https` page) would send the call where
    # its answer can be altered in transit.
    if (urlsplit(root).scheme, urlsplit(root).netloc) != (scheme, netloc) or "?" in root:
        return None
    return ProductJson("woocommerce", f"{root.rstrip('/')}/wc/store/v1/products?slug={quote(slug)}", slug)


# Shopify Ajax API, "Product" (shopify.dev/docs/api/ajax/reference/product): the product's `title`, `url` (its route
# on the shop), `description` (its HTML body), `images` (every image's URL, the featured one first), `vendor` (the name
# the merchant files the product under, its brand or any other label: a fallback for the brand, `Shape.fallbacks`), and
# its `variants`, each with its `featured_image` (the image tied to the variant, its `src`), `title`, `sku`, `barcode`
# (the GTIN, UPC or ISBN), `available` (whether it can be bought: `true` names no one schema.org ItemAvailability,
# since a variant on pre-order or back order can be bought too; `false` is `OutOfStock`) and `price`, an integer in
# cents for every currency ("prices are in cents").
_SHOPIFY_VARIANT: Final = Shape(
    type="Offer",
    at=("variants", ALL),
    properties={
        "name": ("title",),
        "sku": ("sku",),
        "gtin": ("barcode",),
        "price": ("price",),
        "availability": ("available",),
    },
    minor_units={"price": 2},
    booleans={"availability": (None, "https://schema.org/OutOfStock")},
)
# The product's `price_min` and `price_max`, in cents, the lowest and highest of its variants' prices (`price_varies`
# when they differ): an AggregateOffer's `lowPrice` and `highPrice` (schema.org: "the lowest price of all offers
# available"), the range the variants' prices span. Its `price` is `price_min`, no variant's own: it is not read.
_SHOPIFY_RANGE: Final = Shape(
    type="AggregateOffer",
    anchor=("price_min",),
    properties={"lowPrice": ("price_min",), "highPrice": ("price_max",)},
    minor_units={"lowPrice": 2, "highPrice": 2},
)
_SHOPIFY_PRODUCT_MEMBERS: Final[dict[str, Members]] = {
    "name": ("title",),
    "url": ("url",),
    "description": ("description",),
    "brand": ("vendor",),
}
_SHOPIFY_IMAGES: Final[Members] = ("images", ALL)
# The key of no file: an image whose URL names none is tied to no variant.
_NO_FILE: Final = ("", "")


def _shopify_product(
    own: dict[str, Members], images: tuple[Members, ...] = (_SHOPIFY_IMAGES,), variant: Shape = _SHOPIFY_VARIANT
) -> Shape:
    """The product's node, with `own`, the members of the shown variant's identifiers (`_shown_variant`),
    `images`, the members of the images its page shows (`_shown_images`), and `variant`, its variants' Offers
    (`_addressed`)."""
    properties = {**_SHOPIFY_PRODUCT_MEMBERS, **own}
    if images:
        properties["image"] = images[0]
    return Shape(
        type="Product",
        properties=properties,
        also={"image": images[1:]},
        nodes={"offers": (variant, _SHOPIFY_RANGE)},
        fallbacks=frozenset({"brand"}),
    )


def _addressed(answer: dict[str, JsonValue], page_url: str | None) -> Shape:
    """The variants' Offers, each with its `url` (schema.org `Offer.url`), the page that shows the variant: shopify.dev
    Liquid `variant.url`, the product's URL (the answer's `url`, resolved against the page's) with `?variant=` and the
    variant's `id`, the query by which the platform selects it (`_SHOPIFY_VARIANT_PARAM`). Without an absolute product
    URL, the Offers state none."""
    route = answer.get("url")
    try:
        parts = urlsplit(urljoin(page_url or "", route)) if isinstance(route, str) else None
    except ValueError:  # RFC 3986 §3.2.2: an authority no URL has (an unclosed IPv6 bracket) names no page
        parts = None
    if parts is None or not (parts.scheme and parts.netloc):
        return _SHOPIFY_VARIANT
    page = parts._replace(query="", fragment="").geturl()
    return replace(
        _SHOPIFY_VARIANT,
        properties={**_SHOPIFY_VARIANT.properties, "url": ("id",)},
        prefixes={"url": f"{page}?{_SHOPIFY_VARIANT_PARAM}="},
    )


def _shown_images(answer: dict[str, JsonValue], shown: int, page_url: str | None) -> tuple[Members, ...]:
    """The members of the product's images the page of its shown variant shows: an image a variant's `featured_image`
    names (the same file: `_file`) is that variant's, shown only on its page and first there (Liquid
    `selected_or_first_available_variant.featured_image`, the image the page shows for it); an image no variant
    features is the product's, shown on every variant's page after it."""
    images, variants = answer.get("images"), answer.get("variants")
    if not isinstance(images, list) or not isinstance(variants, list):
        return (_SHOPIFY_IMAGES,)
    tied: dict[tuple[str, str], set[int]] = {}
    for index, variant in enumerate(variants):
        featured = variant.get("featured_image") if isinstance(variant, dict) else None
        if isinstance(featured, dict) and (key := _file(featured.get("src"), page_url)) is not None:
            tied.setdefault(key, set()).add(index)
    owners = [tied.get(_file(image, page_url) or _NO_FILE) for image in images]
    own = [n for n, tied_to in enumerate(owners) if tied_to is not None and shown in tied_to]
    kept = own + [n for n, tied_to in enumerate(owners) if tied_to is None]
    if kept == list(range(len(images))):
        return (_SHOPIFY_IMAGES,)
    return tuple(("images", n) for n in kept)


def _file(src: JsonValue, page_url: str | None) -> tuple[str, str] | None:
    """The file an image URL of the answer names: its authority and path (RFC 3986 §3.3: the path identifies the
        resource; the query, `?v=`, only versions it), its protocol-relative URL (`//cdn…`) resolved against the page's;
    None for no URL."""
    if not isinstance(src, str):
        return None
    try:
        parts = urlsplit(urljoin(page_url or "", src))
    except ValueError:  # RFC 3986 §3.2.2: an authority no URL has (an unclosed IPv6 bracket) names no file
        return None
    return (parts.netloc.lower(), parts.path) if parts.netloc and parts.path else None


def _shown_variant(answer: dict[str, JsonValue], page_url: str | None) -> int | None:
    """The index of the variant a Shopify product page shows (shopify.dev Liquid, `product.selected_variant` and
    `product.selected_or_first_available_variant`): the one whose `id` the page URL's `?variant=` names, else, when the
    URL names none or an id the product lacks, the first `available` one, else the first; None without variants."""
    variants = answer.get("variants")
    if not isinstance(variants, list) or not variants:
        return None
    members = [v if isinstance(v, dict) else {} for v in variants]
    selected = parse_qs(urlsplit(page_url or "").query).get(_SHOPIFY_VARIANT_PARAM, [])
    ids = [None if (i := m.get("id")) is None else str(i) for m in members]
    if len(selected) == 1 and selected[0] in ids:
        return ids.index(selected[0])
    return next((n for n, m in enumerate(members) if m.get("available") is True), 0)


# WooCommerce Store API, "Products" (github.com/woocommerce/woocommerce, plugins/woocommerce/src/StoreApi/docs,
# products.md): a list of products, each with `name`, `permalink`, `sku`, `images[].src`, and `prices`, whose `price`
# is a string of the amount in the currency's minor unit, `currency_minor_unit` its decimal places and `currency_code`
# its ISO 4217 code; `price_range` is set when the product's variations are priced differently.
_WOO_PRODUCT_NODE: Final = Shape(
    type="Product",
    at=(ALL,),
    properties={"name": ("name",), "url": ("permalink",), "sku": ("sku",), "image": ("images", ALL, "src")},
)
_WOO_OFFER: Final = Shape(
    type="Offer",
    at=("prices",),
    properties={"price": ("price",), "priceCurrency": ("currency_code",)},
    minor_units={"price": ("currency_minor_unit",)},
)
# A product whose variations share one price: its answer's price is the product's Offer.
_WOO_PRICED_NODE: Final = Shape(
    type="Product", at=_WOO_PRODUCT_NODE.at, properties=_WOO_PRODUCT_NODE.properties, nodes={"offers": (_WOO_OFFER,)}
)


def answer_shape(platform: str, answer: JsonValue, page_url: str | None) -> Shape | None:
    """The schema.org node the documented product JSON `answer` of `platform` holds (module docstring), for the
    product page served at `page_url`; None for a platform with no documented product JSON, or an answer not of its
    documented form."""
    if platform == "shopify" and isinstance(answer, dict):
        shown = _shown_variant(answer, page_url)
        if shown is None:
            return _shopify_product({})
        own: dict[str, Members] = {"sku": ("variants", shown, "sku"), "gtin": ("variants", shown, "barcode")}
        return _shopify_product(own, _shown_images(answer, shown, page_url), _addressed(answer, page_url))
    if platform == "woocommerce" and isinstance(answer, list) and len(answer) == 1 and isinstance(answer[0], dict):
        prices = answer[0].get("prices")
        varies = not isinstance(prices, dict) or prices.get("price_range") is not None
        return _WOO_PRODUCT_NODE if varies else _WOO_PRICED_NODE
    return None
