import { Table, Typography } from "@heroui/react";
import { useId } from "react";
import { useJobEvents } from "../../api/hooks/events";
import { ProblemAlert } from "../molecules/ProblemAlert";
import { StreamStateLabel } from "../molecules/StreamStateLabel";
import { TruncatedText } from "../atoms/TruncatedText";

/** A job's run events as they are stored (`GET /v1/jobs/{id}/events`), live until the job ends. Each event's data is
 * shown as the server sent it, cut to its column and in full on hover or focus: a `task_failed` event carries why the
 * task failed. A dropped stream, or one silent past two heartbeats, reconnects after the last event
 * received. */
export function EventLog({ jobId }: Readonly<{ jobId: string }>) {
  const { events, state, problem } = useJobEvents(jobId);
  const headingId = useId();
  const rows = events.map((event, index) => ({ key: String(index), ...event }));
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <Typography.Heading level={2} id={headingId}>
          Events
        </Typography.Heading>
        <StreamStateLabel state={state} />
      </div>
      {problem === null ? null : <ProblemAlert problem={problem} />}
      <Table>
        <Table.ScrollContainer>
          {/* Fixed column widths: a long event truncates rather than moving the columns. */}
          <Table.Content aria-label="Events" className="table-fixed">
            <Table.Header>
              <Table.Column isRowHeader className="w-1/6">
                Event
              </Table.Column>
              <Table.Column className="w-1/3">ID</Table.Column>
              <Table.Column className="w-1/2">Data</Table.Column>
            </Table.Header>
            <Table.Body
              items={rows}
              renderEmptyState={() => (
                <Typography.Paragraph color="muted">No event yet.</Typography.Paragraph>
              )}
            >
              {(event) => (
                <Table.Row id={event.key}>
                  <Table.Cell className="truncate">{event.kind}</Table.Cell>
                  <Table.Cell className="truncate">{event.id ?? "—"}</Table.Cell>
                  <Table.Cell>
                    <TruncatedText text={event.data} />
                  </Table.Cell>
                </Table.Row>
              )}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
      </Table>
    </section>
  );
}
