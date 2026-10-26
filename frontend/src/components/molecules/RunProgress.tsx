import { ProgressBar, Typography } from "@heroui/react";
import type { JobSummary, TaskCounts } from "../../api/jobs";
import { plural } from "../lib/activityText";
import { DurationText } from "../atoms/Duration";
import { endedTasks, OutcomeSummary, taskTotal } from "./OutcomeCounts";

/** "N of M pages done", where done is fetched to an end (done or failed), and then how many were cancelled: a cancelled
 * page ended without being fetched, so it is not counted as done. */
function progressText(counts: TaskCounts): string {
  const done = `${String(counts.done + counts.failed)} of ${plural(taskTotal(counts), "page")} done`;
  return counts.cancelled === 0 ? done : `${done}, ${String(counts.cancelled)} cancelled`;
}

/** A run's progress: a bar of the pages done, "N of M pages done" and the elapsed time under
 * it, and the outcomes reached so far on the right. */
export function RunProgress({ job }: Readonly<{ job: JobSummary }>) {
  const ended = endedTasks(job.counts);
  const total = taskTotal(job.counts);
  return (
    <div className="flex flex-col gap-3">
      <ProgressBar aria-label="Pages done" size="sm" value={ended} maxValue={Math.max(total, 1)}>
        <ProgressBar.Track>
          <ProgressBar.Fill />
        </ProgressBar.Track>
      </ProgressBar>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-x-3">
          <Typography.Paragraph size="sm" weight="medium">
            {progressText(job.counts)}
          </Typography.Paragraph>
          <Typography.Paragraph size="sm" color="muted">
            Elapsed <DurationText startedAt={job.started_at} finishedAt={job.finished_at} />
          </Typography.Paragraph>
        </div>
        <OutcomeSummary outcomes={job.outcomes} />
      </div>
    </div>
  );
}
