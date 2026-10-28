"""Location normalizer: `{text, locality, region, country, remote_flag}`.

A location publishes what the page states and nothing it does not: no place is looked up, completed or inferred.
- `text` is the place as the page writes it: a string, or a `Place`'s `name` followed by its `PostalAddress`'s stated
  parts in the order the HTML Standard lists the address autofill fields (§4.10.18.7.1: street address, locality,
  region, country name, postal code). A part the text already writes is not repeated: `streetAddress` may hold the
  whole address on its lines (schema.org: "the street address", which pages often write in full), and a `Place`'s
  name may be its first line. A country stated as an ISO 3166-1 alpha-2 code is a code, not how the page writes the
  place: it is `country`, never text. Nothing in the text is parsed: several places, regions or alternatives ("A or
  B") stay one text as written (owner, 2026-10-16).
- `locality`, `region` are a `PostalAddress`'s `addressLocality` and `addressRegion` as written.
- `country` is an ISO 3166-1 alpha-2 code, and only when `addressCountry` states one (schema.org recommends the
  alpha-2 code). A country stated by name stays in `text`; no code is derived from it.
- `remote_flag` is `true` only from `jobLocationType` `TELECOMMUTE` or a `type:remote` phrase, never from a missing
  location. No standard states "not remote" as a flag, so it is `None` when the input does not say.

Each is `None` when the input does not state it. Accepted input: a string, or a schema.org
`PostalAddress`, or a `Place` holding one under `address` or naming itself in `name`, with `jobLocationType` beside
them. Declines: input stating nothing is `Invalid("no_match")`; an `addressCountry` written as two letters that are
no ISO 3166-1 code is `Invalid("unknown_country")`.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import TypeGuard

from pydantic import JsonValue

from snowcrawl.normalize import locales, vocab
from snowcrawl.normalize.base import Context, Invalid, NormalizeResult, Ok, Raw, has_surrogate
from snowcrawl.normalize.strings import collapse, compose, fold_words
from snowcrawl.schemas.types import FieldSpec, FieldType

# schema.org `jobLocationType`: "TELECOMMUTE for telecommute jobs"; JSON-LD may write it as a schema.org IRI or a
# compact IRI (JSON-LD 1.1 §4.1.5).
_TELECOMMUTE = frozenset(
    {"TELECOMMUTE", "https://schema.org/TELECOMMUTE", "http://schema.org/TELECOMMUTE", "schema:TELECOMMUTE"}
)

# The keys of a normalized location (the module docstring).
_VALUE_KEYS = frozenset({"text", "locality", "region", "country", "remote_flag"})
_PARTS = ("locality", "region", "country")
# A word of a part: a run of letters and digits (Unicode categories L and N, as `str.isalnum` reads them).


def normalize(raw: Raw, spec: FieldSpec, ctx: Context) -> NormalizeResult:
    """Normalize a location."""
    if spec.type is not FieldType.LOCATION:
        raise TypeError(f"the location normalizer does not handle type {spec.type}")
    if isinstance(raw, str):
        return _text(raw, ctx)
    if isinstance(raw, dict):
        return _structured(raw, ctx)
    return Invalid("not_a_location")


def equal(a: JsonValue, b: JsonValue, _spec: FieldSpec) -> bool:
    """`remote_flag` and `country` equal, `locality` equal after Unicode case folding; `region` and
    `text` (by `vocab.tokens`, so case, accents of form and punctuation don't count) count only when both values have
    one. Not transitive: a value without a region equals two values in different regions."""
    if a == b:
        return True
    if not (isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys() == _VALUE_KEYS):
        return False
    return (
        a["remote_flag"] == b["remote_flag"]
        and a["country"] == b["country"]
        and _casefolded(a["locality"]) == _casefolded(b["locality"])
        and _either_missing_or(a["region"], b["region"], _casefolded)
        and _either_missing_or(a["text"], b["text"], _tokens)
    )


def names(text: JsonValue, place: JsonValue) -> bool:
    """Whether `text`, a location the page writes as text alone (no part stated), names `place`'s locality, or its
    region when it states no locality: their words (`vocab.tokens`) run in its text (a locality is the place itself; a
    region holds other places, as "New York" does "New York City"). Then both state one place of the item, the text
    as the page shows it ("Austin", "Raleigh; Austin; Denver") and `place` as its structured data does: one member of a
    list of places, not two. Nothing in the text is parsed."""
    if not (_location(text) and _location(place)) or any(text[part] is not None for part in _PARTS):
        return False
    said = text["text"]
    if not isinstance(said, str):
        return False
    part = place["locality"] if place["locality"] is not None else place["region"]
    named = vocab.tokens(part) if isinstance(part, str) else ()
    return bool(named) and _runs_in(named, vocab.tokens(said))


def _location(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict) and value.keys() == _VALUE_KEYS


def _runs_in(part: Sequence[str], words: Sequence[str]) -> bool:
    """Whether `part`'s words occur together, in order, among `words`."""
    return any(tuple(words[i : i + len(part)]) == tuple(part) for i in range(len(words) - len(part) + 1))


def _either_missing_or(a: JsonValue, b: JsonValue, key: Callable[[JsonValue], JsonValue]) -> bool:
    return a is None or b is None or key(a) == key(b)


def _casefolded(value: JsonValue) -> JsonValue:
    return value.casefold() if isinstance(value, str) else value


def _tokens(value: JsonValue) -> JsonValue:
    return list(vocab.tokens(value)) if isinstance(value, str) else value


def _value(text: str | None, parts: Mapping[str, str | None], remote: bool) -> dict[str, JsonValue]:
    # Tri-state: `true` when stated, else `None`: a page that does not say is not a page that says "not remote".
    return {"text": text, **{name: parts.get(name) for name in _PARTS}, "remote_flag": True if remote else None}


def _text(given: str, ctx: Context, *, remote: bool = False) -> NormalizeResult:
    if has_surrogate(given):
        return Invalid("surrogate")
    text = _part(given)
    words = vocab.tokens(text or "")
    if text is None or not words:
        return Invalid("empty")
    return Ok(_value(text, {}, remote or _says_remote(text, ctx)))


def _telecommute(value: JsonValue) -> bool:
    """Whether a `jobLocationType` value is TELECOMMUTE. JSON-LD 1.1 lets a property hold an array of values
    or a node object naming its IRI in `@id`; any one TELECOMMUTE value makes the job remote."""
    if isinstance(value, list):
        return any(_telecommute(item) for item in value)
    if isinstance(value, dict):
        value = value.get("@id")
    return isinstance(value, str) and value in _TELECOMMUTE


def _says_remote(text: str, ctx: Context) -> bool:
    # A phrase is stated only as a whole clause ("Lakeside, Remote"): "not remote" has other words in its clause, and
    # no standard lists the words that negate (C4, C7), so it states nothing.
    return bool(vocab.clause_codes(text, _remote_entries(ctx)))


def mentions_remote(text: str, ctx: Context) -> bool:
    """Whether a `type:remote` phrase is anywhere in `text`, as a run of its words, whatever else its clause says: the
    text speaks of remote work, which is not to say it states the job is remote (`_says_remote`)."""
    words = vocab.tokens(text)
    return any(entries.matches(words) for entries in _remote_entries(ctx))


def _remote_entries(ctx: Context) -> list[vocab.Entries]:
    return vocab.entries_for(vocab.type_vocabulary("remote") or {}, locales.page_languages(ctx, request=True))


def _structured(raw: Mapping[str, JsonValue], ctx: Context) -> NormalizeResult:
    # schema.org: `Place.address` is a PostalAddress (or text); `JobPosting.jobLocationType` is beside the location.
    remote = _telecommute(raw.get("jobLocationType"))
    address = raw.get("address", raw)
    if isinstance(address, str):
        address = {"name": address}
    if not isinstance(address, dict):
        return Invalid("not_a_location")
    country_written = _country_written(address.get("addressCountry"))
    parts = {
        "locality": _part(address.get("addressLocality")),
        "region": _part(address.get("addressRegion")),
        "country": None,
    }
    country_name = country_written
    if country_written is not None and _looks_like_a_code(country_written):
        if country_written.upper() not in locales.iso_3166_alpha2():
            return Invalid("unknown_country")
        parts["country"], country_name = country_written.upper(), None
    name = _part(raw.get("name")) or _part(address.get("name"))
    lines = [name, _part(address.get("streetAddress"))]
    stated = [parts["locality"], parts["region"], country_name, _part(address.get("postalCode"))]
    if text := _written(lines, stated):
        # The written parts are the location's text, so a `type:remote` clause in it reads as it does in a string.
        return Ok(_value(text, parts, remote or _says_remote(text, ctx)))
    return Ok(_value(None, parts, remote)) if remote or parts.get("country") else Invalid("no_match")


def _written(lines: Sequence[str | None], parts: Sequence[str | None]) -> str | None:
    """The place's text: its lines (a name, a street address), each comma-separated segment once, then each stated
    part that no segment before it already writes, joined by commas; None when nothing is stated."""
    segments: list[str] = []
    written: list[tuple[str, ...]] = []
    for segment in (s for line in filter(None, lines) for s in line.split(",")):
        _add(segment, segments, written, lambda words: words in written)
    for part in filter(None, parts):
        _add(part, segments, written, lambda words: any(_holds(line, words) for line in written))
    return ", ".join(segments) or None


def _add(
    given: str, segments: list[str], written: list[tuple[str, ...]], known: Callable[[tuple[str, ...]], bool]
) -> None:
    if (segment := _part(given)) and (words := _words(segment)) and not known(words):
        segments.append(segment)
        written.append(words)


def _words(text: str) -> tuple[str, ...]:
    """A part's words for telling whether the text already writes it (`strings.fold_words`), so case, punctuation and
    compatibility forms do not make a part new."""
    return tuple(fold_words(text))


def _holds(words: tuple[str, ...], part: tuple[str, ...]) -> bool:
    """Whether `part`'s words are a contiguous run of `words`."""
    return any(words[i : i + len(part)] == part for i in range(len(words) - len(part) + 1))


def _country_written(given: JsonValue) -> str | None:
    """`addressCountry` as the page writes it: schema.org allows text or a `Country`, which states itself by its
    `identifier` or `name`."""
    if isinstance(given, dict):
        given = given.get("identifier") or given.get("name")
    return _part(given)


def _looks_like_a_code(written: str) -> bool:
    # ISO 3166-1 alpha-2: two letters of the basic Latin alphabet.
    return len(written) == 2 and written.isascii() and written.isalpha()


def _part(given: JsonValue) -> str | None:
    """A stated part as written, in the text form (`strings.compose`, `strings.collapse`): NFC, the non-whitespace
    control characters removed, each run of white space one space."""
    if not isinstance(given, str):
        return None
    return collapse(compose(given)) or None
