import { Button, Drawer, Table } from "@heroui/react";
import { useMemo, useState, type ReactNode } from "react";
import type { FieldTypes, FieldTypeSpec } from "../../api/field";
import type { ExtractedRecord, StoredRecord } from "../../api/records";
import { confidenceText } from "../molecules/ConfidenceCell";
import { FieldValue } from "../molecules/FieldValue";
import { JsonView } from "../molecules/JsonView";

/** The fields whose published value differs from the version the record supersedes, or null when none, as for a
 * first version. */
export function changedFieldsText({ changed_fields: changed }: StoredRecord): string | null {
  return changed.length === 0 ? null : changed.join(", ");
}

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
function fieldColumn(name: string, spec: FieldTypeSpec | null): Column {
  return {
    id: `field:${name}`,
    name,
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
  { id: "confidence", name: "Confidence", render: (record) => confidenceText(record.confidence) },
];

const STORED_COLUMNS: Column[] = [
  // A job's record against its entity's previous record: new, changed or same.
  { id: "delta", name: "Delta", render: (record) => record.delta?.kind ?? NO_VALUE },
  { id: "version", name: "Version", render: (record) => record.version ?? NO_VALUE },
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

function columnsOf(
  fieldTypes: FieldTypes,
  records: readonly TableRecord[],
  stored: boolean,
): Column[] {
  return [
    ...fieldNames(fieldTypes, records).map((name) => fieldColumn(name, fieldTypes[name] ?? null)),
    ...(stored ? STORED_COLUMNS : []),
    ...RECORD_COLUMNS,
  ];
}

function NestedDrawer({ nested, onClose }: Readonly<{ nested: Nested; onClose: () => void }>) {
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement="right">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>{nested.title}</Drawer.Heading>
            </Drawer.Header>
            <Drawer.Body>
              <JsonView label={nested.title} value={nested.value} filename="field.json" />
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}

/** Records of one schema as a table: the header row is the schema's field names in its order, and
 * each row is one record. A list or object value shows its count and a button that opens it in a drawer. Stored
 * records (with an id) add their delta, version and changed fields, and a row opens by click or by Enter (`onOpen`). Rows are
 * the page shown, at most one API page (100 rows), so there is no more to hold or to virtualize. */
export function RecordTable<R extends TableRecord>({
  label,
  records,
  fieldTypes,
  onOpen,
}: Readonly<{
  label: string;
  records: readonly R[];
  fieldTypes: FieldTypes;
  onOpen?: (record: R) => void;
}>) {
  const stored = records.some((record) => record.id !== undefined);
  const columns = useMemo(
    () => columnsOf(fieldTypes, records, stored),
    [fieldTypes, records, stored],
  );
  const [nested, setNested] = useState<Nested | null>(null);
  const rows = useMemo(
    () => records.map((record, row) => ({ key: record.id ?? `row-${String(row)}`, record, row })),
    [records],
  );
  return (
    <>
      <Table>
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
                  className="min-w-40"
                >
                  {column.name}
                </Table.Column>
              )}
            </Table.Header>
            <Table.Body items={rows}>
              {({ key, record, row }) => (
                <Table.Row id={key} columns={columns}>
                  {(column) => (
                    <Table.Cell className="max-w-72 truncate">
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
