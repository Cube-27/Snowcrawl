"""SQLAlchemy models of the main database. The models and the Alembic revisions own
the DB shape; `tests/db` checks that the migrated database matches these models.

Enumerations are `text` with a CHECK constraint whose values come from the matching `Literal`.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, NewType, get_args

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Text,
    TextClause,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from snowcrawl.core.capture import BlockClass, CaptureMethod
from snowcrawl.core.egress import EgressBasis
from snowcrawl.core.ids import uuid7

# SQL referential action (ISO/IEC 9075-2 §11.8): a deleted parent leaves the reference empty.
_SET_NULL: Final = "SET NULL"

ProjectId = NewType("ProjectId", uuid.UUID)
ApiKeyId = NewType("ApiKeyId", uuid.UUID)
SiteId = NewType("SiteId", uuid.UUID)
SchemaId = NewType("SchemaId", uuid.UUID)
JobId = NewType("JobId", uuid.UUID)
UrlTaskId = NewType("UrlTaskId", uuid.UUID)
JobEventId = NewType("JobEventId", uuid.UUID)
CaptureId = NewType("CaptureId", uuid.UUID)
ExtractionId = NewType("ExtractionId", uuid.UUID)
CandidateId = NewType("CandidateId", uuid.UUID)
RecordId = NewType("RecordId", uuid.UUID)
ExtractionConfigId = NewType("ExtractionConfigId", uuid.UUID)
UsageEventId = NewType("UsageEventId", uuid.UUID)
ScheduleId = NewType("ScheduleId", uuid.UUID)
WebhookEventId = NewType("WebhookEventId", uuid.UUID)
WebhookDeliveryId = NewType("WebhookDeliveryId", uuid.UUID)

FetchMethod = Literal["http", "browser"]
PageKind = Literal["detail", "listing"]
# The browser engines whose cookie state is kept apart (`site_cookie_state`): Patchright's (Google Chrome).
BrowserEngine = Literal["patchright"]
SchemaOrigin = Literal["preset", "user", "prompt_draft"]
SchemaStatus = Literal["active", "retired"]
JobKind = Literal["scrape", "batch_scrape", "map", "crawl", "extract", "learn"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TaskStatus = Literal["queued", "claimed", "done", "failed", "cancelled"]
# The outcomes of a task that ran to the end (status `done`); `failed` is the outcome of a `failed` task.
# The data outcome; the transport outcome is the capture's `block_class`. A page not served ends with its block class:
# a 401's `auth_wall`, where a redirect led instead of the page, or a 2xx answer that is no HTML document
# (`unsupported_media`); a 451 refusal is `geo_blocked`.
FinishedOutcome = Literal[
    "extracted",
    "partial",
    "links_only",
    "blocked",
    "shell",
    "not_found",
    "no_match",
    "auth_wall",
    "soft_404",
    "geo_redirect",
    "redirected_to_listing",
    "unsupported_media",
    "geo_blocked",
]
# The outcomes of a `failed` task: `timeout` when no answer came within the deadline (the fetch rung's, or the task's),
# else `failed`.
TaskOutcome = Literal[FinishedOutcome, "failed", "timeout"]
ExtractionMode = Literal["adhoc", "config"]
# What a task's record is against the previous record of its entity under its schema.
RecordDelta = Literal["new", "changed", "same"]
NormalizeStatus = Literal["ok", "ambiguous", "invalid"]
# `derived`: a page-URL derivation, which no candidate states; no candidate row carries it.
SourceKind = Literal[
    "adapter", "api", "jsonld", "microdata", "og", "js_state", "xhr", "config", "llm", "derived", "dom"
]
# `pending_approval`: a learned config waiting for a person to approve it (`project.require_config_approval`).
ConfigStatus = Literal["draft", "active", "suspended", "pending_approval"]
ConfigOrigin = Literal["adapter", "induced", "llm_synth", "operator", "located"]
# `extraction_config.field_status` values.
FieldStatus = Literal["corroborated", "uncorroborated", "operator", "suspended", "absent"]
ProgressiveMode = Literal["cold", "learning", "active"]
# How a surface's pages are rendered for fields: HTTP, a locate run then the fetch its
# field sources need, or every page in the browser.
RenderMode = Literal["off", "locate", "always"]
# What `usage_event` counts, each in whole units: calls, tokens, seconds, bytes, requests.
UsageResource = Literal[
    "llm_calls", "llm_tokens_in", "llm_tokens_out", "browser_seconds", "proxy_bytes", "http_requests"
]
WebhookStatus = Literal["pending", "delivered", "failed"]
# What a job's webhook can be sent (`snowcrawl.db.webhook_outbox`): a page its task finished, a record a task stored
# (and, for a new version, `record_changed`), a URL a recurring crawl no longer finds, and the job's end.
WebhookEventType = Literal[
    "page", "record", "record_changed", "url_gone", "job_completed", "job_failed", "job_cancelled"
]
# The job kinds a schedule submits (`snowcrawl.services.schedules`): those with `queued_tasks` (`db.jobs.KindTraits`); a
# scheduled scrape is stored as the batch of its one URL.
ScheduledKind = Literal["batch_scrape", "crawl", "extract"]


def one_of(column: str, values: Any) -> str:
    """The CHECK expression restricting `column` to the members of a `Literal`."""
    return f"{column} IN ({', '.join(repr(value) for value in get_args(values))})"


TYPE_ANNOTATION_MAP: dict[Any, Any] = dict.fromkeys(
    (
        ProjectId,
        ApiKeyId,
        SiteId,
        SchemaId,
        JobId,
        UrlTaskId,
        JobEventId,
        CaptureId,
        ExtractionId,
        CandidateId,
        RecordId,
        ExtractionConfigId,
        UsageEventId,
        ScheduleId,
        WebhookEventId,
        WebhookDeliveryId,
    ),
    Uuid,
)
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = TYPE_ANNOTATION_MAP
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def empty_object_default() -> TextClause:
    """The server default of a JSONB object column: an empty object."""
    return text("'{}'::jsonb")


def empty_array_default() -> TextClause:
    """The server default of a JSONB array column: an empty array."""
    return text("'[]'::jsonb")


def version_positive() -> CheckConstraint:
    """Versions count from 1."""
    return CheckConstraint("version >= 1", name="version_positive")


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (
        CheckConstraint("llm_call_cap >= 0", name="llm_call_cap_non_negative"),
        CheckConstraint("draft_call_cap >= 0", name="draft_call_cap_non_negative"),
    )

    id: Mapped[ProjectId] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = created_at_column()
    # The most LLM calls a job of the project may make when its request sets no `budget.llm_calls`; NULL: no cap
    # (owner, 2026-10-16).
    llm_call_cap: Mapped[int | None] = mapped_column(Integer)
    # The most uncharged role S calls (field-name drafts) one API key may make, in all; NULL: no cap.
    draft_call_cap: Mapped[int | None] = mapped_column(Integer)
    # Whether a learned config waits for approval before it serves; off: it activates once validated.
    require_config_approval: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ApiKey(Base):
    __tablename__ = "api_key"
    __table_args__ = (CheckConstraint("draft_calls >= 0", name="draft_calls_non_negative"),)

    id: Mapped[ApiKeyId] = mapped_column(primary_key=True, default=uuid7)
    hashed_key: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    webhook_secret: Mapped[str] = mapped_column(Text)
    # The uncharged role S calls the key has made (`llm.drafts.take_draft_call`); `project.draft_call_cap` bounds it.
    draft_calls: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class Site(Base):
    __tablename__ = "site"
    __table_args__ = (CheckConstraint("host = lower(host)", name="host_lowercase"),)

    id: Mapped[SiteId] = mapped_column(primary_key=True, default=uuid7)
    project_id: Mapped[ProjectId] = mapped_column(ForeignKey(Project.id))
    host: Mapped[str] = mapped_column(Text, unique=True)
    platform: Mapped[str | None] = mapped_column(Text)
    robots_snapshot: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    # The adapters that declined the host (`adapters.registry`), each with the time (ISO 8601, UTC) until
    # which it is not asked again.
    adapter_declined: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=empty_object_default())


class SiteContract(Base):
    """The acquisition contract of a site's pages of one kind: how they were last fetched with an `ok` that HTTP alone
    did not give, learned from the fetch, and how
    the pages extracted since. No row, or a stale one (`failures` at `STALE_FAILURES`), is the default: HTTP.
    `checked_at` starts the 7-day re-check's clock."""

    __tablename__ = "site_contract"
    __table_args__ = (
        CheckConstraint(one_of("page_kind", PageKind), name="page_kind"),
        CheckConstraint(one_of("method", FetchMethod), name="method"),
        CheckConstraint(one_of("engine", BrowserEngine), name="engine"),
        CheckConstraint("failures >= 0", name="failures_non_negative"),
    )

    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id, ondelete="CASCADE"), primary_key=True)
    page_kind: Mapped[PageKind] = mapped_column(Text, primary_key=True)
    # The rung the pages start with: `http` also when the browser was needed only to pass a block (`handoff`).
    method: Mapped[FetchMethod] = mapped_column(Text)
    # The browser engine whose `ok` taught the contract; None when HTTP did.
    engine: Mapped[BrowserEngine | None] = mapped_column(Text)
    # The browser was needed only to pass a block, and its cookies went on to HTTP (`acquire.session`).
    handoff: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The last extraction with a record: the schema's field names it `found` and those `missing`.
    coverage: Mapped[dict[str, list[str]] | None] = mapped_column(JSONB)
    # Consecutive extractions without a record since the last with one; a block is never one.
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SiteBlock(Base):
    """A site's recent blocks: short-lived, apart from
    the durable contract. No row: no block since the last HTTP success, or none within `BLOCK_TTL`."""

    __tablename__ = "site_block"
    __table_args__ = (CheckConstraint("blocks > 0", name="blocks_positive"),)

    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id, ondelete="CASCADE"), primary_key=True)
    blocks: Mapped[int] = mapped_column(Integer)
    last_block_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The vendor the last block's challenge named (`AttemptRecord.challenge`), when one did.
    vendor: Mapped[str | None] = mapped_column(Text)
    # Until when the site's pages start in the browser, once `BROWSER_FIRST_BLOCKS` blocks fell within `BLOCK_TTL`.
    browser_first_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SiteCookieState(Base):
    """The cookies a temporary profile's browser context held after an `ok` page of a site, per registrable domain,
    encrypted (`acquire.cookie_store`); `fingerprint` (an HMAC of the plain
        state) says whether a new state differs. No row: none is kept."""

    __tablename__ = "site_cookie_state"
    __table_args__ = (CheckConstraint(one_of("engine", BrowserEngine), name="engine"),)

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    engine: Mapped[BrowserEngine] = mapped_column(Text, primary_key=True)
    state: Mapped[bytes] = mapped_column(LargeBinary)
    fingerprint: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HostTurn(Base):
    """A host's turn and hold, shared by every process that fetches (`acquire/host_turns.py`): when its next request may
    go, and the fetch holding it, until `held_until` unless renewed.
    Keyed by the URL's host, as the limiter is, not by site. Unlogged: the state is seconds long, so a server crash
    that empties it loses one interval's spacing at most (PostgreSQL CREATE TABLE, UNLOGGED)."""

    __tablename__ = "host_turn"
    __table_args__ = ({"prefixes": ["UNLOGGED"]},)

    host: Mapped[str] = mapped_column(Text, primary_key=True)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    holder: Mapped[str | None] = mapped_column(Text)
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchemaDef(Base):
    __tablename__ = "schema_def"
    __table_args__ = (
        CheckConstraint(one_of("page_kind", PageKind), name="page_kind"),
        CheckConstraint(one_of("origin", SchemaOrigin), name="origin"),
        CheckConstraint(one_of("status", SchemaStatus), name="status"),
    )

    id: Mapped[SchemaId] = mapped_column(primary_key=True, default=uuid7)
    # One row per schema, edited in place.
    name: Mapped[str] = mapped_column(Text, unique=True)
    page_kind: Mapped[PageKind] = mapped_column(Text)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The field names in the order the document gave them: JSONB keeps no key order (`schemas.types.SchemaDef`).
    field_order: Mapped[list[str]] = mapped_column(JSONB, server_default=empty_array_default())
    required_groups: Mapped[list[list[str]]] = mapped_column(JSONB, server_default=empty_array_default())
    origin: Mapped[SchemaOrigin] = mapped_column(Text)
    status: Mapped[SchemaStatus] = mapped_column(Text)
    # Role S's drafted label synonyms by field (`schemas.types.SchemaDef.labels`); null until drafted.
    labels: Mapped[dict[str, list[str]] | None] = mapped_column(JSONB)


class Capture(Base):
    """A fetched page, whose bytes are the bundle at `object_key`."""

    __tablename__ = "capture"
    __table_args__ = (
        CheckConstraint(one_of("method", CaptureMethod), name="method"),
        CheckConstraint(one_of("block_class", BlockClass), name="block_class"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        CheckConstraint("duration_ms >= 0", name="duration_ms_non_negative"),
        # `expire_captures` deletes rows past `expires_at`; a capture without expiry stays out of the index.
        Index(None, "expires_at", postgresql_where=text("expires_at IS NOT NULL")),
        # A site's recent captures: `scrape`'s `max_age` reuse.
        Index(None, "site_id", "fetched_at"),
    )

    id: Mapped[CaptureId] = mapped_column(primary_key=True, default=uuid7)
    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id))
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[CaptureMethod] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    final_url: Mapped[str] = mapped_column(Text)
    block_class: Mapped[BlockClass] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    html_sha256: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_ms: Mapped[int] = mapped_column(Integer)
    fetch_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())


class ExtractionConfig(Base):
    __tablename__ = "extraction_config"
    __table_args__ = (
        UniqueConstraint("site_id", "schema_id", "page_kind", "version"),
        version_positive(),
        CheckConstraint(one_of("page_kind", PageKind), name="page_kind"),
        CheckConstraint(one_of("status", ConfigStatus), name="status"),
        CheckConstraint(one_of("origin", ConfigOrigin), name="origin"),
        # One `active` config per (site, schema, page_kind) and pattern shape: the learned config (`''`) and each
        # custom recipe's pattern set.
        Index(
            "uq_extraction_config_active",
            "site_id",
            "schema_id",
            "page_kind",
            "pattern_shape",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[ExtractionConfigId] = mapped_column(primary_key=True, default=uuid7)
    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id))
    schema_id: Mapped[SchemaId] = mapped_column(ForeignKey(SchemaDef.id))
    page_kind: Mapped[PageKind] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[ConfigStatus] = mapped_column(Text, server_default=text("'draft'"))
    ir: Mapped[dict[str, Any]] = mapped_column(JSONB)
    origin: Mapped[ConfigOrigin] = mapped_column(Text)
    field_status: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Per field, `[matched, valid]` for each of the last pages the config ran on.
    drift_window: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())
    # A custom recipe's URL patterns (`ir.patterns`) and their shape (`url_pattern.patterns_shape`); `[]` and `''`
    # for a learned config.
    patterns: Mapped[list[str]] = mapped_column(JSONB, server_default=empty_array_default())
    pattern_shape: Mapped[str] = mapped_column(Text, server_default=text("''"))


class ProgressiveState(Base):
    """How far a (site, schema, page kind) is on the way to an active config."""

    __tablename__ = "progressive_state"
    __table_args__ = (
        CheckConstraint(one_of("page_kind", PageKind), name="page_kind"),
        CheckConstraint(one_of("mode", ProgressiveMode), name="mode"),
        CheckConstraint("pages_extracted >= 0", name="pages_extracted_non_negative"),
        CheckConstraint("mode <> 'learning' OR learning_started_at IS NOT NULL", name="learning_has_started"),
        CheckConstraint("mode <> 'active' OR active_config_id IS NOT NULL", name="active_has_config"),
        CheckConstraint("retry_at_pages >= 0", name="retry_at_pages_non_negative"),
        CheckConstraint(one_of("render_mode", RenderMode), name="render_mode"),
    )

    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id), primary_key=True)
    schema_id: Mapped[SchemaId] = mapped_column(ForeignKey(SchemaDef.id), primary_key=True)
    page_kind: Mapped[PageKind] = mapped_column(Text, primary_key=True)
    mode: Mapped[ProgressiveMode] = mapped_column(Text, server_default=text("'cold'"))
    pages_extracted: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Set while a `learn` job runs for this state, in any mode.
    learning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When a learning run last activated a config.
    last_learned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_config_id: Mapped[ExtractionConfigId | None] = mapped_column(ForeignKey(ExtractionConfig.id))
    # The `pages_extracted` at which learning is tried again after a failed run; NULL until one fails.
    retry_at_pages: Mapped[int | None] = mapped_column(Integer)
    # The surface's `render.mode`: the last one a job set, which a job submitted without one inherits.
    render_mode: Mapped[RenderMode] = mapped_column(Text, server_default=text("'off'"))


class UrlState(Base):
    """What recurring runs last saw at a URL of a site."""

    __tablename__ = "url_state"
    __table_args__ = (CheckConstraint("missing_runs >= 0", name="missing_runs_non_negative"),)

    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id), primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    missing_runs: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The crawl whose listing last held the URL; NULL while no listing has.
    last_seen_job_id: Mapped[JobId | None] = mapped_column(ForeignKey("job.id", ondelete=_SET_NULL))


class Schedule(Base):
    """A recurring run (§22 "Recurring schedule storage", admitted 2026-10-17): the request of a queued job kind,
    submitted by the scheduler at each due time of its 5-field cron expression in `timezone`, or every `interval_s`.
    Its jobs point back at it (`job.schedule_id`); the last one is its newest."""

    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint(one_of("kind", ScheduledKind), name="kind"),
        CheckConstraint("(cron IS NULL) <> (interval_s IS NULL)", name="one_cadence"),
        CheckConstraint("interval_s >= 60", name="interval_at_least_a_minute"),
        # The scheduler reads the due schedules that are not paused.
        Index(None, "next_run_at", postgresql_where=text("NOT paused")),
    )

    id: Mapped[ScheduleId] = mapped_column(primary_key=True, default=uuid7)
    project_id: Mapped[ProjectId] = mapped_column(ForeignKey(Project.id))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[ScheduledKind] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cron: Mapped[str | None] = mapped_column(Text)
    interval_s: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(Text)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why its last due run submitted no job (a refused target, globs that matched nothing); NULL once one is submitted.
    last_error: Mapped[str | None] = mapped_column(Text)
    # The key that created it: its jobs are submitted as that key's, which signs their webhooks.
    api_key_id: Mapped[ApiKeyId | None] = mapped_column(ForeignKey(ApiKey.id))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(one_of("kind", JobKind), name="kind"),
        CheckConstraint(one_of("status", JobStatus), name="status"),
        CheckConstraint(one_of("egress_basis", EgressBasis), name="egress_basis"),
        # The scheduler's reapers look only at running jobs, oldest start first (`db.queue.reap_stuck_jobs`).
        Index(None, "started_at", postgresql_where=text("status = 'running'")),
        # A schedule's jobs, newest first (its history and its last job).
        Index(None, "schedule_id", "id"),
    )

    id: Mapped[JobId] = mapped_column(primary_key=True, default=uuid7)
    project_id: Mapped[ProjectId] = mapped_column(ForeignKey(Project.id))
    kind: Mapped[JobKind] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schema_id: Mapped[SchemaId | None] = mapped_column(ForeignKey(SchemaDef.id))
    status: Mapped[JobStatus] = mapped_column(Text, server_default=text("'queued'"))
    webhook_url: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str] = mapped_column(Text)
    # What its pages run with, pinned at submit (`services.batch.JobPins`); NULL without records.
    pinned: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())
    created_at: Mapped[datetime] = created_at_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The key that submitted the job: its `webhook_secret` signs the job's webhook. NULL for a job no
    # key submitted (a learning run).
    api_key_id: Mapped[ApiKeyId | None] = mapped_column(ForeignKey(ApiKey.id))
    # A crawl: where its start page is, canonical, after the site's own redirects (`crawl.rules.located`). The start
    # task writes it: it gives the scope's host and the one URL already fetched. NULL before then, and for other kinds.
    start_location: Mapped[str | None] = mapped_column(Text)
    # The events its webhook is sent (`WebhookEventType`) and how they are grouped into deliveries
    # (`snowcrawl.db.webhook_outbox.WebhookBatch`, NULL: one event a delivery), from the request.
    webhook_events: Mapped[list[str]] = mapped_column(JSONB, server_default=empty_array_default())
    webhook_batch: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The schedule that submitted it; NULL for a job a caller submitted, and once its schedule is deleted.
    schedule_id: Mapped[ScheduleId | None] = mapped_column(ForeignKey(Schedule.id, ondelete=_SET_NULL))
    # The country every fetch of the job goes out from, pinned at submit with its proxy pool (`services.location`), and
    # how it is known (`core.egress`); NULL and `unknown` when nobody declared it.
    egress_country: Mapped[str | None] = mapped_column(Text)
    egress_basis: Mapped[EgressBasis] = mapped_column(Text, server_default=text("'unknown'"))


class UrlTask(Base):
    __tablename__ = "url_task"
    __table_args__ = (
        CheckConstraint(one_of("page_kind", PageKind), name="page_kind"),
        CheckConstraint(one_of("status", TaskStatus), name="status"),
        CheckConstraint(one_of("outcome", TaskOutcome), name="outcome"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        Index(None, "status", "next_attempt_at", "priority"),
        # A crawl enqueues each canonical URL once per job. Its index, led by `job_id`, also serves the
        # per-job task counts that settle a job (`snowcrawl.db.queue`).
        UniqueConstraint("job_id", "canonical_url"),
        # Settling a job first asks whether any of its tasks is still open (`snowcrawl.db.queue`).
        Index(None, "job_id", postgresql_where=text("status IN ('queued', 'claimed')")),
        # A job's settled tasks by outcome, for the listing of a large crawl (`services.jobs`).
        Index(None, "job_id", "status", "outcome"),
        # A job's tasks in id order, which its results and its records page by (keyset, `db.paging`).
        Index(None, "job_id", "id"),
        # A crawl takes no URL a task of the job was already fetched at (`crawl.frontier`).
        Index(None, "job_id", "final_url", postgresql_where=text("final_url IS NOT NULL")),
    )

    id: Mapped[UrlTaskId] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    page_kind: Mapped[PageKind | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(Text, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Captures expire after 30 days and the task outlives its capture. Indexed for that delete.
    capture_id: Mapped[CaptureId | None] = mapped_column(ForeignKey(Capture.id, ondelete=_SET_NULL), index=True)
    outcome: Mapped[TaskOutcome | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    depth: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    parent_task_id: Mapped[UrlTaskId | None] = mapped_column(ForeignKey(f"{__tablename__}.id"))
    # Where the task's page was fetched, after its redirects, in canonical form (`normalize.url_key`); NULL until then.
    final_url: Mapped[str | None] = mapped_column(Text)
    # A crawl's lease order within its priority, higher first (`crawl.scoring`); NULL (breadth first) leases after it.
    score: Mapped[float | None] = mapped_column(Float)


class JobEvent(Base):
    """A run event of a job, stored so that SSE clients can resume."""

    __tablename__ = "job_event"
    # The event stream reads a job's events in id order, after the last one sent.
    __table_args__ = (Index(None, "job_id", "id"),)

    id: Mapped[JobEventId] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())


class WebhookDelivery(Base):
    """One POST of a job's webhook: the events assigned to it, `pending` until the target accepts it or
    its attempts run out (`snowcrawl.api.webhooks`)."""

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        CheckConstraint(one_of("status", WebhookStatus), name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # The scheduler reads the pending deliveries that are due.
        Index(None, "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index(None, "job_id"),
        # Retention reads the ended deliveries, oldest first (`scheduler.retention.expire_webhooks`).
        Index(None, "created_at", postgresql_where=text("status <> 'pending'")),
    )

    id: Mapped[WebhookDeliveryId] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    status: Mapped[WebhookStatus] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    # The HTTP status the target answered the last attempt with; NULL before one, or when it gave no response.
    response_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()


class WebhookEvent(Base):
    """An event a job's webhook is owed, written in the transaction that makes it happen (the outbox): it waits
    unassigned until the scheduler groups it into a delivery (`snowcrawl.db.webhook_outbox`)."""

    __tablename__ = "webhook_event"
    __table_args__ = (
        CheckConstraint(one_of("event", WebhookEventType), name="event"),
        # The scheduler groups each job's unassigned events, oldest first.
        Index(None, "job_id", "id", postgresql_where=text("delivery_id IS NULL")),
        # A delivery's events, in order, for its body.
        Index(None, "delivery_id", "id"),
    )

    id: Mapped[WebhookEventId] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    event: Mapped[WebhookEventType] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=empty_object_default())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    delivery_id: Mapped[WebhookDeliveryId | None] = mapped_column(ForeignKey(WebhookDelivery.id))


class UsageEvent(Base):
    """One use of a counted resource by a job: for visibility and budgets, not billing."""

    __tablename__ = "usage_event"
    __table_args__ = (
        CheckConstraint(one_of("resource", UsageResource), name="resource"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        # A job's events, for the LLM cost on its summary.
        Index(None, "job_id", "id"),
    )

    id: Mapped[UsageEventId] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    resource: Mapped[UsageResource] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model_or_pool: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(BigInteger)
    # `metadata` is taken by the declarative base; the column keeps the §6 name.
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=empty_object_default())


class Extraction(Base):
    """One resolver run over a capture."""

    __tablename__ = "extraction"
    __table_args__ = (
        CheckConstraint(one_of("mode", ExtractionMode), name="mode"),
        CheckConstraint("llm_calls >= 0", name="llm_calls_non_negative"),
    )

    id: Mapped[ExtractionId] = mapped_column(primary_key=True, default=uuid7)
    # Indexed for a finished task's events, which report its extraction.
    url_task_id: Mapped[UrlTaskId] = mapped_column(ForeignKey(UrlTask.id), index=True)
    # Captures expire after 30 days and the extraction, which records cite, stays. Indexed for
    # that delete.
    capture_id: Mapped[CaptureId | None] = mapped_column(ForeignKey(Capture.id, ondelete=_SET_NULL), index=True)
    engine_version: Mapped[str] = mapped_column(Text)
    config_version: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[ExtractionMode] = mapped_column(Text)
    llm_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # What the collectors skipped and why: an invalid JSON block, a collector that raised.
    diagnostics: Mapped[list[str]] = mapped_column(JSONB, server_default=empty_array_default())


class Candidate(Base):
    """One value a collector proposed for a field. Kept 30 days."""

    __tablename__ = "candidate"
    __table_args__ = (
        CheckConstraint(one_of("normalize_status", NormalizeStatus), name="normalize_status"),
        CheckConstraint(one_of("source_kind", SourceKind), name="source_kind"),
        # `expire_candidates` deletes rows older than 30 days.
        Index(None, "created_at"),
    )

    id: Mapped[CandidateId] = mapped_column(primary_key=True, default=uuid7)
    extraction_id: Mapped[ExtractionId] = mapped_column(ForeignKey(Extraction.id), index=True)
    entity_ref: Mapped[str] = mapped_column(Text)
    field: Mapped[str] = mapped_column(Text)
    raw_value: Mapped[Any] = mapped_column(JSONB)
    normalized_value: Mapped[Any | None] = mapped_column(JSONB)
    normalize_status: Mapped[NormalizeStatus] = mapped_column(Text)
    source_kind: Mapped[SourceKind] = mapped_column(Text)
    independent: Mapped[bool] = mapped_column(Boolean)
    locator: Mapped[str] = mapped_column(Text)
    chosen: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class Record(Base):
    """A published entity: `data` holds published fields only. A new version points at the one it
    replaces through `supersedes_record_id`."""

    __tablename__ = "record"
    __table_args__ = (
        CheckConstraint(one_of("mode", ExtractionMode), name="mode"),
        CheckConstraint(one_of("egress_basis", EgressBasis), name="egress_basis"),
        version_positive(),
        # A record is compared with the latest version of its entity per (site, entity, schema)
        # (`recurring.versions.latest_versions`).
        Index(None, "site_id", "entity_key", "schema_id", "id"),
        # The change feed (`changed_since`), and the commit stamp of the baseline migration's trigger.
        Index(None, "created_at"),
        # `GET /v1/records` by job or by schema, newest first by id, and a capture's records for replay.
        Index(None, "job_id", "id"),
        Index(None, "schema_id", "id"),
        Index(None, "extraction_id"),
    )

    id: Mapped[RecordId] = mapped_column(primary_key=True, default=uuid7)
    extraction_id: Mapped[ExtractionId] = mapped_column(ForeignKey(Extraction.id))
    job_id: Mapped[JobId] = mapped_column(ForeignKey(Job.id))
    site_id: Mapped[SiteId] = mapped_column(ForeignKey(Site.id))
    schema_id: Mapped[SchemaId] = mapped_column(ForeignKey(SchemaDef.id))
    entity_key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    field_meta: Mapped[dict[str, Any]] = mapped_column(JSONB)
    complete: Mapped[bool] = mapped_column(Boolean)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, server_default=empty_array_default())
    mode: Mapped[ExtractionMode] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    # The digest of the schema document the record was extracted with (`schemas.types.document_digest`): a
    # schema is edited in place, so a record follows the current document only when it is equal.
    schema_digest: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    supersedes_record_id: Mapped[RecordId | None] = mapped_column(ForeignKey(f"{__tablename__}.id"))
    # The country the page was fetched from and how it is known (`core.egress`), the job's pinned egress when the record
    # was stored; NULL and `unknown` when nobody declared it.
    egress_country: Mapped[str | None] = mapped_column(Text)
    egress_basis: Mapped[EgressBasis] = mapped_column(Text, server_default=text("'unknown'"))


class TaskRecord(Base):
    """A record a task's page stands for: one it stored, or the entity's latest version when the
    page's record equalled it and stored nothing. A job's records are those of its tasks (the results), and an
    extraction's are those it stood for, in the page's order. Plumbing for `url_task.record_id`, as a table because a
    listing page stands for one record per row."""

    __tablename__ = "task_record"
    __table_args__ = (CheckConstraint(one_of("delta", RecordDelta), name="delta"),)

    url_task_id: Mapped[UrlTaskId] = mapped_column(ForeignKey(UrlTask.id, ondelete="CASCADE"), primary_key=True)
    # Indexed for the records a job lists, which join on it, and for a record's delete.
    record_id: Mapped[RecordId] = mapped_column(ForeignKey(Record.id, ondelete="CASCADE"), primary_key=True, index=True)
    # The extraction whose page stood for the record, and the record's place among its records (a listing's row). An
    # extraction that stored nothing because its record equalled the entity's latest version still has its values here.
    extraction_id: Mapped[ExtractionId] = mapped_column(ForeignKey(Extraction.id), index=True)
    position: Mapped[int] = mapped_column(Integer)
    # What the record is against the entity's previous record when the task stood for it: `new` (none before), `changed`
    # (a value differs: `record.changed_fields`) or `same` (equal values).
    delta: Mapped[RecordDelta] = mapped_column(Text)
    # When the task stood for the record.
    created_at: Mapped[datetime] = created_at_column()
