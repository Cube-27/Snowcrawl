"""Vocabularies for the normalizers: the shipped data, its tokenizer and
its matcher.

Two kinds, each a directory with a `JUSTIFICATION.md` that names its source:
- `schema/<Enumeration>.json`: a schema.org enumeration's member names, as a JSON array.
- `type/<name>/<language>.json`: a language-tagged type vocabulary, a JSON object from code to its phrases. Each
  phrase is stored in token form (`tokens`), so the file reads as what is matched.

A type vocabulary may instead be pinned data (`backend/data/manifest.json` names its source):
`data/vocab/<name>/<language>.json`, a JSON object from code to its terms as a page writes them. A term matches in its
token form, and, when it holds an apostrophe, also with the apostrophe dropped ("men's" as "mens"): case and the
possessive's apostrophe do not tell two writings of one term apart.

Everything is read once, at import, so a normalizer that uses it stays a pure function.
"""

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable

from snowcrawl.core.engine_version import engine_data_dir
from snowcrawl.normalize.strings import fold_words

Tokens = tuple[str, ...]
_JSON = ".json"


def tokens(text: str) -> Tokens:
    """`text` as a sequence of words: `strings.fold_words` without digits, so a word is a run of
    Unicode letters and marks after NFKC and case folding."""
    return tuple(fold_words(text, digits=False))


@dataclass(frozen=True, slots=True)
class Entries:
    """One language's phrases of one type vocabulary, each with its code."""

    phrases: Mapping[str, str]

    @classmethod
    def of(cls, codes: Mapping[str, Iterable[str]]) -> "Entries":
        """Entries from code → phrases, the file form. A phrase must be in token form, so it matches exactly as
        written, and belongs to one code: a phrase under two codes would make every input that holds it ambiguous."""
        phrases: dict[str, str] = {}
        for code, listed in codes.items():
            for phrase in listed:
                if not phrase or " ".join(tokens(phrase)) != phrase:
                    raise ValueError(f"vocabulary phrase {phrase!r} is not in token form")
                if phrases.setdefault(phrase, code) != code:
                    raise ValueError(f"vocabulary phrase {phrase!r} is listed under two codes")
        return cls(phrases)

    def find(self, words: Tokens) -> frozenset[str]:
        """The codes whose phrase is a contiguous run of `words` (token sequences, never
        substrings).

        Where one match lies inside a longer one, the longer wins ("longest match"); matches at separate places all
        count, so two members in one input both show.
        """
        matches: list[tuple[int, int, str]] = []
        for phrase, code in self.phrases.items():
            needle = tuple(phrase.split(" "))
            n = len(needle)
            matches.extend((i, i + n, code) for i in range(len(words) - n + 1) if words[i : i + n] == needle)
        return frozenset(
            code
            for start, end, code in matches
            if not any(s <= start and end <= e and e - s > end - start for s, e, _ in matches)
        )


def schema_members(enumeration: str) -> frozenset[str] | None:
    """The member names of a schema.org enumeration, or `None` when none is shipped."""
    return _SCHEMA.get(enumeration)


def type_vocabulary(name: str) -> Mapping[str, Entries] | None:
    """A type vocabulary's entries by language (BCP 47 primary subtag), or `None` when none is shipped."""
    return _TYPE.get(name)


@cache
def type_vocabularies_within(members: frozenset[str]) -> tuple[Mapping[str, Entries], ...]:
    """The type vocabularies with phrases whose every code, in every language, is one of `members`."""
    return tuple(
        by_language
        for by_language in _TYPE.values()
        if by_language and all(set(entries.phrases.values()) <= members for entries in by_language.values())
    )


def stated_codes(text: str, entries: Iterable[Entries]) -> frozenset[str]:
    """The codes `text` states: the code whose phrase is the text's whole first clause, with every
    code a phrase names anywhere else in the text (`Entries.find`), so a different code later makes it ambiguous.

    A phrase with other words in its clause ("not in stock", "in stock soon") states nothing: a word before or after
    it may negate or qualify it, and no standard lists which words do (C4, C7: decline rather than guess).
    """
    entries = list(entries)
    clause = " ".join(tokens(first_clause(text)))
    opening = {e.phrases[clause] for e in entries if clause in e.phrases}
    if not opening:
        return frozenset()
    words = tokens(text)
    return frozenset(opening).union(*(e.find(words) for e in entries))


def clause_codes(text: str, entries: Iterable[Entries]) -> frozenset[str]:
    """The codes whose phrase is the whole of one clause of `text` (clauses as `first_clause` ends them), for a text
    that lists several statements, such as the places of a location. A phrase with other words in its clause ("not
    remote") states nothing, as in `stated_codes`; unlike it, a clause after the first counts."""
    entries = list(entries)
    clauses = {" ".join(tokens(clause)) for clause in _clauses(text)}
    return frozenset(e.phrases[c] for e in entries for c in clauses if c in e.phrases)


def _clauses(text: str) -> list[str]:
    """The runs of letters, marks, spaces, dashes, connectors and word-internal apostrophes (`_continues`) between the
    other characters."""
    clauses: list[str] = []
    current: list[str] = []
    for i, char in enumerate(text):
        if _continues(text, i):
            current.append(char)
        elif current:
            clauses.append("".join(current))
            current = []
    return [*clauses, "".join(current)] if current else clauses


def first_clause(text: str) -> str:
    """The text's first clause: from its first letter, through letters, marks, spaces, dashes and connectors (Unicode
    general categories L, M, Zs, Pd, Pc, UAX #44) and apostrophes between two letters, up to the first other character:
    a punctuation mark, symbol, digit or line break ends it. Leading spaces, punctuation and symbols ("✓ In stock") are
    skipped; a leading digit is not ("0 in stock"), so the clause is then empty."""
    start = next((i for i, char in enumerate(text) if not _leads(char)), len(text))
    end = next((i for i in range(start, len(text)) if not _continues(text, i)), len(text))
    return text[start:end]


# Unicode general categories (UAX #44) that join the words of one clause besides letters and marks: space separators,
# dash punctuation ("pre-order") and connector punctuation ("in_stock").
_CLAUSE_JOINERS = frozenset({"Zs", "Pd", "Pc"})


def _leads(char: str) -> bool:
    return char.isspace() or unicodedata.category(char)[0] in "PS"


# Apostrophes UAX #29 does not break a word at when a letter is on each side (WB6/WB7: Single_Quote U+0027 and the
# apostrophes of MidNumLet, U+2019 and U+FF07): "Remote's" is one word, so it does not end its clause.
_WORD_APOSTROPHES = frozenset({"\u0027", "\u2019", "\uff07"})


def _continues(text: str, i: int) -> bool:
    category = unicodedata.category(text[i])
    if category[0] in "LM" or category in _CLAUSE_JOINERS:
        return True
    between_letters = 0 < i < len(text) - 1 and _is_letter(text[i - 1]) and _is_letter(text[i + 1])
    return text[i] in _WORD_APOSTROPHES and between_letters


def _is_letter(char: str) -> bool:
    return unicodedata.category(char)[0] == "L"


@cache
def name_entries(members: frozenset[str]) -> Entries:
    """Each schema.org member name as a phrase of its own, in token form (`InStock` is "instock", as Open Graph
    `product:availability` writes it): a member name in any case states the member."""
    return Entries.of({member: [" ".join(tokens(member))] for member in members})


def code_entries(by_language: Mapping[str, Entries]) -> Entries:
    """Each code of a type vocabulary as a phrase of its own, in token form, whatever the page's language: a type
    vocabulary's codes are its standard's own values (a schema.org member name, a product data attribute value), which
    a page writes the same in any language, as `name_entries` reads a schema.org member name."""
    return name_entries(frozenset(code for entries in by_language.values() for code in entries.phrases.values()))


def entries_for(by_language: Mapping[str, Entries], languages: frozenset[str]) -> list[Entries]:
    """A type vocabulary's entries for the page's `languages` (`page_languages` in `normalize/locales.py`), or for
    every language when the page states none; languages without a file have none."""
    return [by_language[language] for language in sorted(languages or by_language.keys()) if language in by_language]


def _load_schema(directory: Traversable) -> dict[str, frozenset[str]]:
    return {
        e.name.removesuffix(_JSON): frozenset(json.loads(e.read_text(encoding="utf-8"))) for e in _json_files(directory)
    }


def _load_type(directory: Traversable) -> dict[str, dict[str, Entries]]:
    return {
        vocabulary.name: {
            e.name.removesuffix(_JSON): Entries.of(json.loads(e.read_text(encoding="utf-8")))
            for e in _json_files(vocabulary)
        }
        for vocabulary in directory.iterdir()
        if vocabulary.is_dir()
    }


def _load_data_type() -> dict[str, dict[str, Entries]]:
    """The type vocabularies pinned under the engine data's `vocab/` (module docstring); none without engine data."""
    data = engine_data_dir()
    root = None if data is None else data / "vocab"
    if root is None or not root.is_dir():
        return {}
    return {
        vocabulary.name: {
            e.stem: Entries.of(_token_forms(json.loads(e.read_text(encoding="utf-8"))))
            for e in sorted(vocabulary.glob("*" + _JSON))
        }
        for vocabulary in sorted(root.iterdir())
        if vocabulary.is_dir()
    }


# The apostrophes a possessive is written with, those UAX #29 keeps within a word (`_WORD_APOSTROPHES`).
_APOSTROPHES = str.maketrans("", "", "".join(_WORD_APOSTROPHES))


def _token_forms(terms: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Each code's terms in token form, with the form of each term without its apostrophes."""
    return {
        code: sorted({" ".join(tokens(form)) for term in listed for form in (term, term.translate(_APOSTROPHES))})
        for code, listed in terms.items()
    }


def _json_files(directory: Traversable) -> Iterable[Traversable]:
    return sorted((e for e in directory.iterdir() if e.name.endswith(_JSON)), key=lambda e: e.name)


_ROOT = files(__name__)
_SCHEMA = _load_schema(_ROOT / "schema")
_TYPE = _load_type(_ROOT / "type") | _load_data_type()
