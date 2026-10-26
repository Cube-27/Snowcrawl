import { Table } from "@heroui/react";
import { ChevronRight } from "@gravity-ui/icons";
import type { ReactNode } from "react";
import type { JobSummary } from "../../api/jobs";
import { plural } from "../../components/lib/activityText";
import { DurationText } from "../../components/atoms/Duration";
import { JobStatusLabel } from "../../components/molecules/StatusLabels";
import { TaskCountsText } from "../../components/molecules/OutcomeCounts";
import { Timestamp } from "../../components/atoms/Timestamp";
import { runPath } from "../playground/modes";
import { KIND_LABELS } from "./kinds";
import { jobPath } from "./paths";

/** What a run was started on: its start URL, or how many URLs it had. */
function target({ start_url, url_count }: JobSummary): string {
  if (start_url !== null) return start_url;
  return plural(url_count ?? 0, "URL");
}

/** The last column's cue that a row opens its run: the whole row is the link (React Aria row `href`), so this is text,
 * not a second control. */
function OpenCue() {
  return (
    <span className="flex items-center justify-end gap-1 text-accent">
      Open
      <ChevronRight aria-hidden="true" />
    </span>
  );
}

/** A column a jobs table can show: its header and its cell. */
const COLUMNS = {
  kind: { header: "Kind", cell: (job: JobSummary): ReactNode => KIND_LABELS[job.kind] },
  target: { header: "Target", cell: target },
  status: { header: "Status", cell: (job: JobSummary) => <JobStatusLabel status={job.status} /> },
  tasks: { header: "Tasks", cell: (job: JobSummary) => <TaskCountsText counts={job.counts} /> },
  started: { header: "Started", cell: (job: JobSummary) => <Timestamp value={job.started_at} /> },
  created: { header: "Created", cell: (job: JobSummary) => <Timestamp value={job.created_at} /> },
  duration: {
    header: "Duration",
    cell: (job: JobSummary) => (
      <DurationText startedAt={job.started_at} finishedAt={job.finished_at} />
    ),
  },
  // Named for screen readers only: the cue says it in each row.
  open: { header: <span className="sr-only">Open</span>, cell: () => <OpenCue /> },
} satisfies Record<string, { header: ReactNode; cell: (job: JobSummary) => ReactNode }>;

export type JobColumn = keyof typeof COLUMNS;

/** Where a job's row goes: its run in its playground mode, or the job page for a kind no mode runs. */
function rowPath(job: JobSummary): string {
  return runPath(job.kind, job.id) ?? jobPath(job.id);
}

/** Jobs as table rows, newest first, in the `columns` given (each with its fixed width class, so long values truncate
 * rather than move the columns); a row opens its run. The first column is the row header. Recent runs and the Jobs
 * screen both list jobs with it. */
export function JobRows({
  label,
  jobs,
  columns,
}: Readonly<{
  label: string;
  jobs: readonly JobSummary[];
  columns: readonly (readonly [JobColumn, string])[];
}>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label={label} className="table-fixed">
          <Table.Header>
            {columns.map(([id, width], i) => (
              <Table.Column key={id} id={id} isRowHeader={i === 0} className={width}>
                {COLUMNS[id].header}
              </Table.Column>
            ))}
          </Table.Header>
          <Table.Body items={jobs}>
            {(job) => (
              <Table.Row id={job.id} href={rowPath(job)}>
                {columns.map(([id]) => (
                  <Table.Cell key={id} className="truncate">
                    {COLUMNS[id].cell(job)}
                  </Table.Cell>
                ))}
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}
