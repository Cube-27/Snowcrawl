import type { WebhookEvent } from "../../api/jobs";
import type { MethodHint, PageKind } from "../../api/scrape";

/** The choices the scrape and job requests share, with their labels. */

export const PAGE_KIND_LABELS: Readonly<Record<PageKind, string>> = {
  detail: "Detail",
  listing: "Listing",
};

export const PAGE_KINDS: readonly (readonly [PageKind, string])[] = [
  ["detail", PAGE_KIND_LABELS.detail],
  ["listing", PAGE_KIND_LABELS.listing],
];

export const METHOD_HINTS: readonly (readonly [MethodHint, string])[] = [
  ["auto", "Auto"],
  ["http", "HTTP"],
  ["browser", "Browser"],
];

/** The webhook events, in the order the server names them. */
export const WEBHOOK_EVENTS: readonly (readonly [WebhookEvent, string])[] = [
  ["page", "Page done"],
  ["record", "Record stored"],
  ["record_changed", "Record changed"],
  ["url_gone", "URL gone"],
  ["job_completed", "Job completed"],
  ["job_failed", "Job failed"],
  ["job_cancelled", "Job cancelled"],
];
