"""Stand-in markers: a 2xx HTML document that shows a vendor's stand-in marker is `blocked`
and labelled with that vendor; nothing else in the vendor data changes a block class. The markers come from the
adapter package's data, never from this file (C3)."""

import pytest

from snowcrawl.acquire.challenge import block_class_of, challenge_label
from snowcrawl.adapters.challenges import SIGNATURES, ChallengeSignature, stand_in_vendor

_HTML = (("content-type", "text/html"),)
_LINKS = "".join(f'<a href="/item/{i}">Item {i}</a>' for i in range(8))
_STAND_INS = [s for s in SIGNATURES if s.stand_in_markers]
URL = "https://shop.example.test/item/1"
# The vendor whose markers include a pattern: the edge that prints an error reference id.
_EDGE_REFERENCE_VENDOR = next(s.vendor for s in SIGNATURES if any(not isinstance(m, str) for m in s.stand_in_markers))


def _page(extra: str = "") -> str:
    """A page with site structure: enough text and links to other pages of its host."""
    return f"<html><body><h1>Item</h1><p>{'word ' * 80}</p>{_LINKS}{extra}</body></html>"


@pytest.mark.parametrize("sig", _STAND_INS, ids=[s.vendor for s in _STAND_INS])
@pytest.mark.parametrize("status", [200, 202])
def test_a_2xx_page_showing_a_stand_in_marker_is_blocked_and_labelled(sig: ChallengeSignature, status: int) -> None:
    body = _page(f"<!-- {sig.stand_in_markers[0]} -->")

    assert block_class_of(status, _HTML, body) == "blocked"
    assert challenge_label(status, _HTML, body, URL) == sig.vendor


@pytest.mark.parametrize("sig", _STAND_INS, ids=[s.vendor for s in _STAND_INS])
def test_a_2xx_shell_showing_a_stand_in_marker_is_blocked(sig: ChallengeSignature) -> None:
    # A verification document: one short heading and a form, too little text for a page (a shell), no site structure.
    marker = sig.stand_in_markers[0]
    body = f'<html><body><h3>Checking</h3><form method="POST" action="/x">{marker}</form></body></html>'

    assert block_class_of(200, _HTML, body) == "blocked"
    assert challenge_label(200, _HTML, body, URL) == sig.vendor


@pytest.mark.parametrize(
    "reference",
    [
        "Reference&#32;&#35;18&#46;1a2b3c4d&#46;1700000000&#46;5e6f7a8b",  # the edge's own template
        '<div class="note">Reference ERR: 0.1a2b3c4d.1700000000.5e6f7a8b</div>',  # a customer template printing it
    ],
)
def test_a_2xx_page_printing_the_edge_error_reference_is_blocked(reference: str) -> None:
    body = _page(reference)

    assert block_class_of(200, _HTML, body) == "blocked"
    assert challenge_label(200, _HTML, body, URL) == _EDGE_REFERENCE_VENDOR


@pytest.mark.parametrize("signature", [s for s in SIGNATURES if s.cookie_prefixes or s.script_paths])
def test_cookies_and_script_paths_alone_never_block_a_2xx_page(signature: ChallengeSignature) -> None:
    # A vendor sets its cookies and loads its script on the pages it lets through too.
    scripts = "".join(f'<script src="https://cdn.example.test{path}x.js"></script>' for path in signature.script_paths)
    headers = (*_HTML, *(("set-cookie", f"{prefix}1=v") for prefix in signature.cookie_prefixes))

    assert stand_in_vendor(_page(scripts)) is None
    assert block_class_of(200, headers, _page(scripts)) == "ok"


@pytest.mark.parametrize("sig", _STAND_INS, ids=[s.vendor for s in _STAND_INS])
def test_usable_content_beats_a_stand_in_marker(sig: ChallengeSignature) -> None:
    # The marker needs corroboration: a document that carries the page's structured data is the page.
    price = '<meta property="product:price:amount" content="9.50">'
    marked = _page(f"<!-- {sig.stand_in_markers[0]} -->{price}")
    assert block_class_of(200, _HTML, marked, types=("Offer", "Product")) == "ok"


@pytest.mark.parametrize("signature", [s for s in SIGNATURES if s.header_names or s.cookie_prefixes or s.script_paths])
def test_a_2xx_shell_is_labelled_by_cookies_headers_and_script_paths_only_with_structural_corroboration(
    signature: ChallengeSignature,
) -> None:
    # A script-only document a vendor fronts: its signature without a stand-in marker. Over HTTP no script ran, so
    # nothing structural says it stands in for the page; a refusal status, or the browser's render still showing a
    # shell, does.
    scripts = "".join(f'<script src="https://cdn.example.test{path}x.js"></script>' for path in signature.script_paths)
    shell = f'<html><head><script src="/app.js"></script>{scripts}</head><body><div id="root"></div></body></html>'
    headers = (
        *_HTML,
        *((name, "1") for name in signature.header_names),
        *(("set-cookie", f"{prefix}1=v") for prefix in signature.cookie_prefixes),
    )

    assert block_class_of(200, headers, shell) == "shell"
    assert challenge_label(200, headers, shell, URL, block_class="shell") is None
    assert challenge_label(200, headers, shell, URL, block_class="shell", rendered=True) == signature.vendor
    assert challenge_label(403, headers, shell, URL) == signature.vendor
