"""The configs service: a site's configs, operator corrections, custom recipes, forced
learning and exports, over a connection.

A correction is applied to the latest version of its (site, schema, page kind), so an edit never silently
drops a version someone else added, and the result is always stored as the next version: validated on the site's
held-out pages and active when validation keeps it, else a `draft` whose report says why not.

A custom recipe (`config_ir.custom_recipe`) is saved as its scope's next version, active at once beside the learned
config, which stays active; an edit is the next version replacing the recipe, made from its shape's active version.
Saving needs no validation, approval or audit: the engineer tested the recipe. Retiring one suspends it, and the
learned config serves its URLs again.

An export carries the release check run on the pages the config was validated on
(`provenance.validated_on`) that are still stored; a field with no engine value left on any of them is `manual`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import RowMapping, Text, column, func, select
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from snowcrawl.collect.vocabulary import Vocabulary
from snowcrawl.config_ir import corrections
from snowcrawl.config_ir.activation import (
    NotActive,
    NotPending,
    ShapeTaken,
    approve,
    reject,
    retire,
    store_correction,
    store_recipe,
)
from snowcrawl.config_ir.corrections import Correction
from snowcrawl.config_ir.custom_recipe import CustomRecipe, InvalidRecipe, recipe_ir
from snowcrawl.config_ir.exporters.configs import ConfigExport, ConfigFormat, export_config
from snowcrawl.config_ir.held_out import load_captures, load_held_out
from snowcrawl.config_ir.model import ExtractionConfigIR, SentBy
from snowcrawl.config_ir.progressive import LEARN_AFTER_PAGES, StateKey, lock_state, request_learning
from snowcrawl.config_ir.repair import RecipeChange, changes
from snowcrawl.config_ir.validation import ValidationReport, validate
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.core.errors import Refusal
from snowcrawl.db.models import (
    ConfigOrigin,
    ConfigStatus,
    ExtractionConfig,
    ExtractionConfigId,
    FieldStatus,
    JobId,
    PageKind,
    SchemaId,
    Site,
    SiteId,
)
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import Name, document_digest
from snowcrawl.storage.base import ObjectStore


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConfigSummary(_Out):
    """One version of a site's config for one schema and page kind, without its IR."""

    id: ExtractionConfigId
    site: str
    schema_id: SchemaId
    page_kind: PageKind
    version: int
    status: ConfigStatus
    origin: ConfigOrigin
    field_status: dict[str, FieldStatus]
    validation_report: ValidationReport | None
    # A custom recipe's URL patterns (`custom`), the fields only a render fills, and who sends its calls.
    patterns: list[str]
    custom: bool
    renders: list[str]
    sent_by: SentBy
    # The fields drift suspended, and a custom recipe's repair waiting for approval (a later version of its shape).
    drifting: list[Name]
    pending_repair: ExtractionConfigId | None


class ConfigDetail(ConfigSummary):
    ir: ExtractionConfigIR
    # A custom recipe as it is saved and edited (`CustomRecipe`); None for a learned config.
    recipe: CustomRecipe | None
    # A recipe's repair waiting for approval: what it changes in the active version of its shape.
    repair_changes: list[RecipeChange] | None


class LearnRequest(_Out):
    """`schema_id` names the schema, and so the page kind, to learn for; `force` re-learns an active one."""

    schema_id: SchemaId
    force: bool = False


class ExportRequest(_Out):
    format: ConfigFormat


class SiteNotFound(Refusal, LookupError):
    pass


class ConfigNotFound(Refusal, LookupError):
    pass


class ConfigConflict(Refusal):
    """The request is valid, but the stored configs or learning state don't allow it now."""


# The page fields whose source is the browser's render, by name.
_FIELD = func.jsonb_each(ExtractionConfig.ir["fields"]).table_valued(column("key", Text), column("value", JSONB))
_RENDERS = (
    select(func.coalesce(func.jsonb_agg(aggregate_order_by(_FIELD.c.key, _FIELD.c.key)), func.jsonb_build_array()))
    .where(_FIELD.c.value["needs"].astext == "browser")
    .scalar_subquery()
)
# The fields drift suspended, by name.
_STATUS = func.jsonb_each_text(ExtractionConfig.field_status).table_valued(column("key", Text), column("value", Text))
_DRIFTING = (
    select(func.coalesce(func.jsonb_agg(aggregate_order_by(_STATUS.c.key, _STATUS.c.key)), func.jsonb_build_array()))
    .where(_STATUS.c.value == "suspended")
    .scalar_subquery()
)
# A custom recipe's latest repair waiting for approval: a later version of its scope and pattern shape.
_LATER = aliased(ExtractionConfig)
_PENDING_REPAIR = (
    select(_LATER.id)
    .where(
        _LATER.site_id == ExtractionConfig.site_id,
        _LATER.schema_id == ExtractionConfig.schema_id,
        _LATER.page_kind == ExtractionConfig.page_kind,
        _LATER.pattern_shape == ExtractionConfig.pattern_shape,
        _LATER.pattern_shape != "",
        _LATER.status == "pending_approval",
        _LATER.version > ExtractionConfig.version,
    )
    .order_by(_LATER.version.desc())
    .limit(1)
    .scalar_subquery()
)
_COLUMNS = (
    ExtractionConfig.id,
    Site.host,
    ExtractionConfig.site_id,
    ExtractionConfig.schema_id,
    ExtractionConfig.page_kind,
    ExtractionConfig.version,
    ExtractionConfig.status,
    ExtractionConfig.origin,
    ExtractionConfig.field_status,
    ExtractionConfig.validation_report,
    ExtractionConfig.patterns,
    (ExtractionConfig.pattern_shape != "").label("custom"),
    _RENDERS.label("renders"),
    func.coalesce(ExtractionConfig.ir["sent_by"].astext, "http").label("sent_by"),
    _DRIFTING.label("drifting"),
    _PENDING_REPAIR.label("pending_repair"),
)
# A list leaves out each version's IR, the largest column.
_SUMMARIES = select(*_COLUMNS).join(Site, Site.id == ExtractionConfig.site_id)
_DETAILS = _SUMMARIES.add_columns(ExtractionConfig.ir, ExtractionConfig.pattern_shape)


def _model[M: ConfigSummary](model: type[M], row: RowMapping | Mapping[str, Any]) -> M:
    return model.model_validate({name: row["host" if name == "site" else name] for name in model.model_fields})


async def list_for_host(conn: AsyncConnection, host: str) -> list[ConfigSummary]:
    """Every version of every config of the site, by schema, page kind, then version."""
    site_id = await _site_id(conn, host)
    query = _SUMMARIES.where(ExtractionConfig.site_id == site_id).order_by(
        ExtractionConfig.schema_id, ExtractionConfig.page_kind, ExtractionConfig.version
    )
    return [_model(ConfigSummary, row) for row in (await conn.execute(query)).mappings()]


async def get(conn: AsyncConnection, config_id: ExtractionConfigId) -> ConfigDetail:
    row = await _row(conn, config_id)
    recipe = (
        CustomRecipe.from_ir(ExtractionConfigIR.model_validate(row["ir"]), row["schema_id"]) if row["custom"] else None
    )
    return _model(ConfigDetail, {**row, "recipe": recipe, "repair_changes": await _repair_changes(conn, row)})


async def _repair_changes(conn: AsyncConnection, row: RowMapping) -> list[RecipeChange] | None:
    """What a recipe's repair waiting for approval changes in the active version of its shape; None for any other
    config, or when no version of its shape is active."""
    if not row["custom"] or row["status"] != "pending_approval":
        return None
    configs = ExtractionConfig
    key = StateKey(row["site_id"], row["schema_id"], row["page_kind"])
    active = select(configs.ir).where(
        *key.config_scope(), configs.pattern_shape == row["pattern_shape"], configs.status == "active"
    )
    current: dict[str, Any] | None = (await conn.execute(active)).scalar_one_or_none()
    if current is None:
        return None
    before = CustomRecipe.from_ir(ExtractionConfigIR.model_validate(current), key.schema_id)
    return changes(before, CustomRecipe.from_ir(ExtractionConfigIR.model_validate(row["ir"]), key.schema_id))


async def correct(
    conn: AsyncConnection,
    store: ObjectStore,
    vocabulary: Vocabulary,
    config_id: ExtractionConfigId,
    correction: Correction,
) -> ConfigDetail:
    """Apply `correction` to the config `config_id`, which must be its scope's latest version, validate the result on
    the site's held-out pages and store it as the next version; the new version. Raises `InvalidCorrection` when the
    correction does not fit the config."""
    row = await _row(conn, config_id)
    if row["custom"]:
        raise ConfigConflict(f"config {config_id} is a custom recipe: edit it with PUT /v1/configs/{config_id}/recipe")
    key = StateKey(row["site_id"], row["schema_id"], row["page_kind"])
    await lock_state(conn, key)
    latest = await _latest_version(conn, key, "")
    if row["version"] != latest:
        raise ConfigConflict(f"config {config_id} is version {row['version']}; correct the latest, version {latest}")
    schema = await registry.get_by_id(conn, key.schema_id)
    ir = ExtractionConfigIR.model_validate(row["ir"])
    if ir.schema_.digest != document_digest(schema):
        # A schema is edited in place: this config's steps were learned for the document before the edit.
        raise ConfigConflict(f"config {config_id} was learned for an earlier document of schema {schema.name}")
    corrected = corrections.correct(ir, schema, correction, engine_version=ENGINE_VERSION, at=datetime.now(UTC))
    # §4.4: the site's 8 most recent extracted pages; the operator wrote the steps from none of them.
    held_out = await load_held_out(conn, store, key.site_id, schema, limit=LEARN_AFTER_PAGES)
    # Both executors run on every page: CPU work, so a thread keeps the event loop serving meanwhile.
    validated = await asyncio.to_thread(validate, corrected, schema, held_out, vocabulary)
    return await get(conn, await store_correction(conn, key, corrected, validated, correction.operated()))


async def create_recipe(conn: AsyncConnection, host: str, recipe: CustomRecipe) -> ConfigDetail:
    """Save `recipe` as the site's next config version for its schema and page kind, active beside the learned
    config. Raises `InvalidRecipe` when it does not fit the site or schema, and `ConfigConflict` when an active
    recipe of the scope serves a URL pattern of the same shape."""
    site_id = await _site_id(conn, host)
    return await _save_recipe(conn, StateKey(site_id, recipe.schema_id, recipe.page_kind), host, recipe, None)


async def edit_recipe(conn: AsyncConnection, config_id: ExtractionConfigId, recipe: CustomRecipe) -> ConfigDetail:
    """Save `recipe` as the next version replacing the custom recipe `config_id`, which must be the active version of
    its pattern shape (a newer repair waiting for approval, or one refused, is not the recipe); raises like
    `create_recipe`, and `ConfigConflict` for a config that is not a custom recipe or not active."""
    row = await _row(conn, config_id)
    if not row["custom"]:
        raise ConfigConflict(f"config {config_id} is not a custom recipe: correct it with PUT /v1/configs/{config_id}")
    if (recipe.schema_id, recipe.page_kind) != (row["schema_id"], row["page_kind"]):
        raise InvalidRecipe([("schema_id", f"config {config_id} is for schema {row['schema_id']}, {row['page_kind']}")])
    key = StateKey(row["site_id"], row["schema_id"], row["page_kind"])
    await lock_state(conn, key)
    if row["status"] != "active":
        # One version of a shape is active (`uq_extraction_config_active`); `store_recipe` checks it still is, locked.
        raise ConfigConflict(f"config {config_id} is {row['status']} version {row['version']}; edit the active one")
    return await _save_recipe(conn, key, row["host"], recipe, config_id)


async def _save_recipe(
    conn: AsyncConnection, key: StateKey, host: str, recipe: CustomRecipe, replaces: ExtractionConfigId | None
) -> ConfigDetail:
    schema = await registry.get_by_id(conn, key.schema_id)
    ir = recipe_ir(recipe, host, schema, at=datetime.now(UTC), engine_version=ENGINE_VERSION)
    try:
        config_id = await store_recipe(conn, key, ir, replaces=replaces)
    except (ShapeTaken, NotActive) as e:
        raise ConfigConflict(str(e)) from e
    return await get(conn, config_id)


async def retire_recipe(conn: AsyncConnection, config_id: ExtractionConfigId) -> ConfigDetail:
    """Retire the custom recipe `config_id`: it is `suspended`, with any repair of it waiting for approval, and the
    learned config serves its URLs again; `ConfigConflict` when it is not an active custom recipe."""
    row = await _row(conn, config_id)
    try:
        await retire(conn, StateKey(row["site_id"], row["schema_id"], row["page_kind"]), config_id)
    except NotActive as e:
        detail = f"config {config_id} is {row['status']} version {row['version']}, not an active custom recipe"
        raise ConfigConflict(detail) from e
    return await get(conn, config_id)


async def approve_config(conn: AsyncConnection, config_id: ExtractionConfigId) -> ConfigDetail:
    """Approve the config `config_id`, waiting as `pending_approval`: it becomes its site's active config for
    its schema and page kind; the version active before is `suspended`."""
    return await _decide(conn, config_id, approve)


async def reject_config(conn: AsyncConnection, config_id: ExtractionConfigId) -> ConfigDetail:
    """Refuse the config `config_id`, waiting as `pending_approval`: it is `suspended` and what served before
    goes on serving."""
    return await _decide(conn, config_id, reject)


async def _decide(
    conn: AsyncConnection,
    config_id: ExtractionConfigId,
    decide: Callable[[AsyncConnection, StateKey, ExtractionConfigId], Awaitable[None]],
) -> ConfigDetail:
    """The config after a person's decision on it; `ConfigConflict` when it is not waiting for one."""
    row = await _row(conn, config_id)
    try:
        await decide(conn, StateKey(row["site_id"], row["schema_id"], row["page_kind"]), config_id)
    except NotPending as e:
        detail = f"config {config_id} is {row['status']}, not the latest version waiting for approval"
        raise ConfigConflict(detail) from e
    return await get(conn, config_id)


async def export(
    conn: AsyncConnection, store: ObjectStore, config_id: ExtractionConfigId, request: ExportRequest
) -> ConfigExport:
    """The config in `request.format`, with its release check on the captures it was validated on."""
    row = await _row(conn, config_id)
    ir = ExtractionConfigIR.model_validate(row["ir"])
    schema = await registry.get_by_id(conn, row["schema_id"])
    held_out = await load_captures(conn, store, schema, ir.provenance.validated_on)
    # The release executor parses and runs every page: CPU work, so a thread keeps the event loop serving meanwhile.
    return await asyncio.to_thread(export_config, request.format, ir, schema, held_out)


async def learn(conn: AsyncConnection, host: str, request: LearnRequest) -> JobId:
    """Start a learning run for the site and schema, and return its `learn` job."""
    site_id = await _site_id(conn, host)
    schema = await registry.get_by_id(conn, request.schema_id)
    key = StateKey(site_id, schema.id, schema.page_kind)
    job_id = await request_learning(conn, key, force=request.force)
    if job_id is None:
        raise ConfigConflict(
            f"{host}, schema {schema.name}: a learning run is in flight, or the site has an "
            "active config and force is not set"
        )
    return job_id


async def _site_id(conn: AsyncConnection, host: str) -> SiteId:
    site_id: SiteId | None = (await conn.execute(select(Site.id).where(Site.host == host.lower()))).scalar_one_or_none()
    if site_id is None:
        raise SiteNotFound(f"no site {host}")
    return site_id


async def _row(conn: AsyncConnection, config_id: ExtractionConfigId) -> RowMapping:
    row = (await conn.execute(_DETAILS.where(ExtractionConfig.id == config_id))).mappings().one_or_none()
    if row is None:
        raise ConfigNotFound(f"no config {config_id}")
    return row


async def _latest_version(conn: AsyncConnection, key: StateKey, pattern_shape: str) -> int:
    """The latest version of `key`'s configs of one pattern shape (`''`: the learned config's)."""
    query = select(func.max(ExtractionConfig.version)).where(
        *key.config_scope(), ExtractionConfig.pattern_shape == pattern_shape
    )
    version: int = (await conn.execute(query)).scalar_one()
    return version
