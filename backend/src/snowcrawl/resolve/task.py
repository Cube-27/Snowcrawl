"""What a page task's extraction step keeps, decided without storing anything: the config a page runs
(`ActiveConfig`), the page's outcome and record drafts (`page_records`: a detail page's one, a listing's one per row),
and what every record is stored with (`RecordSource`). `page_store` in services stores them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from snowcrawl.collect.config import ConfigExecutor
from snowcrawl.collect.run import Adapters
from snowcrawl.content.signals import unreadable
from snowcrawl.db.models import (
    ExtractionConfigId,
    ExtractionId,
    ExtractionMode,
    FinishedOutcome,
    JobId,
    RecordId,
    SiteId,
    UrlTaskId,
)
from snowcrawl.resolve.records import RecordDraft, assemble, assemble_listing
from snowcrawl.resolve.types import FieldSet
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.storage.capture_bundle import CaptureManifest


@dataclass(frozen=True, slots=True)
class ActiveConfig:
    """A site's active config for one schema and page kind: its row, its version, which
    the extraction records, and its executor (`collect.config`, bound by `config_ir.active`)."""

    id: ExtractionConfigId
    version: int
    executor: ConfigExecutor
    # The executor of the `api` captures the config's API recipe fetched; None without a recipe.
    api: ConfigExecutor | None = None
    # Whether a page `executor` ran on joins the config's drift window, which measures its page
    # steps.
    drifts: bool = True
    # A custom recipe's URL patterns (`config_ir.url_pattern`); none for a learned config.
    patterns: tuple[str, ...] = ()
    # A custom recipe's: the learned config of its site, schema and page kind, which extracts a page the recipe did
    # not fetch; None for a learned config, or when the scope has none.
    rest: "ActiveConfig | None" = None

    @property
    def custom(self) -> bool:
        return bool(self.patterns)

    def for_capture(self, manifest: CaptureManifest, adapters: Adapters | None) -> "ActiveConfig | None":
        """The config as it runs on the capture `manifest` describes; None when it does not run on it. An `api`
        capture no active adapter reads holds the JSON an API recipe fetched: the config's recipe executor
        reads it, and as the page steps did not run, the page joins no drift window. A config
        without a recipe does not run on it: an earlier config's recipe fetched it (a replay), and page steps read a
        page, not JSON. Any other capture is a page: a custom recipe serves only what its calls fetched, so a page
        fetched otherwise (for its formats, or after the recipe failed) is its `rest`'s, and the record names what
        ran."""
        if manifest.method != "api" or (
            adapters is not None and adapters.reader(manifest.url, manifest.final_url) is not None
        ):
            if self.custom:
                return None if self.rest is None else self.rest.for_capture(manifest, adapters)
            return self
        return None if self.api is None else replace(self, executor=self.api, api=None, drifts=False)


@dataclass(frozen=True, slots=True)
class StoredRecord:
    id: RecordId
    draft: RecordDraft


@dataclass(frozen=True, slots=True)
class RecordSource:
    """What every record of one extraction is stored with: the extraction, its task, job, site and mode, and the
    page."""

    extraction_id: ExtractionId
    task_id: UrlTaskId
    job_id: JobId
    site_id: SiteId
    mode: ExtractionMode
    page_url: str


def page_records(
    schema: SchemaDef,
    fields: FieldSet,
    rows: tuple[FieldSet, ...],
    *,
    site_id: SiteId,
    page_url: str,
    caller_supplied: bool,
    diagnostics: Sequence[str],
) -> tuple[FinishedOutcome, tuple[RecordDraft, ...]]:
    """The page's outcome and the records it keeps: a detail page's one, or a listing's one per row.
    `diagnostics` are the extraction's; an unreadable-content signal among them keeps a detail page's record
    (`content.signals.unreadable`)."""
    if schema.page_kind == "listing":
        return assemble_listing(
            schema, fields, rows, site_id=site_id, page_url=page_url, caller_supplied=caller_supplied
        )
    outcome, draft = assemble(
        schema,
        fields,
        site_id=site_id,
        canonical_url=page_url,
        caller_supplied=caller_supplied,
        unreadable=unreadable(diagnostics),
    )
    return outcome, (draft,) if draft is not None else ()
