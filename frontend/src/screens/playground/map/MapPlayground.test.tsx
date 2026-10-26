import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vite-plus/test";
import type { MapResult } from "../../../api/jobs";
import { stubApi, type Call } from "../../../test/api";
import { closeOptions, fill, openOptions, openPlayground, posted } from "../../../test/playground";

// `fetch` answers `POST /v1/map` with the shape the API defines.

/** 25 URLs: more than one page of the table. */
const URLS = Array.from({ length: 30 }, (_, i) => `https://shop.test/items/${String(i + 1)}`);

function mapApi(result: MapResult): Call[] {
  return stubApi({ "POST /v1/map": () => Response.json(result) });
}

async function map() {
  await fill(screen.getByRole("textbox", { name: "URL" }), "shop.test");
  await userEvent.click(screen.getByRole("button", { name: "Map" }));
}

/** The URLs in the links table's rows. */
function shownUrls(grid: HTMLElement): (string | null)[] {
  return within(grid)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getByRole("rowheader").textContent);
}

describe("map playground", () => {
  it("posts the map request the API defines from the bar and options", async () => {
    const calls = mapApi({ urls: URLS, sources: { sitemap: 25, links: 0 }, truncated: false });
    await openPlayground("/map");
    const options = await openOptions();
    await fill(options.getByRole("textbox", { name: "Exclude" }), "https://shop.test/cart/*", true);
    await userEvent.click(options.getByRole("switch", { name: "Use links" }));
    await fill(options.getByRole("textbox", { name: "Page limit" }), "100");
    await closeOptions();

    await map();

    await screen.findByRole("grid", { name: "Links" });
    expect(posted(calls)).toEqual({
      url: "https://shop.test",
      include: [],
      exclude: ["https://shop.test/cart/*"],
      use_sitemaps: true,
      use_links: false,
      limit: 100,
    });
  });

  it("lists the URLs found a page at a time, with where they came from and whether the list was cut", async () => {
    mapApi({ urls: URLS, sources: { sitemap: 25, links: 5 }, truncated: true });
    await openPlayground("/map");

    await map();

    const grid = await screen.findByRole("grid", { name: "Links" });
    expect(screen.getByText("30 URLs: 25 from sitemaps, 5 from links.")).toBeInTheDocument();
    expect(screen.getByText("The list is cut short.")).toBeInTheDocument();
    expect(shownUrls(grid)).toEqual(URLS.slice(0, 25));

    await userEvent.click(screen.getByRole("button", { name: /Next/ }));

    expect(shownUrls(grid)).toEqual(URLS.slice(25));
    expect(screen.getByText("Page 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Next/ })).toBeDisabled();
  });
});
