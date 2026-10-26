import { Chip, Tooltip } from "@heroui/react";
import type { FieldResult, FieldTypeSpec, ScalarFieldType } from "../../api/field";
import { isJsonObject, type JsonObject } from "../../api/json";
import { FieldStatusLabel } from "./StatusLabels";
import { keyedTexts } from "../lib/keys";

// The UI shows server values as they are: it joins the parts of a normalized value and never
// rounds, converts or fills one in.
function part(value: unknown): string | null {
  if (typeof value === "string") return value === "" ? null : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function joinParts(parts: readonly unknown[], separator: string): string {
  return parts
    .map(part)
    .filter((text) => text !== null)
    .join(separator);
}

// Money is `{amount, currency}`, shown as "amount currency". A null currency is one the page did not
// settle: the amount is shown with that said, never with a currency filled in.
function money(value: JsonObject): string {
  return joinParts([value.amount, value.currency ?? "(currency unknown)"], " ");
}

// Location is `{text, locality, region, country, remote_flag}`. `text` is the place as the page writes
// it and is shown as is; the parts stand in only when it is null. The flag is shown as text, not inferred.
function location(value: JsonObject): string {
  const place = part(value.text) ?? joinParts([value.locality, value.region, value.country], ", ");
  return joinParts([place, value.remote_flag === true ? "remote" : null], ", ");
}

// Range is `{min, max, currency|unit, period}`.
function range(value: JsonObject): string {
  const bounds = joinParts([value.min, value.max], "–");
  const period = part(value.period);
  return joinParts(
    [bounds, value.currency ?? value.unit, period === null ? null : `per ${period}`],
    " ",
  );
}

const OBJECT_FORMATS: Partial<Record<ScalarFieldType, (value: JsonObject) => string>> = {
  money,
  location,
  range,
};

function formatValue(value: unknown, spec: FieldTypeSpec): string {
  if (spec.type === "list") {
    return Array.isArray(value) ? value.map((item) => formatValue(item, spec.item)).join(", ") : "";
  }
  if (!isJsonObject(value)) return part(value) ?? "";
  if (spec.type === "object") {
    return Object.entries(spec.fields)
      .map(([name, sub]) => `${name}: ${formatValue(value[name], sub)}`)
      .join("; ");
  }
  return OBJECT_FORMATS[spec.type]?.(value) ?? JSON.stringify(value);
}

// A value whose type the caller doesn't know is shown as the server sent it: a string as is, anything else as JSON.
function formatUntyped(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function formatField(value: unknown, spec: FieldTypeSpec | null): string {
  return spec === null ? formatUntyped(value) : formatValue(value, spec);
}

/** An unpublished or empty field: "—" with its reason on hover or focus, never an empty string. */
function NoValue({ reason }: Readonly<{ reason: string | null }>) {
  const name = reason === null ? "No value" : `No value: ${reason}`;
  return (
    <Tooltip delay={0}>
      <Tooltip.Trigger aria-label={name}>—</Tooltip.Trigger>
      <Tooltip.Content>{reason ?? "No value"}</Tooltip.Content>
    </Tooltip>
  );
}

/** A field's value, shown by its type; `spec` is null when the type is unknown. A field that did not
 * publish shows "—", its reason and, unless the caller shows it in a column of its own (`withStatus` false), its
 * status, and never a value. */
export function FieldValue({
  field,
  spec,
  withStatus = true,
}: Readonly<{ field: FieldResult; spec: FieldTypeSpec | null; withStatus?: boolean }>) {
  if (field.status !== "published") {
    return (
      <span className="inline-flex items-center gap-2">
        <NoValue reason={field.reason} />
        {withStatus ? <FieldStatusLabel status={field.status} /> : null}
      </span>
    );
  }
  if (spec?.type === "list" && Array.isArray(field.value) && field.value.length > 0) {
    return (
      <span className="inline-flex flex-wrap gap-1">
        {keyedTexts(field.value.map((item) => formatValue(item, spec.item))).map(
          ({ key, text }) => (
            <Chip key={key}>
              <Chip.Label>{text}</Chip.Label>
            </Chip>
          ),
        )}
      </span>
    );
  }
  const text = formatField(field.value, spec);
  return text === "" ? <NoValue reason={field.reason} /> : <span>{text}</span>;
}
