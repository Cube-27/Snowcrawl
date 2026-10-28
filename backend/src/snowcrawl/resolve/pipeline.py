"""The extraction pipeline: one capture and one schema in,
every field's outcome out. `extract_page` is the one function the worker and the replay harness run.

A page that was not served (`blocked`, `shell`, `not_found`, `error`) yields its block class as the outcome and nothing
else. A served page is parsed once; every collector reads that one parse, then resolution runs its
stages in order: ownership and intent, preference, derivations, evidence and confidence. A detail page's required fields
still missing after that go to role C once, and its candidates are resolved with the others. A listing's rows are its
stated rows (structured data's, `resolve.listing`, else the config's, else its captured calls' or its state JSON's,
picked once by `collect.row_sources`) and the blocks of its
repeated items, bound into one row per item by the item layer (`collect.items.bind`); each row resolves
its row-scoped fields, the page its page-scoped ones, and the next-page candidates are listed. Role C is asked
once for the row fields a listing's block rows lack and for its missing page fields.
Resolution never publishes part of a page: if it raises, the outcome is `error` and no field is set.

With the site's active config a page is extracted in `config` mode: the config's
executor reads the same parse, its candidates enter resolution with the collectors' (in the bucket of the config field's
status), and role C is asked only for fields the config does not run. A listing's rows are the structured
data's when it gives any, else the config's; role C never adds rows beside a config that runs row fields.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final, Protocol, cast

from pydantic import BaseModel, ConfigDict

from snowcrawl.collect.application import holds_application
from snowcrawl.collect.base import ARRAY_ROW, Candidate, Entity
from snowcrawl.collect.config import ConfigExecutor
from snowcrawl.collect.currencies import with_page_declarations
from snowcrawl.collect.items.bind import Listing, RowBlock, bind
from snowcrawl.collect.items.lines import BlockText, block_texts
from snowcrawl.collect.run import Adapters, Collected, collect_page
from snowcrawl.collect.run import page_urls as page_urls_of
from snowcrawl.collect.schema_org_map import canonical_urls, item_url_fields
from snowcrawl.collect.vocabulary import VOCABULARY_FILE, Vocabulary
from snowcrawl.content.context import page_context
from snowcrawl.content.metadata import canonical_href, canonical_link
from snowcrawl.content.page import ParsedPage
from snowcrawl.core.capture import BlockClass
from snowcrawl.core.engine_version import CALIBRATION_FILE
from snowcrawl.db.models import ExtractionMode, SourceKind
from snowcrawl.normalize.base import Context
from snowcrawl.resolve.derive import Derived, fill, is_application_field, is_page_url_field
from snowcrawl.resolve.evidence import Calibration, assess, assess_fields, load_calibration
from snowcrawl.resolve.intent import AmbiguousTarget, primary_types, select_target, sole_primary
from snowcrawl.resolve.listing import listing_rows, row_lists
from snowcrawl.resolve.main_image import without_main_image
from snowcrawl.resolve.ownership import EntityGraph
from snowcrawl.resolve.page_state import out_of_stock, render_bound, was_rendered
from snowcrawl.resolve.pagination import next_page_candidate, next_page_links
from snowcrawl.resolve.preference import (
    PRECEDENCE_TYPES,
    Chosen,
    Orders,
    PageFacts,
    Preference,
    Resolver,
    intent_ambiguous,
)
from snowcrawl.resolve.records import is_complete, record_confidence
from snowcrawl.resolve.row_keys import keyed_rows
from snowcrawl.resolve.rows import RowField, resolve_rows
from snowcrawl.resolve.score import PRECEDENCE_FILE, load_precedence
from snowcrawl.resolve.types import ExtractionResult, FieldSet, LLMCallRecord, Outcome, Probability
from snowcrawl.schemas.types import FieldSpec, FieldType, SchemaDef, Scope
from snowcrawl.storage.capture_bundle import CaptureBundle

log = logging.getLogger(__name__)
_FAILED = "extraction failed"
# The reason of a `missing` field the active config lists as absent (`config_ir.absent`).
ABSENT: Final = "absent"
# The reason of a `missing` field role C claimed a value for that the page's evidence does not hold.
UNVERIFIED: Final = "unverified"

# Without a `source_precedence.yaml` no source kind is ranked, so text, long_text, url and image_url fields
# decline as `unranked` rather than follow an unmeasured order (C7). The repo commits the order.
UNRANKED: Orders = dict.fromkeys(PRECEDENCE_TYPES, ())
# A capture whose block class is not `ok` is the page's outcome, with no candidates.
_NOT_SERVED: Mapping[BlockClass, Outcome] = {
    "blocked": "blocked",
    "shell": "shell",
    "not_found": "not_found",
    "auth_wall": "auth_wall",
    "soft_404": "soft_404",
    "geo_redirect": "geo_redirect",
    "redirected_to_listing": "redirected_to_listing",
    "unsupported_media": "unsupported_media",
    "error": "error",
}


class PinnedData(BaseModel):
    """What a job pins of `EngineData` when it is submitted: the precedence orders and the calibration. Its
    pages resolve with these, whatever the files of the process that runs them say by then."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    orders: dict[FieldType, tuple[SourceKind, ...]]
    calibration: Calibration | None


@dataclass(frozen=True, slots=True)
class EngineData:
    """What resolution reads besides the page: the schema.org vocabulary, the dev-fitted precedence orders and
    calibration, the active adapters, and the request's `min_confidence`: a calibrated bucket below it abstains
    (None: every chosen value publishes with its confidence)."""

    vocabulary: Vocabulary
    orders: Orders
    calibration: Calibration | None
    adapters: Adapters | None = None
    min_confidence: Probability | None = None

    @classmethod
    def load(cls, data_dir: Path, adapters: Adapters | None = None) -> "EngineData":
        """The data files under `data_dir`. The vocabulary is required; without a fitted precedence file every type is
        `UNRANKED`, and without a calibration every field is uncalibrated (`core.engine_version`: `cal.none`)."""
        precedence = data_dir / PRECEDENCE_FILE
        return cls(
            vocabulary=Vocabulary.read(data_dir / VOCABULARY_FILE),
            orders=load_precedence(precedence) if precedence.is_file() else UNRANKED,
            calibration=load_calibration(data_dir / CALIBRATION_FILE),
            adapters=adapters,
        )

    def with_min_confidence(self, min_confidence: Probability | None) -> "EngineData":
        """This data for a request that asks values of at least `min_confidence` (None: no minimum)."""
        return replace(self, min_confidence=min_confidence)

    def pin(self) -> PinnedData:
        return PinnedData(orders=dict(self.orders), calibration=self.calibration)

    def pinned(self, pin: PinnedData) -> "EngineData":
        """This data with `pin`'s orders and calibration in place of its own."""
        return replace(self, orders=pin.orders, calibration=pin.calibration)


@dataclass(frozen=True, slots=True)
class GapRequest:
    """The fields of `target` that resolution left missing, for role C to read from the page text, without the product's
    own container's `outside` (`collect.product_scope.product_apart`: other items' cards)."""

    page: ParsedPage
    schema: SchemaDef
    ctx: Context
    target: Entity
    missing: tuple[str, ...]
    outside: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class GapReply:
    """Role C's grounded candidates (`source_kind='llm'`, owned by the target) and the calls it made. `failure` is
    set when its call gave no valid reply: the asked fields then stay `missing` with it as their
    reason. `diagnostics` join the page's (values dropped as ungrounded, truncation); `unverified` are the fields
    whose claimed value the page's evidence does not hold, which stay `missing` with reason `UNVERIFIED`."""

    candidates: tuple[Candidate, ...]
    llm_calls: tuple[LLMCallRecord, ...]
    failure: str | None = None
    diagnostics: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListingGapRequest:
    """Listings spec, "Role C rows": the `blocks` whose rows lack row fields (each with the fields to fill), and the
    page entity's `page_missing` fields, for role C to read from the page text."""

    page: ParsedPage
    schema: SchemaDef
    ctx: Context
    blocks: tuple[BlockText, ...]
    page_missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ListingGapReply(GapReply):
    """A listing's `GapReply`: `candidates` are owned by the blocks' rows or by the page entity. `failure`, the page
    fields' call's, leaves the asked page fields `missing` with it."""


class RoleC(Protocol):
    """The LLM hook, filled by role C; the caller binds the LLM client."""

    async def detail(self, request: GapRequest) -> GapReply: ...

    async def listing(self, request: ListingGapRequest) -> ListingGapReply: ...


async def extract_page(
    bundle: CaptureBundle,
    schema: SchemaDef,
    config: ConfigExecutor | None,
    role_c: RoleC | None,
    *,
    data: EngineData,
    parsed: ParsedPage | None = None,
) -> ExtractionResult:
    """The page's outcome, fields and candidates: in `config` mode with `config`, the site's active config for the
    schema and page kind, else `adhoc` (cold). `extracted` when every required field and group publishes (on a
    listing: in each of at least one row), else `partial`: whether an incomplete page or row keeps its record is the
    caller's (`resolve.records.assemble`, `assemble_listing`). `parsed` is the bundle's parse when the caller already
    has one (a `scrape` shares it with its formats)."""
    mode: ExtractionMode = "adhoc" if config is None else "config"
    not_served = _NOT_SERVED.get(bundle.manifest.block_class)
    if not_served is not None:
        return _result(not_served, mode)
    source = _Source(bundle, schema, data, parsed, config)
    if schema.page_kind == "listing":
        return await _extract_listing(source, role_c, mode)
    try:
        # Parsing and resolution are CPU work: a thread keeps the event loop serving other tasks meanwhile.
        page, fields = await asyncio.to_thread(_read_and_resolve, source)
    except Exception:  # §4.14: a resolution bug fails the page, never part of it
        log.exception(_FAILED)
        return _result("error", mode)
    candidates, diagnostics = page.candidates, page.diagnostics
    llm_calls: tuple[LLMCallRecord, ...] = ()
    missing = _missing(schema, fields, page.config.covered)
    if role_c is not None and missing and isinstance(page.target, Entity):
        request = GapRequest(page.page, schema, page.ctx, page.target, missing, page.collected.outside)
        reply = await role_c.detail(request)
        candidates, llm_calls = (*candidates, *reply.candidates), reply.llm_calls
        diagnostics = (*diagnostics, *reply.diagnostics)
        if reply.candidates:  # without new candidates, resolution would decide the same
            try:
                fields = await asyncio.to_thread(page.resolve, candidates)
            except Exception:
                log.exception(_FAILED)
                return _result("error", mode, candidates=candidates, llm_calls=llm_calls, diagnostics=diagnostics)
        fields = _finish(source, page.config.absent, fields, reply, missing)
    else:
        fields = _finish(source, page.config.absent, fields)
    return ExtractionResult(
        outcome="extracted" if is_complete(schema, fields) else "partial",
        entity=fields,
        rows=(),
        candidates=candidates,
        llm_calls=llm_calls,
        mode=mode,
        record_confidence=record_confidence(schema, fields),
        diagnostics=diagnostics,
    )


@dataclass(frozen=True, slots=True)
class _Source:
    """What a page is extracted from: the capture, its parse when the caller has one, and the active config."""

    bundle: CaptureBundle
    schema: SchemaDef
    data: EngineData
    parsed: ParsedPage | None
    config: ConfigExecutor | None


@dataclass(frozen=True, slots=True)
class _Config:
    """What the active config proposed for the page. `covered`: the fields it runs or lists as absent, which role C is
    not asked for; `corroborated`: those whose values take the `config_corroborated` bucket;
    `absent`: those the site's pages do not state (`config_ir.absent`)."""

    rows: tuple[Entity, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    diagnostics: tuple[str, ...] = ()
    covered: frozenset[str] = frozenset()
    corroborated: frozenset[str] = frozenset()
    absent: frozenset[str] = frozenset()

    @classmethod
    def run(cls, config: ConfigExecutor | None, page: ParsedPage, schema: SchemaDef, ctx: Context) -> "_Config":
        if config is None:
            return cls()
        executed = config.execute(page, schema, ctx)
        status = config.field_status
        return cls(
            rows=executed.rows,
            candidates=executed.candidates,
            diagnostics=executed.diagnostics,
            covered=frozenset(status),
            corroborated=frozenset(name for name, s in status.items() if s == "corroborated"),
            absent=frozenset(name for name, s in status.items() if s == "absent"),
        )


@dataclass(frozen=True, slots=True)
class _Page:
    """One served page, parsed once, with its context, every collector's result and the config's, and what
    resolution reads of it whatever the candidates: the entity graph, the target and the URL-parameter facts."""

    page: ParsedPage
    ctx: Context
    schema: SchemaDef
    data: EngineData
    canonical_url: str | None
    canonical_href: str | None  # the canonical link's href as the page writes it (`content.metadata.canonical_href`)
    collected: Collected
    config: _Config
    graph: EntityGraph
    primary: frozenset[str]
    target: Entity | AmbiguousTarget
    facts: PageFacts
    page_urls: tuple[str, ...]  # the URLs naming the page (`collect.run`'s `page_urls`), canonicalized
    # The page's final URL when it holds its posting's application form (`collect.application`), for the apply link.
    application_url: str | None

    @classmethod
    def read(cls, source: _Source) -> "_Page":
        """The page read from `source`; the one read already from the same parse for the same capture, schema, engine
        data and config, when there is one (`ParsedPage.derived`)."""
        page = ParsedPage.of(source.bundle) if source.parsed is None else source.parsed
        key = (source.bundle.manifest, source.schema, source.data, source.config)
        derived = page.derived.get(cls)
        if derived is not None and all(a is b for a, b in zip(derived[0], key, strict=True)):
            return cast(_Page, derived[1])
        read = cls._read(source, page)
        page.derived[cls] = (key, read)
        return read

    @classmethod
    def _read(cls, source: _Source, page: ParsedPage) -> "_Page":
        manifest, schema, data = source.bundle.manifest, source.schema, source.data
        ctx = page_context(
            page,
            manifest.final_url,
            manifest.fetched_at,
            request_url=manifest.url,
            page_url=manifest.page_url,
            headers=manifest.headers.response,
        )
        ctx = with_page_declarations(ctx, page)
        learned = None if source.config is None else source.config.learned_calls
        collected = collect_page(
            page, manifest, schema, ctx, vocabulary=data.vocabulary, adapters=data.adapters, learned=learned
        )
        config = _Config.run(source.config, page, schema, ctx)
        canonical = canonical_link(page, manifest.final_url)
        page_urls = canonical_urls(page_urls_of(page, manifest), ctx)
        graph = EntityGraph.of((*collected.entities, *config.rows), data.vocabulary, page_urls)
        # The candidates of an entity merged into another item's are that item's (`resolve.identity`).
        collected = replace(
            collected,
            candidates=graph.adopt(collected.candidates),
            diagnostics=(*collected.diagnostics, *graph.merges()),
        )
        primary = primary_types(schema, data.vocabulary)
        target = select_target(manifest.url, manifest.final_url, canonical, graph, primary)
        if schema.page_kind == "detail" and (sole := sole_primary(graph, target, primary)) is not None:
            collected = replace(collected, candidates=graph.ratings_apart(sole, collected.candidates))
        facts = PageFacts.of(manifest.url, page.visible_values.visible)
        href = canonical_href(page, manifest.final_url)
        applies = any(is_application_field(schema, spec) for spec in schema.fields.values())
        application = manifest.final_url if applies and holds_application(page, manifest.final_url) else None
        return cls(
            page,
            ctx,
            schema,
            data,
            canonical,
            href,
            collected,
            config,
            graph,
            primary,
            target,
            facts,
            page_urls,
            application,
        )

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return (*self.collected.candidates, *self.config.candidates)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return (*self.collected.diagnostics, *self.config.diagnostics)

    def resolve(self, candidates: Sequence[Candidate]) -> FieldSet:
        """A detail page's fields, for its target."""
        return self.resolve_for(self.target, candidates, page_values=True, detail=self.schema.page_kind == "detail")

    def resolve_listing(
        self, candidates: Sequence[Candidate], rows: Sequence[Entity]
    ) -> tuple[FieldSet, tuple[FieldSet, ...]]:
        """A listing's page-scoped fields, for the page entity, and each row's row-scoped fields."""
        page_fields = self.resolve_for(self.graph.page, candidates, page_values=True)
        return _scoped(self.schema, page_fields, "page"), tuple(
            _scoped(self.schema, self.resolve_for(row, candidates, page_values=False), "row") for row in rows
        )

    def resolve_for(
        self,
        target: Entity | AmbiguousTarget,
        candidates: Sequence[Candidate],
        *,
        page_values: bool,
        detail: bool = False,
    ) -> FieldSet:
        """Preference for `target`, derivations, then evidence and confidence. `page_values`: whether
        page-level candidates are the target's (`EntityGraph.owned_by`); `detail`: whether `target` is a detail page's,
        with the variant it shows, its variant fill and row-mapped fields."""
        schema = self.schema
        decisions: dict[str, Preference | Derived]
        rows: FieldSet = {}
        if isinstance(target, AmbiguousTarget):
            decisions = dict(intent_ambiguous(schema, candidates))
        else:
            resolver = Resolver(
                self.graph, target, self.facts, self.data.orders, page_values, detail=detail, confident=self._confident
            )
            preferences = resolver.prefer(schema, candidates)
            by_field: dict[str, list[Candidate]] = defaultdict(list)
            for candidate in candidates:
                by_field[candidate.field].append(candidate)
            decisions = {
                name: fill(
                    spec,
                    preferences[name],
                    by_field[name],
                    resolver,
                    self.ctx,
                    canonical_url=self.canonical_url,
                    canonical_href=self.canonical_href,
                    page_url_field=is_page_url_field(schema, spec, self.primary),
                    application_url=self.application_url if is_application_field(schema, spec) else None,
                )
                for name, spec in schema.fields.items()
            }
            if detail:
                rows = self._rows(target, candidates)
        return self._assess(decisions) | rows

    def _confident(self, name: str, spec: FieldSpec, chosen: Chosen) -> bool:
        """Whether a chosen value of the field publishes at the request's `min_confidence` (`resolve.evidence`)."""
        outcome = assess(
            spec.type,
            chosen,
            self.data.calibration,
            min_confidence=self.data.min_confidence,
            corroborated=name in self.config.corroborated,
        )
        return outcome.status == "published"

    def _assess(self, decisions: Mapping[str, Preference | Derived]) -> FieldSet:
        return assess_fields(
            self.schema,
            decisions,
            self.data.calibration,
            corroborated=self.config.corroborated,
            min_confidence=self.data.min_confidence,
        )

    def _rows(self, target: Entity, candidates: Sequence[Candidate]) -> FieldSet:
        """A detail target's row-mapped fields, resolved row by row."""
        return {
            name: resolve_rows(
                RowField(
                    name=name,
                    spec=spec,
                    graph=self.graph,
                    target=target,
                    page=self.facts,
                    orders=self.data.orders,
                    calibration=self.data.calibration,
                    min_confidence=self.data.min_confidence,
                    page_urls=self.page_urls,
                    variant_params=self.collected.variant_params,
                ),
                candidates,
            )
            for name, spec in self.schema.fields.items()
            if spec.row_mapped
        }


def _read_and_resolve(source: _Source) -> tuple[_Page, FieldSet]:
    page = _Page.read(source)
    return page, page.resolve(page.candidates)


# Why a listing gave no rows, as `no_rows: <cause>`:
# structured data, the config, the captured calls, the page state and the item layer gave none: the page showed no
# block of items, so role C, which fills the rows of blocks, had none to read.
NO_ROWS: Final = "no_rows"
NO_BLOCKS: Final = "no_blocks"


def no_rows(cause: str) -> str:
    return f"{NO_ROWS}: {cause}"


@dataclass(frozen=True, slots=True)
class _Listing:
    page: _Page
    fields: FieldSet
    rows: tuple[FieldSet, ...]
    next_pages: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    diagnostics: tuple[str, ...]
    llm_calls: tuple[LLMCallRecord, ...] = ()
    entities: tuple[Entity, ...] = ()  # the rows' entities, in `rows` order
    blocks: tuple[RowBlock, ...] = ()  # the rows' blocks, in the bound group's order

    @classmethod
    def read(cls, source: _Source) -> "_Listing":
        page = _Page.read(source)
        links = next_page_links(page.page, source.bundle.manifest.final_url)
        diagnostics = list(page.diagnostics)
        found = next_page_candidate(links, page.schema, page.ctx, diagnostics)
        candidates = (*page.candidates, *(() if found is None else (found,)))
        # The listing's rows are picked once among its sources (`collect.row_sources`). The config's row candidates
        # belong to its own row entities, so structured and config values never meet in one row.
        row_fields = {name for name, spec in page.schema.fields.items() if spec.scope == "row"}
        holders = frozenset(c.entity_id for c in page.collected.candidates if c.field in row_fields)
        structured = listing_rows(page.graph, page.primary, page.target, holders)
        picked = page.collected.rows.pick(structured, page.config.rows)
        # The item layer: the blocks of the page's repeated items are the rows
        # where the stated rows are none or stubs, and extend them; state and call array rows bind to the rows by URL.
        bound = bind(
            Listing(
                page.page,
                page.ctx,
                page.schema,
                page.page_urls,
                picked.items,
                picked.stated,
                tuple(e for e in page.graph.entities.values() if ARRAY_ROW in e.types),
                (*candidates, *picked.candidates),
                partial(_holder, page.graph),
                row_lists(page.graph, structured),
            )
        )
        diagnostics.extend(bound.diagnostics)
        fields, resolved = page.resolve_listing(bound.candidates, bound.rows)
        next_pages = tuple(link.url for link in links)
        return cls(
            page,
            fields,
            resolved,
            next_pages,
            bound.candidates,
            tuple(diagnostics),
            entities=bound.rows,
            blocks=bound.blocks,
        )

    def gap(self) -> ListingGapRequest | None:
        """The role C request: the blocks whose rows lack row fields, each with the
        fields its row lacks, and the missing page fields when a required one is among them (`_gaps`); none of them a
        field the config runs, and no block beside a config that runs a row field (role C's values could not join its
        rows)."""
        schema = self.page.schema
        covered = self.page.config.covered
        row_fields = tuple(name for name, spec in schema.fields.items() if spec.scope == "row")
        blocks = () if covered.intersection(row_fields) else self._blocks(row_fields)
        page_missing = _gaps(schema, self.fields, covered)
        if not blocks and not page_missing:
            return None
        return ListingGapRequest(self.page.page, schema, self.page.ctx, blocks, page_missing)

    def _blocks(self, row_fields: Sequence[str]) -> tuple[BlockText, ...]:
        """The blocks whose rows lack a row field role C can claim, asked when some row lacks a required one or one is
        published on no row: never the item URL, the block's link, which its text does not show, nor an object."""
        schema = self.page.schema
        rows = dict(zip((entity.id for entity in self.entities), self.rows, strict=True))
        askable = _askable(schema, row_fields)
        lacking = {
            block.row: [name for name in askable if not _published(rows[block.row], name)] for block in self.blocks
        }
        unpublished = any(not any(_published(row, name) for row in self.rows) for name in askable)
        required = {name for name in askable if schema.fields[name].required}
        if not unpublished and not any(required.intersection(names) for names in lacking.values()):
            return ()
        return block_texts(self.page.page, self.blocks, lacking)

    def with_reply(self, source: _Source, reply: ListingGapReply, asked: tuple[str, ...]) -> "_Listing":
        """The listing with role C's candidates, owned by the blocks' rows and the page entity: the rows and the page
        fields resolve again with them, then are `finished` with the reply to the call that asked `asked`."""
        candidates = (*self.candidates, *reply.candidates)
        fields, rows = self.page.resolve_listing(candidates, self.entities)
        diagnostics = (*self.diagnostics, *reply.diagnostics)
        answered = replace(
            self, fields=fields, rows=rows, candidates=candidates, diagnostics=diagnostics, llm_calls=reply.llm_calls
        )
        return answered.finished(source, reply, asked)

    def finished(self, source: _Source, reply: GapReply | None = None, asked: tuple[str, ...] = ()) -> "_Listing":
        """The listing with `_finish` run on its page fields and on each of its rows."""
        absent = self.page.config.absent
        fields = _finish(source, absent, self.fields, reply, asked)
        return replace(self, fields=fields, rows=tuple(_finish(source, absent, row) for row in self.rows))

    def _records(self) -> list[FieldSet]:
        # Each row's record holds the page's fields too (`resolve.records.assemble_listing`).
        return [{**self.fields, **row} for row in self.rows]

    def _complete(self) -> bool:
        records = self._records()
        return bool(records) and all(is_complete(self.page.schema, record) for record in records)

    def result(self, mode: ExtractionMode) -> ExtractionResult:
        schema = self.page.schema
        keyed, session = keyed_rows(self.rows, item_url_fields(schema), self.page.page_urls)
        listing = replace(self, rows=keyed)
        records = listing._records()
        return ExtractionResult(
            outcome="extracted" if listing._complete() else "partial",
            entity=self.fields,
            rows=keyed,
            candidates=self.candidates,
            llm_calls=self.llm_calls,
            mode=mode,
            record_confidence=_least_confident(record_confidence(schema, record) for record in records),
            diagnostics=self.diagnostics,
            next_pages=self.next_pages,
            reason=None if self.rows else no_rows(NO_BLOCKS),
            session_params=session,
        )


async def _extract_listing(source: _Source, role_c: RoleC | None, mode: ExtractionMode) -> ExtractionResult:
    try:
        listing = await asyncio.to_thread(_Listing.read, source)
    except Exception:  # §4.14, as for a detail page
        log.exception(_FAILED)
        return _result("error", mode)
    if role_c is None or (request := await asyncio.to_thread(listing.gap)) is None:
        try:
            finished = await asyncio.to_thread(listing.finished, source)
        except Exception:
            log.exception(_FAILED)
            return _result("error", mode, candidates=listing.candidates, diagnostics=listing.diagnostics)
        return finished.result(mode)
    reply = await role_c.listing(request)
    try:
        listing = await asyncio.to_thread(listing.with_reply, source, reply, request.page_missing)
    except Exception:
        log.exception(_FAILED)
        candidates = (*listing.candidates, *reply.candidates)
        diagnostics = (*listing.diagnostics, *reply.diagnostics)
        return _result("error", mode, candidates=candidates, llm_calls=reply.llm_calls, diagnostics=diagnostics)
    return listing.result(mode)


def _holder(graph: EntityGraph, candidate: Candidate) -> str:
    """The entity whose row fields `candidate` fills: its owner, or the item an Offer it is in offers, through an
    AggregateOffer's Offers (`EntityGraph.offered`, atomic offers)."""
    return graph.offered(graph.owner_of(candidate)).id


def _published(fields: FieldSet, name: str) -> bool:
    outcome = fields.get(name)
    return outcome is not None and outcome.status == "published"


def _least_confident(confidences: Iterable[float | None]) -> float | None:
    """§10, per record: the page's is its least confident row's, `None` while any row's is (or there is no row)."""
    known = list(confidences)
    if not known or None in known:
        return None
    return min(c for c in known if c is not None)


def _scoped(schema: SchemaDef, fields: FieldSet, scope: Scope) -> FieldSet:
    return {name: outcome for name, outcome in fields.items() if schema.fields[name].scope == scope}


def _missing(schema: SchemaDef, fields: FieldSet, covered: frozenset[str]) -> tuple[str, ...]:
    """The fields role C is asked for on a detail page, in schema order: every
    `missing` field, optional ones included, that the active config does not run, never a conflict or an abstention.
    An `object` field, and a list of objects (a row-mapped field included), is never asked: a claim is one verbatim
    span or element, which cannot hold typed sub-fields."""
    return tuple(
        name
        for name, outcome in fields.items()
        if outcome.status == "missing" and name not in covered and not _holds_objects(schema.fields[name])
    )


def _gaps(schema: SchemaDef, fields: FieldSet, covered: frozenset[str]) -> tuple[str, ...]:
    """A listing's page fields role C is asked for: the `_missing` ones, only when a
    required one is among them: a required field, or a member of a required group that no member fills yet."""
    published = {name for name, outcome in fields.items() if outcome.status == "published"}
    unmet = {name for group in schema.required_groups if not published.intersection(group) for name in group}
    missing = _missing(schema, fields, covered)
    if not any(schema.fields[name].required or name in unmet for name in missing):
        return ()
    return missing


def _askable(schema: SchemaDef, row_fields: Sequence[str]) -> list[str]:
    """The row fields role C can claim in a block: not the item URL, the block's link, which its text does not show,
    nor an object, whose sub-fields one verbatim span cannot hold."""
    urls = frozenset(item_url_fields(schema))
    return [name for name in row_fields if name not in urls and not _holds_objects(schema.fields[name])]


def _holds_objects(spec: FieldSpec) -> bool:
    return spec.type is FieldType.OBJECT or (spec.item is not None and spec.item.type is FieldType.OBJECT)


def _finish(
    source: _Source,
    absent: frozenset[str],
    fields: FieldSet,
    reply: GapReply | None = None,
    asked: tuple[str, ...] = (),
) -> FieldSet:
    """The one stage after resolution, run on a detail page's fields, a listing's page fields and each listing row:
    the main image left out of its list of images (`main_image`), then every unpublished field's reason, in order role
    C's (`_unanswered`, with `reply`, the call that asked `asked`), the config's (`_absent`), then the page's own
    evidence (`_unpublished_reasons`). Role C's reasons are never a row's: a listing's reply names the page fields'
    failure and unverified fields only (`ListingGapReply`), so rows pass no `reply`. A stage added here applies to all
    three."""
    fields = without_main_image(source.schema, fields)
    if reply is not None:
        if reply.failure is not None:
            fields = _unanswered(fields, asked, reply.failure)
        fields = _unanswered(fields, reply.unverified, UNVERIFIED)
    return _unpublished_reasons(source, _absent(fields, absent))


def _absent(fields: FieldSet, absent: frozenset[str]) -> FieldSet:
    """The fields with each `missing` one the config lists as absent saying so: the site's pages do not state
    it, so role C was not asked for it."""
    return {
        name: outcome.model_copy(update={"reason": ABSENT})
        if name in absent and outcome.status == "missing"
        else outcome
        for name, outcome in fields.items()
    }


def _unpublished_reasons(source: "_Source", fields: FieldSet) -> FieldSet:
    """What the page's own evidence and the site's config say of its unpublished fields (`page_state` module): a price
    missing from a page that states its item is not for sale is `out_of_stock`; a field whose source in the config is
    the browser, missing from a page that was not rendered, is `interaction_required` (`render_bound`). Every other
    unpublished field keeps its reason."""
    fields = out_of_stock(source.schema, fields)
    browser = frozenset() if source.config is None else source.config.browser_fields
    return render_bound(fields, browser, was_rendered(source.bundle.manifest))


def _unanswered(fields: FieldSet, asked: tuple[str, ...], failure: str) -> FieldSet:
    """The fields after a role C call: each of `asked` still `missing` says why, the call's failure when it gave no
    reply, `UNVERIFIED` for a claimed value the evidence does not hold."""
    return {
        name: outcome.model_copy(update={"reason": failure})
        if name in asked and outcome.status == "missing"
        else outcome
        for name, outcome in fields.items()
    }


def _result(
    outcome: Outcome,
    mode: ExtractionMode,
    *,
    candidates: tuple[Candidate, ...] = (),
    llm_calls: tuple[LLMCallRecord, ...] = (),
    diagnostics: tuple[str, ...] = (),
) -> ExtractionResult:
    return ExtractionResult(
        outcome=outcome,
        entity=None,
        rows=(),
        candidates=candidates,
        llm_calls=llm_calls,
        mode=mode,
        record_confidence=None,
        diagnostics=diagnostics,
    )
