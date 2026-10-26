import { Label, Table, TextArea, TextField } from "@heroui/react";
import type { SchemaDef } from "../../api/schemas";
import { FIELD_TYPES } from "../../components/lib/schemaDocument";

/** A schema's top-level fields, one row each. Type parameters and nested fields are in the JSON tab. */
export function FieldsTable({ schema }: Readonly<{ schema: SchemaDef }>) {
  const fields = Object.entries(schema.fields).map(([name, spec]) => ({ id: name, spec }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Fields">
          <Table.Header>
            <Table.Column isRowHeader>Field</Table.Column>
            <Table.Column>Type</Table.Column>
            <Table.Column>Required</Table.Column>
            <Table.Column>Scope</Table.Column>
            <Table.Column>Description</Table.Column>
          </Table.Header>
          <Table.Body items={fields}>
            {(field) => (
              <Table.Row id={field.id}>
                <Table.Cell>{field.id}</Table.Cell>
                <Table.Cell>{FIELD_TYPES[field.spec.type]}</Table.Cell>
                <Table.Cell>{field.spec.required ? "Yes" : "No"}</Table.Cell>
                <Table.Cell>{field.spec.scope}</Table.Cell>
                <Table.Cell>{field.spec.description ?? "—"}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The schema document as JSON, read-only: the members a create sends. */
export function SchemaJson({ schema }: Readonly<{ schema: SchemaDef }>) {
  const { name, page_kind, required_groups, fields } = schema;
  return (
    <TextField
      isReadOnly
      value={JSON.stringify({ name, page_kind, required_groups, fields }, null, 2)}
    >
      <Label>Schema document</Label>
      <TextArea rows={20} />
    </TextField>
  );
}
