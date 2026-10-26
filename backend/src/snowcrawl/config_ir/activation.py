"""Activation: a validated config is stored as the next version of its (site,
schema, page kind) config, `active`, with its field statuses and `validation_report`; the config active before it is
`suspended`, and the learning run in flight ends with the state `active` on the new config.

When the project requires approval, a learning run's validated config is stored `pending_approval` instead
(`store_pending`), and becomes the active config only when a person approves it (`approve`).

An operator's correction is stored as the next version whether or not it validates: active as above, without
ending any learning run, or a `draft` with the report that says why not.

A custom recipe (`store_recipe`) is stored as the next version, active at once, beside the learned config: a scope
holds one active config per pattern shape (`url_pattern.patterns_shape`), the learned one's `''`. Activating or
correcting the learned config suspends only the learned config before it; saving a recipe suspends only the recipe it
replaces, and the progressive state goes on naming the learned config. A learning run only ever replaces the learned
config. A recipe's repair run stores the recipe's next version `pending_approval` against its shape
(`store_pending(shape=)`), and approving it suspends only the version of that shape it repairs.

Only a `Validated` with a config activates: nothing activates without validation. Everything runs on the
caller's connection, in its transaction, so a run that cannot end (`NotLearning`) stores nothing.
"""

from collections.abc import Collection

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.config_ir.executor import ConfigMismatch
from snowcrawl.config_ir.model import ExtractionConfigIR
from snowcrawl.config_ir.progressive import (
    NotLearning,
    StateKey,
    config_approved,
    config_rejected,
    learning_activated,
    learning_awaits_approval,
    lock_state,
    operator_activated,
)
from snowcrawl.config_ir.url_pattern import patterns_shape, shape
from snowcrawl.config_ir.validation import Validated, ValidationReport
from snowcrawl.db.models import (
    ConfigStatus,
    ExtractionConfig,
    ExtractionConfigId,
    ProgressiveState,
    SchemaDef,
    Site,
)
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import document_digest


class NotValidated(ValueError):
    """Validation kept no config to activate."""


class ShapeTaken(ValueError):
    """Another active recipe of the scope serves a URL pattern of the same shape."""


class NotActive(ValueError):
    """The recipe an edit replaces is no longer active: another edit replaced it first."""


async def activate(conn: AsyncConnection, key: StateKey, validated: Validated) -> ExtractionConfigId:
    """Store `validated`'s config for `key` as active and end `key`'s learning run with it; its id. Raises
    `NotValidated` when validation refused the config, `ConfigMismatch` when the config was learned for another site,
    schema or page kind, and `NotLearning` when no learning run is in flight for `key`."""
    ir = validated.ir
    if ir is None:
        raise NotValidated(f"progressive_state {key}: validation refused the config ({validated.report.refused})")
    if ir.patterns:
        raise ConfigMismatch(f"progressive_state {key}: a learning run activates the learned config, not a recipe")
    # One activation per state at a time: the row lock orders them, so versions are taken one after another.
    locked = select(ProgressiveState.learning_started_at).where(*key.where()).with_for_update()
    started = (await conn.execute(locked)).first()
    if started is None or started.learning_started_at is None:
        raise NotLearning(key, "activate")
    await _check_key(conn, key, ir)
    await _suspend_active(conn, key)
    config_id = await _store(conn, key, ir, validated.report, "active")
    await learning_activated(conn, key, config_id)
    return config_id


async def store_pending(
    conn: AsyncConnection, key: StateKey, validated: Validated, *, shape: str = ""
) -> ExtractionConfigId:
    """Store `validated`'s config for `key` as `pending_approval` against the pattern `shape` (`''`: the learned
    config's; else a recipe's repair) and end `key`'s learning run: the config serving before the run, if any, goes on
    serving until a person approves the new one (`approve`). A config of the shape that waited before is `suspended`:
    the newer run replaces it. The state's active config stays the learned one. Raises like `activate`, and
    `ConfigMismatch` when the config's patterns are not of `shape`."""
    ir = validated.ir
    if ir is None:
        raise NotValidated(f"progressive_state {key}: validation refused the config ({validated.report.refused})")
    if patterns_shape(ir.patterns) != shape:
        raise ConfigMismatch(f"progressive_state {key}: a config of another pattern shape stored for {shape!r}")
    locked = select(ProgressiveState.learning_started_at).where(*key.where()).with_for_update()
    started = (await conn.execute(locked)).first()
    if started is None or started.learning_started_at is None:
        raise NotLearning(key, "await_approval")
    await _check_key(conn, key, ir)
    await _suspend_waiting(conn, key, shape)
    config_id = await _store(conn, key, ir, validated.report, "pending_approval")
    await learning_awaits_approval(conn, key)
    return config_id


class NotPending(ValueError):
    """The config is not waiting for approval."""


async def _lock_pending(conn: AsyncConnection, key: StateKey, config_id: ExtractionConfigId) -> str:
    """Lock `key`'s state, then check that `config_id` is the latest version of its pattern shape and waiting for
    approval, and return the shape; a recipe's repair also needs the version it repairs still active.
    Raises `NotPending` otherwise: a newer version, an operator's correction, an edit or a draft, was written knowing
    it."""
    await lock_state(conn, key)
    configs = ExtractionConfig
    pending = select(configs.status, configs.version, configs.pattern_shape).where(
        configs.id == config_id, *key.config_scope()
    )
    found = (await conn.execute(pending.with_for_update(of=configs))).first()
    if found is None or found.status != "pending_approval":
        raise NotPending(f"config {config_id} is not the latest version waiting for approval")
    same_shape = (*key.config_scope(), configs.pattern_shape == found.pattern_shape)
    latest = (await conn.execute(select(func.max(configs.version)).where(*same_shape))).scalar_one()
    repaired = select(configs.id).where(*same_shape, configs.status == "active")
    if found.version != latest or (found.pattern_shape and (await conn.execute(repaired)).first() is None):
        raise NotPending(f"config {config_id} is not the latest version waiting for approval")
    shape_of: str = found.pattern_shape
    return shape_of


async def approve(conn: AsyncConnection, key: StateKey, config_id: ExtractionConfigId) -> None:
    """Make `config_id`, a config of `key` waiting for approval, the active one, suspending the config of its pattern
    shape active before it; a learned config becomes the state's. Raises `NotPending` when it is not
    `pending_approval` or not its shape's latest version."""
    pattern_shape = await _lock_pending(conn, key, config_id)
    await _suspend_active(conn, key, pattern_shape)
    await conn.execute(update(ExtractionConfig).where(ExtractionConfig.id == config_id).values(status="active"))
    if not pattern_shape:
        await config_approved(conn, key, config_id)


async def reject(conn: AsyncConnection, key: StateKey, config_id: ExtractionConfigId) -> None:
    """Refuse `config_id`, a config of `key` waiting for approval: it is `suspended`, the config active before
    it goes on serving, and for a learned config the state learns again only after more pages. Raises `NotPending` as
    `approve` does."""
    pattern_shape = await _lock_pending(conn, key, config_id)
    await conn.execute(update(ExtractionConfig).where(ExtractionConfig.id == config_id).values(status="suspended"))
    if not pattern_shape:
        await config_rejected(conn, key)


async def store_correction(
    conn: AsyncConnection,
    key: StateKey,
    corrected: ExtractionConfigIR,
    validated: Validated,
    operated: Collection[str],
) -> ExtractionConfigId:
    """Store an operator's `corrected` config, validated as `validated`, as `key`'s next version, and return its id.
    It is `validated`'s config, active, only when validation kept every field in `operated` (those the operator gave
    steps or confirmed): activating without one would drop the operator's work and suspend the version that served.
    Otherwise it is `corrected` as a `draft`. The caller holds `key`'s state lock (`progressive.lock_state`). Raises
    `ConfigMismatch` like `activate`."""
    await _check_key(conn, key, corrected)
    if validated.ir is None or not set(operated) <= _field_names(validated.ir):
        return await _store(conn, key, corrected, validated.report, "draft")
    await _suspend_active(conn, key)
    await _suspend_waiting(conn, key, "")  # the operator's config is newer than one waiting for approval
    config_id = await _store(conn, key, validated.ir, validated.report, "active")
    await operator_activated(conn, key, config_id)
    return config_id


async def store_recipe(
    conn: AsyncConnection, key: StateKey, ir: ExtractionConfigIR, *, replaces: ExtractionConfigId | None
) -> ExtractionConfigId:
    """Store the custom recipe `ir` as `key`'s next version, active, and return its id; the recipe `replaces` names,
    if any, is suspended, and no other row changes. Raises `ShapeTaken` when another active recipe of the scope has
    a pattern of one of `ir`'s shapes, `NotActive` when `replaces` is no longer active (an edit that ran first replaced
    it, under the same state lock), and `ConfigMismatch` like `activate`."""
    await lock_state(conn, key)
    await _check_key(conn, key, ir)
    configs = ExtractionConfig
    others = select(configs.id, configs.patterns).where(
        *key.config_scope(), configs.status == "active", configs.pattern_shape != ""
    )
    shapes = {shape(pattern) for pattern in ir.patterns}
    for other in (await conn.execute(others)).all():
        if other.id != replaces and (taken := shapes.intersection(map(shape, other.patterns))):
            raise ShapeTaken(f"config {other.id} serves URL patterns of shape {sorted(taken)}")
    if replaces is not None:
        replaced = update(configs).where(configs.id == replaces, configs.status == "active").values(status="suspended")
        if (await conn.execute(replaced)).rowcount != 1:
            raise NotActive(f"config {replaces} is no longer active: another edit replaced it")
    return await _store(conn, key, ir, None, "active")


def _field_names(ir: ExtractionConfigIR) -> set[str]:
    return set(ir.fields) | (set(ir.listing.fields) if ir.listing is not None else set())


async def _check_key(conn: AsyncConnection, key: StateKey, ir: ExtractionConfigIR) -> None:
    """The config names the state's site, schema and page kind (configs are per schema)."""
    host = select(Site.host).where(Site.id == key.site_id).scalar_subquery()
    schema = select(SchemaDef.name).where(SchemaDef.id == key.schema_id)
    found = (await conn.execute(select(host, schema.scalar_subquery()))).one()
    learned = (ir.site, ir.schema_.name, ir.page_kind)
    if learned != (*found, key.page_kind):
        raise ConfigMismatch(f"config for {learned} activated for progressive_state {key}")
    # A schema is edited in place: a config learned before an edit is for another document.
    if ir.schema_.digest != document_digest(await registry.get_by_id(conn, key.schema_id)):
        raise ConfigMismatch(f"config learned for an earlier document of schema {ir.schema_.name} activated for {key}")


async def _suspend_active(conn: AsyncConnection, key: StateKey, pattern_shape: str = "") -> None:
    # One active config per (site, schema, page kind) and pattern shape; the one before is suspended, the others stay.
    configs = ExtractionConfig
    active = (*key.config_scope(), configs.status == "active", configs.pattern_shape == pattern_shape)
    await conn.execute(update(configs).where(*active).values(status="suspended"))


async def _suspend_waiting(conn: AsyncConnection, key: StateKey, pattern_shape: str) -> None:
    configs = ExtractionConfig
    waiting = (*key.config_scope(), configs.status == "pending_approval", configs.pattern_shape == pattern_shape)
    await conn.execute(update(configs).where(*waiting).values(status="suspended"))


async def _store(
    conn: AsyncConnection,
    key: StateKey,
    ir: ExtractionConfigIR,
    report: ValidationReport | None,
    status: ConfigStatus,
) -> ExtractionConfigId:
    configs = ExtractionConfig
    version = select(func.coalesce(func.max(configs.version), 0) + 1).where(*key.config_scope()).scalar_subquery()
    stored = insert(configs).values(
        **key.columns(),
        version=version,
        status=status,
        ir=ir.model_dump(mode="json", by_alias=True),
        origin=ir.provenance.origin,
        field_status=ir.field_status(),
        validation_report=None if report is None else report.model_dump(mode="json", by_alias=True),
        patterns=ir.patterns,
        pattern_shape=patterns_shape(ir.patterns),
    )
    config_id: ExtractionConfigId = (await conn.execute(stored.returning(configs.id))).scalar_one()
    return config_id
