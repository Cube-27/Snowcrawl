"""Enum normalizer: a member code of the field's vocabulary.

- `schema:<Enumeration>`: the input is a string or a JSON-LD node reference (`{"@id": …}`) naming a member by IRI
  (`https://schema.org/InStock`, `http://schema.org/InStock`), compact IRI (`schema:InStock`) or name (`InStock`).
  The value is the member name. An IRI on the schema.org host also names its member written as a network-path
  reference (`//schema.org/InStock`), with its scheme or host in any case, or the member name in another case
  (`http://schema.org/instock`) when exactly one member has that name. Other text, not an IRI, is read as the
  `type:` path reads it, against each type vocabulary whose codes are all members of the enumeration
  (`type:availability` for `ItemAvailability`, `type:condition` for `OfferItemCondition`), and a member name in any
  case also counts (`instock`), so a page that states "In stock (5 available)" gives `InStock`.
- `type:<name>`: the input is a string, a schema.org IRI (read as its term's name) or a node reference to one; it
  states a code when the tokens of its first clause are exactly one of the code's phrases in the page's languages
  (`snowcrawl.normalize.vocab`) or the code itself in any case and any page language, and no other code's phrase
  appears later in it. "Not in stock" states nothing.

A string or node reference that names no member or code is the value as stated, its white space collapsed and its case
kept, and not checked (`Ok.checked`): the vocabularies may lack a page's word, and the page states it (owner decision
2026-10-28). Two members in one input are `Ambiguous`; anything else (empty, not a string) is `Invalid`.
"""

import re
from collections.abc import Hashable, Mapping
from urllib.parse import urlsplit

from pydantic import JsonValue

from snowcrawl.normalize import locales, vocab
from snowcrawl.normalize.base import Ambiguous, Context, Invalid, NormalizeResult, Ok, Raw, has_surrogate, raw_key
from snowcrawl.schemas.types import FieldSpec, FieldType

# schema.org names its terms under https://schema.org/ and, earlier, http://schema.org/ (schema.org, "Data model");
# `schema:` is the prefix its JSON-LD context declares for compact IRIs (JSON-LD 1.1 §4.1.5).
_SCHEMA_HOST = "schema.org"
_SCHEMA_SCHEMES = frozenset({"https", "http", ""})  # "": a network-path reference, `//schema.org/…` (RFC 3986 §4.2)
_COMPACT = "schema:"
# An absolute IRI starts with its scheme and a colon (RFC 3987 §2.2, RFC 3986 §3.1) and holds no white space; it names
# a resource, so it is never read as text.
_IRI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S*")


def normalize(raw: Raw, spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """Normalize an enum field against its vocabulary. An unknown vocabulary declines, as an
    unknown type does, so a schema that names one cannot crash a job."""
    if spec.type is not FieldType.ENUM or spec.vocab is None:
        raise TypeError(f"the enum normalizer does not handle type {spec.type} without a vocab")
    kind, name = spec.vocab.split(":", 1)
    result = _schema_member(raw, name, ctx) if kind == "schema" else _type_code(raw, name, ctx)
    if isinstance(result, Invalid) and result.reason in _UNMAPPED:
        return _as_stated(raw)
    return result


# The reasons a string names no member or code (rather than being no value): such a string publishes as stated.
_UNMAPPED = frozenset({"not_a_member", "no_match"})


def _as_stated(raw: Raw) -> NormalizeResult:
    """The string `raw` states (a node reference's `@id`), white space collapsed, unchecked."""
    text = raw["@id"] if isinstance(raw, dict) else raw
    if not isinstance(text, str) or has_surrogate(text):
        return Invalid("surrogate")
    return Ok(" ".join(text.split()), checked=False)


def _schema_member(raw: Raw, enumeration: str, ctx: Context) -> NormalizeResult:
    members = vocab.schema_members(enumeration)
    if members is None:
        return Invalid("unknown_vocab")
    # JSON-LD 1.1 §9.5: a node reference is a node object holding only `@id`.
    if isinstance(raw, dict) and raw.keys() == {"@id"}:
        raw = raw["@id"]
    if not isinstance(raw, str):
        return Invalid("not_a_string")
    # Surrounding white space is not part of an IRI (RFC 3987 §3.1 excludes it from the IRI syntax).
    name = raw.strip()
    if not name:
        return Invalid("empty")
    term = _schema_term(name)
    if term is not None:
        return _named_member(term, members)
    if name in members:
        return Ok(name)
    if _IRI.fullmatch(name):
        return Invalid("not_a_member")
    return _stated_member(name, members, ctx)


def _schema_term(name: str) -> str | None:
    """The term a schema.org IRI or compact IRI names; None for other text. The scheme and host of a URI compare in
    any case (RFC 3986 §3.1), and a network-path reference resolves on the page's scheme, http or https
    (RFC 3986 §5.2), so `//schema.org/InStock` names the term as `https://schema.org/InStock` does."""
    if name.startswith(_COMPACT):
        return name.removeprefix(_COMPACT)
    if any(char.isspace() for char in name):  # an IRI holds no white space (RFC 3987 §2.2); urlsplit drops tabs
        return None
    try:
        parts = urlsplit(name)
    except ValueError:  # an invalid IPv6 host (RFC 3986 §3.2.2): no schema.org IRI
        return None
    on_host = parts.scheme.lower() in _SCHEMA_SCHEMES and parts.netloc.lower() == _SCHEMA_HOST
    return parts.path.removeprefix("/") if on_host and not (parts.query or parts.fragment) else None


def _named_member(term: str, members: frozenset[str]) -> NormalizeResult:
    """The member a schema.org term names: itself, else the one member whose name it is in another case (schema.org
    names no two members of an enumeration alike but for case, so the name stays unambiguous)."""
    if term in members:
        return Ok(term)
    folded = [m for m in members if m.casefold() == term.casefold()]
    return Ok(folded[0]) if len(folded) == 1 else Invalid("not_a_member")


def _stated_member(text: str, members: frozenset[str], ctx: Context) -> NormalizeResult:
    """The member that `text` states through the type vocabularies whose codes are all `members` (their phrases name
    members of this enumeration and nothing else, the structural property the match rests on), or by a member name
    in any case: `instock`, as Open Graph `product:availability` writes it. A member the owner's terms state ("Sold")
    is as checked as one its name states: the text maps to a code, and whether it is the field's value is told by
    where the page states it (`collect.admission`, `resolve.ownership`), never by which table maps it."""
    entries = [
        *(e for v in vocab.type_vocabularies_within(members) for e in _page_entries(v, ctx)),
        vocab.name_entries(members),
    ]
    return _one_code(vocab.stated_codes(text, entries), "not_a_member")


def _type_code(raw: Raw, name: str, ctx: Context) -> NormalizeResult:
    by_language = vocab.type_vocabulary(name)
    if by_language is None:
        return Invalid("unknown_vocab")
    # JSON-LD 1.1 §9.5: a node reference is a node object holding only `@id`.
    if isinstance(raw, dict) and raw.keys() == {"@id"}:
        raw = raw["@id"]
    if not isinstance(raw, str):
        return Invalid("not_a_string")
    if has_surrogate(raw):
        return Invalid("surrogate")
    # A schema.org IRI names its term (schema.org, "Data model"): `https://schema.org/Male` states the term `Male`. A
    # term is one path segment, so `https://schema.org/Male/Other` names none.
    term = _schema_term(raw.strip())
    if term is not None and "/" in term:
        return Invalid("no_match")
    text = raw if term is None else term
    if not vocab.tokens(text):
        return Invalid("empty")
    entries = [*_page_entries(by_language, ctx), vocab.code_entries(by_language)]
    return _one_code(vocab.stated_codes(text, entries), "no_match")


def _page_entries(by_language: Mapping[str, vocab.Entries], ctx: Context) -> list[vocab.Entries]:
    return vocab.entries_for(by_language, locales.page_languages(ctx, request=True))


def _one_code(codes: frozenset[str], none: str) -> NormalizeResult:
    """The one code found; two or more are `Ambiguous`, none is `Invalid(none)`."""
    if not codes:
        return Invalid(none)
    if len(codes) > 1:
        return Ambiguous("several_members", tuple(sorted(codes)))
    return Ok(next(iter(codes)))


def key(value: JsonValue, _spec: FieldSpec) -> Hashable:
    """Equality as a key: exact. A normalized value has one form (one code per member)."""
    return raw_key(value)
