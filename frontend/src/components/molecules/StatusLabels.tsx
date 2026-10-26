import type { ConfigStatus, FieldConfigStatus } from "../../api/configs";
import type { FieldStatus } from "../../api/field";
import type { JobStatus, TaskOutcome, TaskStatus } from "../../api/jobs";
import type { RecordField } from "../../api/scrape";
import type { SchemaDef } from "../../api/schemas";
import type { StreamState } from "../../api/events";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

/* Every domain status as a status label: each domain maps each of its values to one tone of `StatusLabel`, once,
 * here, so a status reads the same on every screen. */

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

// One tone per `url_task.outcome`. `partial` and `shell` returned a page without all it should have;
// `blocked`, `timeout` and `failed` returned none. `not_found` is the page's own answer;
// `links_only` (a page read for its links) and `no_match` (a discovered page without its required fields) are
// not failures. `auth_wall` (a 401), `soft_404`, `geo_redirect` and `redirected_to_listing` (where a redirect led
// instead of the page) and `unsupported_media` (an answer that is no HTML page) are the site's answers, as `not_found`
// is. `geo_blocked` (a 451, withheld where the request came from) returned none.
const OUTCOME_TONES: Record<TaskOutcome, StatusTone> = {
  extracted: "success",
  partial: "warning",
  shell: "warning",
  not_found: "neutral",
  links_only: "neutral",
  no_match: "neutral",
  auth_wall: "neutral",
  soft_404: "neutral",
  geo_redirect: "neutral",
  redirected_to_listing: "neutral",
  unsupported_media: "neutral",
  geo_blocked: "danger",
  blocked: "danger",
  timeout: "danger",
  failed: "danger",
};

/** A URL's outcome as its text and icon. */
export function OutcomeLabel({ outcome }: Readonly<{ outcome: TaskOutcome }>) {
  return <StatusLabel text={outcome} tone={OUTCOME_TONES[outcome]} />;
}

// One tone per `extraction_config.status`: only the active version serves; a draft did not pass
// validation; a suspended version was replaced by a newer active one; a pending one waits for approval.
const CONFIG_STATUS_TONES: Record<ConfigStatus, StatusTone> = {
  active: "success",
  draft: "warning",
  suspended: "neutral",
  pending_approval: "accent",
};

// One tone per field status: corroborated by structured values, validated without that, set by an
// operator, suspended when its yield or validity drifted, or absent: no page of the site states it.
const FIELD_STATUS_TONES: Record<FieldConfigStatus, StatusTone> = {
  corroborated: "success",
  uncorroborated: "warning",
  operator: "accent",
  suspended: "danger",
  absent: "neutral",
};

/** A config version's status as its text and icon. */
export function ConfigStatusLabel({ status }: Readonly<{ status: ConfigStatus }>) {
  return <StatusLabel text={status} tone={CONFIG_STATUS_TONES[status]} />;
}

/** A config field's status as its text and icon. */
export function FieldConfigStatusLabel({ status }: Readonly<{ status: FieldConfigStatus }>) {
  return <StatusLabel text={status} tone={FIELD_STATUS_TONES[status]} />;
}

// One tone per `schema_def.status`: a retired schema stays readable but takes no new work.
const SCHEMA_STATUS_TONES: Record<SchemaDef["status"], StatusTone> = {
  active: "success",
  retired: "neutral",
};

/** A schema's status as its text and icon. */
export function SchemaStatusLabel({ status }: Readonly<{ status: SchemaDef["status"] }>) {
  return <StatusLabel text={status} tone={SCHEMA_STATUS_TONES[status]} />;
}

// One tone per field status. A conflict is sources that disagree, so it is the one that needs a look.
const RECORD_FIELD_STATUS_TONES: Record<FieldStatus, StatusTone> = {
  published: "success",
  missing: "neutral",
  interaction_required: "neutral",
  abstained: "warning",
  conflict: "danger",
};

/** A field's status as its text and icon. */
export function FieldStatusLabel({ status }: Readonly<{ status: FieldStatus }>) {
  return <StatusLabel text={status} tone={RECORD_FIELD_STATUS_TONES[status]} />;
}

// One tone per stream state: receiving, waiting to reconnect, closed after the job's end, or refused.
const STATE_TONES: Record<StreamState, StatusTone> = {
  connecting: "neutral",
  live: "accent",
  reconnecting: "warning",
  ended: "success",
  failed: "danger",
};

/** Where a job's event stream is, as a status label. */
export function StreamStateLabel({ state }: Readonly<{ state: StreamState }>) {
  return <StatusLabel text={state} tone={STATE_TONES[state]} />;
}

/** Whether a published field of `records` is uncalibrated: its confidence is a score, not a measured precision.
 * A field that did not publish has no confidence to qualify. */
export function hasUncalibrated(
  records: readonly {
    fields: Readonly<Record<string, Pick<RecordField, "status" | "calibrated">>>;
  }[],
): boolean {
  return records.some((record) =>
    Object.values(record.fields).some((field) => field.status === "published" && !field.calibrated),
  );
}

/** The one "uncalibrated" label of a record, or of the records of a page: shown when a published field is
 * uncalibrated, never once per field. */
export function UncalibratedLabel({
  records,
}: Readonly<{ records: Parameters<typeof hasUncalibrated>[0] }>) {
  return hasUncalibrated(records) ? <StatusLabel text="uncalibrated" tone="warning" /> : null;
}

// A schedule runs on its cadence, or is paused and starts none.
/** Whether a schedule runs, as a status label: paused, or active. */
export function ScheduleStateLabel({ paused }: Readonly<{ paused: boolean }>) {
  return paused ? (
    <StatusLabel text="paused" tone="warning" />
  ) : (
    <StatusLabel text="active" tone="success" />
  );
}
