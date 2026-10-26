import { useState } from "react";
import { Button, Card, Chip, Switch, Typography } from "@heroui/react";
import { TrashBin } from "@gravity-ui/icons";
import type { CustomRecipe, PageField } from "../../api/recipes";
import type { Problem } from "../../api/problem";
import { Choices } from "../../components/molecules/Choices";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { Section } from "../../components/templates/Screen";
import { CallEditor, type EditHandlers } from "./CallEditor";
import { fieldSources } from "./draft";
import { Line } from "./Fields";

type WarmUpKind = "none" | "page" | "url";
const WARM_UPS: readonly (readonly [WarmUpKind, string])[] = [
  ["none", "None"],
  ["page", "The page itself"],
  ["url", "A fixed URL"],
];

function warmUpKind(recipe: CustomRecipe): WarmUpKind {
  if (recipe.warm_up == null) return "none";
  return recipe.warm_up.url == null ? "page" : "url";
}

/** Whether a parameter of a call reads the warm-up's answer (a `warm_up*` binder). */
function readsWarmUp(recipe: CustomRecipe): boolean {
  const calls = [recipe.main, ...Object.values(recipe.calls ?? {})];
  return calls.some((call) =>
    Object.values(call.params_from_url ?? {}).some((binder) => binder.startsWith("warm_up")),
  );
}

/** The request the calls follow, whose cookies they carry and whose answer `warm_up` binders read: none, the page
 * itself, or a fixed URL whose `{name}` placeholders are the URL pattern's parameters. */
function WarmUpEditor({
  recipe,
  handlers,
}: Readonly<{ recipe: CustomRecipe; handlers: EditHandlers }>) {
  const kind = warmUpKind(recipe);
  const isRead = readsWarmUp(recipe);
  return (
    <div className="flex flex-col gap-2">
      <Choices
        label="Warm-up"
        options={WARM_UPS}
        value={kind}
        disabled={isRead ? ["none"] : []}
        {...(isRead
          ? { description: "A parameter reads the warm-up's answer: bind it otherwise first." }
          : {})}
        onChange={(next) => {
          handlers.onEdit(
            ["warm_up"],
            next === "none" ? null : { url: next === "page" ? null : "" },
          );
        }}
      />
      {kind === "url" ? (
        <Line
          label="Warm-up URL"
          value={recipe.warm_up?.url ?? ""}
          placeholder="https://example.com/item/{id}"
          onChange={(url) => {
            handlers.onEdit(["warm_up"], { url });
          }}
        />
      ) : null}
    </div>
  );
}

type PageStep = PageField["steps"][number];
type FirstStep = "xpath" | "jsonld";

function stepPath(step: PageStep): string {
  if ("xpath" in step) return step.xpath;
  return "jsonld" in step ? step.jsonld : "";
}
const FIRST_STEPS: readonly (readonly [FirstStep, string])[] = [
  ["xpath", "XPath"],
  ["jsonld", "JSON-LD"],
];

/** One field read from the page: by XPath 1.0 over its HTML or by JSONPath (RFC 9535) into its JSON-LD nodes,
 * optionally of one schema.org type; on the HTTP page, or flagged as one only a render states. */
function PageFieldRow({
  name,
  field,
  onChange,
}: Readonly<{
  name: string;
  field: PageField | undefined;
  onChange: (next: PageField | undefined) => void;
}>) {
  const steps = field?.steps ?? [];
  const [first] = steps;
  const [kind, setKind] = useState<FirstStep>(
    first !== undefined && "jsonld" in first ? "jsonld" : "xpath",
  );
  // The step edited is the first, when it is of the chosen kind; the steps after it stay, in order (the first valid
  // value wins).
  const current = first !== undefined && kind in first ? first : undefined;
  const rest = current === undefined ? steps : steps.slice(1);
  const needs = field?.needs ?? "http";
  const replace = (step: PageStep | undefined) => {
    const kept = step === undefined ? rest : [step, ...rest];
    onChange(kept.length === 0 ? undefined : { steps: kept, needs });
  };
  return (
    <li className="flex flex-wrap items-end gap-2">
      <Choices
        label={`Reads ${name} with`}
        options={FIRST_STEPS}
        value={kind}
        onChange={(next) => {
          // The step already written stays (the first valid value wins), with the field's `needs`.
          setKind(next);
        }}
      />
      <Line
        label={`${kind === "jsonld" ? "JSON-LD path" : "XPath"} of ${name}`}
        value={current === undefined ? "" : stepPath(current)}
        placeholder={kind === "jsonld" ? "$['offers'][0]['availability']" : "//h1"}
        onChange={(next) => {
          replace(next === "" ? undefined : ({ ...current, [kind]: next } as PageStep));
        }}
      />
      {current !== undefined && "jsonld" in current ? (
        <Line
          label={`JSON-LD type of ${name}`}
          value={current.when_type ?? ""}
          placeholder="Product"
          onChange={(next) => {
            replace({ ...current, when_type: next === "" ? null : next });
          }}
        />
      ) : null}
      <Switch
        isSelected={needs === "browser"}
        isDisabled={field === undefined}
        onChange={(isSelected) => {
          onChange({ steps, needs: isSelected ? "browser" : "http" });
        }}
      >
        <Switch.Content>
          <Switch.Control>
            <Switch.Thumb />
          </Switch.Control>
          Needs a render
        </Switch.Content>
      </Switch>
      {needs === "browser" ? (
        <Chip size="sm" color="warning">
          Rendered
        </Chip>
      ) : null}
    </li>
  );
}

/** The fields no call fills, and those read from the page: each can be read off the page's HTML. */
function PageFieldsEditor({
  recipe,
  fields,
  handlers,
}: Readonly<{ recipe: CustomRecipe; fields: readonly string[]; handlers: EditHandlers }>) {
  const sources = fieldSources(recipe);
  const open = fields.filter((name) =>
    (sources.get(name) ?? []).every((source) => source.kind === "page"),
  );
  if (open.length === 0) {
    return <Typography.Paragraph size="sm">Every field is filled by a call.</Typography.Paragraph>;
  }
  return (
    <ul aria-label="Page fields" className="flex flex-col gap-2">
      {open.map((name) => (
        <PageFieldRow
          key={name}
          name={name}
          field={recipe.fields?.[name]}
          onChange={(next) => {
            handlers.onEdit(["fields", name], next);
          }}
        />
      ))}
    </ul>
  );
}

/** Step 6: the extra calls (added from the call list, each bound from the page URL, the warm-up or the main call's
 * answer), the warm-up, and the fields read off the page's HTML. */
export function ExtrasStep({
  recipe,
  fields,
  problem,
  handlers,
}: Readonly<{
  recipe: CustomRecipe;
  fields: readonly string[];
  /** Why the last call added was not. */
  problem: Problem | null;
  handlers: EditHandlers;
}>) {
  const calls = Object.entries(recipe.calls ?? {});
  return (
    <Section title="6. Extra calls, warm-up and page fields">
      <Typography.Paragraph size="sm" color="muted">
        &quot;Add as extra call&quot; on a group of the call list adds it here, reading the fields
        the main call leaves unfilled.
      </Typography.Paragraph>
      <ProblemAlert problem={problem} />
      {calls.map(([name, call]) => (
        <Card key={name}>
          <Card.Header className="flex flex-row items-center justify-between gap-2">
            <Card.Title className="font-mono">{name}</Card.Title>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Remove call ${name}`}
              onPress={() => {
                handlers.onEdit(["calls", name], undefined);
              }}
            >
              <TrashBin aria-hidden="true" />
              Remove
            </Button>
          </Card.Header>
          <Card.Content>
            <CallEditor
              call={call}
              part={["calls", name]}
              isExtra
              hasWarmUp={recipe.warm_up != null}
              fields={fields}
              handlers={handlers}
            />
          </Card.Content>
        </Card>
      ))}
      <Typography.Heading level={3}>Warm-up</Typography.Heading>
      <WarmUpEditor recipe={recipe} handlers={handlers} />
      <Typography.Heading level={3}>Page fields</Typography.Heading>
      <PageFieldsEditor recipe={recipe} fields={fields} handlers={handlers} />
    </Section>
  );
}
