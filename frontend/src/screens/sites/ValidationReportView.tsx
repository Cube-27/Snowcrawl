import { Alert, Table, Typography } from "@heroui/react";
import type { FieldReport, ValidationReport } from "../../api/configs";
import { confidenceText } from "../../components/molecules/ConfidenceCell";
import { EmptyView } from "../../components/molecules/EmptyView";
import { FieldConfigStatusLabel } from "../../components/molecules/ConfigStatusLabel";
import { StatusLabel } from "../../components/atoms/StatusLabel";
import { ofPages, withNextPage } from "./report";

function FieldOutcome({ report }: Readonly<{ report: FieldReport }>) {
  if (report.status !== null) return <FieldConfigStatusLabel status={report.status} />;
  return <StatusLabel text={`dropped: ${report.dropped ?? "no reason given"}`} tone="danger" />;
}

/** Each field's result on the held-out pages. */
function FieldReportTable({
  label,
  fields,
}: Readonly<{ label: string; fields: Readonly<Record<string, FieldReport>> }>) {
  const rows = Object.entries(fields).map(([name, report]) => ({ id: name, report }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label={label}>
          <Table.Header>
            <Table.Column isRowHeader>Field</Table.Column>
            <Table.Column>Outcome</Table.Column>
            <Table.Column>Matched</Table.Column>
            <Table.Column>Valid</Table.Column>
            <Table.Column>Reproduced</Table.Column>
            <Table.Column>Corroborated</Table.Column>
            <Table.Column>Yield</Table.Column>
            <Table.Column>Validity</Table.Column>
          </Table.Header>
          <Table.Body items={rows}>
            {({ id, report }) => (
              <Table.Row id={id}>
                <Table.Cell>{id}</Table.Cell>
                <Table.Cell>
                  <FieldOutcome report={report} />
                </Table.Cell>
                <Table.Cell>{ofPages(report.matched, report.pages)}</Table.Cell>
                <Table.Cell>{ofPages(report.valid, report.pages)}</Table.Cell>
                <Table.Cell>{ofPages(report.reproduced, report.published)}</Table.Cell>
                <Table.Cell>{ofPages(report.corroborated_on, report.comparable)}</Table.Cell>
                <Table.Cell>{confidenceText(report.yield)}</Table.Cell>
                <Table.Cell>{confidenceText(report.validity)}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

function ListingSection({
  listing,
  heldOut,
}: Readonly<{ listing: NonNullable<ValidationReport["listing"]>; heldOut: number }>) {
  const fields = withNextPage(listing.fields, listing.next);
  return (
    <section className="flex flex-col gap-3">
      <Typography.Heading level={3}>Listing rows</Typography.Heading>
      <Typography.Paragraph>
        Rows selected on {ofPages(listing.rows_ok, heldOut)} held-out pages.
        {listing.dropped == null ? null : ` Dropped: ${listing.dropped}.`}
      </Typography.Paragraph>
      <FieldReportTable label="Row fields" fields={fields} />
    </section>
  );
}

function RecipeSection({
  recipe,
}: Readonly<{ recipe: NonNullable<ValidationReport["api_recipe"]> }>) {
  const lines = Object.entries(recipe.fields).map(
    ([name, field]) => `${name}: reproduced ${ofPages(field.reproduced, field.published)}`,
  );
  return (
    <section className="flex flex-col gap-3">
      <Typography.Heading level={3}>API recipe</Typography.Heading>
      {recipe.refused == null ? null : (
        <Typography.Paragraph>Refused: {recipe.refused}</Typography.Paragraph>
      )}
      {lines.map((line) => (
        <Typography.Paragraph key={line}>{line}</Typography.Paragraph>
      ))}
    </section>
  );
}

/** A config's validation on the site's held-out pages, as the server reported it: why nothing could
 * activate, each field's outcome and counts, the listing's rows and the API recipe. */
export function ValidationReportView({ report }: Readonly<{ report: ValidationReport | null }>) {
  if (report === null) {
    return <EmptyView title="Not validated">This version has no validation report.</EmptyView>;
  }
  const heldOut = report.held_out.length;
  return (
    <div className="flex flex-col gap-4">
      {report.refused == null ? null : (
        <Alert status="warning">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Validation refused the config</Alert.Title>
            <Alert.Description>{report.refused}</Alert.Description>
          </Alert.Content>
        </Alert>
      )}
      <Typography.Paragraph>Validated on {heldOut} held-out pages.</Typography.Paragraph>
      <FieldReportTable label="Field validation" fields={report.fields} />
      {report.listing == null ? null : (
        <ListingSection listing={report.listing} heldOut={heldOut} />
      )}
      {report.api_recipe == null ? null : <RecipeSection recipe={report.api_recipe} />}
    </div>
  );
}
