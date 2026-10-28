# Architecture

What the code does today. One Docker image (linux/amd64 only: Google Chrome has no arm64 build), three roles (`snowcrawl api|worker|scheduler`, `snowcrawl/cli.py`,
which only parses and dispatches); each role runs on one `services/runtime.py` runtime, whose `open_rungs` is the one
builder of the fetching rungs (the `fetch` command's too);
PostgreSQL is the store and the queue, an object store (`storage/`, fs or S3) holds captures.
Layering is enforced by `backend/.importlinter` (`tools/dev.py check-backend-static`): `api > mcp_server > services > crawl/recurring/search
> config_ir > resolve/llm > collect > content > normalize > schemas > acquire/adapters > db/storage > core`.

## Request path

`api/routers/*` (mounted in `api/app.py`) and the MCP server (`mcp_server/server.py`, mounted at `/mcp`) both call
`services/*`. Synchronous `POST /v1/scrape` and queued jobs (batch, crawl, extract, map, search, schedules) run the same
page task: fetched by `services/page_task.py` (the caller waits in place, a queued task is postponed or backed off)
and run by `services/page_run.py`. Workers claim tasks from Postgres (`db/queue.py`, `worker/loop.py`) and run the
handler of the job kind (`worker/handlers/`); `scheduler/loop.py` enqueues schedules, rechecks and retention.

## Pipeline

1. **Acquire** (`acquire/`). `rungs.py` picks the rung: HTTP first (`http.py`, `curl_client.py`, TLS profile in
   `impersonate.py`), the Patchright Chrome browser (`browser.py`, `shared_browser.py`, `browser_profiles.py`) when blocked,
   a JS shell (on a detail page read for an Offer, one whose structured data states no offer too: `classify.lacks_offer`,
   the render kept only when it states one, `rungs._kept`), or `render.mode` asks. The browser is headless by default, with the client-hint override and a per-profile
   screen (`browser_session.py`, `desktop_screen.py`), or headful per the Patchright README
   (`SNOWCRAWL_BROWSER_HEADLESS=false`; Xvfb on Linux without a display, `display.py`). `classify.py`/`challenge.py` set the block class (`ok|blocked|shell|not_found|error`).
   The browser's identity (its naming headers and client hints, which the HTTP and JSON rungs send too) and the
   impersonation built on it are read once per process at startup: after a Chrome upgrade, restart the workers and the
   API, or they keep presenting the old Chrome's headers over the new one's renders.
   Politeness, robots (off by default), SSRF guard and redirects: `politeness.py`, `robots.py`, `ssrf.py`, `redirects.py`.
   Site memory (the rung a site's pages start with): `memory.py`. The result is a capture bundle (`core/capture.py`,
   `storage/capture_bundle.py`); every record can be replayed from it (`POST /v1/captures/{id}/replay`).
   A browser capture keeps the page's data calls (`browser_capture.py`, `FetchPolicy.calls` `data`), or, for an
   engineer capture (`POST /v1/captures`, `services/captures.py`, a scrape in the browser run synchronously in the API
   process like `POST /v1/scrape`, no job), every call (`all`): each
   fetch/xhr answer on any site and each document a page step navigated to, never an event stream or binary media, own
   site first, with the page step it followed. Its calls are listed by endpoint with the paths that carry the schema's
   fields (`services/recipe_calls.py` over `config_ir/api_recipe/discovery.py`) and exported as HAR (`services/har.py`).
   URLs an adapter detects go to the adapter first (`adapters/`: Greenhouse, Lever, Shopify, Workday, … `registry.py`).
   The ladder: custom recipe → adapter → learned recipe → page rungs. A URL a custom recipe's pattern matches is served
   by that recipe before any adapter (`config_ir/api_recipe/execute.py` `RecipeRung` with `Custom`, one fetch whose
   requests share one cookie jar, `acquire/cookies.py` `shared`): warm-up, the HTML page when a binder or page field
   reads it, the main call (GET/POST, JSON or form body, JSON or HTML answer), then extra calls bound from the page
   URL, the pattern, the warm-up or main answer. Its extra answers and page join the `api` capture as marked network
   bodies; its attempts carry the config id (`AttemptRecord.recipe`) and come first when it does not serve and the URL
   goes on down the ladder; its calls also wait for its `rate_ms` (`services/fetching.py` `RecipePaces`). A recipe
   with `sent_by = browser` sends its calls from a warm page of the site's own profile (`acquire/browser_send.py`,
   `Rungs.in_page`): one tab opened on first use at the warm-up URL or the page, kept while the profile is open, each
   call a `fetch()` in its main world with the page's cookies and hooks, holding a shared-browser slot like a render;
   nothing is rendered per URL, and the HTML page it reads still comes over HTTP.
2. **Content** (`content/`). Parse once (`content/page.py`), then markdown/html/links/metadata/chunks for the content formats.
3. **Collect** (`collect/`). `collect/run.py` runs every collector over the one parse: `jsonld`, `microdata`, `og`, `dom`,
   `js_state`, `xhr`, `platform_json`, `adapter`, `config`; listing rows via `collect/items/`, their precedence picked once
   by `collect/row_sources.py`. Each yields candidates with evidence and a locator.
4. **Resolve** (`resolve/`). `resolve/pipeline.py:extract_page` is the one function the worker and replay run:
   ownership/intent (`ownership.py`, `intent.py`, `identity.py`), preference (`preference.py`, data in
   `backend/data/source_precedence.yaml`), derivations (`derive.py`), offers (`offers.py`), evidence and confidence
   (`evidence.py`, `score.py`), listing rows (`listing.py`, `rows.py`), pagination (`pagination.py`). A field the page does
   not state stays unpublished; contested values are conflicts. If resolution raises, the outcome is `error` and nothing is set.
5. **Normalize** (`normalize/`). `dispatch.py` sends each field type (`schemas/types.py`: text, money, enum, url, identifier,
   date, location, range, count, number, list, object, …) to its one module; a normalizer declines rather than guesses.
6. **LLM** (`llm/`). Role C fills only what the structured sources lack (`llm/roles/c_extract.py`), roles S draft schemas and
   labels; every value must be grounded verbatim in the page (`grounding.py`, `claim_checks.py`). Output is data, never code.
7. **Outputs**. Records with per-field evidence, confidence, locator (`services/records.py`, `services/page_store.py`);
   `llms.txt` views; configs export (`config_ir/exporters`).

## Proxies

`SNOWCRAWL_PROXY_POOLS` (`core/proxy.py`) maps a pool name to a proxy URL or to
`{"endpoints": [url, ...], "mode": "rotate"|"sticky", "country"?, "timezone"?, "locale"?}`; a request's
`fetch.proxy_pool` names one, else the fetch is direct, from the place `SNOWCRAWL_EGRESS_COUNTRY`, `_TIMEZONE` and
`_LOCALE` declare. `rotate` takes endpoints in turn, `sticky` keeps a site on one endpoint. An endpoint whose username or
password holds `{session}` is a provider gateway with sticky exits: each site (registrable domain) gets a session id
there, so its HTTP, JSON and browser fetches leave from one address; the exit ends after `SESSION_TTL` or
`SESSION_FETCHES` (as cookie sessions do, `acquire/session.py`) or at a failure through it (block, no answer), and the
next fetch mints another. The exit is part of the browser profile key (`context_key`, profiles of an exit live only
in this process), and a cookie session earned through it ends with it. Timezone rule (`acquire/locality.py`): the
exit's declared IANA zone, else the one zone of its declared country, else the request locale's region's; a country of
several zones and no declared zone sets none (the machine's), which startup logs once (`check_places`). The locale is
the request's, else the exit's declared one, and the HTTP and JSON rungs send it as `Accept-Language` too.

## Learned configs

`config_ir/` holds the IR (`model.py`), learning (the stateless pipeline `learning.py`, `induction.py`,
`listing_induction.py`, `synthesis.py`), validation on held-out pages (`validation.py`, `held_out.py`), activation and
drift (`activation.py`, `drift.py`), operator corrections (`corrections.py`), API recipes (`api_recipe/`) and executors
(`executor.py`, `release_executor.py`, which shares no code with the engine). Configs keep selectors and cookies, never
values. Learn jobs: `learn_job.py` (the job's lifecycle around the pipeline) and `worker/learn.py`.

Custom recipes (`custom_recipe.py`) are `operator` configs an engineer saves through `POST /v1/sites/{host}/configs`
(edits: `PUT /v1/configs/{id}/recipe`; `POST /v1/configs/{id}/retire` suspends one): a main call, extra calls, an optional warm-up and page fields, serving the URLs
their patterns match (`url_pattern.py`: host and path template, `{name}`, `*`, trailing `**`, `?key={name}`). A scope
(site, schema, page kind) holds one active learned config and any number of active recipes, one per pattern shape
(`extraction_config.pattern_shape`, `''` for the learned one); saving a recipe changes no other row, and the
progressive state names the learned config only. Selection (`active.py`): the recipe whose pattern matches the URL
most specifically, else the learned config, which also extracts a page the recipe did not fetch itself
(an `ActiveConfig`'s `rest`); a job pins every active config of a host and picks per URL.

Learning leaves recipes alone: a learning run samples only the pages no active recipe's pattern matches
(`held_out.load_pages(keep=)`) and only ever replaces the learned config. A recipe's drift (`drift.py`) suspends the
field as for a learned config and starts a repair run (`learn_job.py`, `repair.py`), one learn job of the scope: API
recipe learning over the pages the recipe's patterns match, its own endpoint preferred, gives the recipe's next version
with the repaired path, call or parameter, stored `pending_approval` against its pattern shape; approving it suspends
only the version it repairs. Config summaries name the drifting fields and a recipe's pending repair, whose detail
lists what it changes (`repair.changes`).

## Jobs and data

`db/models.py` is the schema (one baseline migration in `backend/migrations/`). Crawl frontier: `crawl/frontier.py`;
recurring diffing: `recurring/`; webhooks: `db/webhook_outbox.py`, `services/webhook_deliveries.py`; usage and LLM cost:
`db/usage.py`, `services/llm_costs.py`.

## Surfaces

REST under `/v1` (`scrape`, `batch/scrape`, `map`, `search`, `crawl`, `extract`, `schemas`, `records`, `captures`, `jobs`,
`schedules`, `configs`/`sites`, `recipes`, `usage/llm`, `project/settings`; `GET /healthz`): the contract is `frontend/openapi.json`
(`tools/dev.py api-client`). Captures' calls and `recipes` answer internals (calls, paths, headers) for the Recipes
screen only. The recipe builder's helpers persist nothing: `POST /v1/recipes/calls` merges two to five captures' calls,
`POST /v1/recipes/propose` drafts a custom recipe from their calls of one endpoint without sending any
(`services/recipe_propose.py`: `binding.propose` leaves an unbound value literal and reports it, `headers.proposed`,
`pagination.infer`, `url_pattern.generalize`), and `POST /v1/recipes/test` serves URLs of a recipe's patterns with it
and with the normal extraction side by side (`services/recipe_trial.py`: a `scrape` job whose trial fetches store no
capture, record or site memory; `PageTask.trial`). Bearer API keys (`snowcrawl apikey create`). Schemas (presets in `schemas/presets/*.json`) are data.
SDKs in `sdk/python`, `sdk/typescript` (`tools/dev.py sdk`); web UI in `frontend/`: shared components in
`frontend/src/components/` by layer (`atoms`, `molecules`, `organisms`, `templates`, and `lib` for their non-UI
helpers), screens in `frontend/src/screens/`, every visual value in `frontend/src/app/theme.css`. The Recipes screen
(`frontend/src/screens/recipes/`, a site's recipes) builds a custom recipe from samples, their calls and the server's
draft, which the engineer's edits lie over (`draft.ts`), tests it, saves it, edits a saved one (`GET /v1/configs/{id}`
answers a custom recipe also as the `CustomRecipe` it is edited as), approves or rejects its pending repair and retires
it.

## Pinned data

Vocabularies live in `backend/data/` and are pinned with sha256 in `backend/data/manifest.json` (`tools/data_manifest_check.py`).
`calibration.json`, `source_precedence.yaml`, `drift_margins.json` are as fitted on 2026-10-18; no code refits them, a refit is flagged to the owner.
