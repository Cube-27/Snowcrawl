"""Other items' cards on a detail page: the elements whose values belong to other items than the page's own, read by
the DOM collectors (`collect.dom`, `collect.dom_images`, `collect.dom_text`) and role C.

Values held by other items than the page's own are not the page's: schema.org microdata's related and similar items
and reviews, sibling blocks that each link to one other document (a list of cards, `other_items`), and the card of any
other item whose price the page shows (`product_apart`). `collect.scope` is the same relation for structured data.
"""

from collections.abc import Iterable, Iterator, Sequence

from selectolax.lexbor import LexborNode

from snowcrawl.collect.base import Candidate
from snowcrawl.collect.dom_images import A_HREF, Links, apart_ids, children
from snowcrawl.collect.items.spot import spots
from snowcrawl.collect.vocabulary import NAMESPACES, schema_term
from snowcrawl.content.page import ParsedPage, node_xpath
from snowcrawl.content.regions import content_region
from snowcrawl.content.repeats import MIN_REPEATS
from snowcrawl.content.text import shown, struck_ids
from snowcrawl.normalize.base import Context, Ok
from snowcrawl.normalize.dispatch import equality_key
from snowcrawl.normalize.strings import collapse
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

# The longest text a typed value is spotted in: an amount or an identifier is short, a longer text is prose around one.
SPOT_LENGTH = 64
# schema.org `Product`: the item type whose microdata items other than the page's are set apart (`product_apart`).
_PRODUCT = "Product"
# HTML Standard §4.10.6 `button` and §4.10.5 `input` in the Submit, Reset and Button states: a control of its form or
# block.
_BUTTONS = 'button, input[type="submit" i], input[type="button" i], input[type="image" i]'
# schema.org properties whose values are other things than the page's item: `review`, `isRelatedTo`, `isSimilarTo`,
# as an `itemprop` token names them, bare or as an IRI in either namespace; and a Review item, whose `itemtype` URL ends
# with `/Review` (HTML Standard §5.4: microdata).
OTHER_ITEMS = ",".join(
    (
        *(
            f'[itemprop~="{namespace}{prop}"]'
            for prop in ("review", "isRelatedTo", "isSimilarTo")
            for namespace in ("", *NAMESPACES)
        ),
        '[itemtype$="/Review"]',
    )
)


def product_apart(page: ParsedPage, schema: SchemaDef, ctx: Context, stated: Sequence[Candidate]) -> frozenset[int]:
    """The elements of a detail page's main content outside its product container: the cards of other items.

    The product's prices are those structured data `stated` for the schema's money fields. A price the page shows
    (`items.spot`, outside chrome and struck text) that is none of them belongs to the one other document its nearest
    block holding hyperlinks leads to (HTML Standard §4.6.1), or the hyperlink's parent when that block is the
    hyperlink and the parent leads nowhere else: that block is the other item's card, unless it shows one of the
    product's prices or holds the page's title, the main content's first shown `h1` (HTML Standard §4.3.6), which are
    the product's own. A block whose hyperlinks lead to several documents is no card. Recommendations, "complete the
    look" and bundles of other items are such cards, whatever their markup. So is a schema.org Product item (HTML
    Standard §5.2: an `itemscope` whose `itemtype` is `Product`, outside other items' content, nested or not) that does
    not hold the title. Without a stated price no price is the product's for certain: no such card is set apart.
    Sibling offer cards are, stated price or not (`_offer_cards`)."""
    region = content_region(page)
    money = next((spec for spec in schema.fields.values() if spec.type is FieldType.MONEY), None)
    if region is None or schema.page_kind != "detail" or page.json_body:
        return frozenset()
    chrome = apart_ids(region) | struck_ids(region)
    title = next((h for h in region.css("h1") if h.mem_id not in chrome and shown(h)), None)
    apart = [item for item in _product_items(page) if title is None or not _holds(item, title)]
    if money is None:
        return inside(apart)
    own = _stated_prices(schema, stated)
    titled = frozenset(node.mem_id for node in _up(title, region)) if title is not None else frozenset()
    priced = _priced(region, chrome, money, ctx)
    offers = _offer_cards(region, priced, own, titled)
    if not own:
        return inside((*apart, *offers))
    return inside((*apart, *offers, *_other_cards(region, priced, own, titled, ctx)))


def other_item_paths(page: ParsedPage, ctx: Context, outside: frozenset[int]) -> frozenset[str]:
    """The node paths of the outermost elements of a detail page that hold other items' content: what `other_items`
    finds in the main content (related and similar items, reviews, lists of other documents' cards) and the blocks
    outside the product container wherever they are (`outside`, `product_apart`: other items' priced cards, schema.org
    Product items in a sidebar too). Role C reads the page's text without them."""
    if page.json_body:
        return frozenset()
    region = content_region(page)
    apart = outside if region is None else inside(other_items(region, Links.of(region, ctx))) | outside
    if not apart:
        return frozenset()
    return frozenset(
        node_xpath(node)
        for node in page.tree.css("*")
        if node.mem_id in apart and (node.parent is None or node.parent.mem_id not in apart)
    )


def _other_cards(
    region: LexborNode,
    priced: Sequence[tuple[LexborNode, object]],
    own: frozenset[object],
    titled: frozenset[int],
    ctx: Context,
) -> Iterator[LexborNode]:
    """The cards of the other items whose prices the region shows (`product_apart`): for each price not `own`, its
    nearest block holding hyperlinks (or that hyperlink's parent), when they lead to one other document and the block
    neither holds the title (is in `titled`) nor shows an `own` price."""
    shows: dict[int, set[object]] = {}
    for element, value in priced:
        for node in _up(element, region):
            shows.setdefault(node.mem_id, set()).add(value)
    targets = _targets(region, ctx)

    def foreign(node: LexborNode) -> bool:  # leads to one other document only, not the title's, shows no own price
        return node.mem_id not in titled and len(targets.get(node.mem_id, ())) == 1 and not own & shows[node.mem_id]

    for element, value in priced:
        card = next((node for node in _up(element, region) if node.mem_id in targets), None)
        if value in own or card is None or not foreign(card):
            continue
        # A hyperlink around the price alone leaves the card's other values (its brand, its name) beside it.
        parent = card.parent if card.tag == "a" else None
        held = parent is not None and parent.mem_id != region.mem_id and foreign(parent)
        yield parent if held and parent is not None and targets[parent.mem_id] == targets[card.mem_id] else card


def _offer_cards(
    region: LexborNode,
    priced: Sequence[tuple[LexborNode, object]],
    own: frozenset[object],
    titled: frozenset[int],
) -> Iterator[LexborNode]:
    """Sibling blocks of one tag (two or more) that each show one price, a different one each, and each hold a button
    of their own (HTML Standard §4.10.6: a control acting for its block): offers of different things, other items
    or the variants of one, whose price none states for the product as a whole (a top-level value is one every
    variant shares). A block holding the title (`titled`) or showing one of the product's stated prices (`own`) is
    the product's own, and leaves its group whole."""
    shows: dict[int, set[object]] = {}
    for element, value in priced:
        for node in _up(element, region):
            shows.setdefault(node.mem_id, set()).add(value)

    def offer(block: LexborNode) -> bool:
        values = shows.get(block.mem_id, set())
        return len(values) == 1 and not values & own and block.mem_id not in titled and _holds_button(block)

    for parent in (region, *region.css("*")):
        groups: dict[str, list[LexborNode]] = {}
        for child in children(parent):
            groups.setdefault(child.tag or "", []).append(child)
        for group in groups.values():
            if len(group) >= MIN_REPEATS and all(offer(block) for block in group) and _distinct(group, shows):
                yield from group


def _holds_button(block: LexborNode) -> bool:
    """Whether the block is or holds a button (HTML Standard §4.10.6 `button`, §4.10.5 `input` of a button type)."""
    return block.tag == "button" or block.css_first(_BUTTONS) is not None


def _distinct(group: Sequence[LexborNode], shows: dict[int, set[object]]) -> bool:
    """Whether the blocks each show a price no other block of the group shows."""
    values = [next(iter(shows[block.mem_id])) for block in group]
    return len(set(values)) == len(values)


def _stated_prices(schema: SchemaDef, stated: Sequence[Candidate]) -> frozenset[object]:
    """The money values structured data states for the schema's money fields, keyed as `_priced` keys them."""
    return frozenset(
        equality_key(c.normalized.value, spec)
        for c in stated
        if isinstance(c.normalized, Ok)
        and (spec := schema.fields.get(c.field)) is not None
        and spec.type is FieldType.MONEY
    )


def _priced(
    region: LexborNode, chrome: frozenset[int], money: FieldSpec, ctx: Context
) -> list[tuple[LexborNode, object]]:
    """The region's shown elements whose own short text is a money value (`items.spot`), outside `chrome` and struck
    text, with the value's equality key."""
    return [
        (element, equality_key(spot.normalized.value, money))
        for element, text in short_texts(region, chrome)
        for spot in spots(text, money, ctx, "", "")
        if shown(element)
    ]


def _up(node: LexborNode, region: LexborNode) -> Iterator[LexborNode]:
    """The node and its ancestors below the region (by identity: `==` compares serialized markup)."""
    current: LexborNode | None = node
    while current is not None and current.mem_id != region.mem_id:
        yield current
        current = current.parent


def _targets(region: LexborNode, ctx: Context) -> dict[int, set[str]]:
    """The other documents the hyperlinks each element of the region is or holds lead to (`Links`), for the elements
    that hold one."""
    links = Links.of(region, ctx)
    held: dict[int, set[str]] = {}
    for a in region.css(A_HREF):
        if (target := links.targets.get(a.mem_id)) is not None:
            for node in _up(a, region):
                held.setdefault(node.mem_id, set()).add(target)
    return held


def _product_items(page: ParsedPage) -> list[LexborNode]:
    """The page's schema.org Product items outside other items' content (`OTHER_ITEMS`), nested ones included."""
    others = inside(page.tree.css(OTHER_ITEMS))
    return [
        element
        for element in page.tree.css("[itemscope][itemtype]")
        if element.mem_id not in others
        and any(schema_term(token) == _PRODUCT for token in (element.attributes.get("itemtype") or "").split())
    ]


def _holds(outer: LexborNode, inner: LexborNode) -> bool:
    """Whether `inner` is `outer` or inside it (by identity: `==` compares serialized markup)."""
    node: LexborNode | None = inner
    while node is not None:
        if node.mem_id == outer.mem_id:
            return True
        node = node.parent
    return False


def _own_text(node: LexborNode) -> str:
    """The text the element holds itself, not its children's: a struck or hidden child's amount is not the parent's."""
    return collapse(node.text(deep=False, separator=" "))


def short_texts(region: LexborNode, excluded: frozenset[int]) -> list[tuple[LexborNode, str]]:
    """The region's elements with the short text each holds itself, outside the `excluded` elements."""
    texts = ((element, _own_text(element)) for element in (region, *region.css("*")) if element.mem_id not in excluded)
    return [(element, text) for element, text in texts if text and len(text) <= SPOT_LENGTH]


def inside(roots: Iterable[LexborNode]) -> frozenset[int]:
    """The elements `roots` are and hold."""
    return frozenset(node.mem_id for root in roots for node in (root, *root.css("*")))


def other_items(region: LexborNode, links: Links) -> Iterator[LexborNode]:
    """The region's elements holding items besides the page's own: what schema.org microdata marks as a related or
    similar item or a review (`itemprop` `isRelatedTo`, `isSimilarTo`, `review`; a `Review` item), and sibling blocks
    of one tag (two or more) that each link to one other document, a different one each: a list of other documents'
    cards (related products), whose values are theirs (HTML Standard §4.6.1: a hyperlink leads to its document)."""
    yield from region.css(OTHER_ITEMS)
    for parent in (region, *region.css("*")):
        groups: dict[str, list[LexborNode]] = {}
        for child in children(parent):
            groups.setdefault(child.tag or "", []).append(child)
        for group in groups.values():
            if len(group) >= MIN_REPEATS and _cards(group, links):
                yield from group


def _cards(group: Sequence[LexborNode], links: Links) -> bool:
    targets = [links.of_block(block) for block in group]
    return all(len(t) == 1 for t in targets) and len(set().union(*targets)) == len(group)
