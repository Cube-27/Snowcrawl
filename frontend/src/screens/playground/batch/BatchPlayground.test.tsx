import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vite-plus/test";
import type { JobSummary, JsonValuesItem } from "../../../api/jobs";
import { stubApi, type Call } from "../../../test/api";
import { catchDownloads } from "../../../test/downloads";
import { jobSummary, storedBatch } from "../../../test/jobs";
import { recordField, storedRecord } from "../../../test/records";
import { quietEvents, recordsPage, runApi, SCHEMA_ID, valuesPage } from "../../../test/runs";
import {
  closeOptions,
  fill,
  openOptions,
  openPlayground,
  optionsButton,
  pick,
  posted,
  toggleFormats,
} from "../../../test/playground";

// `fetch` answers the batch, job, results and events endpoints with the shapes the API defines.

const JOB_ID = "0192a000-0000-7000-8000-0000000000b1";
const JOB = `/v1/jobs/${JOB_ID}`;
const CURSOR = "0192a000-0000-7000-8000-0000000000t2";

/** The batch's job: three tasks, one extracted, one blocked, one queued. */
function job(overrides: Partial<JobSummary> = {}): JobSummary {
  const summary = jobSummary({ id: JOB_ID, ...overrides });
  return { ...summary, outcomes: { ...summary.outcomes, extracted: 1, blocked: 1 }, ...overrides };
}

const FIRST: JsonValuesItem = {
  url: "https://shop.test/items/1",
  outcome: "extracted",
  records: [{ price: "19.90", currency: "EUR", title: "Blue mug" }],
};
const SECOND: JsonValuesItem = {
  url: "https://shop.test/items/2",
  outcome: "blocked",
  records: [],
};
const THIRD: JsonValuesItem = { url: "https://shop.test/items/3", outcome: null, records: [] };

/** The first page's record, as the job stores it. */
const RECORD = storedRecord("0192a000-0000-7000-8000-0000000000r1", SCHEMA_ID, {
  title: recordField({ value: "Blue mug" }),
});

/** The batch API: a POST answers the job's id, and the job's run answers `replies` over these defaults. A batch with
 * a schema, or without one (`pages`), which keeps page outputs. */
function batchApi(replies: Parameters<typeof stubApi>[0] = {}, pages = false): Call[] {
  return runApi(
    {
      submit: "POST /v1/batch/scrape",
      jobId: JOB_ID,
      kind: "batch_scrape",
      job: job(),
      request: pages
        ? storedBatch({ urls: [FIRST.url], formats: ["markdown"], schema: null })
        : storedBatch({ urls: [FIRST.url], formats: ["records"], schema: { id: SCHEMA_ID } }),
      items: [FIRST, SECOND, THIRD],
      records: [RECORD],
    },
    replies,
  );
}

async function typeUrls(text: string) {
  await fill(screen.getByRole("textbox", { name: "URLs" }), text);
}

async function start() {
  await userEvent.click(screen.getByRole("button", { name: "Start batch" }));
}

/** The records table of a batch with a schema, once it has its row. */
async function recordsGrid() {
  const grid = await screen.findByRole("grid", { name: "Records" });
  await within(grid).findByText("Blue mug");
  return grid;
}

/** The pages table of a batch without a schema, once it has rows. */
async function pagesGrid() {
  const grid = await screen.findByRole("grid", { name: "Pages" });
  await within(grid).findByText(FIRST.url);
  return grid;
}

function resultQueries(calls: readonly Call[]): string[] {
  return calls.filter(({ path }) => path === `${JOB}/results`).map(({ query }) => query);
}

describe("batch playground: the command bar", () => {
  it("appends a CSV file's URLs to the list, dropping those already listed", async () => {
    batchApi();
    await openPlayground("/batch");
    await typeUrls("https://shop.test/a");
    const csv = new File(
      ['name,url\nA,https://shop.test/a\n"B, C",https://shop.test/b\n'],
      "urls.csv",
      {
        type: "text/csv",
      },
    );

    await userEvent.upload(screen.getByLabelText("CSV file"), csv);

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "URLs" })).toHaveValue(
        "https://shop.test/a\nhttps://shop.test/b",
      );
    });
    expect(screen.getByText("2 URLs")).toBeInTheDocument();
  });

  it("posts the batch request the API defines, with a schema asking for records alone", async () => {
    const calls = batchApi();
    await openPlayground("/batch");
    await typeUrls("https://shop.test/a\n https://shop.test/b ");
    await toggleFormats("Links");
    await pick(/Schema/, "item_detail");
    expect(screen.getByRole("button", { name: /Format/ })).toBeDisabled();
    const options = await openOptions();
    await userEvent.type(
      options.getByRole("textbox", { name: "Webhook URL" }),
      "https://hooks.test/done",
    );
    await pick(/Fetch method/, "HTTP");
    await closeOptions();

    await start();

    await recordsGrid();
    expect(posted(calls)).toEqual({
      urls: ["https://shop.test/a", "https://shop.test/b"],
      formats: ["records"],
      schema: { id: SCHEMA_ID },
      page_kind: "detail",
      fetch: { method_hint: "http" },
      webhook_url: "https://hooks.test/done",
    });
  });

  it("names a line that is not a web address on the list, and sends nothing", async () => {
    const calls = batchApi();
    await openPlayground("/batch");
    await typeUrls(["https://shop.test/a", "shop dot test"].join("\n"));

    await start();

    const list = screen.getByRole("textbox", { name: "URLs" });
    expect(list).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Line 2 is not a web address: shop dot test")).toBeInTheDocument();
    expect(calls.filter(({ method }) => method === "POST")).toEqual([]);
    expect(screen.queryByRole("dialog", { name: "Options" })).not.toBeInTheDocument();
  });
});

describe("batch playground: the run", () => {
  it("shows the run on the page: status, progress, outcomes and a row per page in task order", async () => {
    batchApi({}, true);
    await openPlayground("/batch");
    await typeUrls("https://shop.test/a");

    await start();

    const grid = await pagesGrid();
    const rows = within(grid).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("rowheader")[0]?.textContent)).toEqual([
      FIRST.url,
      SECOND.url,
      THIRD.url,
    ]);
    const first = within(rows[0] ?? grid);
    expect(first.getByText("extracted")).toBeInTheDocument();
    expect(first.getByText("1")).toBeInTheDocument();
    expect(within(rows[2] ?? grid).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Pages done" })).toHaveAttribute(
      "aria-valuenow",
      "2",
    );
    expect(screen.getByText("2 of 3 pages done")).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "Outcomes" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["extracted1", "blocked1"]);
  });

  it("shows a batch with a schema as one records table, each row's JSON in its last column", async () => {
    batchApi();
    await openPlayground(`/batch?job=${JOB_ID}`);
    const grid = await recordsGrid();
    expect(screen.queryByRole("grid", { name: "Pages" })).not.toBeInTheDocument();
    expect(within(grid).getAllByRole("columnheader").at(-1)).toHaveTextContent("JSON");

    await userEvent.click(within(grid).getByRole("button", { name: "JSON of row 1" }));

    const drawer = await screen.findByRole("dialog");
    expect(
      JSON.parse(within(drawer).getByRole("region", { name: "Row 1 JSON" }).textContent),
    ).toEqual({ title: "Blue mug" });
  });

  it("downloads every page's item in task order", async () => {
    const saved = catchDownloads();
    const calls = batchApi({
      [`GET ${JOB}/results`]: ({ query }) => {
        const params = new URLSearchParams(query);
        if (!params.has("format")) return recordsPage([RECORD]);
        if (params.get("limit") !== "200") return valuesPage([FIRST, SECOND, THIRD]);
        return params.get("cursor") === null
          ? valuesPage([FIRST, SECOND], CURSOR)
          : valuesPage([THIRD]);
      },
    });
    await openPlayground(`/batch?job=${JOB_ID}`);
    await recordsGrid();

    await userEvent.click(screen.getByRole("button", { name: "Export JSON" }));

    await waitFor(() => {
      expect(saved.map(({ name }) => name)).toEqual([`job-${JOB_ID}.json`]);
    });
    expect(JSON.parse(await (saved[0]?.text ?? Promise.resolve("")))).toEqual([
      FIRST,
      SECOND,
      THIRD,
    ]);
    expect(resultQueries(calls).filter((query) => query.includes("limit=200"))).toEqual([
      "format=json_values&limit=200",
      `format=json_values&limit=200&cursor=${CURSOR}`,
    ]);
  });
});

describe("batch playground: runs", () => {
  const OLDER = "0192a000-0000-7000-8000-0000000000b0";

  it("shows a run's request read-only, and edit and re-run starts a new job, keeping the old one", async () => {
    const calls = batchApi({
      [`GET ${JOB}/request`]: () =>
        Response.json(
          storedBatch(
            {
              urls: ["https://shop.test/a", "https://shop.test/b"],
              formats: ["links", "metadata"],
              page_kind: "listing",
              budget: { llm_calls: 40 },
            },
            { method_hint: "browser", respect_robots: true, max_rows: 60 },
          ),
        ),
      "POST /v1/batch/scrape": () => Response.json({ job_id: OLDER }),
      [`GET /v1/jobs/${OLDER}`]: () => Response.json(job({ id: OLDER })),
      [`GET /v1/jobs/${OLDER}/request`]: () =>
        Response.json(storedBatch({ urls: ["https://shop.test/a"] })),
      [`GET /v1/jobs/${OLDER}/events`]: quietEvents,
      [`GET /v1/jobs/${OLDER}/results`]: () => valuesPage([]),
    });
    await openPlayground(`/batch?job=${JOB_ID}`);
    const list = screen.getByRole("textbox", { name: "URLs" });
    await waitFor(() => {
      expect(list).toHaveValue("https://shop.test/a\nhttps://shop.test/b");
    });
    expect(list).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Start batch" })).not.toBeInTheDocument();
    expect(optionsButton()).toHaveAccessibleDescription("Settings changed");
    const options = await openOptions();
    expect(options.getByRole("switch", { name: "Metadata in JSON" })).toBeChecked();
    expect(options.getByRole("switch", { name: "Metadata in JSON" })).toBeDisabled();
    expect(options.getByRole("textbox", { name: "LLM call limit" })).toHaveValue("40");
    expect(options.getByRole("textbox", { name: "Max rows" })).toHaveValue("60");
    await closeOptions();

    await userEvent.click(screen.getByRole("button", { name: "Edit and re-run" }));
    await fill(list, "\nhttps://shop.test/c");
    await start();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Edit and re-run" })).toBeInTheDocument();
    });
    expect(posted(calls)).toEqual({
      urls: ["https://shop.test/a", "https://shop.test/b", "https://shop.test/c"],
      formats: ["links", "metadata"],
      schema: null,
      page_kind: "listing",
      fetch: { method_hint: "browser", max_rows: 60 },
      budget: { llm_calls: 40 },
    });
  });
});
