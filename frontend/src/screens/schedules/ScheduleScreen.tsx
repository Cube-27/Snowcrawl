import type { ReactNode } from "react";
import { useNavigate, useParams } from "react-router";
import { useSchedule } from "../../api/hooks/schedules";
import type { Schedule } from "../../api/schedules";
import { JsonView } from "../../components/molecules/JsonView";
import { QueryView } from "../../components/molecules/ViewStates";
import { Screen, Section } from "../../components/templates/Screen";
import { ScheduleStateLabel } from "../../components/molecules/StatusLabels";
import { Timestamp } from "../../components/atoms/Timestamp";
import { JobPages } from "../jobs/JobTable";
import { KIND_LABELS } from "../jobs/kinds";
import { cadenceText } from "./cadence";
import { LastRun } from "./SchedulesScreen";
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
  const schedule = query.data;
  return (
    <Screen
      title={schedule?.name ?? "Schedule"}
      actions={
        schedule === undefined ? undefined : (
          <ScheduleActions schedule={schedule} onDeleted={() => void navigate("/schedules")} />
        )
      }
      badges={
        <>{schedule === undefined ? null : <ScheduleStateLabel paused={schedule.paused} />}</>
      }
    >
      <QueryView query={query} loading="Loading the schedule">
        {(data) => (
          <>
            <Facts schedule={data} />
            <Section title="Request">
              <JsonView
                label="Request JSON"
                value={data.request}
                filename={`schedule-${data.id}.json`}
              />
            </Section>
            <Section title="Runs">
              <JobPages filters={{ kind: null, status: null, scheduleId: data.id }} />
            </Section>
          </>
        )}
      </QueryView>
    </Screen>
  );
}
