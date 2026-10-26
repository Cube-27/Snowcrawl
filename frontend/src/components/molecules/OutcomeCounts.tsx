import { Typography } from "@heroui/react";
import type { OutcomeCounts as Outcomes, TaskCounts, TaskOutcome, TaskStatus } from "../../api/jobs";
import { plural } from "../lib/activityText";
import { OutcomeLabel } from "./OutcomeLabel";

// In the order a task goes through them, and the outcomes as the API lists them.
const STATUSES: readonly TaskStatus[] = ["queued", "claimed", "done", "failed", "cancelled"];
const OUTCOMES: readonly TaskOutcome[] = [
  "extracted",
  "partial",
  "links_only",
  "blocked",
  "shell",
  "not_found",
  "no_match",
  "auth_wall",
  "soft_404",
  "geo_redirect",
  "redirected_to_listing",
  "unsupported_media",
  "geo_blocked",
  "timeout",
  "failed",
];

/** How many tasks the job has, in every status. */
export function taskTotal(counts: TaskCounts): number {
  return STATUSES.reduce((sum, status) => sum + counts[status], 0);
}

/** How many of the job's tasks have ended: done, failed or cancelled. */
export function endedTasks(counts: TaskCounts): number {
  return counts.done + counts.failed + counts.cancelled;
}

/** A job's tasks in one line: their total, then each status some task has, as "3 tasks: 1 queued, 2 done". */
export function TaskCountsText({ counts }: Readonly<{ counts: TaskCounts }>) {
  const parts = STATUSES.filter((status) => counts[status] > 0).map(
    (status) => `${String(counts[status])} ${status}`,
  );
  const tasks = plural(taskTotal(counts), "task");
  return <>{parts.length === 0 ? tasks : `${tasks}: ${parts.join(", ")}`}</>;
}

/** A job's outcomes on one line, each outcome some task reached with its count (the run header's summary), or
 * a muted line while none has. */
export function OutcomeSummary({ outcomes }: Readonly<{ outcomes: Outcomes }>) {
  const reached = OUTCOMES.filter((outcome) => outcomes[outcome] > 0);
  if (reached.length === 0) {
    return (
      <Typography.Paragraph size="sm" color="muted">
        No outcome yet
      </Typography.Paragraph>
    );
  }
  return (
    <ul aria-label="Outcomes" className="flex flex-wrap items-center gap-x-4 gap-y-2">
      {reached.map((outcome) => (
        <li key={outcome} className="flex items-center gap-1.5">
          <OutcomeLabel outcome={outcome} />
          <Typography.Paragraph size="sm" weight="medium">
            {outcomes[outcome]}
          </Typography.Paragraph>
        </li>
      ))}
    </ul>
  );
}
