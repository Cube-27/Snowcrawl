import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vite-plus/test";
import type { Call } from "../../../test/api";
import { runApi, SCHEMA_ID } from "../../../test/runs";
import {
  closeOptions,
  fill,
  openOptions,
  openPlayground,
  pick,
  posted,
} from "../../../test/playground";
import type { StoredJobRequest } from "../../../api/jobs";
import { storedExtract } from "../../../test/jobs";

// `fetch` answers the extract, job, results and events endpoints with the shapes the API defines.

const JOB_ID = "0192a000-0000-7000-8000-0000000000e1";

function extractApi(request: StoredJobRequest = storedExtract()): Call[] {
  return runApi({ submit: "POST /v1/extract", jobId: JOB_ID, kind: "extract", request });
}

async function extract() {
  await userEvent.click(screen.getByRole("button", { name: "Extract" }));
}

describe("extract playground", () => {
  it("posts the URL, more URLs and globs, and a stored schema of its own page kind", async () => {
    const calls = extractApi();
    await openPlayground("/extract");
    await fill(screen.getByRole("textbox", { name: "URL" }), "shop.test/items/1");
    await pick(/Schema$/, "item_detail");
    const options = await openOptions();
    await fill(
      options.getByRole("textbox", { name: "More URLs" }),
      "https://shop.test/items/*",
      true,
    );
    await fill(options.getByRole("textbox", { name: "Max rows" }), "25");
    await closeOptions();

    await extract();

    await screen.findByText("No records yet");
    expect(posted(calls)).toEqual({
      urls: ["https://shop.test/items/1", "https://shop.test/items/*"],
      schema: { id: SCHEMA_ID },
      page_kind: "detail",
      fetch: { method_hint: "auto", max_rows: 25 },
    });
  });

  it("says at the toolbar's Schema that a schema is missing, leaving the options closed, and sends nothing", async () => {
    const calls = extractApi();
    await openPlayground("/extract");
    await fill(screen.getByRole("textbox", { name: "URL" }), "https://shop.test/items/1");

    await extract();

    expect(await screen.findByText("Choose a schema.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Options" })).not.toBeInTheDocument();
    expect(calls.filter(({ method }) => method === "POST")).toEqual([]);
  });
});
