import type { FieldStatus } from "../../api/field";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

// One tone per field status. A conflict is sources that disagree, so it is the one that needs a look.
const FIELD_STATUS_TONES: Record<FieldStatus, StatusTone> = {
  published: "success",
  missing: "neutral",
  interaction_required: "neutral",
  abstained: "warning",
  conflict: "danger",
};

/** A field's status as its text and icon. */
export function FieldStatusBadge({ status }: Readonly<{ status: FieldStatus }>) {
  return <StatusLabel text={status} tone={FIELD_STATUS_TONES[status]} />;
}
