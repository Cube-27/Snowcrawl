"""A custom recipe tried on URLs of its patterns (`POST /v1/recipes/test`), while the caller waits: per URL, the
records the recipe serves and those of the normal extraction (the ladder as if no recipe's pattern matched: the
site's learned config, else cold), with both fetch trails, for an engineer to compare.

The recipe is the draft in the body or a stored custom recipe. Each URL is fetched twice as a scrape fetches its page
(`page_task.fetch_task`, the caller waiting: `IN_PLACE`), once with the recipe as the site's config and once with its
learned config, and extracted (`page_run.extract_run`, no role C), never from a stored capture. A test persists no
capture, record, config, site memory or adapter decline: its fetches are trials (`PageTask.trial`), and its pages are
never run (`page_run`). It is a `scrape` job with a task per URL (`scrape.start_job`), so its fetches count as the
key's usage; each task ends with the recipe's fetch, without a capture, and the job ends `completed` with no results.
"""

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.config_ir.active import bound_config, learned_config
from snowcrawl.config_ir.custom_recipe import CustomRecipe, InvalidRecipe, recipe_ir
from snowcrawl.config_ir.model import ExtractionConfigIR
from snowcrawl.config_ir.url_pattern import match
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.db.models import ExtractionConfig, ExtractionConfigId, JobId
from snowcrawl.db.queue import ClaimedTask, Failed, Finished, TaskResult, fail_task, settle_task
from snowcrawl.normalize.url_key import canonicalize
from snowcrawl.resolve.pipeline import EngineData
from snowcrawl.resolve.task import ActiveConfig
from snowcrawl.schemas import registry
from snowcrawl.schemas.types import SchemaDef
from snowcrawl.services.captures import unstored_records
from snowcrawl.services.configs import ConfigConflict, ConfigNotFound
from snowcrawl.services.fetch_options import FetchOptions, check_proxy_pool
from snowcrawl.services.fetching import Fetching, check_target
from snowcrawl.services.location import job_egress
from snowcrawl.services.page_formats import FetchSummary, fetch_summary
from snowcrawl.services.page_run import Page, Records, blocked_reason, extract_run, fetch_failure, reached_outcome
from snowcrawl.services.page_store import SiteRow, record_fetch_usage, site_row
from snowcrawl.services.page_task import NotReached, PageTask, fetch_task
from snowcrawl.services.polite import IN_PLACE
from snowcrawl.services.records import ExtractedRecord
from snowcrawl.services.scrape import (
    INTERNAL_ERROR,
    MAX_TIMEOUT_S,
    ScrapeRequest,
    ScrapeTimedOut,
    Usage,
    job_usage,
    start_job,
)
from snowcrawl.services.scrape_request import Input, SchemaById, stored_request

__all__ = ["MAX_TEST_URLS", "RecipeTestRequest", "RecipeTestResult", "Run", "UrlTest", "try_recipe"]

# The URLs one test fetches, each twice, while the caller waits.
MAX_TEST_URLS: Final = 5


def _absolute_http_url(url: str) -> str:
    if canonicalize(url) is None:
        raise ValueError("not an absolute http or https URL")
    return url


class RecipeTestRequest(Input):
    """The body of `POST /v1/recipes/test`: the recipe (a draft, or a stored custom recipe's config id), the URLs of
    its patterns to try it on, and how they are fetched."""

    recipe: CustomRecipe | ExtractionConfigId
    urls: Annotated[
        list[Annotated[str, AfterValidator(_absolute_http_url)]], Field(min_length=1, max_length=MAX_TEST_URLS)
    ]
    fetch: FetchOptions = FetchOptions()
    locale: str | None = None
    timeout_s: float = Field(default=60, gt=0, le=MAX_TIMEOUT_S)

    @field_validator("urls")
    @classmethod
    def _distinct(cls, urls: list[str]) -> list[str]:
        # A job holds one task per canonical URL.
        if len({canonicalize(url) for url in urls}) != len(urls):
            raise ValueError("a URL is named twice")
        return urls


class Run(BaseModel):
    """One fetch and extraction of a URL: the records it made, unstored, and its fetch trail (`fetch`, null when
    nothing was kept: `reason` says why, as a scrape's does)."""

    records: list[ExtractedRecord]
    fetch: FetchSummary | None
    reason: str | None


class UrlTest(BaseModel):
    """A URL tried: with the recipe, and with the normal extraction."""

    url: str
    recipe: Run
    normal: Run


class RecipeTestResult(BaseModel):
    """The test's job, each URL's runs in the order asked, and what the job used."""

    job_id: JobId
    urls: list[UrlTest]
    usage: Usage


@dataclass(frozen=True)
class _Work:
    schema: SchemaDef
    data: EngineData
    recipe: ActiveConfig
    options: FetchOptions
    locale: str | None
    deadline: float


async def try_recipe(
    engine: AsyncEngine, fetching: Fetching, data: EngineData, request: RecipeTestRequest
) -> RecipeTestResult:
    """The test (module docstring). Raises, before any job is created: `ConfigNotFound` for no such config,
    `ConfigConflict` for one that is not a custom recipe, `InvalidRecipe` for a draft that does not fit its schema or a
    URL no pattern matches, `TargetRejected` for a URL the SSRF guard refuses and `UnknownProxyPool`; failing the job,
    `ScrapeTimedOut` past `timeout_s`."""
    config_id, version, ir, schema = await _recipe(engine, request.recipe)
    unmatched = [i for i, url in enumerate(request.urls) if not any(match(p, url) is not None for p in ir.patterns)]
    if unmatched:
        raise InvalidRecipe([(f"urls.{i}", "no URL pattern of the recipe matches it") for i in unmatched])
    for url in request.urls:
        await check_target(fetching.connector, url)
    check_proxy_pool(request.fetch, fetching.egresses.pools)
    egress = job_egress(fetching.egresses, request.fetch)
    tasks = await start_job(engine, _stored(request, schema), schema.id, egress, request.urls, schema.page_kind)
    recipe = bound_config(config_id, version, ir, data.vocabulary)
    # Never a stored capture: the test is of the recipe now.
    options = request.fetch.model_copy(update={"cache": "bypass"})
    work = _Work(schema, data, recipe, options, request.locale, time.monotonic() + request.timeout_s)
    tested: list[UrlTest] = []
    try:
        async with asyncio.timeout(request.timeout_s):
            for task in tasks:
                tested.append(await _test_url(engine, fetching, work, task))
    except TimeoutError as e:
        await _fail(engine, tasks[len(tested) :], "timeout")
        raise ScrapeTimedOut(request.timeout_s, "test") from e
    except Exception:
        await _fail(engine, tasks[len(tested) :], INTERNAL_ERROR)
        raise
    async with engine.connect() as conn:
        usage = await job_usage(conn, tasks[0].job_id)
    return RecipeTestResult(job_id=tasks[0].job_id, urls=tested, usage=usage)


def _stored(request: RecipeTestRequest, schema: SchemaDef) -> dict[str, Any]:
    """The test as its `scrape` job stores it: a scrape of its first URL for `records` (`job_request` reads every
    scrape job's request as one)."""
    scrape = ScrapeRequest.model_validate(
        {
            "url": request.urls[0],
            "formats": ["records"],
            "schema": SchemaById(id=schema.id),
            "page_kind": schema.page_kind,
            "fetch": request.fetch,
            "locale": request.locale,
            "timeout_s": request.timeout_s,
        }
    )
    return stored_request(scrape)


async def _recipe(
    engine: AsyncEngine, recipe: CustomRecipe | ExtractionConfigId
) -> tuple[ExtractionConfigId, int, ExtractionConfigIR, SchemaDef]:
    """The recipe's config: a draft's as it would be saved, under an id of its own, or the stored one's."""
    async with engine.connect() as conn:
        if isinstance(recipe, CustomRecipe):
            schema = await registry.get_by_id(conn, recipe.schema_id)
            ir = recipe_ir(recipe, recipe.host, schema, at=datetime.now(UTC), engine_version=ENGINE_VERSION)
            return ExtractionConfigId(uuid.uuid4()), 0, ir, schema
        configs = ExtractionConfig
        query = select(configs.version, configs.ir, configs.schema_id).where(configs.id == recipe)
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise ConfigNotFound(f"no config {recipe}")
        ir = ExtractionConfigIR.model_validate(row.ir)
        if not ir.patterns:
            raise ConfigConflict(f"config {recipe} is not a custom recipe")
        return recipe, row.version, ir, await registry.get_by_id(conn, row.schema_id)


async def _test_url(engine: AsyncEngine, fetching: Fetching, work: _Work, task: ClaimedTask) -> UrlTest:
    """Both runs of the task's URL; its usage counted, and the task ended with the recipe's fetch."""
    async with engine.begin() as conn:
        site = await site_row(conn, urlsplit(task.url).hostname or "", task.page_kind)
        normal = await learned_config(conn, site.id, work.schema, work.data.vocabulary)
    by_recipe = await _fetched(engine, fetching, site, _page_task(work, task, work.recipe))
    by_normal = await _fetched(engine, fetching, site, _page_task(work, task, normal))
    recipe_run = await _run(work, task, by_recipe, work.recipe)
    normal_run = await _run(work, task, by_normal, normal)
    async with engine.begin() as conn:
        await settle_task(conn, task, _ended(by_recipe))
    return UrlTest(url=task.url, recipe=recipe_run, normal=normal_run)


async def _fetched(engine: AsyncEngine, fetching: Fetching, site: SiteRow, page_task: PageTask) -> Page | NotReached:
    """The task's fetch, its attempts counted as the job's usage at once, so a timeout after it loses none."""
    fetched = await fetch_task(engine, fetching, site, page_task, IN_PLACE)
    async with engine.begin() as conn:
        await record_fetch_usage(conn, page_task.task.job_id, fetched.attempts, fetched.proxy_pool)
    return fetched


def _page_task(work: _Work, task: ClaimedTask, config: ActiveConfig | None) -> PageTask:
    records = Records(work.schema, work.data, config, None)
    return PageTask(
        task,
        work.options,
        work.locale,
        work.deadline,
        ["records"],
        records=records,
        served=True,
        planned=True,
        trial=True,
    )


async def _run(work: _Work, task: ClaimedTask, fetched: Page | NotReached, config: ActiveConfig | None) -> Run:
    if isinstance(fetched, NotReached):
        return Run(records=[], fetch=None, reason=_reason(fetched.task_result))
    extracted = await extract_run(fetched, Records(work.schema, work.data, config, None), None)
    records: list[ExtractedRecord] = []
    if extracted is not None:
        site_id = fetched.site.id
        for result in (extracted.extraction, *(extraction for _, extraction, _ in extracted.further)):
            records += unstored_records(work.schema, result, site_id=site_id, page_url=task.canonical_url)
    manifest = fetched.result.bundle.manifest
    summary = fetch_summary(
        manifest.method,
        manifest.status_code or None,
        manifest.block_class,
        fetched.result.attempts,
        adapters=work.data.adapters,
        url=manifest.url,
        final_url=manifest.final_url,
    )
    return Run(records=records, fetch=summary, reason=blocked_reason(fetched.result))


def _reason(result: TaskResult) -> str | None:
    match result:
        case Finished(reason=reason):
            return reason
        case Failed(error=error):
            return error
    return None


def _ended(fetched: Page | NotReached) -> Finished | Failed:
    """How the task ends: as the recipe's fetch did, with no capture kept."""
    if isinstance(fetched, NotReached):
        result = fetched.task_result
        return result if isinstance(result, (Finished, Failed)) else Failed(INTERNAL_ERROR, transient=False)
    if (failure := fetch_failure(fetched.result)) is not None:
        return Failed(fetched.result.attempts[-1].error or failure, transient=False, exhausted=failure)
    return Finished(reached_outcome(fetched.result), reason=blocked_reason(fetched.result))


async def _fail(engine: AsyncEngine, tasks: Sequence[ClaimedTask], error: str) -> None:
    async with engine.begin() as conn:
        for task in tasks:
            await fail_task(conn, task, error, transient=False)
