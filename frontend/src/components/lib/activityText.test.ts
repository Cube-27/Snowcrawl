import { describe, expect, it } from "vite-plus/test";
import type { StreamEvent } from "../../api/events";
import { activityGroups } from "../organisms/ActivityFeed";
import { activityEntry } from "./activityText";

const URL_A = "https://shop.test/items/1";

function event(kind: string, data: object, id: string | null = "1"): StreamEvent {
  return { id, kind, data: JSON.stringify(data) };
}

// Every event kind the API lists, with the data the worker records for it.
const LINES: readonly [string, object, string | null][] = [
  ["task_fetched", { url: URL_A, method: "http", block_class: "ok" }, "Fetched over HTTP."],
  ["task_fetched", { url: URL_A, method: "browser", block_class: "ok" }, "Fetched with a browser."],
  [
    "task_extracted",
    { url: URL_A, outcome: "partial", mode: "adhoc", llm_calls: 2 },
    "Extracted: partial (adhoc). Used 2 LLM calls.",
  ],
  ["task_abstained", { url: URL_A, fields: ["brand", "price"] }, "Did not publish brand, price."],
  [
    "task_failed",
    { task_id: "t1", url: URL_A, attempts: 3, last_error: "timeout after 30 s" },
    "Failed after 3 attempts: timeout after 30 s",
  ],
  [
    "learning_finished",
    { config_id: "c1", reason: "pending_approval", report: null },
    "Learned this site's config; it waits for approval.",
  ],
  ["progress", { counts: {}, outcomes: {} }, null],
  ["completed", {}, "Run finished."],
];

function activityLine(e: StreamEvent): string | null {
  return activityEntry(e)?.text ?? null;
}

describe("activityEntry", () => {
  it.each(LINES)("words %s %j as %j", (kind, data, line) => {
    expect(activityLine(event(kind, data))).toBe(line);
  });

  it("falls back to the kind name for unknown kinds and unreadable data", () => {
    expect(activityLine(event("task_retried", { url: URL_A }))).toBe("task_retried");
    expect(activityLine({ id: "1", kind: "task_failed", data: "not json" })).toBe("task_failed");
  });
});

describe("activityGroups", () => {
  it("groups lines by task URL, latest group first, without progress", () => {
    const groups = activityGroups([
      event("task_fetched", { url: URL_A, method: "http", block_class: "ok" }, "1"),
      event("progress", {}, null),
      event("completed", {}, "4"),
    ]);

    expect(groups.map(({ url, lines }) => [url, lines.map(({ text }) => text)])).toEqual([
      [null, ["Run finished."]],
      [URL_A, ["Fetched over HTTP."]],
    ]);
  });
});
