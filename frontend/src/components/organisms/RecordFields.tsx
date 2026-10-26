import { Table, Typography } from "@heroui/react";
import type { FieldTypes } from "../../api/field";
import type { ExtractedRecord } from "../../api/records";
import type { RecordField } from "../../api/scrape";
import { ConfidenceCell, confidenceText } from "../molecules/ConfidenceCell";
import { EvidenceText, SourceKindText } from "../molecules/FieldOrigin";
import { FieldStatusLabel, UncalibratedLabel } from "../molecules/StatusLabels";
import { FieldValue } from "../molecules/FieldValue";
import { LocatorText } from "../atoms/LocatorText";
import { StatusLabel } from "../atoms/StatusLabel";

/** Whether a record is complete, with its mode and confidence, and its one uncalibrated badge. */
export function RecordSummary({ record }: Readonly<{ record: ExtractedRecord }>) {
  return (
    <>
      <StatusLabel
        text={record.complete ? "complete" : "incomplete"}
        tone={record.complete ? "success" : "warning"}
      />
      <UncalibratedLabel records={[record]} />
      <Typography.Paragraph size="sm" color="muted">
        Mode {record.mode} · confidence {confidenceText(record.confidence)}
      </Typography.Paragraph>
    </>
  );
}

/** Fields by name, one row each, with every `field_meta` attribute the API returns: a record's, or a
 * listing page's own (the answer's `page` member). */
export function FieldsTable({
  label,
  fields: named,
  fieldTypes,
}: Readonly<{
  label: string;
  fields: Readonly<Record<string, RecordField>>;
  fieldTypes: FieldTypes;
}>) {
  const fields: (RecordField & { name: string })[] = Object.entries(named).map(([name, field]) => ({
    ...field,
    name,
  }));
  return (
    <Table>
      <Table.ScrollContainer>
        {/* Fixed column widths: a long value or locator truncates rather than pushing Evidence off the table. */}
        <Table.Content aria-label={label} className="table-fixed">
          <Table.Header>
            <Table.Column isRowHeader className="w-1/8">
              Field
            </Table.Column>
            <Table.Column className="w-1/5">Value</Table.Column>
            <Table.Column className="w-1/8">Status</Table.Column>
            <Table.Column className="w-1/8">Confidence</Table.Column>
            <Table.Column className="w-1/8">Evidence</Table.Column>
            <Table.Column className="w-1/8">Source</Table.Column>
            <Table.Column className="w-1/12">Locator</Table.Column>
            <Table.Column className="w-1/12">Derived by</Table.Column>
          </Table.Header>
          <Table.Body items={fields}>
            {(field) => (
              <Table.Row id={field.name}>
                <Table.Cell className="truncate">{field.name}</Table.Cell>
                <Table.Cell className="truncate">
                  {/* The status has its own column. */}
                  <FieldValue
                    field={field}
                    spec={fieldTypes[field.name] ?? null}
                    withStatus={false}
                  />
                </Table.Cell>
                <Table.Cell>
                  <FieldStatusLabel status={field.status} />
                </Table.Cell>
                <Table.Cell>
                  <ConfidenceCell confidence={field.confidence} />
                </Table.Cell>
                <Table.Cell className="truncate">
                  <EvidenceText evidence={field.evidence} />
                </Table.Cell>
                <Table.Cell className="truncate">
                  <SourceKindText kind={field.source_kind} />
                </Table.Cell>
                <Table.Cell className="truncate">
                  <LocatorText locator={field.locator} />
                </Table.Cell>
                <Table.Cell className="truncate">{field.derived_by ?? "—"}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** One record's fields (`FieldsTable`). */
export function RecordFieldsTable({
  label,
  record,
  fieldTypes,
}: Readonly<{ label: string; record: ExtractedRecord; fieldTypes: FieldTypes }>) {
  return <FieldsTable label={label} fields={record.fields} fieldTypes={fieldTypes} />;
}
