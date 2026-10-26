import { problemOf } from "../../../api/hooks/query";
import { useSchemas } from "../../../api/hooks/schemas";
import type { SchemaDef } from "../../../api/schemas";
import { isOptionOf, OptionSelect } from "../../../components/molecules/OptionSelect";
import { ProblemAlert } from "../../../components/molecules/ProblemAlert";
import type { SchemaSource } from "./form";

/** The sources other than a stored schema, by option key. Stored schema ids are UUIDs, so no key names one of them. */
const DRAFT_SOURCES: Readonly<Record<Exclude<SchemaSource, "id">, string>> = {
  inline: "Inline JSON",
  prompt: "From a prompt",
  fields: "From field names",
};

const isDraftSource = isOptionOf(DRAFT_SOURCES);

/** Extract's toolbar Schema: one choice of where the schema comes from, the other sources
 * first, then each stored schema. Choosing a stored schema reports it with `onSchema`, another source with
 * `onSource`. It shows the request's `schema` errors, so a missing schema is said here, not in the options. */
export function SchemaSourceSelect({
  source,
  schemaId,
  onSource,
  onSchema,
}: Readonly<{
  source: SchemaSource;
  schemaId: string;
  onSource: (source: Exclude<SchemaSource, "id">) => void;
  onSchema: (schemaId: string, schema: SchemaDef) => void;
}>) {
  const query = useSchemas();
  const problem = problemOf(query.error);
  const schemas = query.data ?? [];
  const options: Record<string, string> = {
    ...DRAFT_SOURCES,
    ...Object.fromEntries(schemas.map((schema) => [schema.id, schema.name])),
  };
  let value: string | null = source;
  if (source === "id") value = schemaId === "" ? null : schemaId;
  return (
    <>
      {problem === null ? null : <ProblemAlert problem={problem} />}
      <OptionSelect
        label="Schema"
        name="schema"
        options={options}
        value={value}
        hideLabel
        placeholder="Choose a schema"
        accepts={isOptionOf(options)}
        onChange={(key) => {
          if (isDraftSource(key)) {
            onSource(key);
            return;
          }
          const schema = schemas.find(({ id }) => id === key);
          if (schema !== undefined) onSchema(key, schema);
        }}
      />
    </>
  );
}
