import type { StoredRecord } from "../api/records";
import type { RecordField } from "../api/scrape";

/** A published field with every §13.1 attribute set; `overrides` replaces any of them. */
export function recordField(overrides: Partial<RecordField>): RecordField {
  return {
    value: "Blue kettle",
    status: "published",
    reason: null,
    evidence: "structured",
    confidence: 0.97,
    calibrated: true,
    source_kind: "jsonld",
    locator: "jsonld:0:$.name",
    derived_by: null,
    ...overrides,
  };
}

/** A stored record of `schemaId` with `fields`, every other member the server's first version of an entity. */
export function storedRecord(
  id: string,
  schemaId: string,
  fields: StoredRecord["fields"],
): StoredRecord {
  return {
    id,
    capture_id: null,
    schema_id: schemaId,
    version: 1,
    changed_fields: [],
    mode: "adhoc",
    complete: true,
    confidence: 0.9,
    egress_country: null,
    egress_basis: "unknown",
    fields,
  };
}
