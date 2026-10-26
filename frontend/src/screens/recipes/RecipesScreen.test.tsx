import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vite-plus/test";
import { routes } from "../../app/router";
import type { CallDetail, CallList, CaptureResult, Endpoint } from "../../api/captures";
import type { ConfigDetail, ConfigSummary } from "../../api/configs";
import type { Proposal, RecipeTestResult, TestRun, WrittenRecipe } from "../../api/recipes";
import type { SchemaDef } from "../../api/schemas";
import { problem, stubApi } from "../../test/api";
import { catchDownloads } from "../../test/downloads";
import { recordField } from "../../test/records";
import { fieldSpec } from "../../test/schemas";

const HOST = "shop.example.test";
const SCHEMA_ID = "0192a000-0000-7000-8000-000000000101";
const RECIPE_ID = "0192a000-0000-7000-8000-000000000501";
const REPAIR_ID = "0192a000-0000-7000-8000-000000000502";
const LEARNED_ID = "0192a000-0000-7000-8000-000000000503";
const CAPTURE_1 = "0192a000-0000-7000-8000-000000000701";
const CAPTURE_2 = "0192a000-0000-7000-8000-000000000702";
const CAPTURE_3 = "0192a000-0000-7000-8000-000000000703";
const SAVED_ID = "0192a000-0000-7000-8000-000000000504";

const schema: SchemaDef = {
  id: SCHEMA_ID,
  name: "item_detail",
  origin: "user",
  status: "active",
  page_kind: "detail",
  required_groups: [["title"]],
  field_order: ["title", "price"],
  labels: null,
  fields: {
    title: fieldSpec({ type: "text", required: true, scope: "row" }),
    price: fieldSpec({ type: "money", required: false, scope: "row" }),
  },
};

function summary(overrides: Partial<ConfigSummary>): ConfigSummary {
  return {
    id: RECIPE_ID,
    site: HOST,
    schema_id: SCHEMA_ID,
    page_kind: "detail",
    version: 3,
    status: "active",
    origin: "operator",
    field_status: { title: "operator" },
    validation_report: null,
    patterns: [`https://${HOST}/item/{id}`],
    custom: true,
    renders: [],
    sent_by: "http",
    drifting: ["price"],
    pending_repair: REPAIR_ID,
    ...overrides,
  };
}

function captured(captureId: string | null, url: string): CaptureResult {
  return {
    capture_id: captureId,
    url,
    outcome: captureId === null ? "blocked" : "extracted",
    reason: captureId === null ? "The site answered with a challenge." : null,
    attempts: [],
    records:
      captureId === null
        ? []
        : [
            {
              id: captureId,
              complete: true,
              confidence: 0.9,
              mode: "adhoc",
              fields: { title: recordField({ value: "Lamp" }) },
            },
          ],
  };
}

function endpoint(overrides: Partial<Endpoint>): Endpoint {
  return {
    method: "GET",
    origin: `https://${HOST}`,
    has_path: true,
    segments: 2,
    suffix: "",
    query: [],
    body_shape: null,
    rows: null,
    samples: [CAPTURE_1, CAPTURE_2],
    calls: [],
    fields: [],
    ...overrides,
  };
}

const call = (captureId: string, index: number, sameSite: boolean) => ({
  capture_id: captureId,
  index,
  status: 200,
  content_type: "application/json",
  size: 2048,
  omitted: null,
  resource_type: "fetch",
  same_site: sameSite,
  step: 0,
});

// The server's order is first called first; the screen puts the page's own site first.
const merged: CallList = {
  schema_id: SCHEMA_ID,
  samples: [CAPTURE_1, CAPTURE_2],
  endpoints: [
    endpoint({
      origin: "https://cdn.example.net",
      query: ["v"],
      samples: [CAPTURE_1],
      calls: [call(CAPTURE_1, 0, false)],
    }),
    endpoint({
      query: ["id"],
      calls: [call(CAPTURE_1, 1, true), call(CAPTURE_2, 0, true)],
      fields: [
        {
          field: "price",
          published: true,
          in_text: 0,
          paths: [{ path: "$.data.price", samples: 2 }],
        },
      ],
    }),
  ],
};

const detail: CallDetail = {
  index: 1,
  url: `https://${HOST}/api/item?id=1`,
  method: "GET",
  status: 200,
  content_type: "application/json",
  size: 2048,
  omitted: null,
  resource_type: "fetch",
  same_site: true,
  step: 0,
  guard_prefix: null,
  request_headers: [["accept", "application/json"]],
  request_body: null,
  response_headers: [["content-type", "application/json"]],
  answer: '{"data":{"price":12.5}}',
};

const json = (body: unknown) => () => Response.json(body);

async function open(path: string, heading: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  await screen.findByRole("heading", { level: 1, name: heading });
}

async function captureSample(url: string) {
  const field = screen.getByRole("textbox", { name: /Sample URL/ });
  await userEvent.clear(field);
  await userEvent.type(field, url);
  await userEvent.click(screen.getByRole("button", { name: "Capture" }));
}

const stored: WrittenRecipe = {
  schema_id: SCHEMA_ID,
  page_kind: "detail",
  patterns: [`${HOST}/item/{id}`],
  main: {
    method: "GET",
    url: `https://${HOST}/api/item?id={id}`,
    params_from_url: { id: "pattern:id" },
    headers: [{ name: "accept", value: "application/json" }],
    body: null,
    pagination: null,
    requires_page_fetch: false,
    answer: "json",
    map: { price: "$.data.price" },
    proof: null,
  },
  calls: {},
  warm_up: null,
  fields: {},
  sent_by: "http",
  rate_ms: null,
};

function proposal(map: Record<string, string>): Proposal {
  return {
    recipe: { ...stored, main: { ...stored.main, map } },
    unfilled: ["title"],
    unbound: [],
    withheld_headers: ["x-token"],
  };
}

function config(id: string, overrides: Partial<ConfigDetail>): ConfigDetail {
  return {
    ...summary({ id, pending_repair: null, drifting: [] }),
    ir: {
      ir_version: "1",
      site: HOST,
      schema: { name: "item_detail", digest: "0".repeat(64) },
      page_kind: "detail",
      fetch: { method: "http" },
      listing: null,
      api_recipe: stored.main,
      recipes: {},
      absent: [],
      patterns: stored.patterns,
      warm_up: null,
      sent_by: "http",
      rate_ms: null,
      fields: {},
      provenance: {
        origin: "operator",
        validated_on: [],
        learned_at: "2026-10-01T00:00:00Z",
        engine_version: "0.1.0",
      },
    },
    recipe: stored,
    repair_changes: null,
    ...overrides,
  };
}

const run = (title: string, recipeStatus: number): TestRun => ({
  records: [
    {
      complete: true,
      confidence: 0.9,
      mode: "adhoc",
      fields: { title: recordField({ value: title }) },
    },
  ],
  fetch: {
    method: "api",
    status_code: recipeStatus,
    block_class: "ok",
    adapter: null,
    attempts: [
      {
        method: "api",
        status: recipeStatus,
        block_class: recipeStatus < 400 ? "ok" : "blocked",
        duration_ms: 12,
        error: null,
        challenge: null,
        steps: [],
        recipe: RECIPE_ID,
      },
    ],
  },
  reason: null,
});

async function pickSchema() {
  await userEvent.click(screen.getByRole("button", { name: /Schema/ }));
  await userEvent.click(await screen.findByRole("option", { name: "item_detail" }));
}

function textbox(name: string, index = 0): HTMLElement {
  const found = screen.getAllByRole("textbox", { name })[index];
  if (found === undefined) throw new Error(`no textbox ${name}`);
  return found;
}

describe("recipes screen", () => {
  it("lists the site's serving recipes with their patterns, drift and pending repair", async () => {
    stubApi({
      [`GET /v1/sites/${HOST}/configs`]: json({
        items: [
          summary({}),
          summary({ id: REPAIR_ID, version: 4, status: "pending_approval" }),
          summary({ id: LEARNED_ID, custom: false, origin: "induced", patterns: [] }),
        ],
      }),
      "GET /v1/schemas": json({ items: [schema] }),
    });
    await open(`/sites/${HOST}`, HOST);
    await userEvent.click(screen.getByRole("link", { name: "Recipes" }));
    await screen.findByRole("heading", { level: 1, name: `Recipes of ${HOST}` });

    const table = screen.getByRole("grid", { name: "Recipes" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    const [row] = rows;
    expect(row).toHaveTextContent(`https://${HOST}/item/{id}`);
    expect(row).toHaveTextContent("price");
    expect(await within(table).findByText("item_detail")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Review the repair" })).toHaveAttribute(
      "href",
      `/configs/${REPAIR_ID}`,
    );
  });

  it("builds from samples: captures each, lists their calls by endpoint, opens one, picks the main call", async () => {
    let captures = 0;
    const replies = [
      captured(null, `https://${HOST}/item/1`),
      captured(CAPTURE_1, `https://${HOST}/item/1`),
      captured(CAPTURE_2, `https://${HOST}/item/2`),
    ];
    const calls = stubApi({
      "GET /v1/schemas": json({ items: [schema] }),
      "POST /v1/captures": () => Response.json(replies[captures++]),
      [`GET /v1/captures/${CAPTURE_1}/calls`]: json({
        ...merged,
        samples: [CAPTURE_1],
        endpoints: [],
      }),
      "POST /v1/recipes/calls": json(merged),
      [`GET /v1/captures/${CAPTURE_1}/calls/1`]: json(detail),
      [`GET /v1/captures/${CAPTURE_1}/calls.har`]: json({ log: { version: "1.2" } }),
    });
    const saved = catchDownloads();
    await open(`/sites/${HOST}/recipes/new`, `New recipe for ${HOST}`);

    await userEvent.click(screen.getByRole("button", { name: /Schema/ }));
    await userEvent.click(await screen.findByRole("option", { name: "item_detail" }));

    // A capture that stored nothing says why and is not a sample.
    await captureSample(`https://${HOST}/item/1`);
    expect(await screen.findByText("The site answered with a challenge.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    expect(await screen.findByText("No calls")).toBeInTheDocument();
    await captureSample(`https://${HOST}/item/2`);

    const samples = await screen.findByRole("list", { name: "Samples" });
    await within(samples).findByText(`2. https://${HOST}/item/2`);
    expect(calls.filter(({ method }) => method === "POST")).toMatchObject([
      { path: "/v1/captures", body: { url: `https://${HOST}/item/1`, page_kind: "detail" } },
      { path: "/v1/captures", body: { url: `https://${HOST}/item/1`, schema_id: SCHEMA_ID } },
      { path: "/v1/captures", body: { url: `https://${HOST}/item/2` } },
      { path: "/v1/recipes/calls", body: { capture_ids: [CAPTURE_1, CAPTURE_2] } },
    ]);

    // The page's own site first, each group with where it carries the fields.
    const pick = await screen.findAllByRole("button", { name: /as the main call/ });
    expect(pick.map((button) => button.getAttribute("aria-label"))).toEqual([
      `Use GET https://${HOST}/{2 segments}?id as the main call`,
      "Use GET https://cdn.example.net/{2 segments}?v as the main call",
    ]);
    expect(screen.getByText("price at $.data.price on 2 of 2")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open call 1" }));
    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByText("accept")).toBeInTheDocument();
    expect(within(drawer).getByText(`GET https://${HOST}/api/item?id=1`)).toBeInTheDocument();
    await userEvent.click(within(drawer).getByRole("button", { name: "Download HAR" }));
    await expect.poll(() => saved.map(({ name }) => name)).toEqual([`capture-${CAPTURE_1}.har`]);
    await userEvent.keyboard("{Escape}");

    const [own] = pick;
    if (own === undefined) throw new Error("no endpoint group");
    await userEvent.click(own);
    const main = screen.getByRole("list", { name: "Main call fields" });
    expect(main).toHaveTextContent("price at $.data.price on 2 of 2");
  });
  it("reviews the draft, keeps edits over a later one, adds an extra call, tests and saves", async () => {
    let captures = 0;
    const replies = [
      captured(CAPTURE_1, `https://${HOST}/item/1`),
      captured(CAPTURE_2, `https://${HOST}/item/2`),
      captured(CAPTURE_3, `https://${HOST}/item/3`),
    ];
    let saves = 0;
    const tested: RecipeTestResult = {
      job_id: "0192a000-0000-7000-8000-000000000901",
      urls: replies.map(({ url }) => ({ url, recipe: run("Lamp", 403), normal: run("Lamp", 200) })),
      usage: {
        http_requests: 6,
        browser_seconds: 0,
        llm_calls: 0,
        llm_tokens_in: 0,
        llm_tokens_out: 0,
      },
    };
    const calls = stubApi({
      "GET /v1/schemas": json({ items: [schema] }),
      "POST /v1/captures": () => Response.json(replies[captures++]),
      "POST /v1/recipes/calls": json(merged),
      "POST /v1/recipes/propose": ({ body }) =>
        Response.json(
          (body as { main: { origin: string } }).main.origin === `https://${HOST}`
            ? proposal({ price: "$.data.price" })
            : proposal({ title: "$.name", price: "$.p" }),
        ),
      "POST /v1/recipes/test": json(tested),
      [`GET /v1/sites/${HOST}/configs`]: json({ items: [summary({})] }),
      [`POST /v1/sites/${HOST}/configs`]: () =>
        saves++ === 0
          ? problem(409, {
              type: "conflict",
              title: "Conflict",
              detail: `config ${RECIPE_ID} serves URL patterns of shape ['${HOST}/item/{}']`,
            })
          : Response.json(config(SAVED_ID, { version: 4 }), { status: 201 }),
      [`GET /v1/configs/${SAVED_ID}`]: json(config(SAVED_ID, { version: 4 })),
    });
    await open(`/sites/${HOST}/recipes/new`, `New recipe for ${HOST}`);
    await pickSchema();
    await captureSample(`https://${HOST}/item/1`);
    await screen.findByText(`1. https://${HOST}/item/1`);
    await captureSample(`https://${HOST}/item/2`);
    await userEvent.click(
      await screen.findByRole("button", {
        name: `Use GET https://${HOST}/{2 segments}?id as the main call`,
      }),
    );

    // The draft, with what it left out; every value editable.
    expect(await screen.findByText("Header left out: x-token")).toBeInTheDocument();
    expect(textbox("URL patterns (one per line)")).toHaveValue(`${HOST}/item/{id}`);
    // Text stays as typed while it is edited: a new line, and JSON not yet whole.
    await userEvent.type(textbox("URL patterns (one per line)"), "{Enter}");
    expect(textbox("URL patterns (one per line)")).toHaveValue(`${HOST}/item/{id}\n`);
    await userEvent.click(screen.getByRole("button", { name: "Reset the patterns" }));
    await userEvent.clear(textbox("Pagination (JSON; null for none)"));
    await userEvent.type(textbox("Pagination (JSON; null for none)"), "{{");
    expect(textbox("Pagination (JSON; null for none)")).toHaveValue("{");
    expect(screen.getByText("Not JSON: the pagination is left as it was.")).toBeInTheDocument();
    await userEvent.clear(textbox("Pagination (JSON; null for none)"));
    await userEvent.type(textbox("Pagination (JSON; null for none)"), "null");
    await userEvent.click(screen.getByRole("button", { name: "Reset the pagination" }));
    await userEvent.clear(textbox("Path of price"));
    await userEvent.type(textbox("Path of price"), "$.data.amount");
    expect(screen.getByRole("button", { name: "Reset the path of price" })).toBeInTheDocument();

    // A third sample drafts anew; the edit outlasts it.
    await captureSample(`https://${HOST}/item/3`);
    await screen.findByText(`3. https://${HOST}/item/3`);
    await expect
      .poll(() => calls.filter(({ path }) => path === "/v1/recipes/propose").length)
      .toBe(2);
    expect(textbox("Path of price")).toHaveValue("$.data.amount");

    // An extra call reads only the field the main call leaves unfilled.
    await userEvent.click(
      screen.getByRole("button", {
        name: "Add GET https://cdn.example.net/{2 segments}?v as an extra call",
      }),
    );
    expect(await screen.findByText("call call_1 at $.name")).toBeInTheDocument();
    expect(screen.getByText("Every field is filled by a call.")).toBeInTheDocument();

    // A browser-sent test whose call the site refuses says it cannot replay.
    await userEvent.click(screen.getByRole("switch", { name: "Sent by the browser" }));
    await userEvent.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findAllByText(/Cannot replay/)).toHaveLength(3);
    const values = screen.getByRole("list", { name: "Values of title" });
    expect(within(values).getAllByText("Lamp")).toHaveLength(3);
    const test = calls.find(({ path }) => path === "/v1/recipes/test");
    expect(test?.body).toMatchObject({
      urls: replies.map(({ url }) => url),
      recipe: {
        sent_by: "browser",
        main: { map: { price: "$.data.amount" } },
        calls: { call_1: { map: { title: "$.name" } } },
      },
    });

    // A refusal names the recipe of the same shape; saving again opens the saved one.
    await userEvent.click(screen.getByRole("button", { name: "Save recipe" }));
    expect(
      await screen.findByRole("link", {
        name: `Open the recipe serving https://${HOST}/item/{id}`,
      }),
    ).toHaveAttribute("href", `/recipes/${RECIPE_ID}`);
    await userEvent.click(screen.getByRole("button", { name: "Save recipe" }));
    await screen.findByRole("heading", { level: 1, name: `Recipe of ${HOST}, version 4` });
  });

  it("reviews a pending repair and edits the recipe in the builder", async () => {
    const repair = config(REPAIR_ID, {
      version: 4,
      status: "pending_approval",
      repair_changes: [{ part: "main.map.price", before: "$.data.price", after: "$.data.cost" }],
    });
    const calls = stubApi({
      "GET /v1/schemas": json({ items: [schema] }),
      [`GET /v1/schemas/${SCHEMA_ID}`]: json(schema),
      [`GET /v1/configs/${RECIPE_ID}`]: json(config(RECIPE_ID, { pending_repair: REPAIR_ID })),
      [`GET /v1/configs/${REPAIR_ID}`]: json(repair),
      [`POST /v1/configs/${REPAIR_ID}/approve`]: json({ ...repair, status: "active" }),
      [`PUT /v1/configs/${RECIPE_ID}/recipe`]: json(config(SAVED_ID, { version: 5 })),
      [`GET /v1/configs/${SAVED_ID}`]: json(config(SAVED_ID, { version: 5 })),
    });
    await open(`/recipes/${RECIPE_ID}`, `Recipe of ${HOST}, version 3`);

    const changes = await screen.findByRole("grid", { name: "Repair changes" });
    expect(within(changes).getByText('"$.data.cost"')).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await expect
      .poll(() => calls.some(({ path }) => path === `/v1/configs/${REPAIR_ID}/approve`))
      .toBe(true);

    await userEvent.click(screen.getByRole("link", { name: "Edit" }));
    await screen.findByRole("heading", { level: 1, name: `Edit recipe of ${HOST}` });
    expect(textbox("URL")).toHaveValue(`https://${HOST}/api/item?id={id}`);
    await userEvent.type(textbox("Path of title"), "$.data.label");
    await userEvent.click(screen.getByRole("button", { name: "Save new version" }));
    await screen.findByRole("heading", { level: 1, name: `Recipe of ${HOST}, version 5` });
    expect(calls.find(({ method }) => method === "PUT")?.body).toMatchObject({
      main: { map: { price: "$.data.price", title: "$.data.label" } },
    });
  });
});
