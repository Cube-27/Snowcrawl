# Decisions

The important product and architecture decisions in force, with the why. Kept until proven wrong; then flagged to the
owner. Code is the source of truth: a decision the code no longer follows is deleted here, not recorded as history.
Reasoning for a single change goes in its PR body.

## Product

- **Schema-driven.** Schemas are data (presets: commerce, jobs, listing); users add or drop fields. Why: any site, any
  schema, one engine.
- **Extract, never enrich.** A field the page does not state stays unpublished; unpublished beats wrong.
- **No LLM fill-all.** The LLM fills only what the structured sources lack, values grounded verbatim in the page, output
  never executed. Why: accuracy and auditability.
- **Every crawl extracts fresh.** Learning keeps selectors and cookies, never values.
- **Numbers as JSON numbers.** Money and ratings are numbers; decimals may be rounded; the meaning of page data is never rewritten.
- **Published URLs are absolute.** A url or image_url publishes in the page's spelling, resolved against the document
  base (RFC 3986 §5.2). Why: a relative or protocol-relative reference (`//cdn…`, `/media/…`) is unusable off the page.
- **Price is never skipped.** An unclear currency never drops a price: page evidence, else the URL locale/subdomain/ccTLD
  (flagged), else the symbol's CLDR primary currency (`$` = USD, flagged), else null. Contested amounts are conflicts.
- **Variants.** Values that vary publish only in `variants` rows (2 or more variants); a top-level field publishes only a
  value every variant shares; no URL-named default variant.
- **Gender is one of `male`, `female`, `unisex`, `kids`** (`type:gender`): the codes from schema.org `suggestedGender`
  and Google Merchant Center `gender`/`age_group`, and the page terms the owner maps to them ("Men's", "Womens",
  "Ladies", "Boys", ...) in `backend/data/vocab/gender/en.json`, pinned in the manifest. Terms match whatever their case
  or possessive apostrophe; a term the file doesn't list stays unpublished. Why: owner 2026-10-28, normalize gender to
  one value per meaning; the term list is the owner's C4 approval.
- **The main image is not repeated in the image list.** A list of images that reads the same schema.org property as
  the record's main image field (`additional_images` beside `image_url`) leaves out that file and its renditions
  (`images.same_file`). Why: owner 2026-10-28, the list is the other images.
- **Brand over vendor.** A stated brand outranks a platform's vendor field. Why: aggregator vendors are not the brand.
- **Jobs.** A page holding its own application form is its `apply_url`.
- **Canonical link wins** for a detail page's own URL.
- **Adapters** only for multi-tenant platforms, measured on at least 3 tenants; all on by default
  (`SNOWCRAWL_DISABLED_ADAPTERS` opts out); a page-stated value outranks an adapter's fallback.
- **Greenfield.** No versioning or compatibility shims; one baseline migration; a schema edit sends its configs to relearning.
- **No in-repo evaluation.** The e2e run and its eval set live outside the repo, scored by one scorer (C1, C10).
- **Custom recipes are engineer-driven.** An engineer picks the calls a site's pages make; the system proposes, never
  saves. Saving needs no approval or audit; the output is always the selected schema.
- **The learned config stays active beside custom recipes.** A recipe serves the URLs its patterns match; the learned
  config serves the rest. Ladder: custom recipe, then adapter, then learned recipe, then page rungs; only
  operator recipes with patterns move ahead of adapters. A page the recipe did not fetch itself is the learned
  config's, and its record says so. An engineer can retire a recipe (it is suspended); the learned config then
  serves its URLs.
- **A recipe that fills fewer fields is saved only on confirmation.** Serving never falls back for a missing field
  (unpublished beats wrong); the save step names what normal extraction filled and the recipe does not. Why: owner
  plan fr, 2026-10-26.
- **Engineer capture is synchronous** in the API process, like `POST /v1/scrape`; no new job kind.
- **Recipe internals are shown on the Recipes screen only,** the one exception to hiding internals from users.

## Acquisition

- **HTTP first.** The browser only when blocked, on a JS shell, or when `render.mode` asks; a missing field never triggers a render.
  Shell pages (no text, no links, no challenge) go to the browser once; `shell` if still empty.
- **robots.txt off by default.** `respect_robots: true` opts in. Why: owner call on coverage; politeness pacing stays.
- **One coherent fingerprint per request.** curl_cffi impersonates its Chrome profile nearest the browser rung's Chrome
  for HTTP and JSON fetches, and every header names the Chrome its handshake names: the browser's own headers when the
  profile is its major; otherwise the profile's own (user agent, client hints, `Accept`, order) on the browser's
  platform and language. A request replaying cookies the browser earned keeps the browser's headers (a clearance may
  be bound to its user agent) over the nearest profile's handshake, a mismatch accepted for it alone; startup warns
  when that profile trails the browser by more than `SNOWCRAWL_IMPERSONATE_LAG` majors. Why: owner 2026-10-24,
  Chrome N headers over a Chrome M handshake are refused; orchestrator 2026-10-26 for the replay.
- **HTTP rung TLS is curl_cffi's newest Chrome target**; it may lag the installed Chrome (today one
  extension, trust anchors 0xca34; HTTP/2 identical). Accepted, no custom ClientHello.
- **Real Chrome through Patchright** (`channel=chrome`), with the browser's own client hints through Chrome's override and
  the user agent through `--user-agent`, so service and shared workers present the same identity. Startup refuses a
  browser that still names headless mode.
- **Headless by default** (with the client-hint override and one common desktop screen per site profile). **Headful
  option** (`SNOWCRAWL_BROWSER_HEADLESS=false`) per the Patchright README's best practice exactly: no user agent, header,
  screen or override of ours. Xvfb on Linux without a display. Why: owner 2026-10-23, the user chooses.
- **Cookies are plain cookies.** Earned clearance cookies are kept and replayed like any other, in the site profile and session.
- **Proxy framework built, not live-tested.** Our own runs go without proxies; no CAPTCHA solving.
- **Exits declare their place.** A proxy pool states its country, timezone and locale, so the browser's zone and
  `Accept-Language` match the address the site sees. Why: one coherent identity per fetch.
- **Sticky exits per site.** A `{session}` gateway gives each site one exit address across its HTTP, JSON and browser
  fetches until the session ages out or a fetch through it fails. Why: a site must not see one visitor from many places.
- **A proxy tunnel goes to the checked address.** Every CONNECT through a pool names the address the SSRF guard
  resolved and checked, never the host name, which the proxy would resolve again, to an address nobody checked. The
  page's Host and TLS server name stay the host's. Cost: our resolver picks the address, so an exit's geo-DNS answer
  (a CDN node near the exit) is lost. Why: the guard must check where the connection goes.
- **Parse once.** Every consumer reads one shared parse of a capture; captures replay through the same `extract_page`.
- **Documented platform product JSON** (Shopify Ajax, WooCommerce Store API) is one companion request per detail page.

## Repo and process

- **Public repo, AGPL-free dependencies,** no customer or site names outside `adapters/` (C3).
- **Complexity at most 15 per function** is the only size gate (C11).
- **Code is the source of truth.** A plan or spec is deleted once implemented; only important decisions are recorded here.
