"""Active mode: the config a page of a site is extracted with, and the drift
record of each page it ran on.

A (site, schema, page kind) with an active config extracts its pages in `config` mode with that config. The config
row is the authority: `extraction_config` holds at most one active learned config per site, schema and page kind, and
any number of active custom recipes, one per pattern shape. A page is extracted with the most specific recipe whose
URL pattern it matches (`url_pattern.most_specific`), else with the learned config, which also extracts a recipe's
page its recipe did not fetch (an `ActiveConfig`'s `rest`); a schema edit suspends the schema's active configs, so
its pages start cold again. Each page the config ran on, served and resolved, is added to its drift window
(`drift.record_run`).
"""

from collections.abc import Collection, Sequence
from dataclasses import replace
from functools import partial
from urllib.parse import urlsplit

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.acquire.api import JsonRequest
from snowcrawl.acquire.rungs import FetchPlan
from snowcrawl.acquire.types import CallEndpoint, FetchPolicy, FetchResult
from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.config_ir.api_recipe.binding import page_data, resolve_params
from snowcrawl.config_ir.api_recipe.execute import Custom, RecipeExecutor
from snowcrawl.config_ir.api_recipe.model import PLACEHOLDER, ApiRecipe
from snowcrawl.config_ir.api_recipe.replay import request_for
from snowcrawl.config_ir.drift import Drifted, DriftMargins, record_run
from snowcrawl.config_ir.executor import EngineExecutor
from snowcrawl.config_ir.model import ExtractionConfigIR, FieldSteps
from snowcrawl.config_ir.progressive import StateKey
from snowcrawl.config_ir.url_pattern import most_specific
from snowcrawl.content.page import ParsedPage
from snowcrawl.db.models import ExtractionConfig, ExtractionConfigId, JobId, RenderMode, SiteId
from snowcrawl.resolve.task import ActiveConfig
from snowcrawl.resolve.types import ExtractionResult
from snowcrawl.schemas.types import SchemaDef

_Config = Row[ExtractionConfigId, list[str]]
# Selection reads the patterns alone; only the chosen config's IR, the large column, is read.
_COLUMNS = (ExtractionConfig.id, ExtractionConfig.patterns)


async def active_config(
    conn: AsyncConnection, site_id: SiteId, schema: SchemaDef, vocabulary: Vocabulary, url: str
) -> ActiveConfig | None:
    """The active config of `site_id` for `schema` and its page kind that serves `url` (`select_config`), bound to
    its executor; None in cold mode."""
    return await _bound(conn, await _active_rows(conn, site_id, schema), url, vocabulary)


async def learned_config(
    conn: AsyncConnection, site_id: SiteId, schema: SchemaDef, vocabulary: Vocabulary
) -> ActiveConfig | None:
    """The active learned config of `site_id` for `schema` and its page kind (the one without URL patterns), bound to
    its executor: what serves a URL no recipe's pattern matches; None in cold mode."""
    learned = next((row for row in await _active_rows(conn, site_id, schema) if not row.patterns), None)
    return await _bound_row(conn, learned, vocabulary)


async def _active_rows(conn: AsyncConnection, site_id: SiteId, schema: SchemaDef) -> Sequence[_Config]:
    key = StateKey(site_id, schema.id, schema.page_kind)
    return (await conn.execute(select(*_COLUMNS).where(*key.config_scope(), ExtractionConfig.status == "active"))).all()


async def pinned_config(
    conn: AsyncConnection, config_ids: Collection[ExtractionConfigId], url: str, vocabulary: Vocabulary
) -> ActiveConfig | None:
    """Of the configs `config_ids` (a job's pins for a host: its learned config and recipes), the one that serves
    `url`, bound to its executor whatever its status now; None when none does."""
    if not config_ids:
        return None
    rows = (await conn.execute(select(*_COLUMNS).where(ExtractionConfig.id.in_(config_ids)))).all()
    return await _bound(conn, rows, url, vocabulary)


def select_config(rows: Sequence[_Config], url: str) -> _Config | None:
    """Of one scope's configs, the recipe with the most specific URL pattern `url` matches, else the learned
    config (the one without patterns); None when neither is there."""
    by_pattern = {pattern: row for row in rows for pattern in row.patterns}
    # Sorted, so a choice between patterns of equal specificity is the same whatever order the rows came in.
    chosen = most_specific(sorted(by_pattern), url)
    if chosen is not None:
        return by_pattern[chosen]
    return next((row for row in rows if not row.patterns), None)


async def _bound(
    conn: AsyncConnection, rows: Sequence[_Config], url: str, vocabulary: Vocabulary
) -> ActiveConfig | None:
    """The config of `rows` that serves `url` (`select_config`), bound; a recipe's with the learned config of `rows`,
    bound, as its `rest`."""
    config = await _bound_row(conn, select_config(rows, url), vocabulary)
    if config is None or not config.custom:
        return config
    learned = next((row for row in rows if not row.patterns), None)
    return replace(config, rest=await _bound_row(conn, learned, vocabulary))


async def _bound_row(conn: AsyncConnection, row: _Config | None, vocabulary: Vocabulary) -> ActiveConfig | None:
    if row is None:
        return None
    configs = ExtractionConfig
    found = (await conn.execute(select(configs.version, configs.ir).where(configs.id == row.id))).one()
    return bound_config(row.id, found.version, ExtractionConfigIR.model_validate(found.ir), vocabulary)


def fetch_plan(config: ActiveConfig | None, render_mode: RenderMode) -> FetchPlan:
    """The fetch plan of the config's field sources: the browser when a running field's source is the browser's render
    or the surface renders every page
    (`always`); the companion call of each recipe a running `call` field names, and its endpoint for the browser's
    ready wait."""
    ir = config.executor.ir if config is not None and isinstance(config.executor, EngineExecutor) else None
    running = [] if ir is None else _running(ir)
    recipes = {} if ir is None else {s.call: ir.recipes[s.call] for s in running if s.call is not None}
    return FetchPlan(
        browser=render_mode == "always" or any(steps.needs == "browser" for steps in running),
        calls=tuple(partial(companion_request, recipe) for recipe in recipes.values()),
        endpoints=tuple(endpoint(recipe) for recipe in recipes.values()),
    )


def _running(ir: ExtractionConfigIR) -> list[FieldSteps]:
    listed = ir.listing.fields.values() if ir.listing is not None else ()
    return [steps for steps in (*ir.fields.values(), *listed) if steps.status != "suspended"]


def companion_request(
    recipe: ApiRecipe, page: FetchResult, policy: FetchPolicy
) -> tuple[JsonRequest, tuple[str, ...]] | None:
    """`recipe`'s request for the HTTP `page`, bound from the URL it was requested at and, when a binder reads them,
    its JSON documents, with the headers it references from the policy's request context, and the values it was bound
    with (the identity check's keys); None when it binds nothing."""
    url = page.bundle.manifest.url
    data = page_data(ParsedPage.of(page.bundle)) if recipe.requires_page_fetch else {}
    request = request_for(recipe, url, data, policy.context)
    params = resolve_params(recipe, url, data)
    return None if request is None or params is None else (request, tuple(params.values()))


def endpoint(recipe: ApiRecipe) -> CallEndpoint:
    """The endpoint `recipe` calls, as the browser's ready wait matches the page's own calls of it."""
    parts = urlsplit(recipe.url.partition("?")[0])
    segments = parts.path[1:].split("/") if parts.path else []
    return CallEndpoint(
        method=recipe.method,
        origin=f"{parts.scheme}://{parts.netloc}",
        segments=tuple(None if PLACEHOLDER.search(segment) else segment for segment in segments),
    )


def recipe_of(config: ActiveConfig) -> ApiRecipe | None:
    """The API recipe of `config`, which serves its pages, as `bound_config` parsed it; None without one."""
    return config.api.recipe if isinstance(config.api, RecipeExecutor) else None


def custom_of(config: ActiveConfig) -> Custom | None:
    """What `config`'s custom recipe serves beyond its main call; None for a learned config."""
    return config.api.custom if isinstance(config.api, RecipeExecutor) else None


def _custom(config_id: ExtractionConfigId, ir: ExtractionConfigIR) -> Custom | None:
    """A custom recipe's serving (`execute.Custom`), from the config with URL patterns that stores it."""
    if not ir.patterns:
        return None
    page_fields = bool(_page_fields(ir))
    return Custom(str(config_id), tuple(ir.patterns), ir.recipes, ir.warm_up, page_fields, ir.rate_ms, ir.sent_by)


def _page_fields(ir: ExtractionConfigIR) -> dict[str, FieldSteps]:
    """The running fields a custom recipe reads off the HTML page: no call's, and not the browser's render."""
    return {
        name: steps
        for name, steps in ir.fields.items()
        if steps.call is None and steps.needs == "http" and steps.status != "suspended"
    }


def bound_config(
    config_id: ExtractionConfigId, version: int, ir: ExtractionConfigIR, vocabulary: Vocabulary
) -> ActiveConfig:
    """The config bound to its executor, and to its recipe's when it has one: the recipe's locators name the config.
    A custom recipe's executor reads its page fields, on the HTML page it fetched, with the config's executor of just
    those fields."""
    executor = EngineExecutor(ir, vocabulary)
    recipe = ir.api_recipe
    custom = _custom(config_id, ir)
    page = None
    if custom is not None and custom.page_fields:
        page = EngineExecutor(ir.model_copy(update={"fields": _page_fields(ir)}), vocabulary)
    api = None if recipe is None else RecipeExecutor.of(recipe, str(config_id), executor.field_status, custom, page)
    return ActiveConfig(config_id, version, executor, api, patterns=tuple(ir.patterns))


async def record_page(
    conn: AsyncConnection,
    config: ActiveConfig,
    result: ExtractionResult,
    *,
    margins: DriftMargins | None,
    job_id: JobId,
) -> list[Drifted]:
    """Add `result`, a page `config` ran on, to the config's drift window, and return the fields it suspended. A page
    that was not served or did not resolve shows nothing about the config, and is not recorded; nor is one its recipe
    served (`ActiveConfig.drifts`)."""
    if not config.drifts or result.outcome not in ("extracted", "partial"):
        return []
    published = [name for name, outcome in (result.entity or {}).items() if outcome.status == "published"]
    return await record_run(conn, config.id, result.candidates, margins=margins, job_id=job_id, published=published)
