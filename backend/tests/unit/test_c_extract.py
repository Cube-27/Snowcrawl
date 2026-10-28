"""Role C on a detail page through the pipeline (role C input, claims and candidates; the LLM
gap hook): it reads the product's own text, not other items' cards; every missing field goes in one call; a claimed
value the evidence does not hold stays `missing` with reason `unverified`. The client is scripted; no provider is
called. Pages are synthetic (`example.test`)."""

import asyncio
from datetime import UTC, datetime

from pydantic import JsonValue

from snowcrawl.collect.base import Entity
from snowcrawl.collect.product_scope import product_apart
from snowcrawl.config_ir.role_c import role_c
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import JobId
from snowcrawl.normalize.base import Context
from snowcrawl.resolve.pipeline import UNVERIFIED, GapRequest
from snowcrawl.schemas.types import FieldSpec, FieldType
from tests.support.fake_llm import ScriptedClient
from tests.unit.test_pipeline import PRODUCT, _bundle, _extract, _html, _product

SCHEMA = PRODUCT.model_copy(
    update={"fields": {**PRODUCT.fields, "released": FieldSpec(type=FieldType.DATE, description="Release date")}}
)
# The product's block, then two other items' cards, each leading to its own page and showing its own price.
CARDS = '<ul><li><a href="/p/2">Oak chair</a> 12.00 EUR</li><li><a href="/p/3">Oak stool</a> 8.00 EUR</li></ul>'
PAGE = _html(_product(price=None), body=f"<p>Solid widget, released 2026-09-30</p>{CARDS}")
# What role C reads: the product's block only.
EVIDENCE = "Widget One\nSolid widget, released 2026-09-30"


def claim(value: str, start: int) -> dict[str, JsonValue]:
    return {"shown": True, "ref": "", "value": value, "start": start, "end": start + len(value)}


def test_role_c_reads_the_product_only_asks_once_and_never_publishes_an_unverified_claim() -> None:
    at = EVIDENCE.index("2026-09-30")
    # The price is quoted from nowhere in the evidence (a card's price is cut from it): it is not grounded.
    reply: dict[str, JsonValue] = {
        "fields": {"price": claim("12.00 EUR", 0), "released": claim("2026-09-30", at)},
    }
    client = ScriptedClient(reply)

    result = _extract(_bundle(PAGE), SCHEMA, role_c=role_c(client, job_id=JobId(uuid7())))

    (request,) = client.requests  # one call for every missing field
    read = request.user.partition("<page_text>")[2].partition("</page_text>")[0]
    assert read.strip() == EVIDENCE  # neither card's name nor price
    assert result.entity is not None
    price, released = result.entity["price"], result.entity["released"]
    assert (price.status, price.reason) == ("missing", UNVERIFIED)
    assert (released.status, released.value) == ("published", "2026-09-30")
    assert not any(c.field == "price" and c.source_kind == "llm" for c in result.candidates)


def test_a_product_card_beside_the_main_content_is_left_out_too() -> None:
    card = '<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Oak chair</span> 12.00 EUR</div>'
    page = ParsedPage(
        f"<html><body><main><h1>Widget One</h1><p>Solid widget</p></main><aside>{card}</aside></body></html>"
    )
    url = "https://shop.example.test/p/1"
    ctx = Context(captured_at=datetime(2026, 10, 1, tzinfo=UTC), page_url=url)
    target = Entity(id="page", types=("Product",), urls=(url,), parent=None)
    request = GapRequest(page, SCHEMA, ctx, target, ("price",), product_apart(page, SCHEMA, ctx, ()))
    client = ScriptedClient({"fields": {"price": {"shown": False, "ref": "", "value": "", "start": 0, "end": 0}}})

    asyncio.run(role_c(client, job_id=None).detail(request))

    (call,) = client.requests
    read = call.user.partition("<page_text>")[2].partition("</page_text>")[0]
    assert read.strip() == "Widget One\nSolid widget"


def test_sibling_offer_cards_each_with_its_price_and_button_are_left_out_without_a_stated_price() -> None:
    # The product states no price (out of stock); beside it, two add-on offers, each with its own price and button.
    offers = (
        "<div><div><p>Plan A</p><p>10.00 EUR</p><button>Add - 10.00 EUR</button></div>"
        "<div><p>Plan B</p><p>75.00 EUR</p><button>Add - 75.00 EUR</button></div></div>"
    )
    page = _html(_product(price=None), body=f"<p>Out of stock</p>{offers}")
    client = ScriptedClient({"fields": {"price": claim("10.00 EUR", 0), "released": claim("x", 0)}})

    result = _extract(_bundle(page), SCHEMA, role_c=role_c(client, job_id=JobId(uuid7())))

    (request,) = client.requests
    read = request.user.partition("<page_text>")[2].partition("</page_text>")[0]
    assert read.strip() == "Widget One\nOut of stock"
    assert result.entity is not None
    assert (result.entity["price"].status, result.entity["price"].reason) == ("missing", UNVERIFIED)
