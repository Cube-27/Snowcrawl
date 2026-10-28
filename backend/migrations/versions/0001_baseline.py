"""Baseline: the whole main database, from the models in `snowcrawl.db.models`.

The project is greenfield (CLAUDE.md): a schema change edits this revision, and databases are recreated, never
migrated with data steps. Besides the tables it holds the `default` project and the trigger that stamps a record's
`created_at` at commit, which `changed_since` reads (`services.records`, whose `_committed` says why).

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from snowcrawl.core.ids import uuid7

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Column defaults, the page kind check and foreign key targets, each stated by more than one table.
_NOW = "now()"
_EMPTY_OBJECT = "'{}'::jsonb"
_EMPTY_ARRAY = "'[]'::jsonb"
_PAGE_KINDS = "page_kind IN ('detail', 'listing')"
_SET_NULL = "SET NULL"
_PROJECT_ID = "project.id"
_SITE_ID = "site.id"
_SCHEMA_DEF_ID = "schema_def.id"
_JOB_ID = "job.id"
_URL_TASK_ID = "url_task.id"
_EXTRACTION_ID = "extraction.id"

# A record's `created_at` is set again as its transaction commits: a deferred constraint trigger fires at commit
# (PostgreSQL §43.10), once per transaction, and holds `services.records.RECORD_FEED_LOCK` shared until the commit is
# visible. The transaction's records are the rows whose `created_at` is still its start, `now()` (the column's
# default). The lock is blake2b("record_feed", digest_size=8) as a signed bigint.
_RECORD_COMMITTED = """
    CREATE FUNCTION record_committed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF current_setting('snowcrawl.records_committed', true) = 'on' THEN
            RETURN NULL;
        END IF;
        PERFORM set_config('snowcrawl.records_committed', 'on', true);
        PERFORM pg_advisory_xact_lock_shared(-7838622165994782610);
        UPDATE record SET created_at = clock_timestamp() WHERE created_at = now();
        RETURN NULL;
    END
    $$
"""


def upgrade() -> None:
    op.create_table(
        "api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hashed_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_secret", sa.Text(), nullable=False),
        sa.Column("draft_calls", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("draft_calls >= 0", name=op.f("ck_api_key_draft_calls_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key")),
        sa.UniqueConstraint("hashed_key", name=op.f("uq_api_key_hashed_key")),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("llm_call_cap", sa.Integer(), nullable=True),
        sa.Column("draft_call_cap", sa.Integer(), nullable=True),
        sa.Column("require_config_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("llm_call_cap >= 0", name=op.f("ck_project_llm_call_cap_non_negative")),
        sa.CheckConstraint("draft_call_cap >= 0", name=op.f("ck_project_draft_call_cap_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project")),
        sa.UniqueConstraint("name", name=op.f("uq_project_name")),
    )
    op.create_table(
        "schema_def",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("page_kind", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "field_order",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_ARRAY),
            nullable=False,
        ),
        sa.Column(
            "required_groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_ARRAY),
            nullable=False,
        ),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("origin IN ('preset', 'user', 'prompt_draft')", name=op.f("ck_schema_def_origin")),
        sa.CheckConstraint(_PAGE_KINDS, name=op.f("ck_schema_def_page_kind")),
        sa.CheckConstraint("status IN ('active', 'retired')", name=op.f("ck_schema_def_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schema_def")),
        sa.UniqueConstraint("name", name=op.f("uq_schema_def_name")),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("pinned", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "usage", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(_EMPTY_OBJECT), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("start_location", sa.Text(), nullable=True),
        sa.Column(
            "webhook_events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_ARRAY),
            nullable=False,
        ),
        sa.Column("webhook_batch", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("egress_country", sa.Text(), nullable=True),
        sa.Column("egress_basis", sa.Text(), server_default=sa.text("'unknown'"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('scrape', 'batch_scrape', 'map', 'crawl', 'extract', 'learn')", name=op.f("ck_job_kind")
        ),
        sa.CheckConstraint("egress_basis IN ('pool', 'machine', 'unknown')", name=op.f("ck_job_egress_basis")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')", name=op.f("ck_job_status")
        ),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_key.id"], name=op.f("fk_job_api_key_id_api_key")),
        sa.ForeignKeyConstraint(["project_id"], [_PROJECT_ID], name=op.f("fk_job_project_id_project")),
        sa.ForeignKeyConstraint(["schema_id"], [_SCHEMA_DEF_ID], name=op.f("fk_job_schema_id_schema_def")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    op.create_index(
        op.f("ix_job_started_at"),
        "job",
        ["started_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "site",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("robots_snapshot", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column(
            "adapter_declined",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_OBJECT),
            nullable=False,
        ),
        sa.CheckConstraint("host = lower(host)", name=op.f("ck_site_host_lowercase")),
        sa.ForeignKeyConstraint(["project_id"], [_PROJECT_ID], name=op.f("fk_site_project_id_project")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site")),
        sa.UniqueConstraint("host", name=op.f("uq_site_host")),
    )
    op.create_table(
        "site_contract",
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("page_kind", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=True),
        sa.Column("handoff", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("engine IN ('patchright')", name=op.f("ck_site_contract_engine")),
        sa.CheckConstraint("failures >= 0", name=op.f("ck_site_contract_failures_non_negative")),
        sa.CheckConstraint("method IN ('http', 'browser')", name=op.f("ck_site_contract_method")),
        sa.CheckConstraint(_PAGE_KINDS, name=op.f("ck_site_contract_page_kind")),
        sa.ForeignKeyConstraint(
            ["site_id"], [_SITE_ID], name=op.f("fk_site_contract_site_id_site"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("site_id", "page_kind", name=op.f("pk_site_contract")),
    )
    op.create_table(
        "site_block",
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("blocks", sa.Integer(), nullable=False),
        sa.Column("last_block_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=True),
        sa.Column("browser_first_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("blocks > 0", name=op.f("ck_site_block_blocks_positive")),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_site_block_site_id_site"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("site_id", name=op.f("pk_site_block")),
    )
    op.create_table(
        "site_cookie_state",
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("engine IN ('patchright')", name=op.f("ck_site_cookie_state_engine")),
        sa.PrimaryKeyConstraint("domain", "engine", name=op.f("pk_site_cookie_state")),
    )
    op.create_table(
        "host_turn",
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("holder", sa.Text(), nullable=True),
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("host", name=op.f("pk_host_turn")),
        prefixes=["UNLOGGED"],
    )
    op.create_table(
        "capture",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("block_class", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("html_sha256", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "fetch_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_OBJECT),
            nullable=False,
        ),
        sa.CheckConstraint(
            "block_class IN ('ok', 'blocked', 'shell', 'not_found', 'error', 'auth_wall', 'soft_404', 'geo_redirect',"
            " 'redirected_to_listing', 'unsupported_media')",
            name=op.f("ck_capture_block_class"),
        ),
        sa.CheckConstraint("method IN ('http', 'browser', 'api')", name=op.f("ck_capture_method")),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_capture_duration_ms_non_negative")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_capture_size_bytes_non_negative")),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_capture_site_id_site")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture")),
        sa.UniqueConstraint("object_key", name=op.f("uq_capture_object_key")),
    )
    op.create_index(
        op.f("ix_capture_expires_at"),
        "capture",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(op.f("ix_capture_site_id_fetched_at"), "capture", ["site_id", "fetched_at"], unique=False)
    op.create_table(
        "extraction_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("page_kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("ir", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column(
            "field_status",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_OBJECT),
            nullable=False,
        ),
        sa.Column("validation_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "drift_window",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_OBJECT),
            nullable=False,
        ),
        sa.Column(
            "patterns", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(_EMPTY_ARRAY), nullable=False
        ),
        sa.Column("pattern_shape", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            "origin IN ('adapter', 'induced', 'llm_synth', 'operator', 'located')",
            name=op.f("ck_extraction_config_origin"),
        ),
        sa.CheckConstraint(_PAGE_KINDS, name=op.f("ck_extraction_config_page_kind")),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'suspended', 'pending_approval')", name=op.f("ck_extraction_config_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_extraction_config_version_positive")),
        sa.ForeignKeyConstraint(
            ["schema_id"], [_SCHEMA_DEF_ID], name=op.f("fk_extraction_config_schema_id_schema_def")
        ),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_extraction_config_site_id_site")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_config")),
        sa.UniqueConstraint(
            "site_id",
            "schema_id",
            "page_kind",
            "version",
            name=op.f("uq_extraction_config_site_id_schema_id_page_kind_version"),
        ),
    )
    op.create_index(
        "uq_extraction_config_active",
        "extraction_config",
        ["site_id", "schema_id", "page_kind", "pattern_shape"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "job_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(_EMPTY_OBJECT), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_job_event_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_event")),
    )
    op.create_index(op.f("ix_job_event_job_id_id"), "job_event", ["job_id", "id"], unique=False)
    op.create_table(
        "usage_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model_or_pool", sa.Text(), nullable=True),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(_EMPTY_OBJECT), nullable=False
        ),
        sa.CheckConstraint(
            "resource IN ('llm_calls', 'llm_tokens_in', 'llm_tokens_out', 'browser_seconds', 'proxy_bytes',"
            " 'http_requests')",
            name=op.f("ck_usage_event_resource"),
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_usage_event_quantity_non_negative")),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_usage_event_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_event")),
    )
    op.create_index(op.f("ix_usage_event_job_id_id"), "usage_event", ["job_id", "id"], unique=False)
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'failed')", name=op.f("ck_webhook_delivery_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_webhook_delivery_attempts_non_negative")),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_webhook_delivery_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_delivery")),
    )
    op.create_index(op.f("ix_webhook_delivery_job_id"), "webhook_delivery", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_webhook_delivery_created_at"),
        "webhook_delivery",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status <> 'pending'"),
    )
    op.create_index(
        op.f("ix_webhook_delivery_next_attempt_at"),
        "webhook_delivery",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "progressive_state",
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("page_kind", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), server_default=sa.text("'cold'"), nullable=False),
        sa.Column("pages_extracted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("learning_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_learned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_config_id", sa.Uuid(), nullable=True),
        sa.Column("retry_at_pages", sa.Integer(), nullable=True),
        sa.Column("render_mode", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.CheckConstraint(
            "mode <> 'active' OR active_config_id IS NOT NULL", name=op.f("ck_progressive_state_active_has_config")
        ),
        sa.CheckConstraint(
            "mode <> 'learning' OR learning_started_at IS NOT NULL",
            name=op.f("ck_progressive_state_learning_has_started"),
        ),
        sa.CheckConstraint("mode IN ('cold', 'learning', 'active')", name=op.f("ck_progressive_state_mode")),
        sa.CheckConstraint(_PAGE_KINDS, name=op.f("ck_progressive_state_page_kind")),
        sa.CheckConstraint("pages_extracted >= 0", name=op.f("ck_progressive_state_pages_extracted_non_negative")),
        sa.CheckConstraint("render_mode IN ('off', 'locate', 'always')", name=op.f("ck_progressive_state_render_mode")),
        sa.CheckConstraint("retry_at_pages >= 0", name=op.f("ck_progressive_state_retry_at_pages_non_negative")),
        sa.ForeignKeyConstraint(
            ["active_config_id"],
            ["extraction_config.id"],
            name=op.f("fk_progressive_state_active_config_id_extraction_config"),
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"], [_SCHEMA_DEF_ID], name=op.f("fk_progressive_state_schema_id_schema_def")
        ),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_progressive_state_site_id_site")),
        sa.PrimaryKeyConstraint("site_id", "schema_id", "page_kind", name=op.f("pk_progressive_state")),
    )
    op.create_table(
        "url_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("page_kind", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("depth", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('extracted', 'partial', 'links_only', 'blocked', 'shell', 'not_found', 'no_match',"
            " 'auth_wall', 'soft_404', 'geo_redirect', 'redirected_to_listing', 'unsupported_media', 'geo_blocked',"
            " 'timeout', 'failed')",
            name=op.f("ck_url_task_outcome"),
        ),
        sa.CheckConstraint(_PAGE_KINDS, name=op.f("ck_url_task_page_kind")),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'done', 'failed', 'cancelled')", name=op.f("ck_url_task_status")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_url_task_attempts_non_negative")),
        sa.CheckConstraint("depth >= 0", name=op.f("ck_url_task_depth_non_negative")),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["capture.id"], name=op.f("fk_url_task_capture_id_capture"), ondelete=_SET_NULL
        ),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_url_task_job_id_job")),
        sa.ForeignKeyConstraint(["parent_task_id"], [_URL_TASK_ID], name=op.f("fk_url_task_parent_task_id_url_task")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_url_task")),
        sa.UniqueConstraint("job_id", "canonical_url", name=op.f("uq_url_task_job_id_canonical_url")),
    )
    op.create_index(op.f("ix_url_task_capture_id"), "url_task", ["capture_id"], unique=False)
    op.create_index(
        op.f("ix_url_task_job_id"),
        "url_task",
        ["job_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_index(
        op.f("ix_url_task_job_id_status_outcome"), "url_task", ["job_id", "status", "outcome"], unique=False
    )
    op.create_index(op.f("ix_url_task_job_id_id"), "url_task", ["job_id", "id"], unique=False)
    op.create_index(
        op.f("ix_url_task_job_id_final_url"),
        "url_task",
        ["job_id", "final_url"],
        unique=False,
        postgresql_where=sa.text("final_url IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_url_task_status_next_attempt_at_priority"),
        "url_task",
        ["status", "next_attempt_at", "priority"],
        unique=False,
    )
    op.create_table(
        "url_state",
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_job_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("missing_runs >= 0", name=op.f("ck_url_state_missing_runs_non_negative")),
        sa.ForeignKeyConstraint(
            ["last_seen_job_id"], [_JOB_ID], name=op.f("fk_url_state_last_seen_job_id_job"), ondelete=_SET_NULL
        ),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_url_state_site_id_site")),
        sa.PrimaryKeyConstraint("site_id", "canonical_url", name=op.f("pk_url_state")),
    )
    op.create_table(
        "extraction",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url_task_id", sa.Uuid(), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=True),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_ARRAY),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('adhoc', 'config')", name=op.f("ck_extraction_mode")),
        sa.CheckConstraint("llm_calls >= 0", name=op.f("ck_extraction_llm_calls_non_negative")),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["capture.id"], name=op.f("fk_extraction_capture_id_capture"), ondelete=_SET_NULL
        ),
        sa.ForeignKeyConstraint(["url_task_id"], [_URL_TASK_ID], name=op.f("fk_extraction_url_task_id_url_task")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction")),
    )
    op.create_index(op.f("ix_extraction_capture_id"), "extraction", ["capture_id"], unique=False)
    op.create_index(op.f("ix_extraction_url_task_id"), "extraction", ["url_task_id"], unique=False)
    op.create_table(
        "candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("entity_ref", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("raw_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("normalize_status", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("independent", sa.Boolean(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("chosen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.CheckConstraint(
            "normalize_status IN ('ok', 'ambiguous', 'invalid')", name=op.f("ck_candidate_normalize_status")
        ),
        sa.CheckConstraint(
            "source_kind IN ('adapter', 'api', 'jsonld', 'microdata', 'og', 'js_state', 'xhr', 'config', 'llm',"
            " 'derived', 'dom')",
            name=op.f("ck_candidate_source_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"], [_EXTRACTION_ID], name=op.f("fk_candidate_extraction_id_extraction")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate")),
    )
    op.create_index(op.f("ix_candidate_created_at"), "candidate", ["created_at"], unique=False)
    op.create_index(op.f("ix_candidate_extraction_id"), "candidate", ["extraction_id"], unique=False)
    op.create_table(
        "record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("field_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column(
            "changed_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(_EMPTY_ARRAY),
            nullable=False,
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("schema_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("supersedes_record_id", sa.Uuid(), nullable=True),
        sa.Column("egress_country", sa.Text(), nullable=True),
        sa.Column("egress_basis", sa.Text(), server_default=sa.text("'unknown'"), nullable=False),
        sa.CheckConstraint("mode IN ('adhoc', 'config')", name=op.f("ck_record_mode")),
        sa.CheckConstraint("egress_basis IN ('pool', 'machine', 'unknown')", name=op.f("ck_record_egress_basis")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_record_version_positive")),
        sa.ForeignKeyConstraint(["extraction_id"], [_EXTRACTION_ID], name=op.f("fk_record_extraction_id_extraction")),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_record_job_id_job")),
        sa.ForeignKeyConstraint(["schema_id"], [_SCHEMA_DEF_ID], name=op.f("fk_record_schema_id_schema_def")),
        sa.ForeignKeyConstraint(["site_id"], [_SITE_ID], name=op.f("fk_record_site_id_site")),
        sa.ForeignKeyConstraint(
            ["supersedes_record_id"], ["record.id"], name=op.f("fk_record_supersedes_record_id_record")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record")),
    )
    op.create_index(op.f("ix_record_created_at"), "record", ["created_at"], unique=False)
    op.create_index(op.f("ix_record_extraction_id"), "record", ["extraction_id"], unique=False)
    op.create_index(op.f("ix_record_job_id_id"), "record", ["job_id", "id"], unique=False)
    op.create_index(op.f("ix_record_schema_id_id"), "record", ["schema_id", "id"], unique=False)
    op.create_index(
        op.f("ix_record_site_id_entity_key_schema_id_id"),
        "record",
        ["site_id", "entity_key", "schema_id", "id"],
        unique=False,
    )
    op.create_table(
        "task_record",
        sa.Column("url_task_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.CheckConstraint("delta IN ('new', 'changed', 'same')", name=op.f("ck_task_record_delta")),
        sa.ForeignKeyConstraint(
            ["extraction_id"], [_EXTRACTION_ID], name=op.f("fk_task_record_extraction_id_extraction")
        ),
        sa.ForeignKeyConstraint(
            ["record_id"], ["record.id"], name=op.f("fk_task_record_record_id_record"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["url_task_id"], [_URL_TASK_ID], name=op.f("fk_task_record_url_task_id_url_task"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("url_task_id", "record_id", name=op.f("pk_task_record")),
    )
    op.create_index(op.f("ix_task_record_record_id"), "task_record", ["record_id"], unique=False)
    op.create_index(op.f("ix_task_record_extraction_id"), "task_record", ["extraction_id"], unique=False)
    _create_schedules_and_webhook_events()
    op.bulk_insert(
        sa.table("project", sa.column("id", sa.Uuid()), sa.column("name", sa.Text())),
        [{"id": uuid7(), "name": "default"}],
    )
    op.execute(_RECORD_COMMITTED)
    op.execute(
        "CREATE CONSTRAINT TRIGGER record_committed AFTER INSERT ON record "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION record_committed()"
    )


def _create_schedules_and_webhook_events() -> None:
    """The recurring schedules (`job.schedule_id` refers to them) and the webhook outbox."""
    op.create_table(
        "schedule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cron", sa.Text(), nullable=True),
        sa.Column("interval_s", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text(_NOW), nullable=False),
        sa.CheckConstraint("kind IN ('batch_scrape', 'crawl', 'extract')", name=op.f("ck_schedule_kind")),
        sa.CheckConstraint("(cron IS NULL) <> (interval_s IS NULL)", name=op.f("ck_schedule_one_cadence")),
        sa.CheckConstraint("interval_s >= 60", name=op.f("ck_schedule_interval_at_least_a_minute")),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_key.id"], name=op.f("fk_schedule_api_key_id_api_key")),
        sa.ForeignKeyConstraint(["project_id"], [_PROJECT_ID], name=op.f("fk_schedule_project_id_project")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
    )
    op.create_index(
        op.f("ix_schedule_next_run_at"),
        "schedule",
        ["next_run_at"],
        unique=False,
        postgresql_where=sa.text("NOT paused"),
    )
    op.create_foreign_key(
        op.f("fk_job_schedule_id_schedule"), "job", "schedule", ["schedule_id"], ["id"], ondelete=_SET_NULL
    )
    op.create_index(op.f("ix_job_schedule_id_id"), "job", ["schedule_id", "id"], unique=False)
    op.create_table(
        "webhook_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(_EMPTY_OBJECT), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.Column("delivery_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "event IN ('page', 'record', 'record_changed', 'url_gone', 'job_completed', 'job_failed', 'job_cancelled')",
            name=op.f("ck_webhook_event_event"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["webhook_delivery.id"], name=op.f("fk_webhook_event_delivery_id_webhook_delivery")
        ),
        sa.ForeignKeyConstraint(["job_id"], [_JOB_ID], name=op.f("fk_webhook_event_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_event")),
    )
    op.create_index(op.f("ix_webhook_event_delivery_id_id"), "webhook_event", ["delivery_id", "id"], unique=False)
    op.create_index(
        op.f("ix_webhook_event_job_id_id"),
        "webhook_event",
        ["job_id", "id"],
        unique=False,
        postgresql_where=sa.text("delivery_id IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER record_committed ON record")
    op.execute("DROP FUNCTION record_committed()")
    # Children before parents; a table's indexes and constraints go with it.
    for table in (
        "task_record",
        "record",
        "candidate",
        "extraction",
        "url_state",
        "url_task",
        "progressive_state",
        "webhook_event",
        "webhook_delivery",
        "usage_event",
        "job_event",
        "extraction_config",
        "capture",
        "site_contract",
        "site_block",
        "site_cookie_state",
        "host_turn",
        "site",
        "job",
        "schedule",
        "schema_def",
        "project",
        "api_key",
    ):
        op.drop_table(table)
