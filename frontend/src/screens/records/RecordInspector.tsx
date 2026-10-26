import { Button, Drawer, Typography } from "@heroui/react";
import type { FieldTypes } from "../../api/field";
import { problemOf } from "../../api/hooks/query";
import { useReplay } from "../../api/hooks/records";
import type { StoredRecord } from "../../api/records";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { RecordFieldsTable, RecordSummary } from "../../components/organisms/RecordFields";
import { changedFieldsText } from "../../components/organisms/RecordTable";
import { ReplayView } from "./ReplayView";

/** The capture the record came from, and a replay of it with the current engine and config. */
function ReplaySection({
  record,
  captureId,
  fieldTypes,
}: Readonly<{ record: StoredRecord; captureId: string; fieldTypes: FieldTypes }>) {
  const replay = useReplay();
  const problem = problemOf(replay.error);
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Typography.Paragraph size="sm">
          Capture <Typography.Code>{captureId}</Typography.Code>
        </Typography.Paragraph>
        <Button
          variant="secondary"
          isPending={replay.isPending}
          onPress={() => {
            replay.mutate(captureId);
          }}
        >
          Replay
        </Button>
      </div>
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {replay.isSuccess ? (
        <ReplayView stored={record} result={replay.data} fieldTypes={fieldTypes} />
      ) : null}
    </section>
  );
}

/** What changed from the version this one supersedes, which is the version before it. */
function changeText(record: StoredRecord): string {
  if (record.version === 1) return "The first version of this record.";
  const previous = String(record.version - 1);
  const changed = changedFieldsText(record);
  return changed === null
    ? `No published value changed from version ${previous}.`
    : `Changed from version ${previous}: ${changed}`;
}

/** Every `field_meta` attribute of one record, and its capture with a replay, in a drawer over the
 * table. The record is not editable here; `onClose` closes the drawer. */
export function RecordInspector({
  record,
  fieldTypes,
  onClose,
}: Readonly<{ record: StoredRecord; fieldTypes: FieldTypes; onClose: () => void }>) {
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement="bottom">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>Record version {record.version}</Drawer.Heading>
              <div className="flex flex-wrap items-center gap-3">
                <RecordSummary record={record} />
              </div>
            </Drawer.Header>
            <Drawer.Body className="flex flex-col gap-6">
              <Typography.Paragraph size="sm">{changeText(record)}</Typography.Paragraph>
              <RecordFieldsTable label="Record fields" record={record} fieldTypes={fieldTypes} />
              {/* Captures expire; the record stays, without one to replay. */}
              {record.capture_id === null ? (
                <Typography.Paragraph size="sm" color="muted">
                  The capture this record came from has expired, so it cannot be replayed.
                </Typography.Paragraph>
              ) : (
                <ReplaySection
                  record={record}
                  captureId={record.capture_id}
                  fieldTypes={fieldTypes}
                />
              )}
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}
