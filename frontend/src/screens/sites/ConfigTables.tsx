import { Table, Typography } from "@heroui/react";
import { useSchemas } from "../../api/hooks/schemas";
import type { ConfigDetail, ConfigSummary, FieldConfigStatus } from "../../api/configs";
import { confidenceText } from "../../components/molecules/ConfidenceCell";
import { ConfigStatusLabel, FieldConfigStatusLabel } from "../../components/molecules/StatusLabels";
import { keyedTexts } from "../../components/lib/keys";
import { LocatorText } from "../../components/atoms/LocatorText";
import { stepText } from "../../components/lib/steps";
import { configuredFields } from "./correction";
import { configPath } from "./paths";

const FIELD_STATUSES: readonly FieldConfigStatus[] = [
  "corroborated",
  "uncorroborated",
  "operator",
  "suspended",
  "absent",
];

/** Names a config's schema by the schemas the API lists; "—" until they are read. */
export function useSchemaLabel(): (config: ConfigSummary) => string {
  const schemas = useSchemas();
  const labels = new Map((schemas.data ?? []).map((schema) => [schema.id, schema.name]));
  return (config) => labels.get(config.schema_id) ?? "—";
}

/** How many of a config's fields have each status, in one line: "2 corroborated, 1 operator". */
function fieldStatusCounts(fieldStatus: ConfigSummary["field_status"]): string {
  const statuses = Object.values(fieldStatus);
  const counts = FIELD_STATUSES.map(
    (status) => [status, statuses.filter((value) => value === status).length] as const,
  ).filter(([, count]) => count > 0);
  return counts.length === 0
    ? "No fields"
    : counts.map(([status, count]) => `${String(count)} ${status}`).join(", ");
}

/** Config versions, one row each; a row opens that version. `schemaLabel` names a schema by its id. */
export function ConfigVersionsTable({
  label,
  configs,
  schemaLabel,
}: Readonly<{
  label: string;
  configs: readonly ConfigSummary[];
  schemaLabel: (config: ConfigSummary) => string;
}>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label={label}>
          <Table.Header>
            <Table.Column isRowHeader>Schema</Table.Column>
            <Table.Column>Page kind</Table.Column>
            <Table.Column className="numeric">Version</Table.Column>
            <Table.Column>Status</Table.Column>
            <Table.Column>Origin</Table.Column>
            <Table.Column>Fields</Table.Column>
          </Table.Header>
          <Table.Body items={configs}>
            {(config) => (
              <Table.Row id={config.id} href={configPath(config.id)}>
                <Table.Cell>{schemaLabel(config)}</Table.Cell>
                <Table.Cell>{config.page_kind}</Table.Cell>
                <Table.Cell className="numeric">{config.version}</Table.Cell>
                <Table.Cell>
                  <ConfigStatusLabel status={config.status} />
                </Table.Cell>
                <Table.Cell>{config.origin}</Table.Cell>
                <Table.Cell>{fieldStatusCounts(config.field_status)}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** A config's fields: each one's steps in order, its status, and its held-out yield and validity. */
export function ConfigFieldsTable({ config }: Readonly<{ config: ConfigDetail }>) {
  const rows = Object.entries(configuredFields(config)).map(([name, steps]) => ({
    id: name,
    steps,
  }));
  const { listing } = config.ir;
  const { absent } = config.ir;
  return (
    <div className="flex flex-col gap-3">
      <Typography.Paragraph>Fetch method: {config.ir.fetch.method}</Typography.Paragraph>
      {listing == null ? null : (
        <Typography.Paragraph>
          Rows: <LocatorText locator={stepText(listing.row)} />
        </Typography.Paragraph>
      )}
      {absent.length === 0 ? null : (
        <Typography.Paragraph>
          Absent (no page of the site states them, so the LLM is not asked): {absent.join(", ")}
        </Typography.Paragraph>
      )}
      <Table>
        <Table.ScrollContainer>
          <Table.Content aria-label="Config fields">
            <Table.Header>
              <Table.Column isRowHeader>Field</Table.Column>
              <Table.Column>Status</Table.Column>
              <Table.Column>Steps</Table.Column>
              <Table.Column className="numeric">Yield</Table.Column>
              <Table.Column className="numeric">Validity</Table.Column>
            </Table.Header>
            <Table.Body items={rows}>
              {({ id, steps }) => (
                <Table.Row id={id}>
                  <Table.Cell>{id}</Table.Cell>
                  <Table.Cell>
                    <FieldConfigStatusLabel status={steps.status} />
                  </Table.Cell>
                  <Table.Cell>
                    <ol className="flex flex-col gap-1">
                      {/* In order: the first valid value wins. */}
                      {keyedTexts(steps.steps.map((step) => stepText(step))).map(
                        ({ key, text }) => (
                          <li key={key}>
                            <LocatorText locator={text} />
                          </li>
                        ),
                      )}
                    </ol>
                  </Table.Cell>
                  <Table.Cell className="numeric">{confidenceText(steps.yield)}</Table.Cell>
                  <Table.Cell className="numeric">{confidenceText(steps.validity)}</Table.Cell>
                </Table.Row>
              )}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
      </Table>
    </div>
  );
}
