"""Derived values: the only values core publishes without a candidate
stating them, each from a named function with its standard.

A derivation fills a field only when preference left it missing: a published field keeps its value, and a conflict
stays a conflict. Each value carries `derived_by`. Adding a derivation is a C2/C13 review item.

A job page that holds its own application form (`collect.application`) is where a candidate applies: its final URL is
the posting's apply link when the page states no other.

The page-URL field's candidates are first held to the page: when it states one canonical link on its own site, a
candidate naming another document is not the page's URL (`OTHER_DOCUMENT`), and preference decides among the rest.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import urlsplit

from pydantic import JsonValue

from snowcrawl.collect.base import Candidate
from snowcrawl.collect.schema_org_map import names_item_url, same_site
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import normalize
from snowcrawl.normalize.url import RELATIVE_WITHOUT_BASE
from snowcrawl.normalize.url_key import document_key
from snowcrawl.resolve.preference import Chosen, Preference, Rejection, Resolver, Unpublished
from snowcrawl.resolve.types import Derivation
from snowcrawl.schemas.paths import parse_paths
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# Why a candidate of the page-URL field does not publish: it names another document than the page (`_held_to_page`).
OTHER_DOCUMENT: Final = "other_document"
# schema.org JobPosting `potentialAction`, an ApplyAction whose `target` is where a candidate applies.
_APPLY_TARGET: Final = ("potentialAction", "target")


@dataclass(frozen=True, slots=True)
class Derived:
    """A value no candidate stated as it publishes; `source` is the candidate it was derived from, if any; `rejected`,
    the field's candidates that did not publish, with their reasons."""

    value: JsonValue
    derived_by: Derivation
    source: Candidate | None
    rejected: tuple[Rejection, ...] = ()
    stated: JsonValue = None  # the value as the page states it, when it differs (`Ok.stated`)


def is_page_url_field(schema: SchemaDef, spec: FieldSpec, primary: frozenset[str]) -> bool:
    """Whether the field is the URL of the page's entity: on a detail page, a url field whose path is `T.url` for a
    primary type T (schema.org Thing.url, "URL of the item"; the primary types of identity-and-intent's target
    selection) in every alternative. A field with any other alternative (an apply link,
    `JobPosting.potentialAction.target`) names a link the page states, which the page's own URL never stands in for."""
    if schema.page_kind != "detail" or spec.type is not FieldType.URL or spec.schema_org is None:
        return False
    paths = parse_paths(spec.schema_org)
    return all(path.root in primary and names_item_url(path) for path in paths)


def is_application_field(schema: SchemaDef, spec: FieldSpec) -> bool:
    """Whether the field is a detail page's apply link: a url field whose path is `JobPosting.potentialAction.target`
    in every alternative (`_APPLY_TARGET`)."""
    if schema.page_kind != "detail" or spec.type is not FieldType.URL or spec.schema_org is None:
        return False
    paths = parse_paths(spec.schema_org)
    return all(path.root == "JobPosting" and path.steps == _APPLY_TARGET and not path.joined for path in paths)


def fill(
    spec: FieldSpec,
    preference: Preference,
    candidates: Sequence[Candidate],
    resolver: Resolver,
    ctx: Context,
    *,
    canonical_url: str | None,
    canonical_href: str | None = None,
    page_url_field: bool,
    application_url: str | None = None,
) -> Preference | Derived:
    """The field's derived value when preference left it missing, else `preference`, the page-URL field's
    held to the page first (`_held_to_page`).

    A value from the field's own candidate comes first; the page URL only when the field is the page's
    (`is_page_url_field`): the canonical link, then the final URL; or `application_url`, the final URL of a page that
    holds the application, for the apply link (`is_application_field`).
    """
    own = _own_url(spec, canonical_url, ctx) if page_url_field else None
    if page_url_field:
        preference = _held_to_page(spec, preference, candidates, resolver, own)
    if not (isinstance(preference, Unpublished) and preference.status == "missing"):
        return preference
    derived = absolutize(spec, candidates, resolver, ctx)
    if derived is not None and derived.source is not None and own is not None and _elsewhere(derived.value, own):
        # A relative reference resolves only here, so it is held to the page once resolved.
        source = derived.source
        kept = tuple(r for r in preference.rejected if r[0] is not source)
        preference = replace(preference, rejected=(*kept, (source, OTHER_DOCUMENT)))
        derived = None
    if derived is None and page_url_field:
        derived = canonical_link(spec, canonical_url, ctx, canonical_href) or final_url(spec, ctx)
    if derived is None and application_url is not None:
        derived = application_form(spec, application_url, ctx)
    return preference if derived is None else replace(derived, rejected=preference.rejected)


def _own_url(spec: FieldSpec, canonical_url: str | None, ctx: Context) -> str | None:
    """The page's own URL: its one canonical link (RFC 6596 §3; `content.metadata.canonical_href` drops conflicting
    ones) when it names a URL on the site of the final URL (RFC 9110 §15.4; HTML Standard §7.1.1, the same registrable
    domain), else None. The canonical link is the page's statement of its preferred URL, so `og:url` or a structured
    `url` naming another document does not name this page."""
    if canonical_url is None or ctx.page_url is None:
        return None
    own = normalize(canonical_url, spec, ctx)
    if not isinstance(own, Ok) or not isinstance(own.value, str):
        return None
    # RFC 9110 §4.2.4: an http(s) URI with userinfo is invalid, so such a link names no page.
    parts = urlsplit(own.value)
    if parts.hostname is None or "@" in parts.netloc:
        return None
    return own.value if same_site(parts.hostname, ctx.page_url) else None


def _held_to_page(
    spec: FieldSpec, preference: Preference, candidates: Sequence[Candidate], resolver: Resolver, own: str | None
) -> Preference:
    """`preference` with the kept candidates naming another document than the page's own URL `own` rejected
    `OTHER_DOCUMENT`, preference deciding among the rest: a structured `url` built from another id than the page's
    (a SKU), or an `og:url` naming another page, names another document, while the page states its own by its
    canonical link."""
    others = [c for c in preference.kept if own is not None and _elsewhere(_value(c), own)]
    if not others:
        return preference
    rest = [c for c in candidates if not any(c is other for other in others)]
    held = resolver.prefer_field(spec, rest)
    return replace(held, rejected=(*held.rejected, *((c, OTHER_DOCUMENT) for c in others)))


def _value(candidate: Candidate) -> object:
    return candidate.normalized.value if isinstance(candidate.normalized, Ok) else None


def _elsewhere(value: object, own: str) -> bool:
    """Whether the URL `value` names another document than `own` (`normalize.url_key.document_key`: a query or a
    fragment the page's URL does not carry may select a view or a part of the same document, a variant or an anchor,
    so it alone names no other one)."""
    return isinstance(value, str) and document_key(value) != document_key(own)


def absolutize(spec: FieldSpec, candidates: Sequence[Candidate], resolver: Resolver, ctx: Context) -> Derived | None:
    """RFC 3986 §5.2 (reference resolution) against the HTML document base URL: a relative url or image_url reference
    a collector could not resolve, resolved and then put through preference like any candidate."""
    if spec.type not in (FieldType.URL, FieldType.IMAGE_URL) or ctx.base_url is None:
        return None
    pairs = [
        (candidate.model_copy(update={"normalized": normalize(candidate.raw, spec, ctx)}), candidate)
        for candidate in candidates
        if candidate.normalized == RELATIVE_WITHOUT_BASE
    ]
    preference = resolver.prefer_field(spec, [resolved for resolved, _ in pairs])
    if not isinstance(preference, Chosen) or not isinstance(preference.candidate.normalized, Ok):
        return None
    source = next(original for resolved, original in pairs if resolved is preference.candidate)
    resolved = preference.candidate.normalized
    return Derived(resolved.value, "absolutize", source, stated=resolved.stated)


def canonical_link(spec: FieldSpec, canonical_url: str | None, ctx: Context, href: str | None = None) -> Derived | None:
    """RFC 6596: the canonical link relation names the page's preferred URL. `canonical_url` is the `<link
    rel=canonical>` href resolved against the document base URL; `href` is that href as the page writes it, the
    form that publishes (`Ok.stated`)."""
    derived = _page_url(spec, canonical_url, ctx, "canonical_link")
    if derived is None or href is None:
        return derived
    written = normalize(href, spec, ctx)
    return replace(derived, stated=written.published) if isinstance(written, Ok) else derived


def final_url(spec: FieldSpec, ctx: Context) -> Derived | None:
    """RFC 9110 §15.4: the URL the request ended at after redirects, which is the page's own: the context's page URL
    (an `api` capture's is the page its answer was requested for, not the endpoint's)."""
    return _page_url(spec, ctx.page_url, ctx, "final_url")


def application_form(spec: FieldSpec, url: str, ctx: Context) -> Derived | None:
    """The final URL `url` (RFC 9110 §15.4) of a page that holds its posting's application form: the document a
    candidate applies on. It names that document as fetched, with any query that selects
    the posting (an embedded board's), never its canonical link."""
    return _page_url(spec, url, ctx, "application_form")


def _page_url(spec: FieldSpec, url: str | None, ctx: Context, derived_by: Derivation) -> Derived | None:
    if url is None:
        return None
    result = normalize(url, spec, ctx)
    return Derived(result.value, derived_by, None) if isinstance(result, Ok) else None
