import type { StoredJobRequest, StoredRequestOf } from "../../../api/jobs";
import { orderedFormats } from "../../../components/lib/formats";
import type { PageFormat } from "../FormatSelect";
import type { WebhookSettings } from "../pageRequest";

/** A job's request as `GET /v1/jobs/{id}/request` answers it, the server's validated request model with every default
 * filled in; a mode's form shows it again. These read the members several forms share. */
type PageRequest = StoredRequestOf<"scrape" | "batch_scrape" | "crawl" | "extract">;
type QueuedRequest = StoredRequestOf<"batch_scrape" | "crawl" | "extract">;

/** A number member that may be null as a form's number: NaN (an empty limit) for null. */
export function orEmpty(value: number | null): number {
  return value ?? Number.NaN;
}

/** `budget.llm_calls`; NaN without a budget. */
export function llmCalls(request: PageRequest): number {
  return orEmpty(request.budget?.llm_calls ?? null);
}

/** `min_confidence` as a percent; NaN when unset. */
export function minConfidencePercent(request: PageRequest): number {
  const value = request.min_confidence;
  return value === null ? Number.NaN : Number((value * 100).toFixed(4));
}

/** The stored schema's id, or "" when the request names none by id. */
export function schemaId(request: PageRequest): string {
  const schema = request.schema;
  return schema !== null && "id" in schema ? schema.id : "";
}

/** The field names the request's schema is drafted from; none when it names its schema otherwise. */
export function schemaFields(request: PageRequest): string[] {
  const schema = request.schema;
  return schema !== null && "fields" in schema ? schema.fields : [];
}

/** Whether the job's results are page outputs rather than records: its request asks for formats, and
 * `records` is not one. An extract's or a map's request has no formats: its results are not page outputs. */
export function pageOutputs(stored: StoredJobRequest): boolean {
  return "formats" in stored.request && !stored.request.formats.includes("records");
}

/** The page formats asked for, and whether `metadata` was, shown as Metadata in JSON: the request is the same either
 * way. `records` comes from the schema, not the Format select. */
export function pageFormats(request: StoredRequestOf<"scrape" | "batch_scrape" | "crawl">): {
  formats: PageFormat[];
  metadataInJson: boolean;
} {
  const asked = orderedFormats(request.formats);
  return {
    formats: asked.filter(
      (format): format is PageFormat => format !== "records" && format !== "metadata",
    ),
    metadataInJson: asked.includes("metadata"),
  };
}

/** The webhook options: `webhook_url` ("" for none), its events and batch. */
export function webhookSettings(request: QueuedRequest): WebhookSettings {
  const batch = request.webhook_batch;
  return {
    url: request.webhook_url ?? "",
    events: request.webhook_events,
    batchEvents: orEmpty(batch?.max_events ?? null),
    batchSeconds: orEmpty(batch?.max_seconds ?? null),
  };
}
