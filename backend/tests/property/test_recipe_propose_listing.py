"""A listing draft (`services.recipe_propose.drafted`) over generated samples whose one call answers rows: the draft
has no pagination (`none`) exactly when every sample's call returns as many rows as the sample published; otherwise
nothing shows how the next page is asked."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from snowcrawl.config_ir.api_recipe.model import OnePagePagination
from snowcrawl.config_ir.custom_recipe import InvalidRecipe
from snowcrawl.config_ir.validation import Facts, HeldOutPage
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import CaptureId, SchemaId
from snowcrawl.normalize.base import Context
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef
from snowcrawl.services.recipe_calls import Samples
from snowcrawl.services.recipe_propose import EndpointRef, drafted
from tests.support.captures import call, capture

SCHEMA = SchemaDef(
    id=SchemaId(uuid7()),
    origin="user",
    status="active",
    name="synthetic",
    page_kind="listing",
    fields={"title": FieldSpec(type=FieldType.TEXT, description="d", required=True)},
)
_MAIN = EndpointRef(method="GET", origin="https://api.example.test", has_path=True, segments=1, suffix="", query=["c"])


def _page(number: int, rows: int, published: int) -> tuple[HeldOutPage, str]:
    """Sample `number`: its call answers `rows` items, the page shows them and the engine published `published`."""
    titles = [f"Item {number} title {i}" for i in range(rows)]
    body = json.dumps({"items": [{"title": title} for title in titles]}).encode()
    url = f"https://shop.example.test/c/{number}"
    network = (call(0, f"https://api.example.test/items?c={number}", body, method="GET"),)
    shown = "".join(f"<li>{title}</li>" for title in titles)
    bundle = capture(f"<html><body><ul>{shown}</ul></body></html>".encode(), url=url, method="browser", network=network)
    ctx = Context(captured_at=datetime(2026, 10, 26, tzinfo=UTC), base_url=url, page_url=url)
    facts = tuple(Facts(published={"title": title}) for title in titles[:published])
    return HeldOutPage(CaptureId(UUID(int=number + 1)), ParsedPage.of(bundle), ctx, Facts(), facts), url


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.integers(3, 8), st.booleans()), min_size=2, max_size=4))
def test_a_listing_whose_one_call_returns_every_row_has_no_pagination(plan: list[tuple[int, bool]]) -> None:
    pages = [_page(number, rows, rows if whole else rows - 1) for number, (rows, whole) in enumerate(plan)]
    samples = Samples(SCHEMA, [page for page, _ in pages], [url for _, url in pages])

    if all(whole for _, whole in plan):
        recipe = drafted(samples, _MAIN).recipe
        assert recipe.main.pagination == OnePagePagination(kind="none", rows="$['items'][*]")
        assert recipe.main.map == {"title": "$['title']"}
    else:
        with pytest.raises(InvalidRecipe):
            drafted(samples, _MAIN)
