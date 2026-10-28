"""A record's main image is not repeated in its list of images.

A schema can read one schema.org property into two fields: a scalar `image_url` field, the property's main value, and
a list of `image_url` items, its values. When both read the same `schema_org` path, the list holds the property's other
values: an item naming a file the scalar field publishes (`images.same_file`: the file or a rendition of it) is left
out. A list left with no item is unpublished (`MAIN_IMAGE_ONLY`): the page states no other image.
"""

from typing import Final

from pydantic import JsonValue

from snowcrawl.normalize.images import same_file
from snowcrawl.resolve.types import FieldOutcome, FieldSet
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# Why a list of images is unpublished: every item it held is the record's main image.
MAIN_IMAGE_ONLY: Final = "main_image_only"


def without_main_image(schema: SchemaDef, fields: FieldSet) -> FieldSet:
    """`fields` with each published list of images holding none of the files its scalar image fields publish."""
    out = dict(fields)
    for name, spec in schema.fields.items():
        outcome = fields.get(name)
        if outcome is None or outcome.status != "published" or not _image_list(spec):
            continue
        main = [
            o.value
            for n, s in schema.fields.items()
            if s.type is FieldType.IMAGE_URL and s.schema_org == spec.schema_org
            if (o := fields.get(n)) is not None and o.status == "published"
        ]
        if main:
            out[name] = _apart(outcome, main)
    return out


def _image_list(spec: FieldSpec) -> bool:
    """A list of images that reads a schema.org path (a field without one shares no property with another)."""
    is_list = spec.type is FieldType.LIST and spec.item is not None and spec.item.type is FieldType.IMAGE_URL
    return is_list and spec.schema_org is not None


def _apart(outcome: FieldOutcome, main: list[JsonValue]) -> FieldOutcome:
    value = outcome.value
    if not isinstance(value, list):
        return outcome
    kept = [i for i, item in enumerate(value) if not any(same_file(item, image) for image in main)]
    if len(kept) == len(value):
        return outcome
    if not kept:
        return outcome.model_copy(
            update={
                "status": "missing",
                "reason": MAIN_IMAGE_ONLY,
                "value": None,
                "stated": None,
                "confidence": None,
                "calibrated": False,
            }
        )
    stated = outcome.stated
    if isinstance(stated, list):  # `Ok.stated` of a list writes each item at its place
        stated = [stated[i] for i in kept] if len(stated) == len(value) else None
    return outcome.model_copy(update={"value": [value[i] for i in kept], "stated": stated})
