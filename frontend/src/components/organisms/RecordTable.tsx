import { Button, Table } from "@heroui/react";
import { useMemo, useState, type ReactNode } from "react";
import type { FieldTypes, FieldTypeSpec } from "../../api/field";
import type { ExtractedRecord, StoredRecord } from "../../api/records";
import { confidenceText } from "../molecules/ConfidenceCell";
import { FieldValue } from "../molecules/FieldValue";
import { JsonView } from "../molecules/JsonView";
import { SideDrawer } from "../molecules/SideDrawer";

/** The fields whose published value differs from the version the record supersedes, or null when none, as for a
 * first version. */
export function changedFieldsText({ changed_fields: changed }: StoredRecord): string | null {
  return changed.length === 0 ? null : changed.join(", ");
}

/** The JSON column's id: fields are prefixed (`field:`), so it never equals one. */
const JSON_COLUMN = "json";

/** A row of the table: a stored record, or one a page just yielded (no id, version or changed fields). */
export type TableRecord = ExtractedRecord &
  Partial<Pick<StoredRecord, "id" | "version" | "changed_fields" | "delta">>;

/** A nested value (a list or an object) opened from its cell: which field of which row. */
interface Nested {
  title: string;
  value: unknown;
}

interface Column {
  id: string;
  name: string;
  /** A column of numbers, set `numeric` (theme.css). */
  numeric?: boolean;
  render: (record: TableRecord, row: number, view: (nested: Nested) => void) => ReactNode;
}

const NO_VALUE = "—";

function isNested(spec: FieldTypeSpec | null, value: unknown): boolean {
  return (
    (spec?.type === "list" || spec?.type === "object") && value !== null && value !== undefined
  );
}

/** The compact form of a list or an object (field types): how many members it has, and a button to see
 * them in a drawer, so a row stays one line however long a variants or images list is. */
function nestedSummary(value: unknown): string {
  if (Array.isArray(value)) return value.length === 1 ? "1 item" : `${String(value.length)} items`;
  if (typeof value === "object" && value !== null) {
    const count = Object.keys(value).length;
    return count === 1 ? "1 field" : `${String(count)} fields`;
  }
  return NO_VALUE;
}

function NestedCell({
  name,
  value,
  onView,
}: Readonly<{ name: string; value: unknown; onView: () => void }>) {
  if (Array.isArray(value) && value.length === 0) return NO_VALUE;
  return (
    <span className="inline-flex items-center gap-2">
      <span>{nestedSummary(value)}</span>
      <Button size="sm" variant="secondary" aria-label={`View ${name}`} onPress={onView}>
        View
      </Button>
    </span>
  );
}

// Field names are user data and may equal a member's name, so a field column's id carries a prefix.
// The field types whose values are numbers: an amount, a count, a quantity.
const NUMERIC_TYPES: ReadonlySet<string> = new Set(["money", "count", "number"]);

function fieldColumn(name: string, spec: FieldTypeSpec | null): Column {
  return {
    id: `field:${name}`,
    name,
    numeric: spec !== null && NUMERIC_TYPES.has(spec.type),
    render: (record, row, view) => {
      const field = record.fields[name];
      if (field === undefined) return NO_VALUE;
      if (field.status === "published" && isNested(spec, field.value)) {
        return (
          <NestedCell
            name={name}
            value={field.value}
            onView={() => {
              view({ title: `${name} of row ${String(row + 1)}`, value: field.value });
            }}
          />
        );
      }
      return <FieldValue field={field} spec={spec} />;
    },
  };
}

const RECORD_COLUMNS: Column[] = [
  { id: "mode", name: "Mode", render: (record) => record.mode },
  {
    id: "confidence",
    name: "Confidence",
    numeric: true,
    render: (record) => confidenceText(record.confidence),
  },
];

const STORED_COLUMNS: Column[] = [
  // A job's record against its entity's previous record: new, changed or same.
  { id: "delta", name: "Delta", render: (record) => record.delta?.kind ?? NO_VALUE },
  { id: "version", name: "Version", numeric: true, render: (record) => record.version ?? NO_VALUE },
  {
    id: "changed",
    name: "Changed",
    render: (record) => (record.changed_fields?.join(", ") ?? "") || NO_VALUE,
  },
];

/** The field names, in the schema's order; for a schema the UI does not know the types of, the names the first record
 * carries. */
function fieldNames(fieldTypes: FieldTypes, records: readonly TableRecord[]): string[] {
  const named = Object.keys(fieldTypes);
  return named.length > 0 ? named : Object.keys(records[0]?.fields ?? {});
}

/** The last column: a compact button that opens the row's JSON (`json`) in a drawer. */
function jsonColumn(json: (record: TableRecord) => unknown): Column {
  return {
    id: JSON_COLUMN,
    name: "JSON",
    render: (record, row, view) => (
      <Button
        size="sm"
        variant="secondary"
        aria-label={`JSON of row ${String(row + 1)}`}
        onPress={() => {
          view({ title: `Row ${String(row + 1)} JSON`, value: json(record) });
        }}
      >
        JSON
      </Button>
    ),
  };
}

function columnsOf(
  fieldTypes: FieldTypes,
  records: readonly TableRecord[],
  stored: boolean,
  json: ((record: TableRecord) => unknown) | undefined,
): Column[] {
  return [
    ...fieldNames(fieldTypes, records).map((name) => fieldColumn(name, fieldTypes[name] ?? null)),
    ...(stored ? STORED_COLUMNS : []),
    ...RECORD_COLUMNS,
    ...(json === undefined ? [] : [jsonColumn(json)]),
  ];
}

/** A header's or a cell's classes: the JSON column as narrow as its button, a field's `sized` to keep a row one
 * line, and a column of numbers `numeric`. */
function columnClass(column: Column, sized: string): string {
  if (column.id === JSON_COLUMN) return "w-px";
  return column.numeric === true ? `${sized} numeric` : sized;
}

function NestedDrawer({ nested, onClose }: Readonly<{ nested: Nested; onClose: () => void }>) {
  return (
    <SideDrawer title={nested.title} onClose={onClose}>
      <JsonView
        label={nested.title}
        value={nested.value}
        filename={`${nested.title.toLowerCase().replaceAll(" ", "-")}.json`}
      />
    </SideDrawer>
  );
}

/** Records of one schema as a table: the header row is the schema's field names in its order, and
 * each row is one record. A list or object value shows its count and a button that opens it in a drawer. Stored
 * records (with an id) add their delta, version and changed fields, and a row opens by click or by Enter (`onOpen`). Rows are
 * the page shown, at most one API page (100 rows), so there is no more to hold or to virtualize. With `json`, a last
 * column opens each row's JSON. The header row, the first column and the JSON column stay in view as the table
 * scrolls either way (`table-sticky` in theme.css). */
export function RecordTable<R extends TableRecord>({
  label,
  records,
  fieldTypes,
  onOpen,
  json,
}: Readonly<{
  label: string;
  records: readonly R[];
  fieldTypes: FieldTypes;
  onOpen?: (record: R) => void;
  /** A row's JSON, for the JSON column; no such column without it. */
  json?: (record: TableRecord) => unknown;
}>) {
  const stored = records.some((record) => record.id !== undefined);
  const columns = useMemo(
    () => columnsOf(fieldTypes, records, stored, json),
    [fieldTypes, records, stored, json],
  );
  const [nested, setNested] = useState<Nested | null>(null);
  const rows = useMemo(
    () => records.map((record, row) => ({ key: record.id ?? `row-${String(row)}`, record, row })),
    [records],
  );
  return (
    <>
      <Table className={json === undefined ? "table-sticky" : "table-sticky table-sticky-end"}>
        <Table.ScrollContainer>
          <Table.Content
            aria-label={label}
            {...(onOpen === undefined
              ? {}
              : {
                  onRowAction: (key) => {
                    const row = rows.find((candidate) => candidate.key === key);
                    if (row !== undefined) onOpen(row.record);
                  },
                })}
          >
            <Table.Header columns={columns}>
              {(column) => (
                <Table.Column
                  id={column.id}
                  isRowHeader={column === columns[0]}
                  className={columnClass(column, "min-w-40")}
                >
                  {column.name}
                </Table.Column>
              )}
            </Table.Header>
            <Table.Body items={rows}>
              {({ key, record, row }) => (
                <Table.Row id={key} columns={columns}>
                  {(column) => (
                    <Table.Cell className={columnClass(column, "max-w-72 truncate")}>
                      {column.render(record, row, setNested)}
                    </Table.Cell>
                  )}
                </Table.Row>
              )}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
      </Table>
      {nested === null ? null : (
        <NestedDrawer
          nested={nested}
          onClose={() => {
            setNested(null);
          }}
        />
      )}
    </>
  );
}
