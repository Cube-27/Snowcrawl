import { problemOf } from "../../api/hooks/query";
import { useSchemas } from "../../api/hooks/schemas";
import type { SchemaDef } from "../../api/schemas";
import { isOptionOf, OptionSelect } from "./OptionSelect";
import { ProblemAlert } from "./ViewStates";

/** The option key for "no schema": schema ids are UUIDs, so it names none of them. */
const NONE = "none";

/** A choice of one stored schema, by id; `onChange` also gets the chosen schema. Required unless `noneLabel`
 * names an option for no schema, which reports the id "". */
export function SchemaSelect({
  label = "Schema",
  value,
  noneLabel,
  hideLabel = false,
  isDisabled = false,
  onChange,
}: Readonly<{
  label?: string;
  value: string;
  noneLabel?: string;
  hideLabel?: boolean;
  isDisabled?: boolean;
  onChange: (schemaId: string, schema: SchemaDef | null) => void;
}>) {
  const query = useSchemas();
  const problem = problemOf(query.error);
  const schemas = query.data ?? [];
  const options: Record<string, string> = {
    ...(noneLabel === undefined ? {} : { [NONE]: noneLabel }),
    ...Object.fromEntries(schemas.map((schema) => [schema.id, schema.name])),
  };
  const empty = noneLabel === undefined ? null : NONE;
  return (
    <>
      <ProblemAlert problem={problem} />
      <OptionSelect
        label={label}
        options={options}
        value={value === "" ? empty : value}
        isRequired={noneLabel === undefined}
        hideLabel={hideLabel}
        placeholder="Choose a schema"
        isDisabled={isDisabled}
        accepts={isOptionOf(options)}
        onChange={(key) => {
          const schema = schemas.find(({ id }) => id === key) ?? null;
          onChange(schema === null ? "" : key, schema);
        }}
      />
    </>
  );
}
