import type { ScrapeFormat } from "../../api/scrape";

/** The formats in the order the API lists them; a request sends the chosen ones in this order. */
export const FORMATS: readonly (readonly [ScrapeFormat, string])[] = [
  ["markdown", "Markdown"],
  ["markdown_fit", "Markdown (fit)"],
  ["chunks", "Chunks"],
  ["html", "HTML"],
  ["raw_html", "Raw HTML"],
  ["links", "Links"],
  ["metadata", "Metadata"],
  ["screenshot", "Screenshot"],
  ["pdf", "PDF"],
  ["records", "Records"],
];

/** `chosen` in the order of `FORMATS`. */
export function orderedFormats(chosen: readonly string[]): ScrapeFormat[] {
  return FORMATS.map(([format]) => format).filter((format) => chosen.includes(format));
}
