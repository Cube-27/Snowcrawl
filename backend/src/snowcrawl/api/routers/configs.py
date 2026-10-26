"""`/v1/sites/{host}/configs`, `/v1/configs/{id}`, `/v1/configs/{id}/recipe`, `/v1/configs/{id}/approve`,
`/v1/configs/{id}/reject`, `/v1/configs/{id}/retire`, `/v1/configs/{id}/export` and `/v1/sites/{host}/learn`: a site's
configs, operator corrections (C5), custom recipes and their retirement, approval and refusal of learned configs,
exports and forced learning. The
rules are the configs service's; this router translates HTTP to it and its errors to problems."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from snowcrawl.api.auth import require_api_key
from snowcrawl.api.dto import PROBLEM_RESPONSE, ConfigList, LearnStarted
from snowcrawl.api.problems import conflict, invalid, not_found, records_unavailable
from snowcrawl.api.runtime import database, runtime
from snowcrawl.config_ir.corrections import Correction, InvalidCorrection
from snowcrawl.config_ir.custom_recipe import CustomRecipe, InvalidRecipe
from snowcrawl.config_ir.exporters.configs import ConfigExport
from snowcrawl.db.models import ExtractionConfigId
from snowcrawl.schemas.registry import SchemaNotFound
from snowcrawl.services import configs
from snowcrawl.services.configs import (
    ConfigConflict,
    ConfigDetail,
    ConfigNotFound,
    ExportRequest,
    LearnRequest,
    SiteNotFound,
)

router = APIRouter(
    prefix="/v1",
    tags=["configs"],
    dependencies=[Depends(require_api_key)],
    responses={401: PROBLEM_RESPONSE, 422: PROBLEM_RESPONSE, 429: PROBLEM_RESPONSE},
)
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: PROBLEM_RESPONSE}
_CHANGES: dict[int | str, dict[str, Any]] = {404: PROBLEM_RESPONSE, 409: PROBLEM_RESPONSE}


@asynccontextmanager
async def _transaction(request: Request) -> AsyncIterator[AsyncConnection]:
    """One transaction for the request, with the service's errors answered as problems."""
    engine = database(request)
    try:
        async with engine.begin() as conn:
            yield conn
    except (SiteNotFound, ConfigNotFound, SchemaNotFound) as e:
        raise not_found(str(e)) from e
    except ConfigConflict as e:
        raise conflict(str(e)) from e
    except (InvalidCorrection, InvalidRecipe) as e:
        raise invalid(str(e), e.errors) from e


@router.get("/sites/{host}/configs", responses=_NOT_FOUND)
async def list_site_configs(host: str, request: Request) -> ConfigList:
    """Every version of the site's configs, custom recipes included, with status, field status and validation report,
    by schema, page kind and version."""
    async with _transaction(request) as conn:
        return ConfigList(items=await configs.list_for_host(conn, host))


@router.post("/sites/{host}/configs", status_code=201, responses=_CHANGES)
async def create_recipe(host: str, body: CustomRecipe, request: Request) -> ConfigDetail:
    """Saves a custom recipe: the site's next config version for the schema and page kind, `origin=operator`,
    `active` at once. It serves the URLs its patterns match; the learned config stays active and serves the rest.
    409 when an active recipe of the site, schema and page kind already serves a URL pattern of the same shape (the
    pattern with its parameter names left out); 422 when the recipe does not fit the site or schema."""
    async with _transaction(request) as conn:
        return await configs.create_recipe(conn, host, body)


@router.put("/configs/{config_id}/recipe", responses=_CHANGES)
async def edit_recipe(config_id: ExtractionConfigId, body: CustomRecipe, request: Request) -> ConfigDetail:
    """Edits a custom recipe: the next version, `active`, replacing `config_id`, which is `suspended`. 409 when
    `config_id` is not a custom recipe or not the latest version of its patterns' shape, or when another active recipe
    serves a pattern of a new shape."""
    async with _transaction(request) as conn:
        return await configs.edit_recipe(conn, config_id, body)


@router.post("/configs/{config_id}/retire", responses=_CHANGES)
async def retire_recipe(config_id: ExtractionConfigId, request: Request) -> ConfigDetail:
    """Retires a custom recipe: its active version is `suspended`, with any repair of it waiting for approval, and the
    URLs its patterns match are served by the site's learned config again. 409 when `config_id` is not an active
    custom recipe."""
    async with _transaction(request) as conn:
        return await configs.retire_recipe(conn, config_id)


@router.get("/configs/{config_id}", responses=_NOT_FOUND)
async def get_config(config_id: ExtractionConfigId, request: Request) -> ConfigDetail:
    """The config's IR and validation report."""
    async with _transaction(request) as conn:
        return await configs.get(conn, config_id)


@router.put("/configs/{config_id}", responses={**_CHANGES, 503: PROBLEM_RESPONSE})
async def correct_config(config_id: ExtractionConfigId, body: Correction, request: Request) -> ConfigDetail:
    """Corrects or confirms fields (C5): the next version, `origin=operator`, validated on the site's held-out pages.
    It is `active` when validation keeps a field, else `draft`; its validation report says why. `config_id` must be
    the latest version of its site, schema and page kind; otherwise 409."""
    data = runtime(request).engine_data
    if data is None:
        raise records_unavailable("the engine's data files are not installed, so no config can be validated")
    fetching = runtime(request).fetching
    async with _transaction(request) as conn:
        return await configs.correct(conn, fetching.store, data.vocabulary, config_id, body)


@router.post("/configs/{config_id}/approve", responses=_CHANGES)
async def approve_config(config_id: ExtractionConfigId, request: Request) -> ConfigDetail:
    """Approves a learned config waiting as `pending_approval` (the project's `require_config_approval`): it becomes the
    active config of its site, schema and page kind, and the version active before is `suspended`. 409 for a config
    in any other status."""
    async with _transaction(request) as conn:
        return await configs.approve_config(conn, config_id)


@router.post("/configs/{config_id}/reject", responses=_CHANGES)
async def reject_config(config_id: ExtractionConfigId, request: Request) -> ConfigDetail:
    """Refuses a learned config waiting as `pending_approval`: it becomes `suspended`, the version active before goes on
    serving, and the site learns again only after 8 more extracted pages. 409 for a config in any other status or not
    the latest version."""
    async with _transaction(request) as conn:
        return await configs.reject_config(conn, config_id)


@router.post("/configs/{config_id}/export", responses=_NOT_FOUND)
async def export_config(config_id: ExtractionConfigId, body: ExportRequest, request: Request) -> ConfigExport:
    """The config as `json` (the IR as stored) or `platform_draft` (`draft: true` until the platform's format is known),
    and the release check on the pages the config was validated on: a field the independent release executor does not
    reproduce on every such page with an engine value while there are fewer than 10, or on 95% from 10 on, is
    `manual`."""
    fetching = runtime(request).fetching
    async with _transaction(request) as conn:
        return await configs.export(conn, fetching.store, config_id, body)


@router.post("/sites/{host}/learn", status_code=202, responses=_CHANGES)
async def learn_site(host: str, body: LearnRequest, request: Request) -> LearnStarted:
    """Starts learning the site's config for a schema; 409 while a run is in flight, or when the site has an
    active config and `force` is not set."""
    async with _transaction(request) as conn:
        return LearnStarted(job_id=await configs.learn(conn, host, body))
