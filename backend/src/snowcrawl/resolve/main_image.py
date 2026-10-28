"""A record's main image is not repeated in its list of images.

A schema can read one schema.org property into two fields: a scalar `image_url` field, the property's main value, and
a list of `image_url` items, its values. When both read the same `schema_org` path, the list holds the property's other
values: an item naming a file the scalar field publishes (`images.same_file`: the file or a rendition of it) is left
out. Both name one file, so the scalar field publishes it at the largest of their renditions
(`images.preferred_rendition`), the list's copy of it when that is larger: leaving the item out never drops the larger
copy. A list left with no item is unpublished (`MAIN_IMAGE_ONLY`): the page states no other image.
"""

from typing import Final

from pydantic import JsonValue

from snowcrawl.normalize.images import preferred_rendition, same_file
from snowcrawl.resolve.types import FieldOutcome, FieldSet
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# Why a list of images is unpublished: every item it held is the record's main image.
MAIN_IMAGE_ONLY: Final = "main_image_only"


def without_main_image(schema: SchemaDef, fields: FieldSet) -> FieldSet:
    """`fields` with each published list of images holding none of the files its scalar image fields publish, and
    each of those at the largest rendition the list also states."""
    out = dict(fields)
    for name, spec in schema.fields.items():
        outcome = fields.get(name)
        if outcome is None or outcome.status != "published" or not _image_list(spec):
            continue
        main = _main_fields(schema, spec, out)
        for n in main:
            out[n] = _at_largest(out[n], outcome)
        if main:
            out[name] = _apart(outcome, [out[n].value for n in main])
    return out


def _main_fields(schema: SchemaDef, spec: FieldSpec, fields: FieldSet) -> list[str]:
    """The published scalar image fields reading the list's schema.org path."""
    return [
        n
        for n, s in schema.fields.items()
        if s.type is FieldType.IMAGE_URL and s.schema_org == spec.schema_org
        if (o := fields.get(n)) is not None and o.status == "published"
    ]


def _at_largest(main: FieldOutcome, listing: FieldOutcome) -> FieldOutcome:
    """The main image at the largest rendition of its file among it and the list's copies of it (`same_file`)."""
    items = listing.value
    if not isinstance(main.value, str) or not isinstance(items, list):
        return main
    copies = {i: item for i, item in enumerate(items) if isinstance(item, str) and same_file(item, main.value)}
    best = preferred_rendition([main.value, *copies.values()])
    if best == main.value:
        return main
    place = next(i for i, item in copies.items() if item == best)
    written = listing.stated[place] if isinstance(listing.stated, list) and len(listing.stated) == len(items) else None
    return main.model_copy(update={"value": best, "stated": None if written == best else written})


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
