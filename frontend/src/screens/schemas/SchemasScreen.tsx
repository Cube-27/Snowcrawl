import { Button, Table } from "@heroui/react";
import { useNavigate } from "react-router";
import { problemOf } from "../../api/hooks/query";
import { useSchemas } from "../../api/hooks/schemas";
import type { SchemaDef } from "../../api/schemas";
import { EmptyView } from "../../components/molecules/EmptyView";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { SchemaStatusLabel } from "../../components/molecules/SchemaStatusLabel";
import { schemaPath } from "./paths";

function NewSchemaButton() {
  const navigate = useNavigate();
  return <Button onPress={() => void navigate("/schemas/new")}>New schema</Button>;
}

function SchemaTable({ schemas }: Readonly<{ schemas: readonly SchemaDef[] }>) {
  if (schemas.length === 0) {
    return (
      <EmptyView title="No schemas yet" action={<NewSchemaButton />}>
        Create one, or adopt a draft a scrape made from a prompt.
      </EmptyView>
    );
  }
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Schemas">
          <Table.Header>
            <Table.Column isRowHeader>Name</Table.Column>
            <Table.Column>Origin</Table.Column>
            <Table.Column>Status</Table.Column>
            <Table.Column>Page kind</Table.Column>
          </Table.Header>
          <Table.Body items={schemas}>
            {(schema) => (
              <Table.Row id={schema.id} href={schemaPath(schema.id)}>
                <Table.Cell>{schema.name}</Table.Cell>
                <Table.Cell>{schema.origin}</Table.Cell>
                <Table.Cell>
                  <SchemaStatusLabel status={schema.status} />
                </Table.Cell>
                <Table.Cell>{schema.page_kind}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** Every schema, one row per name, as the API lists them. */
export function SchemasScreen() {
  const query = useSchemas();
  const problem = problemOf(query.error);

  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title="Schemas" actions={<NewSchemaButton />} />
      {query.isPending ? <LoadingView label="Loading schemas" /> : null}
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {query.isSuccess ? <SchemaTable schemas={query.data} /> : null}
    </section>
  );
}
