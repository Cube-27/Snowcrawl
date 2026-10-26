import type { ConfigDetail, Correction, FieldSteps } from "../../api/configs";
import { draftOf, sameSteps, toCorrectedStep, type DraftStep } from "../../components/lib/steps";

/** One schema field in the correction form: the steps the config has for it, the steps as edited, and whether the
 * operator confirms the stored steps. */
export interface FieldDraft {
  name: string;
  /** The config's steps for the field; null when the config has none. */
  stored: FieldSteps | null;
  steps: DraftStep[];
  confirm: boolean;
}

/** Every step list of the config by field: the page's fields and, on a listing, the fields read in each row. */
export function configuredFields(config: ConfigDetail): Readonly<Record<string, FieldSteps>> {
  return { ...config.ir.fields, ...config.ir.listing?.fields };
}

/** The form's fields: each of the schema's, in the schema's order, then any the config has that the schema lacks. */
export function fieldDrafts(config: ConfigDetail, schemaFields: readonly string[]): FieldDraft[] {
  const configured = configuredFields(config);
  const names = [...new Set([...schemaFields, ...Object.keys(configured)])];
  return names.map((name) => {
    const stored = configured[name] ?? null;
    return { name, stored, steps: (stored?.steps ?? []).map(draftOf), confirm: false };
  });
}

/** The `PUT /v1/configs/{id}` body: the steps of each changed field, null for a field whose steps
 * were all removed, and the confirmed fields. Unchanged fields are left out, so the server keeps them as they are. */
export function correctionOf(drafts: readonly FieldDraft[]): Correction {
  const fields: Correction["fields"] = {};
  for (const draft of drafts) {
    const stored = (draft.stored?.steps ?? []).map(draftOf);
    if (sameSteps(stored, draft.steps)) continue;
    fields[draft.name] =
      draft.steps.length === 0 ? null : { steps: draft.steps.map(toCorrectedStep) };
  }
  return { fields, confirm: drafts.filter((draft) => draft.confirm).map((draft) => draft.name) };
}
