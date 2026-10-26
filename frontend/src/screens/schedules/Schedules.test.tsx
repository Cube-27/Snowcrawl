import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vite-plus/test";
import { routes } from "../../app/router";
import type { Schedule } from "../../api/schedules";
import { stubApi, type Call } from "../../test/api";
import { fill, openPlayground, posted } from "../../test/playground";
import { runApi } from "../../test/runs";
import { scheduleChange } from "./cadence";
import { storedCrawl } from "../../test/jobs";

// `fetch` answers `/v1/schedules` and `/v1/jobs` with the shapes the API defines.

const SCHEDULE_ID = "0192a000-0000-7000-8000-00000000a001";
const JOB_ID = "0192a000-0000-7000-8000-00000000a0j1";

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: SCHEDULE_ID,
    name: "Shop crawl",
    kind: "crawl",
    request: { url: "https://shop.test/", formats: ["markdown"] },
    cron: null,
    interval_s: 3600,
    timezone: "UTC",
    paused: false,
    next_run_at: "2026-10-17T10:00:00Z",
    last_run_at: "2026-10-17T09:00:00Z",
    last_job_id: JOB_ID,
    last_error: null,
    created_at: "2026-10-17T08:00:00Z",
    updated_at: "2026-10-17T08:00:00Z",
    ...overrides,
  };
}

async function openAt(path: string, heading: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  await screen.findByRole("heading", { level: 1, name: heading });
}

function crawlApi(replies: Parameters<typeof stubApi>[0] = {}): Call[] {
  return runApi(
    {
      submit: "POST /v1/crawl",
      jobId: JOB_ID,
      kind: "crawl",
      request: storedCrawl({ url: "https://shop.test/" }),
    },
    {
      "POST /v1/schedules": () => Response.json(schedule(), { status: 201 }),
      [`GET /v1/schedules/${SCHEDULE_ID}`]: () => Response.json(schedule()),
      ...replies,
    },
  );
}

describe("schedules", () => {
  it("schedules the crawl the form would start, every 60 minutes, and opens the schedule", async () => {
    const calls = crawlApi();
    await openPlayground("/crawl");
    await fill(screen.getByRole("textbox", { name: "URL" }), "https://shop.test/");

    await userEvent.click(screen.getByRole("button", { name: "Schedule this" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Schedule this request" }));
    await fill(dialog.getByRole("textbox", { name: "Name" }), "Shop crawl");
    await userEvent.click(dialog.getByRole("button", { name: "Create schedule" }));

    await screen.findByRole("heading", { level: 1, name: "Shop crawl" });
    expect(posted(calls)).toEqual({
      name: "Shop crawl",
      kind: "crawl",
      request: {
        url: "https://shop.test/",
        formats: ["markdown"],
        schema: null,
        page_kind_rules: [],
        include: [],
        exclude: [],
        fetch: { method_hint: "auto" },
      },
      interval_s: 3600,
      timezone: expect.any(String) as unknown,
    });
  });

  it("lists the schedules with their cadence and last run, and pauses one", async () => {
    let paused = false;
    const calls = stubApi({
      "GET /v1/schedules": () =>
        Response.json({ items: [schedule({ paused })], next_cursor: null }),
      [`POST /v1/schedules/${SCHEDULE_ID}/pause`]: () => {
        paused = true;
        return Response.json(schedule({ paused }));
      },
    });
    await openAt("/schedules", "Schedules");

    const table = within(await screen.findByRole("grid", { name: "Schedules" }));
    expect(await table.findByText("Every 1 h")).toBeInTheDocument();
    expect(table.getByText("active")).toBeInTheDocument();
    await userEvent.click(table.getByRole("button", { name: "Pause" }));

    expect(await table.findByText("paused")).toBeInTheDocument();
    expect(calls.map(({ method, path }) => `${method} ${path}`)).toContain(
      `POST /v1/schedules/${SCHEDULE_ID}/pause`,
    );
  });

  it("edits a schedule's name and cadence and sends only what changed", async () => {
    let current = schedule();
    const calls = stubApi({
      [`GET /v1/schedules/${SCHEDULE_ID}`]: () => Response.json(current),
      [`PATCH /v1/schedules/${SCHEDULE_ID}`]: (call) => {
        current = schedule({
          ...(call.body as Partial<Schedule>),
          cron: "0 6 * * 1-5",
          interval_s: null,
        });
        return Response.json(current);
      },
      "GET /v1/jobs": () => Response.json({ items: [], next_cursor: null }),
    });
    await openAt(`/schedules/${SCHEDULE_ID}`, "Shop crawl");

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Edit Shop crawl" }));
    expect(dialog.getByRole("textbox", { name: "Request (JSON)" })).toHaveValue(
      JSON.stringify(current.request, null, 2),
    );
    await fill(dialog.getByRole("textbox", { name: "Name" }), " renamed");
    await userEvent.click(dialog.getByRole("radio", { name: "Cron expression" }));
    await fill(dialog.getByRole("textbox", { name: /Cron expression/ }), "0 6 * * 1-5");
    await userEvent.click(dialog.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: /Edit/ })).not.toBeInTheDocument();
    });
    const sent = calls.find(({ method }) => method === "PATCH");
    expect(sent?.body).toEqual({
      name: "Shop crawl renamed",
      timezone: "UTC",
      cron: "0 6 * * 1-5",
    });
    expect(await screen.findByText("Cron 0 6 * * 1-5 (UTC)")).toBeInTheDocument();
  });

  it("changes only what the edit changed: nothing for an untouched draft, an interval as seconds", () => {
    const current = schedule();
    const draft = {
      name: "Shop crawl",
      every: "interval",
      minutes: 60,
      cron: "",
      timezone: "UTC",
    } as const;
    const text = JSON.stringify(current.request, null, 2);

    expect(scheduleChange(current, draft, { text, initial: text })).toEqual({});
    expect(scheduleChange(current, { ...draft, minutes: 15 }, { text, initial: text })).toEqual({
      timezone: "UTC",
      interval_s: 900,
    });
    expect(scheduleChange(current, draft, { text: "{", initial: text })).toBeNull();
  });
});
