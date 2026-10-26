import { Button, Chip, Tabs, toast } from "@heroui/react";
import { useNavigate, useParams } from "react-router";
import { problemOf } from "../../api/hooks/query";
import { useRetireSchema, useSchema } from "../../api/hooks/schemas";
import type { Problem } from "../../api/problem";
import type { SchemaDef } from "../../api/schemas";
import { ConfirmDialog } from "../../components/molecules/ConfirmDialog";
import { LoadingView, ProblemAlert } from "../../components/molecules/ViewStates";
import { Screen } from "../../components/templates/Screen";
import { SchemaStatusLabel } from "../../components/molecules/StatusLabels";
import { adoptSchemaPath, editSchemaPath } from "./paths";
import { FieldsTable, SchemaJson } from "./SchemaViews";

/** The changes the server accepts for this schema: a preset changes only through its files, and only an
 * active schema is edited or retired. A refusal is still shown (409). */
function allowedActions(schema: SchemaDef) {
  const changeable = schema.origin !== "preset" && schema.status === "active";
  return {
    edit: changeable,
    retire: changeable,
    adopt: schema.origin === "prompt_draft",
  };
}

function DetailActions({
  schema,
  onRetire,
}: Readonly<{ schema: SchemaDef; onRetire: () => void }>) {
  const navigate = useNavigate();
  const allowed = allowedActions(schema);
  const { id, name } = schema;
  return (
    <>
      {allowed.retire ? (
        <ConfirmDialog
          trigger={<Button variant="danger-soft">Retire</Button>}
          title={`Retire ${name}?`}
          confirmLabel="Retire"
          onConfirm={onRetire}
        >
          A retired schema stays readable, since records and configs refer to it, but it can no
          longer be edited.
        </ConfirmDialog>
      ) : null}
      {allowed.edit ? (
        <Button variant="secondary" onPress={() => void navigate(editSchemaPath(id))}>
          Edit
        </Button>
      ) : null}
      {allowed.adopt ? (
        <Button onPress={() => void navigate(adoptSchemaPath(id))}>Adopt as user schema</Button>
      ) : null}
    </>
  );
}

const TABS = ["Fields", "JSON"] as const;

function SchemaDetailView({
  schema,
  problem,
  onRetire,
}: Readonly<{ schema: SchemaDef; problem: Problem | null; onRetire: () => void }>) {
  return (
    <Screen
      title={schema.name}
      badges={
        <>
          <Chip>{schema.origin}</Chip>
          <SchemaStatusLabel status={schema.status} />
        </>
      }
      actions={<DetailActions schema={schema} onRetire={onRetire} />}
    >
      <ProblemAlert problem={problem} />
      <Tabs>
        <Tabs.ListContainer>
          <Tabs.List aria-label="Schema">
            {TABS.map((tab) => (
              <Tabs.Tab key={tab} id={tab}>
                {tab}
                <Tabs.Indicator />
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs.ListContainer>
        <Tabs.Panel id="Fields">
          <FieldsTable schema={schema} />
        </Tabs.Panel>
        <Tabs.Panel id="JSON">
          <SchemaJson schema={schema} />
        </Tabs.Panel>
      </Tabs>
    </Screen>
  );
}

/** One schema: its fields and its document as JSON, with the changes the server allows for it. */
export function SchemaScreen() {
  const { id = "" } = useParams();
  const schema = useSchema(id);
  const retire = useRetireSchema();
  const loadProblem = problemOf(schema.error);

  if (loadProblem !== null) return <ProblemAlert problem={loadProblem} />;
  if (schema.data === undefined) return <LoadingView label="Loading the schema" />;
  const { name } = schema.data;
  const onRetire = () => {
    retire.mutate(id, {
      onSuccess: () => {
        toast.success(`Retired ${name}.`);
      },
    });
  };
  return (
    <SchemaDetailView schema={schema.data} problem={problemOf(retire.error)} onRetire={onRetire} />
  );
}
