"""Check-digit passes by chance among a list's rows.

A GS1 check digit (GS1 General Specifications §7.9.1) is one digit of ten, so one in ten arbitrary digit strings of a
GTIN length passes it. A source that states a value at one path for each of a list's rows (one property of each
variant) states values of one kind there. When more of its values of one length fail the check than pass it, the path
does not hold check-digit identifiers of that length (identifiers pass, arbitrary numbers pass one time in ten), and its
passing values of that length are numbers that pass by chance, not identifiers: they are rejected
(`CHECK_DIGIT_CHANCE`). The same holds for an ISBN's check (ISO 2108).
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Final

from snowcrawl.collect.base import Candidate
from snowcrawl.normalize.base import Invalid, Ok
from snowcrawl.normalize.identifier import has_check_digit
from snowcrawl.schemas.types import FieldSpec

CHECK_DIGIT_CHANCE: Final = "check_digit_chance"
# A list index in a locator's path (RFC 9535 index selector, `[3]`): the rows' values at one path differ only there.
_INDEX = re.compile(r"\[\d+\]")
_CHECK_FAILED = Invalid("check_digit")
_ISBN_PREFIX = re.compile(r"\AISBN\s+")
_ISBN_SEPARATORS = re.compile(r"[- ]")
_DIGITS = re.compile(r"\d+[Xx]?", re.ASCII)


def chance_passes(name: str, sub_fields: Mapping[str, FieldSpec], candidates: Sequence[Candidate]) -> list[Candidate]:
    """The candidates of the row field `name`'s check-digit sub-fields that pass their check by chance (module
    docstring), in candidate order."""
    chance: set[int] = set()
    for members in _same_path_and_length(name, sub_fields, candidates).values():
        passed = [c for c in members if isinstance(c.normalized, Ok)]
        if sum(c.normalized == _CHECK_FAILED for c in members) > len(passed):
            chance.update(id(c) for c in passed)
    return [c for c in candidates if id(c) in chance]


def _same_path_and_length(
    name: str, sub_fields: Mapping[str, FieldSpec], candidates: Sequence[Candidate]
) -> dict[tuple[str, str, str, int], list[Candidate]]:
    """The digit-string candidates of the check-digit sub-fields, by sub-field, source kind, path with its list
    indices aside, and length."""
    groups: dict[tuple[str, str, str, int], list[Candidate]] = defaultdict(list)
    for c in candidates:
        spec = sub_fields.get(c.field.removeprefix(f"{name}."))
        digits = _digits(c)
        if spec is not None and has_check_digit(spec) and digits is not None:
            groups[(c.field, c.source_kind, _INDEX.sub("[]", c.locator), len(digits))].append(c)
    return groups


def _digits(c: Candidate) -> str | None:
    """The candidate's raw value as its ASCII digits (a JSON integer as its decimal digits; an ISBN as printed, without
    its prefix and separators, its `X` check character kept), else None."""
    raw = c.raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    # ISO 2108: a printed ISBN may carry the "ISBN" prefix, hyphens or spaces, and `X` as its check character.
    digits = _ISBN_SEPARATORS.sub("", _ISBN_PREFIX.sub("", raw.strip(), count=1))
    return digits if _DIGITS.fullmatch(digits) else None
