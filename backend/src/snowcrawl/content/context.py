"""The normalization context of a captured page: what the page states about its
language and URLs, and when it was fetched.

Only evidence the page or the request states is read (C7): `<html lang>`, `og:locale`, the `hreflang` of the alternate
links to the page itself, the Content-Language the response and the page declare, the document base URL and the final
URL. `cctld_territory` is left empty, so a number or date that needs it declines rather than guesses; the money rules
alone read the ccTLD, to narrow a shared currency sign. `page_currencies`, `declared_currencies`
and `state_currencies` come from the page, which `collect.currencies` reads (this module sits below the collectors).
"""

from collections.abc import Sequence
from datetime import datetime

from snowcrawl.content.dom import document_base_url, strip
from snowcrawl.content.metadata import canonical_link, html_lang, language_alternates, open_graph
from snowcrawl.content.page import ParsedPage
from snowcrawl.normalize.base import Context
from snowcrawl.normalize.url import canonical_url


def page_context(
    page: ParsedPage,
    final_url: str,
    captured_at: datetime,
    *,
    request_locale: str | None = None,
    request_url: str | None = None,
    page_url: str | None = None,
    headers: Sequence[tuple[str, str]] = (),
) -> Context:
    """The page's context; `final_url` is the URL its document came from, `request_url` the URL it was requested at,
    `page_url` the page it stands for when that is another (`CaptureManifest.page_url`), `headers` the response's, as
    received."""
    return Context(
        html_lang=strip(html_lang(page)),
        og_locale=strip(open_graph(page).get("og:locale")),
        hreflang=hreflang(page, final_url),
        content_language=content_language(page, headers),
        request_locale=request_locale,
        captured_at=captured_at,
        base_url=document_base_url(page, final_url),
        page_url=page_url or final_url,
        request_url=request_url,
    )


def hreflang(page: ParsedPage, final_url: str) -> tuple[str, ...]:
    """The languages the page itself states it is in through `hreflang`: HTML Living Standard §4.6.2 and §4.6.7.1, the
    `hreflang` of a `link` whose `rel` has `alternate` names the language of the document that link points to. Only a
    link to the page itself (its final URL or its canonical link, RFC 6596), resolved against the document base URL,
    states this page's language; the others name the languages of other pages. Each once, in document order."""
    own = {url for url in (canonical_url(final_url, None), canonical_link(page, final_url)) if url is not None}
    return tuple(dict.fromkeys(language for language, url in language_alternates(page, final_url) if url in own))


def content_language(page: ParsedPage, headers: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The language tags the response's `Content-Language` field (RFC 9110 §8.5, a comma-separated list) and the page's
    `<meta http-equiv="content-language">` pragma (HTML Living Standard §4.2.5.3) declare, each once, in that order."""
    written = [value for name, value in headers if name.lower() == "content-language"]
    for node in page.tree.css("meta[http-equiv][content]"):
        if (node.attributes.get("http-equiv") or "").strip().lower() == "content-language":
            written.append(node.attributes.get("content") or "")
    tags = (tag.strip() for value in written for tag in value.split(","))
    return tuple(dict.fromkeys(tag for tag in tags if tag))
