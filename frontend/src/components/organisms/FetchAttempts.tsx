import { Table, Typography } from "@heroui/react";
import type { FetchAttempt, FetchSummary } from "../../api/scrape";

/** Every rung tried to fetch a page, in order: its method, the HTTP status, the block class, the
 * challenge label the response presented (`acquire.challenge`), the time it took and the error it ended with. A
 * blocked page's attempts say why it was. */
export function FetchAttemptsTable({ attempts }: Readonly<{ attempts: readonly FetchAttempt[] }>) {
  const rows = attempts.map((attempt, id) => ({ ...attempt, id }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Fetch attempts">
          <Table.Header>
            <Table.Column isRowHeader>Rung</Table.Column>
            <Table.Column>HTTP status</Table.Column>
            <Table.Column>Block class</Table.Column>
            <Table.Column>Challenge</Table.Column>
            <Table.Column>Duration (ms)</Table.Column>
            <Table.Column>Error</Table.Column>
          </Table.Header>
          <Table.Body items={rows}>
            {(attempt) => (
              <Table.Row id={attempt.id}>
                <Table.Cell>{attempt.method}</Table.Cell>
                <Table.Cell>{attempt.status === null ? "—" : String(attempt.status)}</Table.Cell>
                <Table.Cell>{attempt.block_class}</Table.Cell>
                <Table.Cell>{attempt.challenge ?? "—"}</Table.Cell>
                <Table.Cell>{String(attempt.duration_ms)}</Table.Cell>
                <Table.Cell>{attempt.error ?? "—"}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** One line on how the fetch ended: the platform adapter that read the page, the method that served it, or the class
 * it ended as when no attempt reached the page. */
function trailText({ adapter, method, block_class, attempts }: FetchSummary): string {
  if (block_class !== "ok") {
    return `Not reached: ${block_class} after ${String(attempts.length)} attempt${attempts.length === 1 ? "" : "s"}.`;
  }
  return adapter === null
    ? `Fetched by ${method}.`
    : `Read from the ${adapter} platform adapter's API.`;
}

/** How a page was fetched, in a drawer: how it ended, then every attempt. */
export function FetchTrail({ fetch }: Readonly<{ fetch: FetchSummary }>) {
  return (
    <section className="flex flex-col gap-2">
      <Typography.Heading level={3}>Fetch</Typography.Heading>
      <Typography.Paragraph size="sm" color="muted">
        {trailText(fetch)}
      </Typography.Paragraph>
      <FetchAttemptsTable attempts={fetch.attempts} />
    </section>
  );
}
