import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { routes } from "../../app/router";
import type { JobPage, JobSummary } from "../../api/jobs";
import type { RecordPage } from "../../api/records";
import type { SchemaDef } from "../../api/schemas";
import { sse, stubApi, type Call } from "../../test/api";
import { jobSummary } from "../../test/jobs";
import { recordField } from "../../test/records";
import { fieldSpec } from "../../test/schemas";

const JOB_ID = "0192a000-0000-7000-8000-000000000901";
const SCHEMA_ID = "0192a000-0000-7000-8000-000000000101";
const RECORD_ID = "0192a000-0000-7000-8000-000000000301";
const SECOND_JOB_ID = "0192a000-0000-7000-8000-000000000902";
const JOB = `/v1/jobs/${JOB_ID}`;

/** A `learn` job: no playground mode runs it, so it keeps its job page. */
function job(overrides: Partial<JobSummary> = {}): JobSummary {
  return jobSummary({ id: JOB_ID, kind: "learn", ...overrides });
}

const schema: SchemaDef = {
  id: SCHEMA_ID,
  name: "item_detail",
  origin: "user",
  status: "active",
  page_kind: "detail",
  required_groups: [["title"]],
  field_order: ["title"],
  labels: null,
  fields: { title: fieldSpec({ type: "text", required: true, scope: "row" }) },
};

const records: RecordPage = {
  items: [
    {
      id: RECORD_ID,
      schema_id: SCHEMA_ID,
      capture_id: null,
      mode: "adhoc",
      version: 1,
      complete: true,
      confidence: 0.97,
      changed_fields: [],
      egress_country: null,
      egress_basis: "unknown",
      fields: { title: recordField({ value: "Blue kettle" }) },
    },
  ],
  next_cursor: null,
  total: 1,
};

const ENDED = () =>
  sse(
    ["1", "task_fetched", { url: "https://shop.example.test/item/1" }],
    [null, "progress", {}],
    ["2", "completed", {}],
  );

/** A `GET /v1/jobs` answer. */
function jobPage(items: JobSummary[], next_cursor: string | null = null, total = items.length) {
  return () => Response.json({ items, next_cursor, total } satisfies JobPage);
}

/** The jobs table, once it shows rows. */
function findJobsGrid(): Promise<HTMLElement> {
  return screen.findByRole("grid", { name: "Jobs" });
}

/** The query of each `GET /v1/jobs` the screen sent, in order. */
function listQueries(calls: Call[]): string[] {
  return calls.filter(({ path }) => path === "/v1/jobs").map(({ query }) => query);
}

function jobApi(extra: Record<string, (call: Call) => Response> = {}) {
  return stubApi({
    "GET /v1/jobs": jobPage([]),
    [`GET ${JOB}`]: () => Response.json(job()),
    [`GET ${JOB}/events`]: ENDED,
    [`GET ${JOB}/results`]: () => Response.json(records),
    [`GET /v1/schemas/${SCHEMA_ID}`]: () => Response.json(schema),
    ...extra,
  });
}

async function open(path: string, heading: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  await screen.findByRole("heading", { level: 1, name: heading });
}

/** The job's status, beside the page title. */
function jobStatus(): HTMLElement | null {
  return screen.getByRole("heading", { level: 1 }).parentElement;
}

function rowsOf(grid: HTMLElement): (string | null)[] {
  return within(grid)
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.textContent);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Job screen", () => {
  it("cancels the job after confirmation and shows its new status", async () => {
    const calls = jobApi({
      [`DELETE ${JOB}`]: () => Response.json(job({ status: "cancelled" })),
    });
    await open(`/jobs/${JOB_ID}`, "Learn job");

    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel job" }));

    await waitFor(() => {
      expect(jobStatus()).toHaveTextContent("cancelled");
    });
    expect(calls.filter(({ method }) => method === "DELETE")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Cancel job" })).not.toBeInTheDocument();
  });
});

describe("Jobs table", () => {
  it("lists the jobs with their kind, target, status, task counts and creation time", async () => {
    jobApi({
      "GET /v1/jobs": jobPage([
        job(),
        job({
          id: SECOND_JOB_ID,
          kind: "crawl",
          status: "failed",
          counts: { queued: 0, claimed: 0, done: 0, failed: 3, cancelled: 1 },
          created_at: "2026-10-13T08:00:00Z",
          start_url: "https://shop.example.test/",
          url_count: null,
        }),
      ]),
    });
    await open("/jobs", "Jobs");

    const table = await screen.findByRole("grid", { name: "Jobs" });
    expect(rowsOf(table)).toEqual([
      "Learn" + "3 URLs" + "running" + "3 tasks: 1 queued, 2 done" + "Oct 14, 2026, 09:00" + "Open",
      "Crawl" +
        "https://shop.example.test/" +
        "failed" +
        "4 tasks: 3 failed, 1 cancelled" +
        "Oct 13, 2026, 08:00" +
        "Open",
    ]);
  });

  it("pages forward with the cursor the API returned, and back to the first page", async () => {
    const calls = jobApi({
      "GET /v1/jobs": (call) =>
        call.query.includes("cursor=c2")
          ? jobPage([job({ id: SECOND_JOB_ID, kind: "map" })], null, 51)()
          : jobPage([job()], "c2", 51)(),
    });
    await open("/jobs", "Jobs");
    await screen.findByRole("grid", { name: "Jobs" });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await within(await findJobsGrid()).findByText("Map")).toBeInTheDocument();
    expect(screen.getByText("Rows 26–26 of 51")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Rows 1–1 of 51")).toBeInTheDocument();
    expect(listQueries(calls)).toEqual(["limit=25", "cursor=c2&limit=25", "limit=25"]);
  });
});
