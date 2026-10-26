import { today } from "@internationalized/date";
import type { LlmUsage } from "../../api/usage";
import { formatUsd } from "../../components/lib/usd";

/** The ranges the cost history offers, in days through today (UTC). The API takes up to a year. */
export const RANGES = {
  "7": "Last 7 days",
  "30": "Last 30 days",
  "90": "Last 90 days",
  "365": "Last 365 days",
} as const;

export type RangeKey = keyof typeof RANGES;

export function isRange(key: unknown): key is RangeKey {
  return typeof key === "string" && key in RANGES;
}

// The API's days are UTC days, so the range is counted in UTC whatever the viewer's zone.
const ZONE = "UTC";

/** The first day of the range, as an ISO 8601 date. */
export function rangeStart(range: RangeKey): string {
  return today(ZONE)
    .subtract({ days: Number(range) - 1 })
    .toString();
}

/** Each `prompt_kind` by the role that sends it. */
const ROLE_LABELS: Readonly<Record<string, string>> = {
  c_extract_detail: "Extraction, detail page",
  c_extract_listing: "Extraction, listing page",
  a_synth: "Config synthesis",
  a_repair: "Config repair",
  d_bind: "API binding",
  s_draft: "Schema drafting",
  s_labels: "Label vocabulary",
};

/** The role's name; a kind this screen does not know is shown as the API names it. */
export function roleLabel(kind: string | null): string {
  if (kind === null) return "—";
  return ROLE_LABELS[kind] ?? kind;
}

const COUNT = new Intl.NumberFormat("en-US");

export function formatCount(n: number): string {
  return COUNT.format(n);
}

/** The estimated cost, or why there is none: tokens without a price are never guessed at (C7). */
export function formatCost(usage: LlmUsage): string {
  if (usage.estimated_usd === null) {
    return `Unknown: ${formatCount(usage.unpriced_tokens)} tokens have no price`;
  }
  return formatUsd(usage.estimated_usd);
}
