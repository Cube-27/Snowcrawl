import { Button, Link, Table, Typography } from "@heroui/react";
import { useParams } from "react-router";
import { configVersionLabel, type ConfigDetail } from "../../api/configs";
import { useApproveConfig, useConfig, useRejectConfig } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { ConfigStatusLabel } from "../../components/molecules/ConfigStatusLabel";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { Section } from "../../components/templates/Section";
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
                <Table.Cell className="font-mono text-sm">{row.part}</Table.Cell>
                <Table.Cell className="font-mono text-sm break-all">
                  {partText(row.before)}
                </Table.Cell>
                <Table.Cell className="font-mono text-sm break-all">
                  {partText(row.after)}
                </Table.Cell>
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
      {problem === null ? null : <ProblemAlert problem={problem} />}
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
      <ul aria-label="Patterns" className="flex flex-col gap-1 font-mono text-sm">
        {recipe.patterns.map((pattern) => (
          <li key={pattern}>{pattern}</li>
        ))}
      </ul>
      {main == null ? null : (
        <Typography.Paragraph className="font-mono text-sm break-all">
          Main call: {main.method} {main.url}
        </Typography.Paragraph>
      )}
      <Typography.Paragraph>
        Drifting: {recipe.drifting.length === 0 ? "none" : recipe.drifting.join(", ")}
      </Typography.Paragraph>
    </div>
  );
}

function recipeTitle(recipe: ConfigDetail): string {
  const { site } = recipe;
  return `Recipe of ${site}, ${configVersionLabel(recipe)}`;
}

/** One custom recipe: what it serves, its main call and its fields, and a repair waiting for review with what it
 * changes; "Edit" opens it in the builder, and the config screen exports it. */
export function RecipeScreen() {
  const { id = "" } = useParams();
  const recipe = useConfig(id);
  const problem = problemOf(recipe.error);
  return (
    <section className="flex flex-col gap-stack">
      {recipe.isPending ? <LoadingView label="Loading the recipe" /> : null}
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {recipe.isSuccess ? (
        <>
          <PageHeader
            title={recipeTitle(recipe.data)}
            actions={
              <>
                <Link href={recipesPath(recipe.data.site)}>All recipes</Link>
                <Link href={configPath(recipe.data.id)}>Open as a config</Link>
                {recipe.data.status === "active" && recipe.data.recipe !== null ? (
                  <Link href={editRecipePath(recipe.data.id)}>Edit</Link>
                ) : null}
              </>
            }
          >
            <ConfigStatusLabel status={recipe.data.status} />
          </PageHeader>
          <RecipeFacts recipe={recipe.data} />
          {recipe.data.pending_repair === null ? null : (
            <PendingRepair id={recipe.data.pending_repair} />
          )}
          <Section title="Fields">
            <ConfigFieldsTable config={recipe.data} />
          </Section>
        </>
      ) : null}
    </section>
  );
}
