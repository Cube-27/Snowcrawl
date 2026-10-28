"""A record's main image is not repeated in its list of images, and a check-digit pass among rows whose same-length
values mostly fail is a chance pass, not an identifier."""

from typing import Any

import pytest

from snowcrawl.collect.base import Candidate
from snowcrawl.core.ids import uuid7
from snowcrawl.db.models import SchemaId
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import normalize
from snowcrawl.resolve.check_digits import chance_passes
from snowcrawl.resolve.main_image import MAIN_IMAGE_ONLY, without_main_image
from snowcrawl.resolve.types import FieldOutcome
from snowcrawl.schemas.registry import load_presets
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

IMAGE = "https://cdn.example.test/files/widget.jpg"
OTHER = "https://cdn.example.test/files/side.jpg"
GTIN = FieldSpec(type=FieldType.IDENTIFIER, scheme="gtin")
CTX = Context(captured_at="2026-10-28T00:00:00Z")  # type: ignore[arg-type]


def _schema() -> SchemaDef:
    (document,) = (d for d in load_presets() if d.name == "commerce_detail")
    return SchemaDef(id=SchemaId(uuid7()), origin="preset", status="active", **document.model_dump())


def _published(value: Any) -> FieldOutcome:
    return FieldOutcome(
        status="published",
        reason=None,
        value=value,
        evidence="structured",
        confidence=None,
        calibrated=False,
        source_kind="jsonld",
        locator="jsonld:0:$['image']",
        derived_by=None,
        chosen=None,
        rejected=(),
    )


@pytest.mark.parametrize(("main", "listed"), [(900, 300), (300, 900)])
def test_the_list_of_images_leaves_out_the_main_image_which_publishes_at_its_larger_copy(
    main: int, listed: int
) -> None:
    fields = {
        "image_url": _published(f"{IMAGE}?width={main}"),
        "additional_images": _published([f"{IMAGE}?width={listed}", OTHER]),
    }

    out = without_main_image(_schema(), fields)

    assert out["image_url"].value == f"{IMAGE}?width=900"
    assert (out["additional_images"].status, out["additional_images"].value) == ("published", [OTHER])


def test_a_list_holding_only_the_main_image_is_unpublished() -> None:
    fields = {"image_url": _published(IMAGE), "additional_images": _published([IMAGE])}

    images = without_main_image(_schema(), fields)["additional_images"]

    assert (images.status, images.reason, images.value) == ("missing", MAIN_IMAGE_ONLY, None)


def _gtin(raw: str, row: int, key: str = "barcode") -> Candidate:
    return Candidate(
        field="variants.gtin",
        entity_id=f"adapter:p:$['variants'][{row}]",
        source_kind="adapter",
        raw=raw,
        normalized=normalize(raw, GTIN, CTX),
        locator=f"adapter:p:$['variants'][{row}]['{key}']",
        independent=True,
    )


def test_a_pass_among_mostly_failing_same_length_values_at_one_path_is_by_chance() -> None:
    # 10000007 and 10000014 pass the GS1 check digit; 10000001, 10000002 fail it. The 13-digit values all pass.
    sub_fields = {"gtin": GTIN}
    eight = [_gtin(raw, n) for n, raw in enumerate(["10000001", "10000002", "10000007"])]
    thirteen = [_gtin("4006381333931", 3), _gtin("4006381333931", 4, key="ean")]
    assert isinstance(eight[2].normalized, Ok)

    assert chance_passes("variants", sub_fields, [*eight, *thirteen]) == [eight[2]]
    assert chance_passes("variants", sub_fields, [_gtin("10000007", 0), _gtin("10000014", 1), eight[0]]) == []


def test_a_printed_isbn_counts_by_its_digits() -> None:
    isbn = FieldSpec(type=FieldType.IDENTIFIER, scheme="isbn")
    printed = [
        Candidate(
            field="variants.isbn",
            entity_id=f"jsonld:0:$['offers'][{n}]",
            source_kind="jsonld",
            raw=raw,
            normalized=normalize(raw, isbn, CTX),
            locator=f"jsonld:0:$['offers'][{n}]['isbn']",
            independent=True,
        )
        for n, raw in enumerate(["ISBN 0-306-40615-1", "0-306-40615-3", "0-306-40615-2"])
    ]
    assert isinstance(printed[2].normalized, Ok)

    assert chance_passes("variants", {"isbn": isbn}, printed) == [printed[2]]
