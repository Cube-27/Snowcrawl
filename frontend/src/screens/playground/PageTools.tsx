import { SchemaSelect } from "../../components/molecules/SchemaSelect";
import { FormatSelect } from "./FormatSelect";
import { withSchema, type FormatForm } from "./pageRequest";

/** The tools of a mode that takes formats and a schema (Scrape, Batch, Crawl): Format and Schema.
 * `formatsUnused` disables Format when the request sends no page format (a crawl with a schema asks for records
 * alone). */
export function PageTools<F extends FormatForm>({
  form,
  formatsUnused = false,
  onChange,
}: Readonly<{
  form: F;
  formatsUnused?: boolean;
  onChange: (form: F) => void;
}>) {
  return (
    <>
      <FormatSelect
        value={form.formats}
        isDisabled={formatsUnused}
        onChange={(formats) => {
          onChange({ ...form, formats });
        }}
      />
      <SchemaSelect
        value={form.schemaId}
        noneLabel="No schema"
        hideLabel
        onChange={(schemaId, schema) => {
          onChange(withSchema(form, schemaId, schema?.page_kind ?? null));
        }}
      />
    </>
  );
}
