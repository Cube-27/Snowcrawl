import { Button, Link, Table } from "@heroui/react";
import { useNavigate, useParams } from "react-router";
import type { ConfigSummary } from "../../api/configs";
import { useSiteConfigs } from "../../api/hooks/configs";
import { ApiProblem } from "../../api/hooks/query";
import { EmptyView, QueryView } from "../../components/molecules/ViewStates";
import { Screen } from "../../components/templates/Screen";
import { useSchemaLabel } from "../sites/ConfigTables";
import { configPath, sitePath } from "../sites/paths";
import { newRecipePath, recipePath } from "./paths";

/** The recipes that serve: a custom recipe's older versions are suspended and stay on the site's config list. */
function servingRecipes(configs: readonly ConfigSummary[]): ConfigSummary[] {
  return configs.filter((config) => config.custom && config.status === "active");
}

/** RFC 9110 §15.5.5: the site has no configs yet, so it has no recipes: an empty list, not a failure. */
function isUnknownSite(error: Error | null): boolean {
  return error instanceof ApiProblem && error.problem.status === 404;
}

function NewRecipeButton({ host }: Readonly<{ host: string }>) {
  const navigate = useNavigate();
  return <Button onPress={() => void navigate(newRecipePath(host))}>New recipe</Button>;
}

function RecipesTable({
  recipes,
  schemaLabel,
}: Readonly<{
  recipes: readonly ConfigSummary[];
  schemaLabel: (config: ConfigSummary) => string;
}>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Recipes">
          <Table.Header>
            <Table.Column isRowHeader>Patterns</Table.Column>
            <Table.Column>Page kind</Table.Column>
            <Table.Column>Schema</Table.Column>
            <Table.Column>Version</Table.Column>
            <Table.Column>Drifting</Table.Column>
            <Table.Column>Pending repair</Table.Column>
          </Table.Header>
          <Table.Body items={recipes}>
            {(recipe) => (
              <Table.Row id={recipe.id} href={recipePath(recipe.id)}>
                <Table.Cell>
                  <ul className="flex flex-col gap-1 text-code">
                    {recipe.patterns.map((pattern) => (
                      <li key={pattern}>{pattern}</li>
                    ))}
                  </ul>
                </Table.Cell>
                <Table.Cell>{recipe.page_kind}</Table.Cell>
                <Table.Cell>{schemaLabel(recipe)}</Table.Cell>
                <Table.Cell>{recipe.version}</Table.Cell>
                <Table.Cell>
                  {recipe.drifting.length === 0 ? "None" : recipe.drifting.join(", ")}
                </Table.Cell>
                <Table.Cell>
                  {recipe.pending_repair === null ? (
                    "None"
                  ) : (
                    <Link href={configPath(recipe.pending_repair)}>Review the repair</Link>
                  )}
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** One site's custom recipes: the replayable calls an engineer chose, each serving the URLs its patterns match, with
 * the fields drifting since and a repair waiting for review. A row opens the recipe; "New recipe" builds one. */
export function RecipesScreen() {
  const { host = "" } = useParams();
  const configs = useSiteConfigs(host);
  const schemaLabel = useSchemaLabel();
  const empty = (
    <EmptyView title="No recipes yet">
      A recipe serves this site's pages from the calls they make, without a browser. Build one from
      two to five sample pages.
    </EmptyView>
  );
  return (
    <Screen
      title={`Recipes of ${host}`}
      actions={
        <>
          <Link href={sitePath(host)}>Site configs</Link>
          <NewRecipeButton host={host} />
        </>
      }
    >
      <QueryView
        query={isUnknownSite(configs.error) ? { ...configs, error: null, data: [] } : configs}
        loading="Loading the site's recipes"
        isEmpty={(data) => servingRecipes(data).length === 0}
        empty={empty}
      >
        {(data) => <RecipesTable recipes={servingRecipes(data)} schemaLabel={schemaLabel} />}
      </QueryView>
    </Screen>
  );
}
