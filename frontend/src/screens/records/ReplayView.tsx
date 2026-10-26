import { Table, Typography } from "@heroui/react";
import type { FieldTypes, FieldTypeSpec } from "../../api/field";
import type { ExtractedRecord, ReplayResult } from "../../api/records";
import type { RecordField } from "../../api/scrape";
import { EmptyView } from "../../components/molecules/EmptyView";
import { FieldValue } from "../../components/molecules/FieldValue";
import { ExtractedRecords } from "../../components/organisms/ExtractedRecords";
import { StatusLabel } from "../../components/atoms/StatusLabel";
import { diffFields } from "./diff";

function SideValue({
  field,
  spec,
}: Readonly<{ field: RecordField | null; spec: FieldTypeSpec | null }>) {
  return field === null ? "not in this record" : <FieldValue field={field} spec={spec} />;
}

/** The stored record beside its replay, one row per field, each marked changed or same. */
function ReplayDiff({
  stored,
  replayed,
  fieldTypes,
}: Readonly<{ stored: ExtractedRecord; replayed: ExtractedRecord; fieldTypes: FieldTypes }>) {
  const changes = diffFields(stored.fields, replayed.fields);
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Replay differences">
          <Table.Header>
            <Table.Column isRowHeader>Field</Table.Column>
            <Table.Column>Stored</Table.Column>
            <Table.Column>Replay</Table.Column>
            <Table.Column>Change</Table.Column>
          </Table.Header>
          <Table.Body items={changes}>
            {(change) => {
              const spec = fieldTypes[change.name] ?? null;
              return (
                <Table.Row id={change.name}>
                  <Table.Cell>{change.name}</Table.Cell>
                  <Table.Cell>
                    <SideValue field={change.stored} spec={spec} />
                  </Table.Cell>
                  <Table.Cell>
                    <SideValue field={change.replayed} spec={spec} />
                  </Table.Cell>
                  <Table.Cell>
                    {/* One tone per change: a changed field is the one to look at. */}
                    <StatusLabel
                      text={change.changed ? "changed" : "same"}
                      tone={change.changed ? "warning" : "neutral"}
                    />
                  </Table.Cell>
                </Table.Row>
              );
            }}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** What a replay of the record's capture extracted, against the stored record. A capture can hold several records (a
 * listing page) and a replayed record has no id of the stored one, so the diff is shown only when the replay made one
 * record; otherwise its records are listed as they are. */
export function ReplayView({
  stored,
  result,
  fieldTypes,
}: Readonly<{ stored: ExtractedRecord; result: ReplayResult; fieldTypes: FieldTypes }>) {
  const [only, ...others] = result.records;
  if (only === undefined) {
    return (
      <EmptyView title="No records">The replay extracted no record from the capture.</EmptyView>
    );
  }
  if (others.length === 0) {
    return <ReplayDiff stored={stored} replayed={only} fieldTypes={fieldTypes} />;
  }
  return (
    <div className="flex flex-col gap-4">
      <Typography.Paragraph size="sm" color="muted">
        The replay extracted {result.records.length} records from the capture, so it is not compared
        with this one.
      </Typography.Paragraph>
      <ExtractedRecords records={result.records} fieldTypes={fieldTypes} />
    </div>
  );
}
