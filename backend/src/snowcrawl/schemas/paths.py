"""The `schema_org` path grammar: a field's path parsed into its alternatives, and
the price role a money field's path gives it.

A path `T.p{…}[.q{…}][+r[+s]]` starts at type `T` and follows the properties `p`, `q`; each `+` joins another property
of the last node to the value. A step's filter predicates keep only the nodes they match: `{name=Value}` a node whose
`name` is `Value` (as an IRI or a term), `{name}` one stating `name`, `{!name}` one not stating it. `|` pools
alternatives and `>` ranks them. The schema document's pattern (`schemas.types.SchemaOrgPath`) has already checked the
syntax, so parsing never fails.
"""

import re
from typing import Literal, NamedTuple

from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaContent


class Predicate(NamedTuple):
    """A step's filter: the property it reads, the value it must equal (`None`: any), and whether it must be absent."""

    name: str
    value: str | None = None
    absent: bool = False


Filters = tuple[Predicate, ...]


class MappedPath(NamedTuple):
    root: str  # the type the path starts at
    steps: tuple[str, ...]  # the properties followed, the last one holding the value
    joined: tuple[str, ...] = ()  # `+r`: properties of the last node joined to the value
    rank: int = 0  # how many `>` come before it: read on an entity only when no lower rank reaches a value there
    filters: tuple[Filters, ...] = ()  # each step's predicates, by step; a step past the end has none

    def where(self, step: int) -> Filters:
        """The predicates of step `step`."""
        return self.filters[step] if step < len(self.filters) else ()


# One step: a property, then its predicates (`{!name}`, `{name}`, `{name=Value}`).
_STEP = re.compile(r"([a-z][A-Za-z0-9]*)((?:\{[^}]*\})*)")
_PREDICATE = re.compile(r"\{(!?)([a-z][A-Za-z0-9]*)(?:=([A-Za-z][A-Za-z0-9]*))?\}")


def parse_paths(path: str) -> tuple[MappedPath, ...]:
    """A `schema_org` path's alternatives, in order, each with its rank."""
    return tuple(
        _parse_alternative(alternative, rank)
        for rank, ranked in enumerate(path.split(">"))
        for alternative in ranked.split("|")
    )


def _parse_alternative(path: str, rank: int) -> MappedPath:
    path, *joined = path.split("+")
    root, *steps = path.split(".")
    parsed = [_STEP.fullmatch(step) for step in steps]
    names = tuple(m.group(1) if m else step for m, step in zip(parsed, steps, strict=True))
    filters = tuple(_predicates(m.group(2)) if m else () for m in parsed)
    return MappedPath(root, names, tuple(joined), rank, filters if any(filters) else ())


def _predicates(written: str) -> Filters:
    return tuple(Predicate(m.group(2), m.group(3) or None, m.group(1) == "!") for m in _PREDICATE.finditer(written))


# schema.org data model ("Conformance"): where a property expects a Thing, a text value is accepted; the text names
# that thing. So a path ending in `.name` takes the text that stands in for the thing.
NAME = ("name",)


def required_types(schema: SchemaContent) -> tuple[str, ...]:
    """The schema.org types the schema's required row fields' `schema_org` paths start at (each alternative's), sorted:
    the types of the items a page is read for (a listing's page fields describe the page, not its rows)."""
    return _roots(schema, {name for name, spec in schema.fields.items() if spec.required})


def target_types(schema: SchemaContent) -> tuple[str, ...]:
    """`required_types`, the fields a `required_groups` group names counted as required: a schema may require its row
    data only through a group."""
    grouped = {name for group in schema.required_groups for name in group}
    return _roots(schema, grouped | {name for name, spec in schema.fields.items() if spec.required})


def rooted_at(schema: SchemaContent, root: str) -> tuple[str, ...]:
    """The schema's row fields every alternative of whose `schema_org` path starts at type `root`, sorted: for
    `Offer`, the fields an item's schema.org Offer states (its price, its availability)."""
    return tuple(
        sorted(
            name
            for name, spec in schema.fields.items()
            if spec.schema_org and spec.scope == "row" and all(p.root == root for p in parse_paths(spec.schema_org))
        )
    )


def _roots(schema: SchemaContent, names: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                path.root
                for name in names
                if (spec := schema.fields.get(name)) is not None and spec.schema_org and spec.scope == "row"
                for path in parse_paths(spec.schema_org)
            }
        )
    )


def named_property(path: MappedPath) -> str:
    """The schema.org property whose value a path reaches: its last step, or the step before a final `.name`, since
    the text standing in for a thing names it (`NAME`): `Product.brand.name` names `brand`."""
    if len(path.steps) > 1 and path.steps[-1:] == NAME and not path.joined:
        return path.steps[-2]
    return path.steps[-1]


PriceRole = Literal["price", "list_price", "unit_price", "member_price"]

# schema.org `price`: "The offer price of a product, or of a price component when attached to PriceSpecification".
_PRICE = "price"
# schema.org `priceType` (PriceTypeEnumeration): `StrikethroughPrice` "the price shown as a strikethrough" and
# `ListPrice` "the list price", both a former or reference price rather than the price paid.
_PRICE_TYPE = "priceType"
_LIST_TYPES = frozenset({"StrikethroughPrice", "ListPrice"})
# `SalePrice` "the price is a sale price": the price paid now, the active price.
_SALE_TYPE = "SalePrice"
# schema.org `referenceQuantity`: "The reference quantity for which a certain price applies" (UnitPriceSpecification).
_REFERENCE_QUANTITY = "referenceQuantity"
# schema.org `validForMemberTier`: "The membership program tier an Offer (or a PriceSpecification) is valid for".
_MEMBER_TIER = "validForMemberTier"


def price_role(spec: FieldSpec) -> PriceRole | None:
    """The price role a money field's path gives it: every alternative reaches schema.org
    `price`, through a node its predicates require to be a unit price (`{referenceQuantity}`), a list or struck price
    (`{priceType=StrikethroughPrice}`, `ListPrice`) or a member price (`{validForMemberTier}`), else the active price
    when it requires nothing but a sale price (`{priceType=SalePrice}`). `None` for any other field, and for
    alternatives that give different roles."""
    if spec.type is not FieldType.MONEY or spec.schema_org is None:
        return None
    roles = {_alternative_role(path) for path in parse_paths(spec.schema_org)}
    return roles.pop() if len(roles) == 1 else None


def _alternative_role(path: MappedPath) -> PriceRole | None:
    if path.steps[-1] != _PRICE:
        return None
    required = [p for filters in path.filters for p in filters if not p.absent]
    if any(p.name == _REFERENCE_QUANTITY for p in required):
        return "unit_price"
    if any(p.name == _PRICE_TYPE and p.value in _LIST_TYPES for p in required):
        return "list_price"
    if any(p.name == _MEMBER_TIER for p in required):
        return "member_price"
    return "price" if all(p.name == _PRICE_TYPE and p.value == _SALE_TYPE for p in required) else None
