import { isJsonObject, type JsonObject } from "../../api/json";
import type { Problem } from "../../api/problem";
import type {
  FieldSpecInput,
  FieldType,
  NewSchema,
  SchemaContent,
  SchemaPageKind,
} from "../../api/schemas";

/** What `SchemaForm` edits: a schema document with its fields as ordered rows. The UI checks only what
 * it needs to build the request; every schema rule is the server's, and its 422 is shown. */

type Scheme = NonNullable<FieldSpecInput["scheme"]>;
type Scope = NonNullable<FieldSpecInput["scope"]>;

/** The generated `FieldType` enum with a label each; a `Record` so a type the API adds fails the build here. */
export const FIELD_TYPES: Readonly<Record<FieldType, string>> = {
  text: "Text",
  long_text: "Long text",
  money: "Money",
  enum: "Enum",
  url: "URL",
  image_url: "Image URL",
  identifier: "Identifier",
  date: "Date",
  datetime: "Date and time",
  location: "Location",
  range: "Range",
  count: "Count",
  number: "Number",
  list: "List",
  object: "Object",
};

/** The generated identifier schemes, labelled. */
export const SCHEMES: Readonly<Record<Scheme, string>> = {
  gtin: "GTIN",
  isbn: "ISBN",
  mpn: "MPN",
  sku: "SKU",
  custom: "Custom",
};

/** The generated page kinds and scopes, labelled, as `Choices` takes them. */
export const PAGE_KIND_OPTIONS: readonly (readonly [SchemaPageKind, string])[] = [
  ["detail", "Detail"],
  ["listing", "Listing"],
];
export const SCOPE_OPTIONS: readonly (readonly [Scope, string])[] = [
  ["row", "Row"],
  ["page", "Page"],
];

export interface FieldRow {
  /** Stable across renames, for React keys. */
  key: string;
  name: string;
  spec: FieldSpecInput;
}

export interface SchemaFormState {
  name: string;
  /** Null until chosen: the form takes no default page kind. */
  pageKind: SchemaPageKind | null;
  /** One group per line, its field names separated by commas. */
  requiredGroups: string;
  fields: readonly FieldRow[];
}

/** A stored or drafted schema document, as `formOf` reads it. */
interface SchemaDocument {
  name: string;
  /** Null in the JSON tab until a page kind is chosen. */
  page_kind: SchemaPageKind | null;
  required_groups?: string[][];
  fields: Readonly<Record<string, FieldSpecInput>>;
}

let lastKey = 0;
function rowKey(): string {
  lastKey += 1;
  return `field-${String(lastKey)}`;
}

export function newRow(): FieldRow {
  return { key: rowKey(), name: "", spec: { type: "text", required: false, scope: "row" } };
}

export const EMPTY_FORM: SchemaFormState = {
  name: "",
  pageKind: null,
  requiredGroups: "",
  fields: [],
};

function groupsText(groups: readonly (readonly string[])[]): string {
  return groups.map((group) => group.join(", ")).join("\n");
}

function parseGroups(text: string): string[][] {
  return text
    .split("\n")
    .map((line) =>
      line
        .split(",")
        .map((name) => name.trim())
        .filter((name) => name !== ""),
    )
    .filter((group) => group.length > 0);
}

export function formOf(schema: SchemaDocument): SchemaFormState {
  return {
    name: schema.name,
    pageKind: schema.page_kind,
    requiredGroups: groupsText(schema.required_groups ?? []),
    fields: Object.entries(schema.fields).map(([name, spec]) => ({ key: rowKey(), name, spec })),
  };
}

/** The `PUT` body for `form`: the whole document without the name, which an edit keeps; null until a page kind is
 * chosen. */
export function contentOf(form: SchemaFormState): SchemaContent | null {
  if (form.pageKind === null) return null;
  return {
    page_kind: form.pageKind,
    required_groups: parseGroups(form.requiredGroups),
    fields: Object.fromEntries(form.fields.map((row) => [row.name.trim(), row.spec])),
  };
}

/** The `POST` body for `form`; null until a page kind is chosen. */
export function newSchemaOf(form: SchemaFormState): NewSchema | null {
  const content = contentOf(form);
  return content === null ? null : { name: form.name.trim(), ...content };
}

/** `spec` changed to `type`: the members every type has are kept, the ones only the old type takes (`vocab`,
 * `scheme`, `item`, `fields`) are dropped, since the API accepts them only for their own type. */
export function withType(spec: FieldSpecInput, type: FieldType): FieldSpecInput {
  return {
    type,
    ...(spec.required === undefined ? {} : { required: spec.required }),
    ...(spec.scope === undefined ? {} : { scope: spec.scope }),
    ...(spec.description == null ? {} : { description: spec.description }),
    ...(spec.schema_org == null ? {} : { schema_org: spec.schema_org }),
  };
}

/** Whether `name` is taken by another row. */
export function isDuplicateName(form: SchemaFormState, key: string, name: string): boolean {
  const trimmed = name.trim();
  return form.fields.some((row) => row.key !== key && row.name.trim() === trimmed);
}

// --- JSON view -----------------------------------------------------------------------------------------------------

export type FormMode = "create" | "edit";

/** `form` as the JSON the JSON tab edits: the request body, with `page_kind` null until chosen. */
export function jsonOf(form: SchemaFormState, mode: FormMode): string {
  const body = {
    ...(mode === "create" ? { name: form.name } : {}),
    page_kind: form.pageKind,
    required_groups: parseGroups(form.requiredGroups),
    fields: Object.fromEntries(form.fields.map((row) => [row.name, row.spec])),
  };
  return JSON.stringify(body, null, 2);
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

export function isFieldType(value: unknown): value is FieldType {
  return typeof value === "string" && Object.hasOwn(FIELD_TYPES, value);
}

export function isScheme(value: unknown): value is Scheme {
  return typeof value === "string" && Object.hasOwn(SCHEMES, value);
}

function isOptionalScheme(value: unknown): boolean {
  return value === undefined || value === null || isScheme(value);
}

function hasValidMembers(value: JsonObject): boolean {
  return (
    isFieldType(value.type) &&
    (value.required === undefined || typeof value.required === "boolean") &&
    (value.scope === undefined || SCOPE_OPTIONS.some(([scope]) => scope === value.scope)) &&
    isOptionalString(value.description) &&
    isOptionalString(value.schema_org) &&
    isOptionalString(value.vocab) &&
    isOptionalScheme(value.scheme)
  );
}

/** A field spec in the generated shape (its nested `item` and `fields` too). Members the type does not declare are
 * kept, for the server to judge. */
export function isFieldSpec(value: unknown): value is FieldSpecInput {
  if (!isJsonObject(value) || !hasValidMembers(value)) return false;
  const { item, fields } = value;
  return (
    (item === undefined || item === null || isFieldSpec(item)) &&
    (fields === undefined || fields === null || isFieldSpecs(fields))
  );
}

export function isFieldSpecs(value: unknown): value is Readonly<Record<string, FieldSpecInput>> {
  return isJsonObject(value) && Object.values(value).every(isFieldSpec);
}

function isGroups(value: unknown): value is string[][] {
  return (
    Array.isArray(value) &&
    value.every((group) => Array.isArray(group) && group.every((name) => typeof name === "string"))
  );
}

function isPageKind(value: unknown): value is SchemaPageKind {
  return PAGE_KIND_OPTIONS.some(([kind]) => kind === value);
}

/** A schema document in the generated `NewSchema` shape, as written: only the shape is checked; the schema rules are
 * the server's. */
export function isNewSchema(value: unknown): value is NewSchema {
  return (
    isJsonObject(value) &&
    typeof value.name === "string" &&
    isPageKind(value.page_kind) &&
    (value.required_groups === undefined || isGroups(value.required_groups)) &&
    isFieldSpecs(value.fields)
  );
}

type ParsedForm = { kind: "ok"; form: SchemaFormState } | { kind: "invalid"; reason: string };

function invalid(reason: string): ParsedForm {
  return { kind: "invalid", reason };
}

/** The JSON value in `text`; undefined when it is not JSON. */
export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** The form the JSON tab's `text` describes, or why it can't be read into one. In edit mode the name stays
 * the current name. Only the shape is checked; the schema rules are the server's. */
export function parseForm(text: string, mode: FormMode, current: SchemaFormState): ParsedForm {
  const value = parseJson(text);
  if (!isJsonObject(value)) return invalid("Enter the schema as a JSON object.");
  if (mode === "create" && typeof value.name !== "string")
    return invalid("`name` must be a string.");
  const pageKind = value.page_kind ?? null;
  if (pageKind !== null && !isPageKind(pageKind))
    return invalid("`page_kind` must be detail or listing.");
  const groups = value.required_groups ?? [];
  if (!isGroups(groups)) return invalid("`required_groups` must be lists of field names.");
  if (!isFieldSpecs(value.fields))
    return invalid("`fields` must map names to field specs with a known type.");
  const name = typeof value.name === "string" && mode === "create" ? value.name : current.name;
  return {
    kind: "ok",
    form: formOf({ name, page_kind: pageKind, required_groups: groups, fields: value.fields }),
  };
}

// --- Server errors -------------------------------------------------------------------------------------------------

/** Where a problem's error path is shown: under the field row it names (`fields.<name>…`), else under the input it
 * names (`name`, `page_kind`). React Aria's `Form` matches these keys to its inputs' `name`. */
export function inputErrors(problem: Problem | null): Readonly<Record<string, string>> {
  if (problem === null) return {};
  const byInput = new Map<string, string[]>();
  for (const { field, reason } of problem.errors) {
    const [head, name] = field.split(".");
    const input = head === "fields" && name !== undefined ? `fields.${name}` : field;
    byInput.set(input, [...(byInput.get(input) ?? []), reason]);
  }
  return Object.fromEntries([...byInput].map(([input, reasons]) => [input, reasons.join(" ")]));
}
