"""The call list (`services.recipe_calls.listed`) over generated samples: each endpoint group holds exactly the calls of
its endpoint and the samples it was called on, and a field's path counts the samples on which a call of the group read
the value that sample published; a field no sample published lists the paths of its type instead."""

import json
from datetime import UTC, datetime
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from snowcrawl.config_ir.validation import Facts, HeldOutPage
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import CaptureId, SchemaId
from snowcrawl.normalize.base import Context
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef
from snowcrawl.services.recipe_calls import Endpoint, listed
from snowcrawl.storage.capture_bundle import NetworkBody
from tests.support.captures import call, capture

SCHEMA = SchemaDef(
    id=SchemaId(uuid7()),
    origin="user",
    status="active",
    name="synthetic",
    page_kind="detail",
    fields={
        "title": FieldSpec(type=FieldType.TEXT, description="d", required=True),
        "subtitle": FieldSpec(type=FieldType.TEXT, description="d"),
    },
)
# Two endpoints of distinct URL shapes: one path segment, and two.
ENDPOINTS = ("https://api.example.test/item", "https://api.example.test/v1/item")

# Per sample, per endpoint: not called, or called with the title the sample published (True) or another (False).
_Call = st.none() | st.booleans()
_Samples = st.lists(st.tuples(_Call, _Call), min_size=1, max_size=5)


def _page(number: int, plan: tuple[bool | None, bool | None]) -> HeldOutPage:
    title = f"Title of item {number}"
    network: list[tuple[NetworkBody, bytes]] = []
    for endpoint, carries in zip(ENDPOINTS, plan, strict=True):
        if carries is not None:
            body = {"name": title if carries else "Some other words", "flag": True}
            network.append(call(len(network), f"{endpoint}?id={number}", json.dumps(body).encode(), method="GET"))
    url = f"https://shop.example.test/p/{number}"
    bundle = capture(b"<html><body></body></html>", url=url, method="browser", network=tuple(network))
    ctx = Context(captured_at=datetime(2026, 10, 14, tzinfo=UTC), base_url=url, page_url=url)
    published = Facts(published={"title": title})
    return HeldOutPage(CaptureId(UUID(int=number + 1)), ParsedPage.of(bundle), ctx, published)


@settings(max_examples=50, deadline=None)
@given(_Samples)
def test_groups_hold_their_endpoint_s_calls_and_count_the_samples_that_read_each_value(
    plans: list[tuple[bool | None, bool | None]],
) -> None:
    pages = [_page(number, plan) for number, plan in enumerate(plans)]

    found = listed(pages, SCHEMA)

    assert found.samples == [page.capture_id for page in pages]
    by_segments = {endpoint.segments: endpoint for endpoint in found.endpoints}
    assert len(by_segments) == len(found.endpoints)
    for column, segments in enumerate((1, 2)):
        called = [page.capture_id for page, plan in zip(pages, plans, strict=True) if plan[column] is not None]
        endpoint = by_segments.get(segments)
        if not called:
            assert endpoint is None
        else:
            assert endpoint is not None
            _check(endpoint, called, carried=sum(plan[column] is True for plan in plans))


def _check(endpoint: Endpoint, called: list[CaptureId], *, carried: int) -> None:
    """`endpoint` was called on the samples `called`, `carried` of which published the title its calls read."""
    assert endpoint.samples == called
    assert [c.capture_id for c in endpoint.calls] == called
    fields = {match.field: match for match in endpoint.fields}
    title = fields.get("title")
    if carried:
        assert title is not None
        assert title.published
        assert [(p.path, p.samples) for p in title.paths] == [("$['name']", carried)]
    else:
        # Every sample published a title, so none lists the paths of its type instead.
        assert title is None
    # No sample published a subtitle: its paths are those whose value is text, read on every sample called.
    subtitle = fields["subtitle"]
    assert not subtitle.published
    assert [(p.path, p.samples) for p in subtitle.paths] == [("$['name']", len(called))]


def test_a_slug_is_no_url_path_and_a_currency_stating_money_reading_drops_the_others() -> None:
    schema = SchemaDef(
        id=SchemaId(uuid7()),
        origin="user",
        status="active",
        name="synthetic",
        page_kind="detail",
        fields={
            "url": FieldSpec(type=FieldType.URL, description="d"),
            "price": FieldSpec(type=FieldType.MONEY, description="d"),
        },
    )
    body = {"handle": "desk-lamp", "path": "/p/desk-lamp", "price": "19.50", "currency": "EUR"}
    url = "https://shop.example.test/p/desk-lamp"
    published: dict[str, JsonValue] = {"url": url, "price": {"amount": "19.5", "currency": "EUR"}}
    network = (call(0, "https://api.example.test/p?id=1", json.dumps(body).encode(), method="GET"),)
    bundle = capture(b"<html><body></body></html>", url=url, method="browser", network=network)
    ctx = Context(captured_at=datetime(2026, 10, 14, tzinfo=UTC), base_url=url, page_url=url)
    page = HeldOutPage(CaptureId(UUID(int=1)), ParsedPage.of(bundle), ctx, Facts(published=published))

    (endpoint,) = listed([page], schema).endpoints

    paths = {match.field: [p.path for p in match.paths] for match in endpoint.fields}
    assert paths["url"] == ["$['path']"]
    assert paths["price"]
    assert all("+" in path for path in paths["price"])  # the pair, never the amount alone
