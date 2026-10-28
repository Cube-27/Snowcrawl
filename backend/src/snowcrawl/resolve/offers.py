"""The target's Offers (Atomic offers), as preference reads them.

An Offer field is one whose `schema_org` path starts at Offer. The anchor Offer is the one holding kept values
for the most offer fields, the first seen on a tie; on a detail page, the Offers of the variant the target shows win:
the variants and Offers whose sku or gtin is one the target publishes as its own, one per source (GS1 GTIN and a
seller's SKU identify one variant, whichever source states it), and each other source's lone Offer of the target. A
value only Offers outside the shown variant state is theirs, not the item's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from snowcrawl.collect.base import Candidate, Entity
from snowcrawl.normalize.base import Ok
from snowcrawl.normalize.url_key import refined_by
from snowcrawl.resolve.identity import VARIANT
from snowcrawl.resolve.ownership import OFFER, EntityGraph, names_item
from snowcrawl.resolve.score import Group, same_value
from snowcrawl.schemas.paths import parse_paths
from snowcrawl.schemas.types import FieldSpec, FieldType

# GS1 GTIN and a seller's SKU: the identifiers that name one variant.
VARIANT_IDS = frozenset({"gtin", "sku"})


@dataclass(frozen=True, slots=True)
class PageOffers:
    """The page's Offers as preference reads them: `anchor`, the Offers whose values have scope 1 (none: every Offer
    has), and `shown`, the Offers of the variant the target shows."""

    anchor: frozenset[str] = frozenset()
    shown: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TargetOffers:
    """The Offers and variants of `target` in the page's entity graph."""

    graph: EntityGraph
    target: Entity

    def shown_value(self, contenders: list[Group], offers: PageOffers) -> Group | None:
        """The one value (an amount, an availability) the shown variant's Offers state when every other value is
        only an Offer outside it: the other variants' values are theirs, not the item's."""

        def inside(group: Group) -> bool:
            return any(self.offer_of(c) in offers.shown for c in group.members)

        def elsewhere(group: Group) -> bool:
            return all((o := self.offer_of(c)) is not None and o not in offers.shown for c in group.members)

        shown = [g for g in contenders if inside(g)]
        return shown[0] if len(shown) == 1 and all(elsewhere(g) for g in contenders if g not in shown) else None

    def split(self, top: Group, offers: PageOffers) -> bool:
        """Whether only Offers other than the anchor state the value."""
        return bool(offers.anchor) and all(self.offer_of(c) not in (None, *offers.anchor) for c in top.members)

    def offers_disagree(self, groups: list[Group]) -> bool:
        """Whether two of the field's values are stated by Offers."""
        return sum(any(self.offer_of(c) is not None for c in g.members) for g in groups) > 1

    def lone_offers(self, named: frozenset[str]) -> frozenset[str]:
        """The Offers of the target each of whose sources (`source_of`) other than `named` states one alone: a source's
        one Offer of the item is its offer, the variant the page shows (Atomic offers). A source
        listing several states which of them each is by its sku or gtin."""
        by_source: dict[str, list[str]] = {}
        for offer in self.graph.offers(self.target):
            by_source.setdefault(source_of(offer.id), []).append(offer.id)
        return frozenset(o[0] for source, o in by_source.items() if len(o) == 1 and source not in named)

    def shown_offers(
        self,
        fields: Mapping[str, FieldSpec],
        candidates: Sequence[Candidate],
        own: list[tuple[Candidate, FieldSpec]],
        request_url: str,
    ) -> tuple[frozenset[str], frozenset[str]]:
        """The Offers of the variant the target shows: of the target's Offers and variants, those
        whose sku or gtin (a field's or a row sub-field's) equals one of `own`, or whose own URL `request_url` refines
        (`addressed`), at most one per source, with every other source's Offer stating the same sku or gtin as one of
        them (GS1 GTIN and a seller's SKU identify one variant, whichever source states it); none when no unit matches
        or a source names two. Second, the Offers whose sku or gtin names another variant than those: never the shown
        one's."""
        ids = variant_ids(fields)
        stated = [
            (unit, c, ids[c.field])
            for c in candidates
            if c.field in ids and isinstance(c.normalized, Ok) and (unit := self._unit(c)) is not None
        ]
        addressed = self.addressed(request_url)
        matched = _shown_units(stated, own, addressed)
        other = {unit for unit, _, _ in stated if (own or addressed) and unit not in matched}
        return self._offers_of(matched), self._offers_of(other)

    def addressed(self, request_url: str) -> set[str]:
        """The target's variants and Offers whose own URL the request URL refines (`url_key.refined_by`: the same
        document, every query pair of the unit's URL among the request's, RFC 3986 §3.4), where the request URL is not
        the target's own: a variant's URL (schema.org `url`, an Offer's `url`) addresses the page showing that variant,
        so a request for it, whatever pairs it adds, is a request for the page with that variant selected."""
        if names_item(self.target, request_url):
            return set()
        units = [*self.graph.variants(self.target), *self.graph.offers(self.target)]
        return {unit.id for unit in units if any(refined_by(own, request_url) for own in unit.urls)}

    def _offers_of(self, units: set[str]) -> frozenset[str]:
        return frozenset(offer for unit in units for offer in self._unit_offers(unit))

    def _unit_offers(self, unit: str) -> frozenset[str]:
        """A variant's Offers, or the one Offer `unit` is."""
        held = self.graph.entities[unit]
        return frozenset(o.id for o in self.graph.offers(held)) if held.via == VARIANT else frozenset({held.id})

    def _unit(self, candidate: Candidate) -> str | None:
        """The variant or Offer of the target that holds the candidate: a `hasVariant` member, or an Offer of the
        target itself; None for any other owner."""
        owner = self.graph.owner_of(candidate)
        if self.graph.is_a(owner, OFFER) and owner.parent is not None:
            holder = self.graph.entities[owner.parent]
            return owner.id if holder.id == self.target.id else self._variant_id(holder)
        return self._variant_id(owner)

    def _variant_id(self, entity: Entity) -> str | None:
        return entity.id if entity.via == VARIANT and entity.parent == self.target.id else None

    def is_offer_field(self, spec: FieldSpec) -> bool:
        # A field whose `schema_org` path starts at Offer (or a subtype), in every alternative, holds a part of an
        # offer.
        return spec.schema_org is not None and all(
            self.graph.vocabulary.is_a(path.root, OFFER) for path in parse_paths(spec.schema_org)
        )

    def offer_of(self, candidate: Candidate) -> str | None:
        owner = self.graph.owner_of(candidate)
        return owner.id if self.graph.is_a(owner, OFFER) else None

    def anchor(self, offer_fields: Mapping[str, Sequence[Candidate]]) -> str | None:
        """The Offer the page's offer fields come from: the one holding kept candidates for the most of
        them, the first seen on a tie; `None` when no Offer holds any."""
        coverage: dict[str, int] = {}
        for kept in offer_fields.values():
            for offer in dict.fromkeys(o for c in kept if (o := self.offer_of(c)) is not None):
                coverage[offer] = coverage.get(offer, 0) + 1
        return max(coverage, key=coverage.__getitem__) if coverage else None


def variant_ids(fields: Mapping[str, FieldSpec]) -> dict[str, FieldSpec]:
    """The sku and gtin fields, a row-mapped field's sub-fields (`<field>.<sub-field>`) included, by candidate field."""
    specs = dict(fields)
    for name, spec in fields.items():
        if spec.item is not None and spec.item.fields is not None:
            specs.update({f"{name}.{sub}": sub_spec for sub, sub_spec in spec.item.fields.items()})
    return {
        name: spec for name, spec in specs.items() if spec.type is FieldType.IDENTIFIER and spec.scheme in VARIANT_IDS
    }


def _names(candidate: Candidate, spec: FieldSpec, ids: Sequence[tuple[Candidate, FieldSpec]]) -> bool:
    """Whether a sku or gtin candidate states one of `ids` of its own scheme."""
    return any(spec.scheme == o_spec.scheme and same_value(candidate, o, spec) for o, o_spec in ids)


_Stated = tuple[str, Candidate, FieldSpec]  # a variant or Offer, one sku or gtin candidate of it, and its field


def _shown_units(
    stated: Sequence[_Stated], own: Sequence[tuple[Candidate, FieldSpec]], addressed: set[str]
) -> set[str]:
    """The variants and Offers whose sku or gtin is one of `own`, or that the request URL addresses, one per source,
    and the one of each other source stating the same sku or gtin as them; none when a source names two
    (`_one_each`)."""
    matched = {unit for unit, c, spec in stated if _names(c, spec, own)} | addressed
    if not matched or _one_each(matched) != matched:
        return set()
    named = {source_of(unit) for unit in matched}
    held = [(c, spec) for unit, c, spec in stated if unit in matched]
    return matched | _one_each({u for u, c, spec in stated if source_of(u) not in named and _names(c, spec, held)})


def _one_each(units: set[str]) -> set[str]:
    """The units whose source (`source_of`) names no other of them: a source naming two variants names none."""
    sources = [source_of(unit) for unit in units]
    return {unit for unit, source in zip(units, sources, strict=True) if sources.count(source) == 1}


# The collectors whose entity ids name the document they read after the collector (`<collector>:<document>:<path>`):
# an adapter's answer (`adapter:<name>:`), and a JSON-LD script block (`jsonld:<block>:`), each a JSON-LD document of
# its own (JSON-LD 1.1 §7.1: a script element embeds one document), often written by another producer than the page's.
_DOCUMENTS = frozenset({"adapter", "jsonld"})


def source_of(entity_id: str) -> str:
    """The source an entity id names (`resolve.identity`): its collector, and for an adapter or a JSON-LD block the
    document it reads (`_DOCUMENTS`), so two adapters' answers, or two blocks, are two sources."""
    collector, _, rest = entity_id.partition(":")
    return f"{collector}:{rest.partition(':')[0]}" if collector in _DOCUMENTS else collector
