import { Table, Typography } from "@heroui/react";

/** A job's usage (`job.usage`) by the names the server gives it. */
export function UsageSummary({ usage }: Readonly<{ usage: Readonly<Record<string, number>> }>) {
  const rows = Object.entries(usage).map(([kind, amount]) => ({ id: kind, amount }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Usage">
          <Table.Header>
            <Table.Column isRowHeader>Usage</Table.Column>
            <Table.Column>Amount</Table.Column>
          </Table.Header>
          <Table.Body
            items={rows}
            renderEmptyState={() => (
              <Typography.Paragraph color="muted">No usage recorded.</Typography.Paragraph>
            )}
          >
            {({ id, amount }) => (
              <Table.Row id={id}>
                <Table.Cell>{id}</Table.Cell>
                <Table.Cell>{amount}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}
