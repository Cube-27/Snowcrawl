"""A call's rows (`data_calls`): rows the engine published choose the array whose items hold them over another array
the page shows. Over generated white space: an item's strings are compared in the text form the page text is indexed
in, so white space around or inside a value changes neither which items show a string of their own nor how many do."""

import json

from hypothesis import given
from hypothesis import strategies as st

from snowcrawl.acquire.items import MIN_ITEMS
from snowcrawl.collect.network_match import data_calls
from snowcrawl.content.page import ParsedPage
from tests.support.captures import call

_NAMES = ("Alpha widget", "Beta gadget", "Gamma tool", "Delta part", "Epsilon kit", "Zeta set")
# HTML Standard §2.3 ASCII whitespace, and U+00A0, which `str.split` also splits on.
_Space = st.text(alphabet=" \t\n\r\f\u00a0", max_size=3)


def _padded(value: str, before: str, after: str) -> str:
    return before + value.replace(" ", " " + before) + after


def _rows(page_html: str, items: list[dict[str, str]]) -> list[tuple[str | None, int]]:
    entry, body = call(0, "https://api.example.test/items", json.dumps({"items": items}).encode(), method="GET")
    return [(data.rows, data.row_hits) for data in data_calls(ParsedPage(page_html, (body,), (entry,)))]


@given(st.lists(st.tuples(_Space, _Space), min_size=len(_NAMES), max_size=len(_NAMES)))
def test_white_space_in_an_item_s_strings_changes_no_row_hits(spaces: list[tuple[str, str]]) -> None:
    shown = "<ul>" + "".join(f"<li>{name}</li>" for name in _NAMES) + "</ul>"
    items = [{"name": _padded(name, *space)} for name, space in zip(_NAMES, spaces, strict=True)]
    assert _rows(f"<html><body>{shown}</body></html>", items) == [("$['items'][*]", len(_NAMES))]


@given(st.lists(_Space, min_size=MIN_ITEMS, max_size=len(_NAMES), unique=True))
def test_one_shown_value_every_item_repeats_is_no_item_s_own(spaces: list[str]) -> None:
    items = [{"id": f"unshown-{i}", "category": f"Footwear{space}"} for i, space in enumerate(spaces)]
    assert _rows("<html><body><h1>Footwear</h1></body></html>", items) == [(None, 0)]


@given(st.integers(min_value=1, max_value=4), st.permutations(("facets", "items")))
def test_published_rows_choose_the_array_whose_items_hold_them(extra: int, order: tuple[str, ...]) -> None:
    # A filter list the page also shows, with more items than the rows: shown strings alone choose it.
    labels = [f"Filter label {i}" for i in range(len(_NAMES) + extra)]
    arrays = {"facets": [{"label": label} for label in labels], "items": [{"name": name} for name in _NAMES]}
    shown = "".join(f"<li>{text}</li>" for text in [*labels, *_NAMES])
    entry, body = call(0, "https://api.example.test/search", json.dumps({key: arrays[key] for key in order}).encode())
    page = ParsedPage(f"<html><body><ul>{shown}</ul></body></html>", (body,), (entry,))

    assert [data.rows for data in data_calls(page)] == ["$['facets'][*]"]
    assert [data.rows for data in data_calls(page, [{"title": name} for name in _NAMES])] == ["$['items'][*]"]
