"""Intent: the one entity a detail page is about, or `intent_ambiguous`.

The candidates are the entities of the schema's primary types that are not the main entity of another page
(`collect.scope.main_entity_elsewhere`), without those inside a list, inside another
primary entity's relation properties, a primary entity's variants, the group it is a variant of, or the item a primary
entity's review or rating names as reviewed (`itemReviewed`, the entity itself). One candidate is
the target. Of several, the one whose URLs name the request, final or canonical URL is. Failing that, the page's own
candidates are those that are not another page's item: an entity whose every URL is another document, or one of a
repeated set of items in the markup (a carousel, a comparison table). One own candidate is the target; of several,
the one naming a page URL, and of several of those the one product group with variants. Otherwise the page is
ambiguous and nothing is guessed. A page with no entity of a primary type at all is about itself, the `page` entity.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from snowcrawl.collect.base import Entity
from snowcrawl.collect.scope import LISTS, RELATIONS, leads_elsewhere, main_entity_elsewhere, page_documents
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.resolve.identity import VARIANT, repeated
from snowcrawl.resolve.ownership import OFFER, EntityGraph, names_any
from snowcrawl.schemas.paths import required_types
from snowcrawl.schemas.types import SchemaDef

# schema.org `itemReviewed` ("the item that is being reviewed/rated") points back at the item its Review or
# AggregateRating describes: under a primary entity it is that entity restated, not another subject.
_BACK_REFERENCES = frozenset({"itemReviewed"})
# A primary entity's variants are members of it (`VARIANT`, schema.org `hasVariant`), not other things the page is
# about: they often repeat the group's URL, which would leave the page ambiguous. schema.org `isVariantOf`, the inverse
# of `hasVariant`, holds the group its holder is a variant of: a more general thing than the holder.
_NOT_THE_SUBJECT = RELATIONS | {VARIANT, "isVariantOf"}


@dataclass(frozen=True, slots=True)
class AmbiguousTarget:
    """No single entity is the target: `candidates` are the entities the page URLs did not settle between."""

    candidates: tuple[str, ...]
    reason: Literal["intent_ambiguous"] = "intent_ambiguous"


def primary_types(schema: SchemaDef, vocabulary: Vocabulary) -> frozenset[str]:
    """The types the schema's required row fields' `schema_org` paths start at (each alternative's), without Offer
    when another type remains: an Offer belongs to the item it offers (`OFFER`). A listing's page fields describe the
    page, not its rows, so they name no primary type."""
    roots = required_types(schema)
    items = frozenset(root for root in roots if not vocabulary.is_a(root, OFFER))
    return items or frozenset(roots)


def select_target(
    request_url: str,
    final_url: str,
    canonical_url: str | None,
    graph: EntityGraph,
    primary: frozenset[str],
) -> Entity | AmbiguousTarget:
    # The graph's page URLs add the page's alternates by language (`collect.run`'s `page_urls`).
    page_urls = list(dict.fromkeys((*(u for u in (request_url, final_url, canonical_url) if u), *graph.page_urls)))
    documents = page_documents(page_urls)
    primaries = [
        e
        for e in graph.entities.values()
        if is_primary(graph, e, primary) and not main_entity_elsewhere(e, graph.entities, documents)
    ]
    if not primaries:
        return graph.page
    candidates = [e for e in primaries if not _excluded(graph, e, primary)]
    if len(candidates) == 1:
        return candidates[0]
    named = [e for e in candidates if names_any(e, page_urls)]
    for settled in _tie_breaks(graph, candidates, named, page_urls):
        if len(settled) == 1:
            return settled[0]
    return AmbiguousTarget(tuple(e.id for e in named or candidates))


def _tie_breaks(
    graph: EntityGraph, candidates: list[Entity], named: list[Entity], page_urls: list[str]
) -> Iterator[list[Entity]]:
    """The candidate sets that settle the target when one holds exactly one entity, in order: those naming a page URL,
    the page's own, its own naming a page URL, and of those the product groups with variants."""
    yield named
    own = _own(candidates, page_urls)
    yield own
    named_own = [e for e in named if e in own]
    yield named_own
    yield [e for e in named_own if _has_variants(graph, e)]


def is_primary(graph: EntityGraph, entity: Entity, primary: frozenset[str]) -> bool:
    return any(graph.is_a(entity, type_) for type_ in primary)


def sole_primary(graph: EntityGraph, target: Entity | AmbiguousTarget, primary: frozenset[str]) -> Entity | None:
    """`target` when it is the page's one entity of a primary type, the only page whose item a rating stated apart
    can rate (`EntityGraph.ratings_apart`); else None."""
    primaries = [e for e in graph.entities.values() if is_primary(graph, e, primary)]
    return target if isinstance(target, Entity) and primaries == [target] else None


def _own(candidates: list[Entity], page_urls: list[str]) -> list[Entity]:
    """The candidates that are not another page's item: not leading only to other documents, not repeated."""
    documents = page_documents(page_urls)
    apart = repeated(candidates)
    return [e for e in candidates if not leads_elsewhere(e, documents) and e.id not in apart]


def _has_variants(graph: EntityGraph, entity: Entity) -> bool:
    return any(e.parent == entity.id and e.via == VARIANT for e in graph.entities.values())


def _excluded(graph: EntityGraph, entity: Entity, primary: frozenset[str]) -> bool:
    """Whether the entity is inside a list, inside a relation property of a primary entity or one of its
    variants (§4.6 step 1), or a primary entity's back-reference to itself (`itemReviewed` of its rating)."""
    child, back = entity, False
    for ancestor in graph.ancestors(entity):
        if any(graph.is_a(ancestor, t) for t in LISTS):
            return True
        back = back or child.via in _BACK_REFERENCES
        if (back or child.via in _NOT_THE_SUBJECT) and is_primary(graph, ancestor, primary):
            return True
        child = ancestor
    return False
