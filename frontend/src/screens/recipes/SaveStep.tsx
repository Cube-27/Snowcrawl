import { Button, Link, Typography } from "@heroui/react";
import { useNavigate } from "react-router";
import type { ConfigSummary } from "../../api/configs";
import { useSiteConfigs } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { useSaveRecipe } from "../../api/hooks/recipes";
import type { Problem } from "../../api/problem";
import type { CustomRecipe } from "../../api/recipes";
import { FormActions } from "../../components/molecules/FormActions";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { Section } from "../../components/templates/Screen";
import { NumberRow, SwitchRow } from "../playground/OptionsPanel";
import type { EditHandlers } from "./CallEditor";
import { recipePath } from "./paths";

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
 * the one edited (`replaces`). Saving opens the saved recipe; a refusal says why, naming the recipe whose patterns
 * have the same shape. */
export function SaveStep({
  host,
  recipe,
  replaces,
  isDrafting,
  handlers,
}: Readonly<{
  host: string;
  recipe: CustomRecipe;
  replaces: string | null;
  isDrafting: boolean;
  handlers: EditHandlers;
}>) {
  const save = useSaveRecipe(host, replaces);
  const navigate = useNavigate();
  const problem = problemOf(save.error);
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
      <FormActions>
        <Button
          isPending={save.isPending}
          isDisabled={isDrafting}
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
