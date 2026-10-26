import { Typography } from "@heroui/react";
import type { ReactNode } from "react";
import { useNavigate, useParams } from "react-router";
import { problemOf } from "../../api/hooks/query";
import { useSchedule } from "../../api/hooks/schedules";
import type { Schedule } from "../../api/schedules";
import { JsonView } from "../../components/molecules/JsonView";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { Timestamp } from "../../components/atoms/Timestamp";
import { JobPages } from "../jobs/JobTable";
import { KIND_LABELS } from "../jobs/kinds";
import { cadenceText } from "./cadence";
import { LastRun, ScheduleState } from "./SchedulesScreen";
import { ScheduleActions } from "./ScheduleActions";

function Facts({ schedule }: Readonly<{ schedule: Schedule }>) {
  const facts: [string, ReactNode][] = [
    ["Kind", KIND_LABELS[schedule.kind]],
    ["Runs", cadenceText(schedule)],
    [
      "Next run",
      schedule.paused ? "Paused" : <Timestamp key="next" value={schedule.next_run_at} />,
    ],
    ["Last run", <LastRun key="last" schedule={schedule} />],
    ["Created", <Timestamp key="created" value={schedule.created_at} />],
  ];
  return (
    <dl className="flex flex-col gap-2">
      {facts.map(([term, value]) => (
        <div key={term} className="flex flex-wrap gap-x-6">
          <dt className="w-28 shrink-0 text-muted">{term}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A schedule: its cadence and last run, its actions, the request each run submits, and its jobs, newest
 * first (`GET /v1/jobs?schedule_id=`). */
export function ScheduleScreen() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const query = useSchedule(id);
  const problem = problemOf(query.error);
  const schedule = query.data;
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader
        title={schedule?.name ?? "Schedule"}
        actions={
          schedule === undefined ? undefined : (
            <ScheduleActions schedule={schedule} onDeleted={() => void navigate("/schedules")} />
          )
        }
      >
        {schedule === undefined ? null : <ScheduleState paused={schedule.paused} />}
      </PageHeader>
      {query.isPending ? <LoadingView label="Loading the schedule" /> : null}
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {schedule === undefined ? null : (
        <>
          <Facts schedule={schedule} />
          <Typography.Heading level={2}>Request</Typography.Heading>
          <JsonView
            label="Request JSON"
            value={schedule.request}
            filename={`schedule-${schedule.id}.json`}
          />
          <Typography.Heading level={2}>Runs</Typography.Heading>
          <JobPages filters={{ kind: null, status: null, scheduleId: schedule.id }} />
        </>
      )}
    </section>
  );
}
