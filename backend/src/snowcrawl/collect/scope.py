"""Another item's scope ("Candidate score": scope match): the structured-data entities that belong to
another thing than the product a page is about, whatever page it is.

A thing that is the value of a relation property of another (schema.org `isRelatedTo`, `isSimilarTo`) is another thing
than its holder, and a thing in a list (an `ItemList` and its `ListItem`s) is one of many; whatever such a thing holds
(its Offers, their prices) is in its scope. So is a root item every one of whose URLs names another document than
the page (`normalize.url_key.document_key`: scheme, authority and path; a query selects within it) while another root
item of its kind (one of their types is the other's or a subtype of it) names the page: an item described here and
sold elsewhere, as recommendations and bundles are. A root of another kind naming the page (its WebPage, a breadcrumb)
says nothing of which product is the page's, and where no root of its kind names the page, an item is not set apart
by its URLs (the page's own may state only a URL the request did not use). A thing that is the `mainEntity` of a
page node (schema.org `mainEntity`: "the primary entity described in some page") whose own URLs all name another
document than the page is that document's subject, not this page's, unless it names the page itself
(`main_entity_elsewhere`). Collection reads it
to know the page product's stated prices (`collect.product_scope.product_apart`); resolution's ownership and intent
apply the same relations.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from snowcrawl.collect.base import Entity
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.normalize.url_key import DocumentKey, document_key

# schema.org relation properties of a Product: a thing that is the value of one is another thing than its holder.
RELATIONS: Final = frozenset({"isRelatedTo", "isSimilarTo"})
# schema.org: an ItemList (with its ListItems) lists things, so a thing in it is one of many, not the page's own.
LISTS: Final = ("ItemList", "ListItem")
# schema.org CreativeWork.mainEntity: "Indicates the primary entity described in some page or other CreativeWork".
MAIN_ENTITY: Final = "mainEntity"


def other_items(entities: Sequence[Entity], vocabulary: Vocabulary, page_urls: Iterable[str]) -> frozenset[str]:
    """The ids of the entities in another item's scope (module docstring)."""
    by_id = {e.id: e for e in entities}
    documents = page_documents(page_urls)
    namers = [e for e in entities if e.parent is None and not documents.isdisjoint(map(document_key, _own_urls(e)))]
    return frozenset(
        e.id
        for e in entities
        if main_entity_elsewhere(e, by_id, documents)
        or any(_another(n, vocabulary, documents, namers) for n in _chain(e, by_id))
    )


Documents = frozenset[DocumentKey]


def page_documents(page_urls: Iterable[str]) -> Documents:
    """The documents the page's URLs name (`normalize.url_key.document_key`)."""
    return frozenset(d for url in page_urls if (d := document_key(url)) is not None)


def leads_elsewhere(entity: Entity, documents: Documents) -> bool:
    """Whether the entity states URLs of its own (its item URLs, else every URL it holds) and none names one of the
    page's `documents`: a thing described here whose page is another."""
    urls = _own_urls(entity)
    return bool(urls) and documents.isdisjoint(map(document_key, urls))


def main_entity_elsewhere(entity: Entity, by_id: Mapping[str, Entity], documents: Documents) -> bool:
    """Whether the entity, or a thing holding it, is the `mainEntity` of a node whose own URLs all name another
    document than the page's `documents` (`leads_elsewhere`), and states no URL naming the page itself: the primary
    entity of that other page (schema.org `mainEntity`; `url`: "URL of the item")."""
    for node in _chain(entity, by_id):
        holder = by_id.get(node.parent) if node.parent is not None else None
        if (
            holder is not None
            and node.via == MAIN_ENTITY
            and leads_elsewhere(holder, documents)
            and documents.isdisjoint(map(document_key, _own_urls(node)))
        ):
            return True
    return False


def _own_urls(entity: Entity) -> tuple[str, ...]:
    return entity.item_urls or entity.urls


def _chain(entity: Entity, by_id: Mapping[str, Entity]) -> Iterable[Entity]:
    """The entity and its ancestors, up to the root (`CollectResult` keeps entities a tree)."""
    node: Entity | None = entity
    while node is not None:
        yield node
        node = by_id.get(node.parent) if node.parent is not None else None


def _another(entity: Entity, vocabulary: Vocabulary, documents: Documents, namers: Sequence[Entity]) -> bool:
    if entity.via in RELATIONS or any(vocabulary.is_a(t, list_type) for t in entity.types for list_type in LISTS):
        return True
    return (
        entity.parent is None
        and leads_elsewhere(entity, documents)
        and any(_kin(entity, namer, vocabulary) for namer in namers)
    )


def _kin(a: Entity, b: Entity, vocabulary: Vocabulary) -> bool:
    return any(vocabulary.is_a(s, t) or vocabulary.is_a(t, s) for s in a.types for t in b.types)
