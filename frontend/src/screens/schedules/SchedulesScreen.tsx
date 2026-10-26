import { Link, Table } from "@heroui/react";
import { problemOf } from "../../api/hooks/query";
import { useSchedules } from "../../api/hooks/schedules";
import type { Schedule } from "../../api/schedules";
import { DEFAULT_PAGE_SIZE, useCursorPages } from "../../components/lib/cursors";
import { EmptyView } from "../../components/molecules/ViewStates";
import { PagedTable } from "../../components/organisms/PagedTable";
import { Screen } from "../../components/templates/Screen";
import { ScheduleStateLabel } from "../../components/molecules/StatusLabels";
import { Timestamp } from "../../components/atoms/Timestamp";
import { KIND_LABELS } from "../jobs/kinds";
import { runPath } from "../playground/modes";
import { cadenceText } from "./cadence";
import { schedulePath } from "./paths";
import { ScheduleActions } from "./ScheduleActions";

/** The last run: the job it started (a link to its run), or why it started none, or a dash before the first. */
export function LastRun({ schedule }: Readonly<{ schedule: Schedule }>) {
  if (schedule.last_error !== null)
    return <span className="text-danger">{schedule.last_error}</span>;
  const path = schedule.last_job_id === null ? null : runPath(schedule.kind, schedule.last_job_id);
  if (path === null) return <Timestamp value={schedule.last_run_at} />;
  return (
    <Link href={path}>
      <Timestamp value={schedule.last_run_at} />
    </Link>
  );
}

function ScheduleRows({ schedules }: Readonly<{ schedules: readonly Schedule[] }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Schedules">
          <Table.Header>
            <Table.Column isRowHeader>Name</Table.Column>
            <Table.Column>Kind</Table.Column>
            <Table.Column>Runs</Table.Column>
            <Table.Column>State</Table.Column>
            <Table.Column>Next run</Table.Column>
            <Table.Column>Last run</Table.Column>
            <Table.Column>
              <span className="sr-only">Actions</span>
            </Table.Column>
          </Table.Header>
          <Table.Body items={schedules}>
            {(schedule) => (
              <Table.Row id={schedule.id}>
                <Table.Cell>
                  <Link href={schedulePath(schedule.id)}>{schedule.name}</Link>
                </Table.Cell>
                <Table.Cell>{KIND_LABELS[schedule.kind]}</Table.Cell>
                <Table.Cell>{cadenceText(schedule)}</Table.Cell>
                <Table.Cell>
                  <ScheduleStateLabel paused={schedule.paused} />
                </Table.Cell>
                <Table.Cell>
                  {schedule.paused ? "—" : <Timestamp value={schedule.next_run_at} />}
                </Table.Cell>
                <Table.Cell>
                  <LastRun schedule={schedule} />
                </Table.Cell>
                <Table.Cell>
                  <ScheduleActions schedule={schedule} />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The recurring schedules, newest first, a page at a time: how often each runs, when next, its last run,
 * and its actions. A schedule is made from a playground request with Schedule this. */
export function SchedulesScreen() {
  const { cursor, page, previous, nextTo } = useCursorPages();
  const schedules = useSchedules({ cursor, limit: DEFAULT_PAGE_SIZE });
  const items = schedules.data?.items ?? [];
  return (
    <Screen
      title="Schedules"
      description="Requests that run again on a cadence. Make one with Schedule this in the playground."
    >
      <PagedTable
        problem={problemOf(schedules.error)}
        loading={schedules.isSuccess ? null : "Loading schedules"}
        page={page}
        onPrevious={previous}
        onNext={schedules.isSuccess ? nextTo(schedules.data.next_cursor) : null}
      >
        {items.length === 0 && previous === null ? (
          <EmptyView title="No schedules yet">
            Fill a Scrape, Batch, Crawl or Extract request and choose Schedule this.
          </EmptyView>
        ) : (
          <ScheduleRows schedules={items} />
        )}
      </PagedTable>
    </Screen>
  );
}
