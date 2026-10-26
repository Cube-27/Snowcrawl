import { Table } from "@heroui/react";
import { useWebhookDeliveries } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import type { WebhookDelivery } from "../../api/jobs";
import { Collapsible } from "../molecules/Collapsible";
import { useCursorPages } from "../lib/cursors";
import { EmptyView } from "../molecules/EmptyView";
import { PAGE_SIZE, PagedTable } from "./PagedTable";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";
import { Timestamp } from "../atoms/Timestamp";
import { TruncatedText } from "../atoms/TruncatedText";

// One tone per `webhook_delivery.status`: still owed, accepted by the receiver, or given up on.
const DELIVERY_TONES: Record<WebhookDelivery["status"], StatusTone> = {
  pending: "warning",
  delivered: "success",
  failed: "danger",
};

function DeliveryRows({ deliveries }: Readonly<{ deliveries: readonly WebhookDelivery[] }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        {/* Fixed column widths: a long error truncates rather than pushing the times off the table. */}
        <Table.Content aria-label="Webhook deliveries" className="table-fixed">
          <Table.Header>
            <Table.Column isRowHeader className="w-1/6">
              Status
            </Table.Column>
            <Table.Column className="w-1/12">Attempts</Table.Column>
            <Table.Column className="w-1/8">Response</Table.Column>
            <Table.Column className="w-1/12">Events</Table.Column>
            <Table.Column className="w-1/4">Last error</Table.Column>
            <Table.Column className="w-1/6">Created</Table.Column>
            <Table.Column className="w-1/6">Next attempt</Table.Column>
          </Table.Header>
          <Table.Body items={deliveries}>
            {(delivery) => (
              <Table.Row id={delivery.id}>
                <Table.Cell>
                  <StatusLabel text={delivery.status} tone={DELIVERY_TONES[delivery.status]} />
                </Table.Cell>
                <Table.Cell>{delivery.attempts}</Table.Cell>
                <Table.Cell>{delivery.response_code ?? "—"}</Table.Cell>
                <Table.Cell>{delivery.event_count}</Table.Cell>
                <Table.Cell>
                  {delivery.last_error === null ? (
                    "—"
                  ) : (
                    <TruncatedText text={delivery.last_error} />
                  )}
                </Table.Cell>
                <Table.Cell>
                  <Timestamp value={delivery.created_at} />
                </Table.Cell>
                <Table.Cell>
                  <Timestamp value={delivery.next_attempt_at} />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

function DeliveryPages({ jobId }: Readonly<{ jobId: string }>) {
  const { cursor, page, previous, nextTo } = useCursorPages();
  const deliveries = useWebhookDeliveries(jobId, { cursor, limit: PAGE_SIZE });
  return (
    <PagedTable
      problem={problemOf(deliveries.error)}
      loading={deliveries.isSuccess ? null : "Loading webhook deliveries"}
      page={page}
      onPrevious={previous}
      onNext={deliveries.isSuccess ? nextTo(deliveries.data.next_cursor) : null}
    >
      {deliveries.isSuccess && deliveries.data.items.length === 0 && previous === null ? (
        <EmptyView title="No deliveries yet">
          The job's webhook events are grouped into deliveries and sent a few seconds after they
          happen.
        </EmptyView>
      ) : null}
      {deliveries.isSuccess && deliveries.data.items.length > 0 ? (
        <DeliveryRows deliveries={deliveries.data.items} />
      ) : null}
    </PagedTable>
  );
}

/** What the job's webhook has been sent (`GET /v1/jobs/{id}/webhook-deliveries`): each delivery's status, attempts,
 * the receiver's HTTP status and the last error, newest first, collapsed. Shown only for a job that has a webhook
 * (`webhook_url`), so a receiver can be debugged from the job's page. */
export function WebhookDeliveries({
  jobId,
  webhookUrl,
}: Readonly<{ jobId: string; webhookUrl: string | null }>) {
  if (webhookUrl === null) return null;
  return (
    <Collapsible title="Deliveries">
      <DeliveryPages jobId={jobId} />
    </Collapsible>
  );
}
