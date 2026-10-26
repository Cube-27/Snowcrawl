import { Button } from "@heroui/react";
import { useCancelJob } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import { ConfirmDialog } from "./ConfirmDialog";
import { ProblemAlert } from "./ProblemAlert";

/** Cancels the job after a confirmation: its queued pages are not fetched. `isDisabled` keeps the
 * button in place for a job that can't be cancelled, so the row around it does not move. */
export function CancelJobAction({
  id,
  isDisabled = false,
}: Readonly<{ id: string; isDisabled?: boolean }>) {
  const cancel = useCancelJob(id);
  const problem = problemOf(cancel.error);
  return (
    <>
      <ConfirmDialog
        trigger={
          <Button variant="danger-soft" isPending={cancel.isPending} isDisabled={isDisabled}>
            Cancel job
          </Button>
        }
        title="Cancel this job?"
        confirmLabel="Cancel job"
        onConfirm={() => {
          cancel.mutate();
        }}
      >
        Its queued pages are not fetched. Pages already fetched keep their results.
      </ConfirmDialog>
      {problem === null ? null : <ProblemAlert problem={problem} />}
    </>
  );
}
