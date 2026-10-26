import { Button, FieldError, Label, Modal, TextArea, TextField } from "@heroui/react";
import { useState } from "react";
import { problemOf } from "../../api/hooks/query";
import { useUpdateSchedule } from "../../api/hooks/schedules";
import type { Schedule } from "../../api/schedules";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { draftErrors, draftOf, parseRequest, scheduleChange, type ScheduleDraft } from "./cadence";
import { DraftFields } from "./DraftFields";

const NOT_AN_OBJECT = "The request is a JSON object.";

/** The edit's form: the name and cadence fields, then the request each run submits as JSON. Saving sends only what
 * changed (`PATCH /v1/schedules/{id}`); the server checks a changed request as it checks a new schedule's, and a
 * problem it finds shows above the buttons. */
function EditForm({ schedule, onSaved }: Readonly<{ schedule: Schedule; onSaved: () => void }>) {
  const update = useUpdateSchedule(schedule.id);
  const [draft, setDraft] = useState<ScheduleDraft>(() => draftOf(schedule));
  const initial = JSON.stringify(schedule.request, null, 2);
  const [text, setText] = useState(initial);
  const [checked, setChecked] = useState(false);
  const errors: Record<string, string> = checked ? { ...draftErrors(draft) } : {};
  if (checked && parseRequest(text) === null) errors["request"] = NOT_AN_OBJECT;
  const problem = problemOf(update.error);
  const save = () => {
    setChecked(true);
    const change = scheduleChange(schedule, draft, { text, initial });
    if (change === null || Object.keys(draftErrors(draft)).length > 0) return;
    update.mutate(change, { onSuccess: onSaved });
  };
  return (
    <>
      <Modal.Body className="flex flex-col gap-4">
        <DraftFields draft={draft} errors={errors} onChange={setDraft} />
        <TextField name="request" isInvalid={"request" in errors} value={text} onChange={setText}>
          <Label>Request (JSON)</Label>
          <TextArea rows={10} spellCheck={false} className="font-mono" />
          <FieldError>{errors["request"]}</FieldError>
        </TextField>
        <ProblemAlert problem={problem} />
      </Modal.Body>
      <Modal.Footer>
        <Button isPending={update.isPending} onPress={save}>
          Save changes
        </Button>
      </Modal.Footer>
    </>
  );
}

/** A schedule's Edit: a dialog over its name, cadence and request. It starts from the schedule as stored and
 * is rebuilt when the schedule is saved (`updated_at`), so it never shows a stale draft. */
export function EditSchedule({ schedule }: Readonly<{ schedule: Schedule }>) {
  const [open, setOpen] = useState(false);
  return (
    <Modal isOpen={open} onOpenChange={setOpen}>
      <Button size="sm" variant="secondary">
        Edit
      </Button>
      <Modal.Backdrop>
        <Modal.Container>
          <Modal.Dialog>
            <Modal.CloseTrigger />
            <Modal.Header>
              <Modal.Heading>Edit {schedule.name}</Modal.Heading>
            </Modal.Header>
            <EditForm
              key={schedule.updated_at}
              schedule={schedule}
              onSaved={() => {
                setOpen(false);
              }}
            />
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
