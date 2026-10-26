import type { RecordField } from "../../api/scrape";
import { StatusLabel } from "../atoms/StatusLabel";

/** Whether a published field of `records` is uncalibrated: its confidence is a score, not a measured precision.
 * A field that did not publish has no confidence to qualify. */
export function hasUncalibrated(
  records: readonly {
    fields: Readonly<Record<string, Pick<RecordField, "status" | "calibrated">>>;
  }[],
): boolean {
  return records.some((record) =>
    Object.values(record.fields).some((field) => field.status === "published" && !field.calibrated),
  );
}

/** The one "uncalibrated" badge of a record, or of the records of a page: shown when a published field is
 * uncalibrated, never once per field. */
export function UncalibratedBadge({
  records,
}: Readonly<{ records: Parameters<typeof hasUncalibrated>[0] }>) {
  return hasUncalibrated(records) ? <StatusLabel text="uncalibrated" tone="warning" /> : null;
}
