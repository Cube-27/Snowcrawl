"""Preference: which of a field's candidates publishes.

A candidate is kept when it normalized (validity), belongs to the target (ownership, §4.6: scope match 0 otherwise) and
is not a value taken from the request URL's query; every other one is rejected with its reason. The kept
candidates are scored (`resolve.score`: source weight times scope match times agreement) and the value of the best score
publishes; the candidates of every other value are rejected `outscored`. A field whose kept candidates state more than
one value publishes with a reason, the disagreement flag (`disagree`, `offers_disagree`, `offer_split`). Where the best
score is shared by two values the field is a `conflict`. A list of the item's facts (a job's places) publishes every
member its values state instead (`_union`): two sources stating different members are not rivals. A price is a
`conflict` too when amounts differ and no role explains it (`_price_winner`: the visible price, the shown variant);
amounts that differ only in currency publish, the currency unknown (C7: an unclear currency never drops a price).

Offers (`resolve.offers`): the anchor Offer is the one of the variant the target shows (its
own sku or gtin is that variant's), else the one holding values for the most offer fields; its values and the target's
own have scope 1, another Offer's `OTHER_OFFER`. Availability and the other offer fields but money are a variant's:
where the Offers disagree, the target takes only the shown variant's value, else the field is `offers_disagree`; a
field only another Offer than the anchor states is `offer_split`. An identifier field the target states nothing for
takes the value every variant states alike (variant fill).

Evidence buckets, confidence and min_confidence are the next stage's (`resolve.evidence`); this module only chooses.
"""

import json
import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Final, Literal, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import JsonValue

from snowcrawl.collect.base import PAGE, Candidate, Entity, is_visible_amount
from snowcrawl.collect.json_doc import scalars
from snowcrawl.db.models import SourceKind
from snowcrawl.normalize import images, money
from snowcrawl.normalize.base import Ambiguous, Invalid, Ok, Raw, raw_key
from snowcrawl.normalize.dispatch import with_member
from snowcrawl.normalize.text import text_form
from snowcrawl.resolve.offers import VARIANT_IDS, PageOffers, TargetOffers, source_of, variant_ids
from snowcrawl.resolve.ownership import EntityGraph
from snowcrawl.resolve.score import (
    OTHER_OFFER,
    OWN,
    PRECEDENCE_TYPES,
    Group,
    Scope,
    rank,
    rule_type,
    same_value,
    value_of,
)
from snowcrawl.schemas.paths import price_role
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef

__all__ = ["PRECEDENCE_TYPES", "Chosen", "PageFacts", "Preference", "Resolver", "Unpublished", "rule_type"]

# The screening stages in the order a candidate meets them; a missing field states the furthest one reached.
# §8.2 rule 4: a value the request URL carries that the page does not show.
URL_PARAM: Final = "url_param"
_STAGES = ("invalid", "ambiguous", "not_owned", URL_PARAM, "range_bound")
# Why a field publishes with a flag, or is a conflict.
DISAGREE = "disagree"
OFFERS_DISAGREE = "offers_disagree"
OFFER_SPLIT = "offer_split"
OUTSCORED = "outscored"

Orders = Mapping[FieldType, tuple[SourceKind, ...]]  # per precedence type, the source kinds, first weighs most
Rejection = tuple[Candidate, str]  # a candidate that does not publish, with its reason
_Bound = tuple[Decimal, str | None]  # a range bound's amount and the range's currency (`None`: unknown)


@dataclass(frozen=True, slots=True)
class Chosen:
    """The field publishes `candidate`'s normalized value. `agreeing` are the other kept candidates of its value;
    `kept` are all the candidates that passed screening, `candidate` included. `flag`: why a published field's kept
    candidates did not all agree (`disagree`, `offers_disagree`, `offer_split`), else None."""

    candidate: Candidate
    agreeing: tuple[Candidate, ...]
    rejected: tuple[Rejection, ...]
    kept: tuple[Candidate, ...]
    flag: str | None = None


@dataclass(frozen=True, slots=True)
class Unpublished:
    status: Literal["missing", "conflict"]
    reason: str
    rejected: tuple[Rejection, ...]
    kept: tuple[Candidate, ...] = ()  # the candidates that passed screening and still did not publish


Preference = Chosen | Unpublished


@dataclass(frozen=True, slots=True)
class PageFacts:
    """What the URL-parameter rule reads of the page: the request URL's query values, and whether a value is shown;
    and the request URL, which may name the variant the page shows."""

    query_values: frozenset[str]
    visible: Callable[[str], bool]
    request_url: str

    @classmethod
    def of(cls, request_url: str, visible: Callable[[str], bool]) -> "PageFacts":
        # WHATWG URL Standard, application/x-www-form-urlencoded parsing: `+` is a space, triplets decode.
        pairs = parse_qsl(urlsplit(request_url).query)
        return cls(frozenset(form for _, value in pairs if (form := text_form(value))), visible, request_url)


@dataclass(frozen=True, slots=True)
class Resolver:
    """Preference for one entity, `target`: a detail page's target, a listing's page or one of its rows.
        Its fields' candidates are screened, then scored. `page_values`: whether page-level candidates are the target's
        (`EntityGraph.owned_by`). `owns` stands in for ownership where the owner is not one entity: a variant's rows.
    `detail`: a detail page's target, which reads the variant it shows and fills from its variants;
        `variant`: one variant's row, whose candidates are all that variant's.
        `confident`: whether a chosen value of the named field would publish at the request's `min_confidence`
        (`resolve.evidence`); None: every one would."""

    graph: EntityGraph
    target: Entity
    page: PageFacts
    orders: Orders
    page_values: bool = True
    detail: bool = False
    owns: Callable[[Candidate], bool] | None = None
    variant: bool = False  # one variant's row: every candidate it owns is that variant's
    confident: Callable[[str, FieldSpec, "Chosen"], bool] | None = None
    target_offers: TargetOffers = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_offers", TargetOffers(self.graph, self.target))

    def prefer(self, schema: SchemaDef, candidates: Sequence[Candidate]) -> dict[str, Preference]:
        return self.prefer_fields(schema.fields, candidates)

    def prefer_fields(
        self, fields: Mapping[str, FieldSpec], candidates: Sequence[Candidate], *, prefix: str = ""
    ) -> dict[str, Preference]:
        """Every field's preference; a candidate is a field's when its field is `prefix` and the name (a row-mapped
        field's sub-fields are `<field>.<sub-field>`)."""
        by_field = {name: [c for c in candidates if c.field == f"{prefix}{name}"] for name in fields}
        screened = {name: self.screen(by_field[name]) for name in fields}
        screened = {name: self._variants_yield(fields[name], *found) for name, found in screened.items()}
        # In schema order: the anchor takes the first Offer seen on a tie.
        offer_fields = [name for name, spec in fields.items() if self.target_offers.is_offer_field(spec)]
        offers = self._page_offers(fields, candidates, screened, offer_fields)
        screened, offers = self._bounded(fields, candidates, screened, offer_fields, offers)
        chosen = {
            name: self._choose(spec, *screened[name], offers=offers, offer_field=name in offer_fields)
            for name, spec in fields.items()
        }
        return {name: self._fill(spec, by_field[name], chosen[name], offers) for name, spec in fields.items()}

    def prefer_field(self, spec: FieldSpec, candidates: Sequence[Candidate]) -> Preference:
        """One field on its own, outside the page's Offers: for values derived after preference (`derive`)."""
        return self._choose(spec, *self.screen(candidates), offers=PageOffers(), offer_field=False)

    def screen(self, candidates: Sequence[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
        """The candidates kept by validity, ownership and the URL-parameter rule, and the others with their reason."""
        kept: list[Candidate] = []
        rejected: list[Rejection] = []
        for candidate in candidates:
            reason = self._reject_reason(candidate)
            if reason is None:
                kept.append(candidate)
            else:
                rejected.append((candidate, reason))
        return kept, rejected

    def _reject_reason(self, candidate: Candidate) -> str | None:
        if isinstance(candidate.normalized, Invalid):
            return "invalid"
        if isinstance(candidate.normalized, Ambiguous):
            return "ambiguous"
        if not self._owned(candidate):
            return "not_owned"
        # §8.2 rule 4: a value the request URL carries is not evidence about the page unless the page shows it.
        if any(text_form(s) in self.page.query_values and not self.page.visible(s) for s in _scalars(candidate.raw)):
            return URL_PARAM
        return None

    def _owned(self, candidate: Candidate) -> bool:
        if self.owns is not None:
            return self.owns(candidate)
        return self.graph.owned_by(candidate, self.target, page_values=self.page_values)

    def _choose(
        self,
        spec: FieldSpec,
        kept: list[Candidate],
        rejected: list[Rejection],
        *,
        offers: PageOffers,
        offer_field: bool,
    ) -> Preference:
        if not kept:
            return Unpublished("missing", _missing_reason(rejected), tuple(rejected))
        groups = rank(spec, kept, self._scope(offers), self.orders.get(rule_type(spec), ()))
        if groups[0].score <= 0:  # every kind outside the fitted order, or a fragment (resolve.score)
            return _all_rejected("missing", "unranked", kept, rejected)
        if not offer_field and _members(spec):
            return _union(spec, [g for g in groups if g.score > 0], kept, rejected)
        return self._settled(spec, groups, kept, rejected, offers=offers, offer_field=offer_field)

    def _settled(
        self,
        spec: FieldSpec,
        groups: list[Group],
        kept: list[Candidate],
        rejected: list[Rejection],
        *,
        offers: PageOffers,
        offer_field: bool,
    ) -> Preference:
        """The value that publishes of the ranked `groups` (a scored best one), or the field's conflict."""
        top = groups[0]
        if rule_type(spec) is FieldType.MONEY:
            won = self._price_winner(spec, top, groups, offers, offer_field=offer_field)
            if isinstance(won, str):
                return _all_rejected("conflict", won, kept, rejected)
            top = won
        elif offer_field:
            top = self.target_offers.shown_value([g for g in groups if g.score > 0], offers) or top
        top, rivals, tied = _contest(spec, groups, top, kept)
        flag = self._flag(top, groups, rivals, offers, offer_field=offer_field)
        if rule_type(spec) is FieldType.MONEY:
            return _price(spec, top, tied, kept, rejected, flag)
        refused = self._refused(top, tied, groups, offers, offer_field=offer_field)
        if refused is not None:
            return _all_rejected("conflict", refused, kept, rejected)
        return _published(spec, top, kept, rejected, flag)

    def _price_winner(
        self, spec: FieldSpec, top: Group, groups: list[Group], offers: PageOffers, *, offer_field: bool
    ) -> Group | str:
        """The group whose amount publishes as the price, or why none does (`conflict`). Amounts that differ are
        rival prices, and each is explained by its role before the field is refused:
        - the visible price: the one amount a rendered `dom` value states beats the structured, meta or data-call
          amounts (stale markup is the common cause; HTML Standard: struck `s`/`del` text is already no value);
        - the shown variant: its Offer's amount beats the amounts of other Offers (other variants or sellers);
        - a list or was price, a unit, member, installment or bundle price are not candidates of this field: the
          price path's predicates leave out struck, list, member and unit price specifications (`schemas.paths`), the
          DOM collectors skip struck text and amounts written per unit or per period (`normalize.units`), and an
          amount bounding a stated price range is rejected `range_bound` (`_without_bounds`).
        An amount two candidates of one source role state for the same variant, with nothing explaining it, is the
        product's price by no evidence: `conflict` (owner: accuracy before coverage). Amounts that differ only in
        currency are not rivals (C7: the currency is unknown, not the price)."""
        contenders = [top, *(g for g in groups[1:] if g.score > 0 and not _same_amount(top, g))]
        visible = [g for g in contenders if any(is_visible_amount(c, spec) for c in g.members)]
        if len(contenders) > 1 and len(visible) == 1:
            return visible[0]
        if (shown := self.target_offers.shown_value(contenders, offers)) is not None:
            return shown  # the shown variant's Offer states its own price, however the others score
        if offer_field and self.target_offers.split(top, offers):  # only another Offer than the anchor states it
            return OFFER_SPLIT
        if len(contenders) == 1:
            return top
        return OFFERS_DISAGREE if offer_field and self.target_offers.offers_disagree(groups) else DISAGREE

    def _refused(
        self, top: Group, tied: list[Group], groups: list[Group], offers: PageOffers, *, offer_field: bool
    ) -> str | None:
        """Why the best value of a field other than money does not publish: Offers that disagree on a variant's value
        when it is not the shown variant's (`offers_disagree`), a value only another Offer than the anchor states
        (`offer_split`), or a best score two values share (`disagree`, C7: decline rather than guess)."""
        if offer_field and self.target_offers.offers_disagree(groups):
            return None if self.target_offers.shown_value([top, *tied], offers) is top else OFFERS_DISAGREE
        if offer_field and self.target_offers.split(top, offers):
            return OFFER_SPLIT
        return DISAGREE if tied else None

    def _flag(
        self, top: Group, groups: list[Group], rivals: list[Group], offers: PageOffers, *, offer_field: bool
    ) -> str | None:
        """The flag of a published value: its Offers disagree (the shown variant's value publishes all the same), only
        another Offer than the anchor states it (a price), or its other candidates state other values."""
        if offer_field and self.target_offers.offers_disagree(groups):
            return OFFERS_DISAGREE
        if offer_field and self.target_offers.split(top, offers):
            return OFFER_SPLIT
        return DISAGREE if rivals else None

    def _scope(self, offers: PageOffers) -> Scope:
        """Scope match (`resolve.score`): 1 for the target's own values and the anchor Offer's, `OTHER_OFFER` for
        another Offer's."""

        def scope(candidate: Candidate) -> Fraction:
            offer = self.target_offers.offer_of(candidate)
            return OWN if not offers.anchor or offer is None or offer in offers.anchor else OTHER_OFFER

        return scope

    def _bounded(
        self,
        fields: Mapping[str, FieldSpec],
        candidates: Sequence[Candidate],
        screened: dict[str, tuple[list[Candidate], list[Rejection]]],
        offer_fields: Sequence[str],
        offers: PageOffers,
    ) -> tuple[dict[str, tuple[list[Candidate], list[Rejection]]], PageOffers]:
        """The screened candidates without the active prices' range bounds (`_without_bounds`), and the page's Offers
        read again from what is left: a bound is no Offer's own value, so the anchor is the Offer holding the most of
        the rest."""
        bounds = _range_bounds(fields, screened) | self._stated_ranges()
        if not bounds:
            return screened, offers
        screened = {
            name: self._without_bounds(fields[name], *found, bounds, offers)
            if price_role(fields[name]) == "price"
            else found
            for name, found in screened.items()
        }
        return screened, self._page_offers(fields, candidates, screened, offer_fields)

    def _stated_ranges(self) -> frozenset[_Bound]:
        """The bounds of the price ranges the target's AggregateOffers state (`Entity.price_range`), each with its
        range's currency: a platform answer's `price_min` and `price_max` as well as JSON-LD's `lowPrice` and
        `highPrice`."""
        return frozenset(
            (bound, currency)
            for e in self.graph.entities.values()
            if e.price_range is not None and self.graph.offered(e).id == self.target.id
            for low, high, currency in (e.price_range,)
            for bound in (low, high)
        )

    def _without_bounds(
        self,
        spec: FieldSpec,
        kept: list[Candidate],
        rejected: list[Rejection],
        bounds: frozenset[_Bound],
        offers: PageOffers,
    ) -> tuple[list[Candidate], list[Rejection]]:
        """The price's candidates with the amounts that are a stated range's bound (`_range_bounds`, `_stated_ranges`)
        rejected `range_bound`: a visible one always ("From $80" beside
        `lowPrice` 80 states the range, not the price), a structured, meta or data-call one while another amount
        stands for the shown variant (`_shown_amount`), so that a variant that is the cheapest keeps its price. An
        amount in another currency than the range's is no bound; a currency either leaves unknown may be."""
        bound = [c for c in kept if _bounds_it(money.amount_and_currency(value_of(c)), bounds)]
        rival = any(self._shown_amount(c, offers) for c in kept if c not in bound)
        dropped = [c for c in bound if rival or is_visible_amount(c, spec)]
        return [c for c in kept if c not in dropped], [*rejected, *((c, RANGE_BOUND) for c in dropped)]

    def _shown_amount(self, candidate: Candidate, offers: PageOffers) -> bool:
        """Whether the candidate states the price of the variant the target shows: any of one variant's row (`variant`);
        on a detail page whose shown variant is known, one no Offer outside its Offers states."""
        if self.variant:
            return True
        offer = self.target_offers.offer_of(candidate)
        return self.detail and bool(offers.shown) and (offer is None or offer in offers.shown)

    def _page_offers(
        self,
        fields: Mapping[str, FieldSpec],
        candidates: Sequence[Candidate],
        screened: Mapping[str, tuple[list[Candidate], list[Rejection]]],
        offer_fields: Sequence[str],
    ) -> PageOffers:
        """The anchor Offer: on a detail page the Offers of the variant the target shows, else the one holding values
        for the most offer fields."""
        anchor = self.target_offers.anchor({name: screened[name][0] for name in offer_fields})
        offers = PageOffers(frozenset(() if anchor is None else (anchor,)))
        if not self.detail:
            return offers
        # The target's own sku and gtin, as they publish, select the variant it shows, and so does the
        # shown variant's that yielded to the product's own (`_variants_yield`): it still names that variant; so does
        # the request URL where it is one variant's own.
        ids = [name for name in variant_ids(fields) if name in fields]
        stated = {name: self._choose(fields[name], *screened[name], offers=offers, offer_field=False) for name in ids}
        yielded = [(c, fields[name]) for name in ids for c, why in screened[name][1] if why == OUTSCORED]
        own = [*self._published_ids(fields, stated), *yielded]
        shown, other = self.target_offers.shown_offers(fields, candidates, own, self.page.request_url)
        shown |= self.target_offers.lone_offers(frozenset(source_of(offer) for offer in shown)) - other
        return PageOffers(shown, shown) if shown else offers

    def _published_ids(
        self, fields: Mapping[str, FieldSpec], stated: Mapping[str, Preference]
    ) -> list[tuple[Candidate, FieldSpec]]:
        """The sku and gtin values the target publishes as its own: chosen, its own or the page's (not an Offer's),
        and confident at the request's `min_confidence`."""
        return [
            (decision.candidate, fields[name])
            for name, decision in stated.items()
            if isinstance(decision, Chosen)
            and self.graph.owner_of(decision.candidate).id in (self.target.id, PAGE)
            and (self.confident is None or self.confident(name, fields[name], decision))
        ]

    def _variants_yield(
        self, spec: FieldSpec, kept: list[Candidate], rejected: list[Rejection]
    ) -> tuple[list[Candidate], list[Rejection]]:
        """Variant fill: a gtin or sku the target holds from inside the node of one of its Offers or
        variants (a platform answer stating the shown variant's identifiers as the product's, `adapters.platforms`) is
        that variant's, and stands for the product only where nothing else the target owns states the field: a value
        the product states itself is the product's (GS1 GTIN and a seller's SKU identify one variant), so the
        variant's other value is `outscored`, not its rival, and so is a page state value admitted only by equality
        to it."""
        if not self._fills(spec):
            return kept, rejected
        nodes = tuple(e.id for e in (*self.graph.offers(self.target), *self.graph.variants(self.target)))
        held = [c for c in kept if any(_inside(c.locator, node) for node in nodes)]
        stated = [c for c in kept if c.independent and not any(c is h for h in held)]
        if not held or not stated:
            return kept, rejected
        lower = [c for c in kept if not any(same_value(c, s, spec) for s in stated)]
        return [c for c in kept if not any(c is low for low in lower)], [*rejected, *((c, OUTSCORED) for c in lower)]

    def _fills(self, spec: FieldSpec) -> bool:
        return self.detail and spec.type is FieldType.IDENTIFIER and spec.scheme in VARIANT_IDS

    def _fill(
        self, spec: FieldSpec, candidates: Sequence[Candidate], preference: Preference, offers: PageOffers
    ) -> Preference:
        """Variant fill: a gtin or sku field the target states nothing for (no candidate is the
        target's) takes its variants' candidates when every variant has one and all agree: the value is common to
        the variants; else those of the one variant the target shows (`offers.shown`), whose identifiers they are.
        A value the target states is never replaced, and no other field is filled."""
        if not self._fills(spec):
            return preference
        if any(self.graph.owned_by(c, self.target, page_values=self.page_values) for c in candidates):
            return preference
        kept = self._variants_value(spec, candidates, offers)
        if kept is None:
            return preference
        rejected = tuple(r for r in preference.rejected if not any(r[0] is c for c in kept))
        return Chosen(kept[0], tuple(kept[1:]), rejected, tuple(kept))

    def _variants_value(
        self, spec: FieldSpec, candidates: Sequence[Candidate], offers: PageOffers
    ) -> list[Candidate] | None:
        """The variants' candidates of one value when every variant has one and all agree, else the shown variant's
        when they agree (`_fill`); None otherwise."""
        members = self.graph.variants(self.target)
        pooled = [self._variant_kept(member, candidates) for member in members]
        kept = [c for member_kept in pooled for c in member_kept]
        if pooled and all(pooled) and _one_value(spec, kept):
            return kept
        shown = [found for member, found in zip(members, pooled, strict=True) if self._shows(member, offers)]
        return shown[0] if len(shown) == 1 and shown[0] and _one_value(spec, shown[0]) else None

    def _shows(self, member: Entity, offers: PageOffers) -> bool:
        """Whether the variant `member` is the one the target shows: its Offers are among the shown variant's."""
        own = {o.id for o in self.graph.offers(member)}
        return bool(own) and own <= offers.shown

    def _variant_kept(self, member: Entity, candidates: Sequence[Candidate]) -> list[Candidate]:
        """The candidates the variant `member` holds itself that pass screening, as they would for it as a target."""
        own = [c for c in candidates if c.entity_id == member.id]
        return Resolver(self.graph, member, self.page, self.orders, page_values=False).screen(own)[0]


def _one_value(spec: FieldSpec, candidates: Sequence[Candidate]) -> bool:
    return all(same_value(a, b, spec) for a, b in combinations(candidates, 2))


def intent_ambiguous(schema: SchemaDef, candidates: Sequence[Candidate]) -> dict[str, Preference]:
    """§4.6: with no single target, every field is a conflict and no candidate publishes."""
    return {
        name: Unpublished(
            "conflict", "intent_ambiguous", tuple((c, "intent_ambiguous") for c in candidates if c.field == name)
        )
        for name in schema.fields
    }


# schema.org AggregateOffer: `lowPrice` and `highPrice` state the range of its offers' prices.
_LOW_PRICE, _HIGH_PRICE = "lowPrice", "highPrice"
RANGE_BOUND = "range_bound"


def _bounds_it(shown: _Bound | None, bounds: frozenset[_Bound]) -> bool:
    if shown is None:
        return False
    amount, currency = shown
    return any(bound == amount and (None in (currency, ours) or currency == ours) for bound, ours in bounds)


def _range_bounds(
    fields: Mapping[str, FieldSpec], screened: Mapping[str, tuple[list[Candidate], list[Rejection]]]
) -> frozenset[_Bound]:
    """The amounts that bound a price range structured data states (an AggregateOffer's `lowPrice` and `highPrice`,
    kept for a range field) whose bounds differ, each with the range's currency: the page's prices span them, so
    neither one is the item's price."""
    ranges = (c for name, (kept, _) in screened.items() if fields[name].type is FieldType.RANGE for c in kept)
    return frozenset(bound for candidate in ranges for bound in _stated_bounds(candidate))


def _stated_bounds(candidate: Candidate) -> tuple[_Bound, ...]:
    """A price range candidate's bounds, when it is an AggregateOffer's and they differ."""
    value, raw = value_of(candidate), candidate.raw
    if not (isinstance(raw, dict) and _LOW_PRICE in raw and _HIGH_PRICE in raw and isinstance(value, dict)):
        return ()
    low, high = Decimal(str(value["min"])), Decimal(str(value["max"]))
    currency = value.get("currency")
    known = currency if isinstance(currency, str) else None
    bounds = {(low, known), (high, known)}
    return tuple(bounds) if len(bounds) > 1 else ()  # equal bounds state one price, no range


def _same_amount(a: Group, b: Group) -> bool:
    return money.same_amount([value_of(a.members[0]), value_of(b.members[0])])


def _price(
    spec: FieldSpec, top: Group, tied: list[Group], kept: list[Candidate], rejected: list[Rejection], flag: str | None
) -> Chosen:
    """A price no other amount contests publishes (`_price_winner`): of values sharing the best score, which state one
    amount, the first stated; in different currencies, the amount with its currency unknown (basis `unknown`: an
    unclear currency never drops a price, C7)."""
    chosen = _published(spec, top, kept, rejected, flag)
    if not tied or not money.same_amount([value_of(c) for g in (top, *tied) for c in g.best]):
        return chosen
    unknown = _currency_unknown(chosen.candidate)
    return Chosen(
        unknown,
        chosen.agreeing,
        chosen.rejected,
        tuple(unknown if c is chosen.candidate else c for c in kept),
        chosen.flag,
    )


def _published(
    spec: FieldSpec, top: Group, kept: list[Candidate], rejected: list[Rejection], flag: str | None
) -> Chosen:
    """The best group's best member publishes, the others of its value agree, and every other value is outscored."""
    chosen = _image_choice(top, spec)
    agreeing = tuple(c for c in top.members if c is not chosen)
    lower = [(c, OUTSCORED) for c in kept if not any(c is m for m in top.members)]
    return Chosen(chosen, agreeing, (*rejected, *lower), tuple(kept), flag)


# The item types whose lists hold members rather than one rival value per source (`_members`): a list of lists or of
# objects (variants) is a value as a whole, a list of images or links is settled by the files it names, and amounts
# are rival prices (`_price_winner`).
_WHOLE_ITEMS = frozenset({FieldType.LIST, FieldType.OBJECT, FieldType.URL, FieldType.IMAGE_URL, FieldType.MONEY})


def _members(spec: FieldSpec) -> bool:
    """Whether the field is a list of facts of the item, each one a member (a job's places): two sources stating
    different ones state more members of the item, not rival values."""
    return spec.type is FieldType.LIST and spec.item is not None and spec.item.type not in _WHOLE_ITEMS


def _union(spec: FieldSpec, groups: list[Group], kept: list[Candidate], rejected: list[Rejection]) -> Chosen:
    """The list of every member the field's ranked values state, best first (`dispatch.with_member`: one member for
    two statements of it), as the best group's candidate: no other value is outscored. One value publishes as is."""
    top = groups[0]
    if len(groups) == 1:
        return _published(spec, top, kept, rejected, None)
    item = cast(FieldSpec, spec.item)
    members: list[JsonValue] = []
    for group in groups:
        stated = value_of(group.best[0])
        for value in stated if isinstance(stated, list) else [stated]:
            members = with_member(members, value, item)
    chosen = top.best[0]
    union = _listing(chosen, members, [c for g in groups for c in g.best[:1]])
    agreeing = tuple(c for c in top.members if c is not chosen)
    return Chosen(union, agreeing, tuple(rejected), tuple(kept))


def _currency_unknown(candidate: Candidate) -> Candidate:
    """The candidate's amount with its currency unknown (basis `unknown`): its evidence is contradicted (C7)."""
    normalized = candidate.normalized
    checked = normalized.checked if isinstance(normalized, Ok) else False
    value = money.without_currency(value_of(candidate))
    return candidate.model_copy(update={"normalized": Ok(value, basis="unknown", checked=checked)})


# A locator of one item of a list (`…['image'][2]`): the list's own locator.
_LIST_ITEM = re.compile(r"(.+)\[\d{1,9}\]")


def _contest(
    spec: FieldSpec, groups: list[Group], top: Group, kept: Sequence[Candidate]
) -> tuple[Group, list[Group], list[Group]]:
    """The best group, the other scored groups (its rivals), and those that tie with it (`_first_listed`)."""
    rivals = [g for g in groups if g is not top and g.score > 0]
    tied = [g for g in rivals if g.score == top.score]
    first = _first_listed(spec, [top, *tied], kept)
    if first is None:
        return top, rivals, tied
    return first, [g for g in groups if g is not first and g.score > 0], []


def _first_listed(spec: FieldSpec, best: Sequence[Group], kept: Sequence[Candidate]) -> Group | None:
    """Of image values that tie for the best score, the group holding the first item when all are items of one list of
    one node: a source listing a thing's images in order (RFC 8259 §1: an array is an ordered sequence) states its main
    image first, as Open Graph prefers the first of a repeated `og:image` (Open Graph protocol, "Arrays"). The first is
    the one its collector reads first (`kept` is in collector order): a platform answer's images are read in the order
    the page of the shown variant shows them (`adapters.platforms`), whatever their places in the answer's array."""
    if spec.type is not FieldType.IMAGE_URL or len(best) < 2:
        return None
    place = {id(c): n for n, c in enumerate(kept)}
    items = [(g, c, _LIST_ITEM.fullmatch(c.locator)) for g in best for c in g.best]
    found = [(g, (c.entity_id, c.source_kind, m.group(1)), place[id(c)]) for g, c, m in items if m]
    if len(found) != len(items) or len({key for _, key, _ in found}) != 1:
        return None
    return min(found, key=lambda item: item[2])[0]


def _image_choice(top: Group, spec: FieldSpec) -> Candidate:
    """The member of the best group that publishes. For an image value, of the members of the group's score the secure
    one (`images.preferred_value`), with each file at the largest rendition any member states
    (`images.largest_of_each`): the members name the same files (`resource_key`), so the sources' weights settle which
    files the image is, and the rendition the page also states larger publishes, whichever source states it. A scalar
    publishes the member stating it; a list the chosen candidate with those renditions. Any other value is the first
    member of the score."""
    best = top.best
    if rule_type(spec) is not FieldType.IMAGE_URL:
        return best[0]
    chosen = best[images.preferred_value([value_of(c) for c in best], single=spec.type is FieldType.IMAGE_URL)]
    if len(top.members) == 1:
        return chosen
    members = [chosen, *(c for c in top.members if c is not chosen)]
    stated = [images.image_list(value_of(c)) for c in members]
    lists = [found for found in stated if found is not None]
    if len(lists) != len(members):
        return chosen
    largest = images.largest_of_each(lists[0], lists[1:])
    if spec.type is FieldType.IMAGE_URL:
        return next((c for c, found in zip(members, lists, strict=True) if found == largest), chosen)
    if largest == lists[0]:
        return chosen
    return _listing(chosen, list(largest), members)


def _listing(chosen: Candidate, items: list[JsonValue], sources: Sequence[Candidate]) -> Candidate:
    """`chosen` holding the list `items`, gathered from `sources`' values, each item published as one of them writes it
    (`Ok.stated`: a URL as the page writes it, never its resolved form): one writing it in full, else the first."""
    written: dict[Hashable, JsonValue] = {}
    for value, as_written in (pair for source in sources for pair in _writings(source)):
        # Of two writings of one item, the one in full (an absolute URL in canonical form) publishes.
        if as_written == value or raw_key(value) not in written:
            written[raw_key(value)] = as_written
    stated = [written.get(raw_key(item), item) for item in items]
    normalized = replace(cast(Ok, chosen.normalized), value=items, stated=None if stated == items else stated)
    return chosen.model_copy(update={"normalized": normalized})


def _writings(candidate: Candidate) -> list[tuple[JsonValue, JsonValue]]:
    """Each value the candidate states, an item of its list or its scalar, with its writing (`Ok.published`)."""
    if not isinstance(candidate.normalized, Ok):
        return []
    value, published = candidate.normalized.value, candidate.normalized.published
    if isinstance(value, list) and isinstance(published, list):
        return list(zip(value, published, strict=True))
    return [(value, published)]


def _all_rejected(
    status: Literal["missing", "conflict"], reason: str, kept: list[Candidate], rejected: list[Rejection]
) -> Unpublished:
    return Unpublished(status, reason, (*rejected, *((c, reason) for c in kept)), tuple(kept))


def _inside(locator: str, node: str) -> bool:
    """Whether a value's locator is a member path below the node `node` names (RFC 9535 JSONPath: a child segment
    `[...]` selects a member of the node the path before it selects)."""
    return locator.startswith(f"{node}[")


def _missing_reason(rejected: list[Rejection]) -> str:
    if not rejected:
        return "no_candidates"
    return max((reason for _, reason in rejected), key=_STAGES.index)


def _scalars(raw: Raw) -> Iterator[str]:
    """The raw value's strings and numbers as text: what could have been copied from a query parameter."""
    return (value if isinstance(value, str) else json.dumps(value) for _, value in scalars(raw))
