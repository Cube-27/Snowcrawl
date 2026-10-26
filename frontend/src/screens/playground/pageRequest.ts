import type { WebhookEvent } from "../../api/jobs";
import type { PageKind, SchemaRef, ScrapeFormat, ScreenshotArea } from "../../api/scrape";
import type { SchemaPageKind } from "../../api/schemas";
import { orderedFormats } from "../../components/lib/formats";
import type { PageFormat } from "./FormatSelect";

/** The non-blank lines of `text`, trimmed: a list entered one entry per line. */
export function lines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** The events a request that names none is sent: the job's end. */
export const JOB_END_EVENTS: readonly WebhookEvent[] = [
  "job_completed",
  "job_failed",
  "job_cancelled",
];

/** A queued mode's webhook options: an empty `url` sends none of them. `batchEvents` and `batchSeconds`
 * (NaN while empty) group events into deliveries, both or neither. */
export interface WebhookSettings {
  url: string;
  events: readonly WebhookEvent[];
  batchEvents: number;
  batchSeconds: number;
}

export const NO_WEBHOOK: WebhookSettings = {
  url: "",
  events: JOB_END_EVENTS,
  batchEvents: Number.NaN,
  batchSeconds: Number.NaN,
};

function sameEvents(a: readonly WebhookEvent[], b: readonly WebhookEvent[]): boolean {
  return a.length === b.length && a.every((event) => b.includes(event));
}

/** Why the webhook options can't be sent, by request member. */
export function webhookErrors(settings: WebhookSettings): Readonly<Record<string, string>> {
  if (settings.url.trim() === "") return NO_ERRORS;
  if (settings.events.length === 0) return { webhook_events: "Choose at least one event." };
  const both = "Give both the events and the seconds of a batch, or neither.";
  if (Number.isNaN(settings.batchEvents) && !Number.isNaN(settings.batchSeconds))
    return { "webhook_batch.max_events": both };
  if (!Number.isNaN(settings.batchEvents) && Number.isNaN(settings.batchSeconds))
    return { "webhook_batch.max_seconds": both };
  return NO_ERRORS;
}

/** `errors` with the webhook's own after them; `errors` itself when the webhook has none. */
export function withWebhookErrors(
  errors: Readonly<Record<string, string>>,
  settings: WebhookSettings,
): Readonly<Record<string, string>> {
  const own = webhookErrors(settings);
  return own === NO_ERRORS ? errors : { ...errors, ...own };
}

/** The optional `webhook_url`, `webhook_events` and `webhook_batch` members: all left out without a
 * URL, the events when they are the job's end (the server's own choice), the batch while it is empty. */
export function webhook(settings: WebhookSettings): {
  webhook_url?: string;
  webhook_events?: WebhookEvent[];
  webhook_batch?: { max_events: number; max_seconds: number };
} {
  const url = settings.url.trim();
  if (url === "") return {};
  const batched = !Number.isNaN(settings.batchEvents) && !Number.isNaN(settings.batchSeconds);
  return {
    webhook_url: url,
    ...(sameEvents(settings.events, JOB_END_EVENTS)
      ? {}
      : { webhook_events: [...settings.events] }),
    ...(batched
      ? { webhook_batch: { max_events: settings.batchEvents, max_seconds: settings.batchSeconds } }
      : {}),
  };
}

/** An optional number member: left out while its field is empty (NaN), so the server's default applies. */
export function optionalNumber<K extends string>(
  key: K,
  value: number,
): Partial<Record<K, number>> {
  const member: Partial<Record<K, number>> = {};
  if (!Number.isNaN(value)) member[key] = value;
  return member;
}

/** The area `settings` give a screenshot, for `fetchMember`, when `formats` has one. */
export function screenshotArea(
  formats: readonly ScrapeFormat[],
  settings: { screenshot: ScreenshotArea },
): ScreenshotArea | undefined {
  return formats.includes("screenshot") ? settings.screenshot : undefined;
}

/** The optional `budget.llm_calls` member, left out while empty. */
export function llmBudget(llmCalls: number): { budget?: { llm_calls: number } } {
  return Number.isNaN(llmCalls) ? {} : { budget: { llm_calls: llmCalls } };
}

/** The optional `min_confidence` member, from the percent the form holds (0 to 100), left out while empty (NaN): every
 * value then publishes with its confidence. */
export function minConfidence(percent: number): { min_confidence?: number } {
  return Number.isNaN(percent) ? {} : { min_confidence: Number((percent / 100).toFixed(6)) };
}

/** What the toolbar's Format and Schema hold, the Metadata in JSON view option and the field names, in every mode
 * that takes them (Scrape, Batch, Crawl). `schemaId` is a stored schema's id, or "" for none;
 * `schemaPageKind` is that schema's page kind. */
export interface FormatForm {
  formats: readonly PageFormat[];
  schemaId: string;
  schemaPageKind: SchemaPageKind | null;
  options: { metadataInJson: boolean; fields: readonly string[] };
}

/** A mode whose request names one page kind for every page (Scrape, Batch). */
export interface PageKindForm extends FormatForm {
  options: { metadataInJson: boolean; fields: readonly string[]; pageKind: PageKind | null };
}

/** The request's schema: the stored one, else the field names the server drafts one from, else
 * none. */
export function requestSchema(
  form: FormatForm,
): Extract<SchemaRef, { id: string } | { fields: string[] }> | null {
  if (form.schemaId !== "") return { id: form.schemaId };
  const { fields } = form.options;
  return fields.length === 0 ? null : { fields: [...fields] };
}

/** Whether the request names a schema: a stored one or field names. */
export function hasSchema(form: FormatForm): boolean {
  return form.schemaId !== "" || form.options.fields.length > 0;
}

/** `form` with the stored schema `schemaId`, for `schemaPageKind` pages. */
export function withSchema<F extends Pick<FormatForm, "schemaId" | "schemaPageKind">>(
  form: F,
  schemaId: string,
  schemaPageKind: SchemaPageKind | null,
): F {
  return { ...form, schemaId, schemaPageKind };
}

/** The request's formats: the page formats chosen, `metadata` for Metadata in JSON, and `records` with a schema or
 * field names. */
export function requestFormats(form: FormatForm): ScrapeFormat[] {
  return orderedFormats([
    ...form.formats,
    ...(form.options.metadataInJson ? ["metadata"] : []),
    ...(hasSchema(form) ? ["records"] : []),
  ]);
}

/** A batch's or crawl's formats: `records` alone with a schema or field names (its results are records
 * or page outputs), else `requestFormats`. */
export function jobFormats(form: FormatForm): ScrapeFormat[] {
  return hasSchema(form) ? ["records"] : requestFormats(form);
}

/** Whether `form` asks for no format at all, which the server refuses. */
export function asksForNothing(form: FormatForm): boolean {
  return requestFormats(form).length === 0;
}

/** The page kind the request names. A stored schema's records are for its own page kind only (`check_records`), so with a schema it is that kind, or null when there is none. */
export function requestPageKind(form: PageKindForm): PageKind | null {
  return form.schemaId === "" ? form.options.pageKind : form.schemaPageKind;
}

export const NO_ERRORS: Readonly<Record<string, string>> = {};
export const NO_FORMAT = { formats: "Choose a format, Metadata in JSON, a schema or fields." };
const NO_SCHEMA_PAGE_KIND = { page_kind: "The schema names no page kind, which its records need." };
export const SCHEMA_AND_FIELDS = { fields: "Use the stored schema or fields, not both." };
const NO_FIELDS_PAGE_KIND = { page_kind: "Choose the page kind the fields are read from." };

/** Why a stored schema's records can't be asked for: it names no page kind; null when they can. */
export function schemaPageKindErrors(
  schemaPageKind: SchemaPageKind | null,
): Readonly<Record<string, string>> | null {
  return schemaPageKind === null ? NO_SCHEMA_PAGE_KIND : null;
}

/** Why field names can't be drafted: no page kind is chosen; null when one is. */
export function fieldsPageKindErrors(
  pageKind: PageKind | null,
): Readonly<Record<string, string>> | null {
  return pageKind === null ? NO_FIELDS_PAGE_KIND : null;
}

/** Why `form` can't be sent, by field: it asks for no format; it names a stored schema and fields; its schema's
 * records are for no page kind (the schema names none); or its fields have no page kind. Derived from the current
 * form, so a change anywhere that fixes it clears it; each answer is one constant, so the errors change only when the
 * reason does. */
export function pageKindErrors(form: PageKindForm): Readonly<Record<string, string>> {
  if (asksForNothing(form)) return NO_FORMAT;
  const hasFields = form.options.fields.length > 0;
  if (form.schemaId !== "" && hasFields) return SCHEMA_AND_FIELDS;
  if (hasFields) return fieldsPageKindErrors(form.options.pageKind) ?? NO_ERRORS;
  if (form.schemaId === "") return NO_ERRORS;
  return schemaPageKindErrors(form.schemaPageKind) ?? NO_ERRORS;
}

/** SameValue (ECMA-262), so an empty limit (NaN) equals the default; a list equals one with the same entries, and a
 * group of settings (Advanced) one with the same members. */
function sameSetting(value: unknown, other: unknown): boolean {
  if (Array.isArray(value) && Array.isArray(other))
    return value.length === other.length && value.every((entry, i) => sameSetting(entry, other[i]));
  if (isGroup(value) && isGroup(other)) {
    const keys = Object.keys(value);
    return (
      keys.length === Object.keys(other).length &&
      keys.every((key) => sameSetting(value[key], other[key]))
    );
  }
  return Object.is(value, other);
}

function isGroup(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether any setting differs from its default. */
export function optionsChanged<O extends object>(options: O, defaults: O): boolean {
  return (Object.keys(defaults) as (keyof O)[]).some(
    (key) => !sameSetting(options[key], defaults[key]),
  );
}
