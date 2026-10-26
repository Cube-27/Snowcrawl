import { Table } from "@heroui/react";
import type { DayUsage, JobUsage, LlmUsage, ModelRoleUsage } from "../../api/usage";
import { Timestamp } from "../../components/atoms/Timestamp";
import { KIND_LABELS } from "../jobs/kinds";
import { jobPath } from "../jobs/paths";
import { formatCost, formatCount, roleLabel } from "./costs";

/** The usage columns every cost table ends with. */
function UsageColumns() {
  return (
    <>
      <Table.Column>Calls</Table.Column>
      <Table.Column>Input tokens</Table.Column>
      <Table.Column>Output tokens</Table.Column>
      <Table.Column>Estimated cost</Table.Column>
    </>
  );
}

function UsageCells({ usage }: Readonly<{ usage: LlmUsage }>) {
  return (
    <>
      <Table.Cell>{formatCount(usage.calls)}</Table.Cell>
      <Table.Cell>{formatCount(usage.tokens_in)}</Table.Cell>
      <Table.Cell>{formatCount(usage.tokens_out)}</Table.Cell>
      <Table.Cell>{formatCost(usage)}</Table.Cell>
    </>
  );
}

export function TotalTable({ total }: Readonly<{ total: LlmUsage }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Total">
          <Table.Header>
            <Table.Column isRowHeader>Range</Table.Column>
            <UsageColumns />
          </Table.Header>
          <Table.Body>
            <Table.Row id="total">
              <Table.Cell>Total</Table.Cell>
              <UsageCells usage={total} />
            </Table.Row>
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The days of the range, newest first. */
export function DayTable({ days }: Readonly<{ days: readonly DayUsage[] }>) {
  const newestFirst = [...days].reverse().map((day) => ({ ...day, id: day.day }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Per day">
          <Table.Header>
            <Table.Column isRowHeader>Day (UTC)</Table.Column>
            <UsageColumns />
          </Table.Header>
          <Table.Body items={newestFirst}>
            {(day) => (
              <Table.Row id={day.id}>
                <Table.Cell>{day.day}</Table.Cell>
                <UsageCells usage={day} />
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** Per provider, model and role, in the API's order: unknown costs first, then the costliest. */
export function BreakdownTable({ rows }: Readonly<{ rows: readonly ModelRoleUsage[] }>) {
  const items = rows.map((row) => ({
    ...row,
    id: [row.provider, row.model, row.prompt_kind].join(" "),
  }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="By model and role">
          <Table.Header>
            <Table.Column isRowHeader>Model</Table.Column>
            <Table.Column>Provider</Table.Column>
            <Table.Column>Role</Table.Column>
            <UsageColumns />
          </Table.Header>
          <Table.Body items={items}>
            {(row) => (
              <Table.Row id={row.id}>
                <Table.Cell>{row.model ?? "—"}</Table.Cell>
                <Table.Cell>{row.provider ?? "—"}</Table.Cell>
                <Table.Cell>{roleLabel(row.prompt_kind)}</Table.Cell>
                <UsageCells usage={row} />
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The costliest jobs of the range; a row opens its job. */
export function TopJobsTable({ jobs }: Readonly<{ jobs: readonly JobUsage[] }>) {
  const items = jobs.map((job) => ({ ...job, id: job.job_id }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Top jobs by cost">
          <Table.Header>
            <Table.Column isRowHeader>Job</Table.Column>
            <Table.Column>Created</Table.Column>
            <UsageColumns />
          </Table.Header>
          <Table.Body items={items}>
            {(job) => (
              <Table.Row id={job.id} href={jobPath(job.job_id)}>
                <Table.Cell>{KIND_LABELS[job.kind]}</Table.Cell>
                <Table.Cell>
                  <Timestamp value={job.created_at} />
                </Table.Cell>
                <UsageCells usage={job} />
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}
