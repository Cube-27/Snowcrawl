import { Button, Modal } from "@heroui/react";
import { Clock } from "@gravity-ui/icons";
import { useState } from "react";
import { useNavigate } from "react-router";
import type { ScheduleKind } from "../../api/schedules";
import { problemOf } from "../../api/hooks/query";
import { useCreateSchedule } from "../../api/hooks/schedules";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { draftErrors, scheduleBody, type ScheduleDraft } from "./cadence";
import { DraftFields, viewerZone } from "./DraftFields";
import { schedulePath } from "./paths";

/** A button that opens a dialog storing `request` (the body a `kind` of job would be started with; null while it can't
 * be sent) as a recurring schedule (`POST /v1/schedules`) and opens it. `label` names the button, which says
 * where the request comes from: a form (Schedule this) or a stored run. */
export function ScheduleDialog({
  kind,
  request,
  label,
}: Readonly<{ kind: ScheduleKind; request: object | null; label: string }>) {
  const navigate = useNavigate();
  const create = useCreateSchedule();
  const [draft, setDraft] = useState<ScheduleDraft>({
    name: "",
    every: "interval",
    minutes: 60,
    cron: "",
    timezone: viewerZone(),
  });
  const [checked, setChecked] = useState(false);
  const errors = checked ? draftErrors(draft) : {};
  const problem = problemOf(create.error);
  const save = () => {
    setChecked(true);
    if (request === null || Object.keys(draftErrors(draft)).length > 0) return;
    create.mutate(scheduleBody(kind, request, draft), {
      onSuccess: (schedule) => void navigate(schedulePath(schedule.id)),
    });
  };
  return (
    <Modal>
      <Button variant="secondary" isDisabled={request === null}>
        <Clock aria-hidden="true" />
        {label}
      </Button>
      <Modal.Backdrop>
        <Modal.Container>
          <Modal.Dialog>
            <Modal.CloseTrigger />
            <Modal.Header>
              <Modal.Heading>Schedule this request</Modal.Heading>
            </Modal.Header>
            {/* No form of its own: the dialog sits inside the playground's form in React's tree, where a submit
                would bubble to it and start the run. */}
            <Modal.Body className="flex flex-col gap-4">
              <DraftFields draft={draft} errors={errors} onChange={setDraft} />
              {problem === null ? null : <ProblemAlert problem={problem} />}
            </Modal.Body>
            <Modal.Footer>
              <Button isPending={create.isPending} onPress={save}>
                Create schedule
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
