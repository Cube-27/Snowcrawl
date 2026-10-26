import { Button } from "@heroui/react";
import { useNavigate } from "react-router";
import { useDeleteSchedule, useRunSchedule, useSetSchedulePaused } from "../../api/hooks/schedules";
import type { Schedule } from "../../api/schedules";
import { ConfirmDialog } from "../../components/molecules/ConfirmDialog";
import { runPath } from "../playground/modes";
import { EditSchedule } from "./EditSchedule";

/** A schedule's Edit, Pause or Resume, Run now (which opens the run it starts) and Delete (asked first; its jobs stay). */
export function ScheduleActions({
  schedule,
  onDeleted,
}: Readonly<{ schedule: Schedule; onDeleted?: () => void }>) {
  const navigate = useNavigate();
  const paused = useSetSchedulePaused();
  const run = useRunSchedule();
  const remove = useDeleteSchedule();
  return (
    <div className="flex flex-wrap gap-2">
      <Button
        size="sm"
        variant="secondary"
        isPending={paused.isPending}
        onPress={() => {
          paused.mutate({ id: schedule.id, paused: !schedule.paused });
        }}
      >
        {schedule.paused ? "Resume" : "Pause"}
      </Button>
      <Button
        size="sm"
        variant="secondary"
        isPending={run.isPending}
        onPress={() => {
          run.mutate(schedule.id, {
            onSuccess: ({ job_id }) => {
              const path = runPath(schedule.kind, job_id);
              if (path !== null) void navigate(path);
            },
          });
        }}
      >
        Run now
      </Button>
      <EditSchedule schedule={schedule} />
      <ConfirmDialog
        trigger={
          <Button size="sm" variant="danger-soft">
            Delete
          </Button>
        }
        title={`Delete ${schedule.name}?`}
        confirmLabel="Delete schedule"
        onConfirm={() => {
          remove.mutate(schedule.id, { onSuccess: () => onDeleted?.() });
        }}
      >
        It stops running. The jobs it ran stay in Jobs.
      </ConfirmDialog>
    </div>
  );
}
