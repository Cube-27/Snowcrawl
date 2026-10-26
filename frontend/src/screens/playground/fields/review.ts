import type { Problem } from "../../../api/problem";
import type { NewSchema, SchemaDef } from "../../../api/schemas";
import { formOf, newSchemaOf, type SchemaFormState } from "../../../components/lib/schemaDocument";

/** What a run sends for a reviewed draft: the field names while the draft is unchanged, so the request
 * is the one Get code shows and the server reads the same stored draft; else the edited schema, inline. */
export type ReviewedSchema = { fields: string[] } | { inline: NewSchema };

export type Review =
  | { kind: "run"; schema: ReviewedSchema }
  | { kind: "refused"; problem: Problem };

const RENAME =
  "The draft's name stays with the drafted schema: give the edited schema a name of its own.";

function refused(field: string, reason: string): Review {
  return {
    kind: "refused",
    problem: {
      title: "The edited schema can't run",
      detail: null,
      errors: [{ field, reason }],
      retryAfter: null,
    },
  };
}

/** The document `draft` holds, as the review form writes it, so an unchanged form compares equal. */
function draftDocument(draft: SchemaDef): string {
  return JSON.stringify(newSchemaOf(formOf(draft)));
}

/** What the run sends for `form`, the review of `draft`, drafted from `fields`. An edited schema keeping the draft's
 * name is refused here: the server keeps one document per name, and refuses an inline schema that differs from it
 * (409). */
export function reviewOf(
  draft: SchemaDef,
  form: SchemaFormState,
  fields: readonly string[],
): Review {
  const edited = newSchemaOf(form);
  if (edited === null) return refused("page_kind", "Choose a page kind.");
  if (JSON.stringify(edited) === draftDocument(draft))
    return { kind: "run", schema: { fields: [...fields] } };
  if (edited.name === draft.name) return refused("name", RENAME);
  return { kind: "run", schema: { inline: edited } };
}
