import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vite-plus/test";
import { routes } from "../../app/router";
import type { ConfigDetail, ConfigSummary, ValidationReport } from "../../api/configs";
import type { SchemaDef } from "../../api/schemas";
import { sse, stubApi } from "../../test/api";
import { jobSummary } from "../../test/jobs";
import { fieldSpec } from "../../test/schemas";

const HOST = "shop.example.test";
const SCHEMA_ID = "0192a000-0000-7000-8000-000000000101";
const V1_ID = "0192a000-0000-7000-8000-000000000501";
const V2_ID = "0192a000-0000-7000-8000-000000000502";
const V3_ID = "0192a000-0000-7000-8000-000000000503";
const JOB_ID = "0192a000-0000-7000-8000-000000000901";
const HELD_OUT = ["0192a000-0000-7000-8000-000000000701", "0192a000-0000-7000-8000-000000000702"];

const schema: SchemaDef = {
  id: SCHEMA_ID,
  name: "item_detail",
  origin: "user",
  status: "active",
  page_kind: "detail",
  required_groups: [["title"]],
  field_order: ["title", "price", "sku"],
  labels: null,
  fields: {
    title: fieldSpec({ type: "text", required: true, scope: "row" }),
    price: fieldSpec({ type: "money", required: false, scope: "row" }),
    sku: fieldSpec({ type: "identifier", scheme: "sku", required: false, scope: "row" }),
  },
};

const report: ValidationReport = {
  held_out: HELD_OUT,
  fields: {
    title: {
      pages: 2,
      matched: 2,
      valid: 2,
      published: 2,
      reproduced: 2,
      comparable: 2,
      corroborated_on: 2,
      status: "corroborated",
      yield: 1,
      validity: 1,
    },
    price: {
      pages: 2,
      matched: 1,
      valid: 0,
      published: 0,
      reproduced: 0,
      comparable: 0,
      corroborated_on: 0,
      status: null,
      dropped: "validity",
      yield: 0.5,
      validity: 0,
    },
  },
  listing: null,
};

function summary(overrides: Partial<ConfigSummary> = {}): ConfigSummary {
  return {
    id: V2_ID,
    site: HOST,
    schema_id: SCHEMA_ID,
    page_kind: "detail",
    version: 2,
    status: "active",
    origin: "induced",
    field_status: { title: "corroborated", price: "uncorroborated" },
    validation_report: report,
    patterns: [],
    custom: false,
    renders: [],
    sent_by: "http",
    drifting: [],
    pending_repair: null,
    ...overrides,
  };
}

const v1 = summary({ id: V1_ID, version: 1, status: "suspended", field_status: {} });
const v2 = summary();

const detail: ConfigDetail = {
  ...v2,
  recipe: null,
  repair_changes: null,
  ir: {
    ir_version: "1",
    site: HOST,
    schema: { name: "item_detail", digest: "0".repeat(64) },
    page_kind: "detail",
    fetch: { method: "http" },
    listing: null,
    api_recipe: null,
    recipes: {},
    absent: [],
    patterns: [],
    warm_up: null,
    sent_by: "http",
    rate_ms: null,
    fields: {
      title: {
        needs: "http",
        call: null,
        status: "corroborated",
        steps: [{ xpath: "//h1", post: [{ trim: {} }] }],
        yield: 1,
        validity: 1,
      },
      price: {
        needs: "http",
        call: null,
        status: "uncorroborated",
        steps: [{ jsonld: "$.offers.price", when_type: "Product", post: [{ money: {} }] }],
        yield: 0.5,
        validity: 0.25,
      },
    },
    provenance: {
      origin: "induced",
      validated_on: HELD_OUT,
      learned_at: "2026-10-01T00:00:00Z",
      engine_version: "0.1.0",
    },
  },
};

const corrected: ConfigDetail = {
  ...detail,
  id: V3_ID,
  version: 3,
  origin: "operator",
  field_status: { title: "operator", price: "operator", sku: "operator" },
};

const json = (body: unknown) => () => Response.json(body);

async function open(path: string, heading: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  await screen.findByRole("heading", { level: 1, name: heading });
}

/** Replaces a field's text as a paste does: one input event, not one per character, so each step of a long form
 * test re-renders the form once. */
async function replaceText(field: HTMLElement, text: string) {
  await userEvent.clear(field);
  await userEvent.paste(text);
}

async function openTab(name: string) {
  await userEvent.click(await screen.findByRole("tab", { name }));
  return within(screen.getByRole("tabpanel", { name }));
}

function configApi(extra: Record<string, () => Response> = {}) {
  return stubApi({
    [`GET /v1/configs/${V2_ID}`]: json(detail),
    [`GET /v1/sites/${HOST}/configs`]: json({ items: [v1, v2] }),
    "GET /v1/schemas": json({ items: [schema] }),
    [`GET /v1/schemas/${SCHEMA_ID}`]: json(schema),
    ...extra,
  });
}

describe("sites screen", () => {
  it("opens the site of a page URL entered for its host", async () => {
    stubApi({
      [`GET /v1/sites/${HOST}/configs`]: json({ items: [] }),
      "GET /v1/schemas": json({ items: [schema] }),
    });
    await open("/sites", "Sites");

    await userEvent.type(
      screen.getByRole("textbox", { name: "Host" }),
      `https://${HOST}:8443/catalog/item-1?ref=x`,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open site" }));

    expect(await screen.findByRole("heading", { level: 1, name: HOST })).toBeInTheDocument();
    expect(await screen.findByText("No configs yet")).toBeInTheDocument();
  });

  it("starts learning a site that has no config yet, for the schema chosen", async () => {
    const calls = stubApi({
      [`GET /v1/sites/${HOST}/configs`]: json({ items: [] }),
      "GET /v1/schemas": json({ items: [schema] }),
      [`POST /v1/sites/${HOST}/learn`]: () => Response.json({ job_id: JOB_ID }, { status: 202 }),
      [`GET /v1/jobs/${JOB_ID}`]: () =>
        Response.json(jobSummary({ id: JOB_ID, kind: "learn", status: "failed" })),
      [`GET /v1/jobs/${JOB_ID}/events`]: () => sse(["1", "failed", {}]),
    });
    await open(`/sites/${HOST}`, HOST);
    await screen.findByText("No configs yet");

    await userEvent.click(screen.getByRole("button", { name: /Schema/ }));
    await userEvent.click(await screen.findByRole("option", { name: "item_detail" }));
    await userEvent.click(screen.getByRole("button", { name: "Learn now" }));

    expect(await screen.findByText(`Learning started as job ${JOB_ID}.`)).toBeInTheDocument();
    expect(calls.filter((call) => call.method === "POST")).toMatchObject([
      { path: `/v1/sites/${HOST}/learn`, body: { schema_id: SCHEMA_ID, force: false } },
    ]);
    // The run's end shows where it was started, with the way to its job page.
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the job" })).toHaveAttribute(
      "href",
      `/jobs/${JOB_ID}`,
    );
  });
});

describe("config screen: correction", () => {
  it("sends the changed steps and confirmations, then shows the new version and its report", async () => {
    const calls = configApi({ [`PUT /v1/configs/${V2_ID}`]: json(corrected) });
    await open(`/configs/${V2_ID}`, HOST);
    const form = await openTab("Correct");

    // title: a different XPath. price: confirmed as it is. sku: a first step, with a post operation.
    const title = form.getByRole("group", { name: "title" });
    const xpath = within(title).getByRole("textbox", { name: "XPath" });
    await replaceText(xpath, "//main//h1");
    await userEvent.click(
      within(form.getByRole("group", { name: "price" })).getByRole("checkbox", {
        name: "Confirm the steps of price",
      }),
    );
    const sku = form.getByRole("group", { name: "sku" });
    await userEvent.click(within(sku).getByRole("button", { name: "Add step" }));
    await replaceText(within(sku).getByRole("textbox", { name: "XPath" }), "//span[@itemprop]");
    await userEvent.click(within(sku).getByRole("button", { name: "Add operation" }));
    await userEvent.click(form.getByRole("button", { name: "Submit correction" }));

    const result = await screen.findByRole("region", { name: "Correction result" });
    expect(calls.find((call) => call.method === "PUT")).toMatchObject({
      path: `/v1/configs/${V2_ID}`,
      body: {
        fields: {
          title: { steps: [{ xpath: "//main//h1", post: [{ trim: {} }] }] },
          sku: { steps: [{ xpath: "//span[@itemprop]", post: [{ trim: {} }] }] },
        },
        confirm: ["price"],
      },
    });
    expect(within(result).getByText("Saved as version 3, active.")).toBeInTheDocument();
    expect(within(result).getByRole("link", { name: "Open version 3" })).toHaveAttribute(
      "href",
      `/configs/${V3_ID}`,
    );
    expect(within(result).getByRole("grid", { name: "Field validation" })).toBeInTheDocument();
  });
});
