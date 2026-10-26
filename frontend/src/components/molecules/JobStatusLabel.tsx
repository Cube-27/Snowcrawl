import type { JobStatus, TaskStatus } from "../../api/jobs";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

// One tone per `job.status`: waiting, in progress, or ended; a cancel is the caller's own end.
const JOB_STATUS_TONES: Record<JobStatus, StatusTone> = {
  queued: "neutral",
  running: "accent",
  completed: "success",
  failed: "danger",
  cancelled: "neutral",
};

// One tone per `url_task.status`, as for jobs; `claimed` is a task a worker is running.
const TASK_STATUS_TONES: Record<TaskStatus, StatusTone> = {
  queued: "neutral",
  claimed: "accent",
  done: "success",
  failed: "danger",
  cancelled: "neutral",
};

/** Each `job.status` as people read it: its own name. */
export const JOB_STATUS_TEXT: Readonly<Record<JobStatus, string>> = {
  queued: "queued",
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
};

/** A job's status as its text and icon. */
export function JobStatusLabel({ status }: Readonly<{ status: JobStatus }>) {
  return <StatusLabel text={JOB_STATUS_TEXT[status]} tone={JOB_STATUS_TONES[status]} />;
}

/** A task's status as its text and icon. */
export function TaskStatusLabel({ status }: Readonly<{ status: TaskStatus }>) {
  return <StatusLabel text={status} tone={TASK_STATUS_TONES[status]} />;
}
