"""Anti-bot challenge signatures: site data, which names vendors and so lives in the adapter package (C3).

`acquire.challenge` labels a refused response with the vendor whose signature it shows: a response header only that
vendor sends, a cookie only it sets, or the path of its challenge resource. The label is recorded on the attempt and
counted. Only a stand-in marker changes the block class: markup that appears only in the
document a vendor serves in place of the page (its challenge, waiting room or block page), never on a page it lets
through, so a 2xx document showing one is `blocked`. Header names, cookies and script paths only label, and a 2xx shell
only with structural corroboration: a vendor sets its cookies and loads its script on the pages it lets through too.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["SIGNATURES", "ChallengeSignature", "stand_in_vendor", "vendor_of"]


@dataclass(frozen=True)
class ChallengeSignature:
    vendor: str
    header_names: frozenset[str] = frozenset()  # lowercase response header names
    cookie_prefixes: tuple[str, ...] = ()  # cookie names, or the fixed start of names the vendor numbers
    script_paths: tuple[str, ...] = ()  # in a challenge document's markup
    # In the markup of the document served in place of the page only: a fixed string, or a pattern for a token the
    # vendor fills in.
    stand_in_markers: tuple[str | re.Pattern[str], ...] = ()


SIGNATURES: tuple[ChallengeSignature, ...] = (
    ChallengeSignature(
        "cloudflare",
        header_names=frozenset({"cf-mitigated", "cf-chl-bypass"}),
        cookie_prefixes=("__cf_bm", "cf_clearance", "__cf_chl_"),
        script_paths=("/cdn-cgi/challenge-platform/",),
    ),
    ChallengeSignature(
        "datadome",
        header_names=frozenset({"x-datadome", "x-datadome-cid", "x-dd-b"}),
        cookie_prefixes=("datadome",),
        script_paths=("captcha-delivery.com", "js.datadome.co"),
    ),
    ChallengeSignature(
        "akamai",
        cookie_prefixes=("_abck", "bm_sz", "ak_bmsc", "bm_sv"),
        script_paths=("/akam/",),
        stand_in_markers=(
            # The edge's waiting-room and failover template, whose ESI comments name it (served with 200).
            "AKAMAI-UPA:",
            # The edge's error reference, in its own error template and in the customer templates that print it in
            # place of the page: "Reference" and the id <error code>.<edge server hex>.<epoch seconds>.<request
            # hex>, its dots written as character references (HTML §13.5) in the edge's own template.
            re.compile(
                r"Reference[^<]{0,16}?\b\d{1,3}(?:\.|&#46;)[0-9a-f]{6,8}(?:\.|&#46;)\d{10}(?:\.|&#46;)[0-9a-f]{4,8}\b"
            ),
        ),
    ),
    ChallengeSignature(
        "imperva",
        header_names=frozenset({"x-iinfo"}),
        cookie_prefixes=("incap_ses_", "visid_incap_", "reese84", "nlbi_"),
        script_paths=("/_Incapsula_Resource",),
        stand_in_markers=("Incapsula incident ID",),
    ),
    ChallengeSignature(
        "human",
        cookie_prefixes=("_px3", "_pxhd", "_pxvid", "_px2", "_pxde"),
        script_paths=("captcha.px-cdn.net", "client.perimeterx.net"),
    ),
    ChallengeSignature(
        "kasada",
        header_names=frozenset({"x-kpsdk-ct", "x-kpsdk-cd", "x-kpsdk-c"}),
        script_paths=("/ips.js?",),
    ),
    ChallengeSignature(
        "aws_waf",
        header_names=frozenset({"x-amzn-waf-action"}),
        cookie_prefixes=("aws-waf-token",),
        script_paths=("token.awswaf.com", "captcha.awswaf.com"),
        # The challenge interstitial's own configuration object (served with 202).
        stand_in_markers=("window.gokuProps",),
    ),
    ChallengeSignature(
        "litespeed",
        # The web server's reCAPTCHA bot verification: the form of the document it serves in place of the page
        # (with 200) posts the token to this server-handled path, which no page it lets through links.
        stand_in_markers=("/.lsrecap/recaptcha",),
    ),
)


def vendor_of(header_names: Iterable[str], cookie_names: Iterable[str], markup: str) -> ChallengeSignature | None:
    """The first signature the response shows, if any: header names compared case-insensitively (RFC 9110 §5.1),
    cookie names exactly (RFC 6265 §4.1.1)."""
    headers = {name.lower() for name in header_names}
    cookies = tuple(cookie_names)
    for signature in SIGNATURES:
        if not headers.isdisjoint(signature.header_names):
            return signature
        if any(name.startswith(signature.cookie_prefixes) for name in cookies if signature.cookie_prefixes):
            return signature
        if any(path in markup for path in signature.script_paths) or _shows_stand_in(signature, markup):
            return signature
    return None


def stand_in_vendor(markup: str) -> ChallengeSignature | None:
    """The first signature whose stand-in marker the markup shows, if any."""
    return next((s for s in SIGNATURES if _shows_stand_in(s, markup)), None)


def _shows_stand_in(signature: ChallengeSignature, markup: str) -> bool:
    return any(
        marker.search(markup) is not None if isinstance(marker, re.Pattern) else marker in markup
        for marker in signature.stand_in_markers
    )
