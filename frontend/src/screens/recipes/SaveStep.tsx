import { Alert, Button, Checkbox, Label, Link, Typography } from "@heroui/react";
import { useState } from "react";
import { useNavigate } from "react-router";
import type { ConfigSummary } from "../../api/configs";
import { useSiteConfigs } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { useSaveRecipe } from "../../api/hooks/recipes";
import type { Problem } from "../../api/problem";
import type { CustomRecipe, RecipeTestResult, TestRun } from "../../api/recipes";
import { FormActions } from "../../components/molecules/FormActions";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { Section } from "../../components/templates/Screen";
import { NumberRow, SwitchRow } from "../playground/OptionsPanel";
import type { EditHandlers } from "./CallEditor";
import { recipePath } from "./paths";

/** The fields a run's records publish. */
function published(run: TestRun): Set<string> {
  return new Set(
    run.records.flatMap((record) =>
      Object.entries(record.fields)
        .filter(([, field]) => field.status === "published")
        .map(([name]) => name),
    ),
  );
}

/** The fields normal extraction published that the recipe left unfilled on a URL of the last test, in name order. */
function fewerFields(tested: RecipeTestResult | undefined): string[] {
  const missing = new Set<string>();
  for (const { recipe, normal } of tested?.urls ?? []) {
    const byRecipe = published(recipe);
    const byNormal = published(normal);
    for (const name of byNormal) if (!byRecipe.has(name)) missing.add(name);
  }
  return [...missing].sort((a, b) => a.localeCompare(b));
}

/** The warning that the recipe fills fewer fields than normal extraction did on the last test, naming them, with the
 * engineer's explicit confirmation that saving it is meant. */
function FewerFields({
  fields,
  isConfirmed,
  onConfirm,
}: Readonly<{
  fields: readonly string[];
  isConfirmed: boolean;
  onConfirm: (isSelected: boolean) => void;
}>) {
  return (
    <Alert status="warning">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>The recipe leaves fields unfilled that normal extraction filled</Alert.Title>
        <Alert.Description>
          On the last test, normal extraction filled {fields.join(", ")}, which the recipe leaves
          unfilled on the URLs it serves.
        </Alert.Description>
        <Checkbox isSelected={isConfirmed} onChange={onConfirm}>
          <Checkbox.Content>
            <Checkbox.Control>
              <Checkbox.Indicator />
            </Checkbox.Control>
            <Label>Save it anyway</Label>
          </Checkbox.Content>
        </Checkbox>
      </Alert.Content>
    </Alert>
  );
}

/** The site's active recipes a refusal names: the one serving a pattern of the same shape (409). */
function namedRecipes(problem: Problem, configs: readonly ConfigSummary[]): ConfigSummary[] {
  const detail = problem.detail ?? "";
  return configs.filter(
    (config) => config.custom && config.status === "active" && detail.includes(config.id),
  );
}

function Conflicting({ host, problem }: Readonly<{ host: string; problem: Problem }>) {
  const configs = useSiteConfigs(host);
  const named = namedRecipes(problem, configs.data ?? []);
  if (named.length === 0) return null;
  return (
    <ul aria-label="Conflicting recipes" className="flex flex-col gap-1">
      {named.map((config) => (
        <li key={config.id}>
          <Link href={recipePath(config.id)}>
            Open the recipe serving {config.patterns.join(", ")}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Step 8: who sends the calls, the least interval between them, and saving: a new recipe, or the next version of
 * the one edited (`replaces`). When the last test (`tested`) shows the recipe filling fewer fields than normal
 * extraction, saving needs the engineer's confirmation. Saving opens the saved recipe; a refusal says why, naming the
 * recipe whose patterns have the same shape. */
export function SaveStep({
  host,
  recipe,
  replaces,
  tested,
  isDrafting,
  handlers,
}: Readonly<{
  host: string;
  recipe: CustomRecipe;
  replaces: string | null;
  tested: RecipeTestResult | undefined;
  isDrafting: boolean;
  handlers: EditHandlers;
}>) {
  const save = useSaveRecipe(host, replaces);
  const navigate = useNavigate();
  const problem = problemOf(save.error);
  const fewer = fewerFields(tested);
  // A confirmation holds for the test it was given on; a new test asks again.
  const [confirmed, setConfirmed] = useState<RecipeTestResult | null>(null);
  const isConfirmed = tested !== undefined && confirmed === tested;
  return (
    <Section title="8. Save">
      <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-4">
        <SwitchRow
          label="Sent by the browser"
          help="The calls are sent from a warm page of the site in the browser, with its cookies and the headers its scripts add. Off: sent over HTTP."
          isSelected={recipe.sent_by === "browser"}
          onChange={(isSelected) => {
            handlers.onEdit(["sent_by"], isSelected ? "browser" : "http");
          }}
        />
        <NumberRow
          label="Least interval (ms)"
          help="The least time between two of the recipe's calls. Empty: the site's rate alone."
          name="rate_ms"
          value={recipe.rate_ms ?? Number.NaN}
          minValue={0}
          placeholder="None"
          onChange={(value) => {
            handlers.onEdit(["rate_ms"], Number.isNaN(value) ? null : value);
          }}
        />
      </div>
      <Typography.Paragraph size="sm" color="muted">
        Saving serves the URLs the patterns match from now on; the learned config serves the rest.
      </Typography.Paragraph>
      {fewer.length === 0 ? null : (
        <FewerFields
          fields={fewer}
          isConfirmed={isConfirmed}
          onConfirm={(isSelected) => {
            setConfirmed(isSelected ? (tested ?? null) : null);
          }}
        />
      )}
      <FormActions>
        <Button
          isPending={save.isPending}
          isDisabled={isDrafting || (fewer.length > 0 && !isConfirmed)}
          onPress={() => {
            save.mutate(recipe, {
              onSuccess: (saved) => {
                void navigate(recipePath(saved.id));
              },
            });
          }}
        >
          {replaces === null ? "Save recipe" : "Save new version"}
        </Button>
      </FormActions>
      {problem === null ? null : (
        <>
          <ProblemAlert problem={problem} />
          <Conflicting host={host} problem={problem} />
        </>
      )}
    </Section>
  );
}
