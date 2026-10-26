import { Button, Link, Table, Typography } from "@heroui/react";
import { useParams } from "react-router";
import { configVersionLabel, type ConfigDetail } from "../../api/configs";
import {
  useApproveConfig,
  useConfig,
  useRejectConfig,
  useRetireRecipe,
} from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { ConfirmDialog } from "../../components/molecules/ConfirmDialog";
import { ConfigStatusLabel } from "../../components/molecules/StatusLabels";
import { LoadingView, ProblemAlert, QueryView } from "../../components/molecules/ViewStates";
import { Screen, Section } from "../../components/templates/Screen";
import { ConfigFieldsTable, useSchemaLabel } from "../sites/ConfigTables";
import { configPath } from "../sites/paths";
import { editRecipePath, recipesPath } from "./paths";

type RecipeChange = NonNullable<ConfigDetail["repair_changes"]>[number];

/** A part's value as JSON (RFC 8259); "—" where the version has none. */
function partText(value: unknown): string {
  return value === null || value === undefined ? "—" : JSON.stringify(value);
}

function ChangesTable({ changes }: Readonly<{ changes: readonly RecipeChange[] }>) {
  const rows = changes.map((change) => ({ ...change, id: change.part }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Repair changes">
          <Table.Header>
            <Table.Column isRowHeader>Part</Table.Column>
            <Table.Column>Now</Table.Column>
            <Table.Column>Repaired</Table.Column>
          </Table.Header>
          <Table.Body items={rows}>
            {(row) => (
              <Table.Row id={row.id}>
                <Table.Cell className="text-code">{row.part}</Table.Cell>
                <Table.Cell className="text-code break-all">{partText(row.before)}</Table.Cell>
                <Table.Cell className="text-code break-all">{partText(row.after)}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The repair drift proposed for the recipe: what it changes in the version serving now, approved (it serves in its
 * place) or rejected. */
function PendingRepair({ id }: Readonly<{ id: string }>) {
  const repair = useConfig(id);
  const approve = useApproveConfig(id);
  const reject = useRejectConfig(id);
  const problem = problemOf(repair.error) ?? problemOf(approve.error) ?? problemOf(reject.error);
  const busy = approve.isPending || reject.isPending;
  return (
    <Section
      title="Pending repair"
      action={
        <div className="flex gap-2">
          <Button
            variant="secondary"
            isPending={reject.isPending}
            isDisabled={busy}
            onPress={() => {
              reject.mutate();
            }}
          >
            Reject
          </Button>
          <Button
            isPending={approve.isPending}
            isDisabled={busy}
            onPress={() => {
              approve.mutate();
            }}
          >
            Approve
          </Button>
        </div>
      }
    >
      {repair.isPending ? <LoadingView label="Loading the repair" /> : null}
      <ProblemAlert problem={problem} />
      {repair.data === undefined ? null : (
        <>
          <Link href={configPath(id)}>Open the repair as a config</Link>
          <ChangesTable changes={repair.data.repair_changes ?? []} />
        </>
      )}
    </Section>
  );
}

/** What the recipe serves and how: its patterns, schema and page kind, its main call and drifting fields. */
function RecipeFacts({ recipe }: Readonly<{ recipe: ConfigDetail }>) {
  const schemaLabel = useSchemaLabel();
  const main = recipe.ir.api_recipe;
  return (
    <div className="flex flex-col gap-2">
      <Typography.Paragraph>
        {schemaLabel(recipe)}, {recipe.page_kind} pages, sent by {recipe.sent_by}
      </Typography.Paragraph>
      <ul aria-label="Patterns" className="flex flex-col gap-1 text-code">
        {recipe.patterns.map((pattern) => (
          <li key={pattern}>{pattern}</li>
        ))}
      </ul>
      {main == null ? null : (
        <Typography.Paragraph className="text-code break-all">
          Main call: {main.method} {main.url}
        </Typography.Paragraph>
      )}
      <Typography.Paragraph>
        Drifting: {recipe.drifting.length === 0 ? "none" : recipe.drifting.join(", ")}
      </Typography.Paragraph>
    </div>
  );
}

/** Retires the recipe after a confirmation: the learned config serves its URLs again. */
function RetireAction({ id }: Readonly<{ id: string }>) {
  const retire = useRetireRecipe(id);
  return (
    <>
      <ConfirmDialog
        trigger={
          <Button variant="danger-soft" isPending={retire.isPending}>
            Retire
          </Button>
        }
        title="Retire this recipe?"
        confirmLabel="Retire"
        onConfirm={() => {
          retire.mutate();
        }}
      >
        It stops serving: the site's learned config extracts the URLs its patterns match. A repair
        waiting for review is refused with it. Its versions stay listed.
      </ConfirmDialog>
      <ProblemAlert problem={problemOf(retire.error)} />
    </>
  );
}

function recipeTitle(recipe: ConfigDetail): string {
  const { site } = recipe;
  return `Recipe of ${site}, ${configVersionLabel(recipe)}`;
}

/** One custom recipe: what it serves, its main call and its fields, and a repair waiting for review with what it
 * changes; "Edit" opens it in the builder, "Retire" stops it serving, and the config screen exports it. */
export function RecipeScreen() {
  const { id = "" } = useParams();
  const recipe = useConfig(id);
  return (
    <QueryView query={recipe} loading="Loading the recipe">
      {(data) => (
        <Screen
          title={recipeTitle(data)}
          badges={<ConfigStatusLabel status={data.status} />}
          actions={
            <>
              <Link href={recipesPath(data.site)}>All recipes</Link>
              <Link href={configPath(data.id)}>Open as a config</Link>
              {data.status === "active" && data.recipe !== null ? (
                <>
                  <Link href={editRecipePath(data.id)}>Edit</Link>
                  <RetireAction id={data.id} />
                </>
              ) : null}
            </>
          }
        >
          <RecipeFacts recipe={data} />
          {data.pending_repair === null ? null : <PendingRepair id={data.pending_repair} />}
          <Section title="Fields">
            <ConfigFieldsTable config={data} />
          </Section>
        </Screen>
      )}
    </QueryView>
  );
}
