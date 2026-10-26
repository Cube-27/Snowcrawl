import type { SchemaDef } from "../../api/schemas";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

// One tone per `schema_def.status`: a retired schema stays readable but takes no new work.
const SCHEMA_STATUS_TONES: Record<SchemaDef["status"], StatusTone> = {
  active: "success",
  retired: "neutral",
};

/** A schema's status as its text and icon. */
export function SchemaStatusLabel({ status }: Readonly<{ status: SchemaDef["status"] }>) {
  return <StatusLabel text={status} tone={SCHEMA_STATUS_TONES[status]} />;
}
