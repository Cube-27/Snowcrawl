import { Link, Typography } from "@heroui/react";
import { useMemo, useState } from "react";
import { useParams } from "react-router";
import type { Endpoint } from "../../api/captures";
import { parseFieldTypes, type FieldTypes } from "../../api/field";
import { useConfig } from "../../api/hooks/configs";
import { useCallList, useProposal, useProposeCall, useTestRecipe } from "../../api/hooks/recipes";
import { problemOf } from "../../api/hooks/query";
import { useSchema } from "../../api/hooks/schemas";
import type { Problem } from "../../api/problem";
import {
  MAX_SAMPLES,
  type CustomRecipe,
  type Proposal,
  type ProposeBody,
  type WrittenRecipe,
} from "../../api/recipes";
import type { SchemaDef } from "../../api/schemas";
import { EmptyView, LoadingView, ProblemAlert } from "../../components/molecules/ViewStates";
import { Screen, Section } from "../../components/templates/Screen";
import { SchemaSelect } from "../../components/molecules/SchemaSelect";
import type { EditHandlers } from "./CallEditor";
import { CallList, FieldMatches, type SampleNumber } from "./CallList";
import { applied, fieldSources, NO_EDITS, withEdit, withoutEdit, type Edits } from "./draft";
import { endpointKey, endpointRef, endpointText, ownSiteFirst } from "./endpoints";
import { ExtrasStep } from "./ExtrasStep";
import { recipePath, recipesPath } from "./paths";
import { ReviewStep } from "./ReviewStep";
import { SampleForm, SampleList, type Sample } from "./Samples";
import { SaveStep } from "./SaveStep";
import { TestStep } from "./TestStep";

/** The fewest samples a recipe is drafted from: one page cannot tell a call's fixed parts from those the URL
 * varies. */
const MIN_SAMPLES = 2;

function samplesHelp(count: number): string {
  if (count < MIN_SAMPLES)
    return `Capture at least ${String(MIN_SAMPLES)} pages, at most ${String(MAX_SAMPLES)}.`;
  if (count >= MAX_SAMPLES) return `${String(MAX_SAMPLES)} samples: remove one to capture another.`;
  return `${String(count)} samples; up to ${String(MAX_SAMPLES)}.`;
}

/** Step 4: the main call chosen, where it carries each field, and how its draft is coming. */
function MainCall({
  chosen,
  samples,
  proposal,
}: Readonly<{
  chosen: Endpoint | undefined;
  samples: number;
  proposal: ReturnType<typeof useProposal>;
}>) {
  const problem = problemOf(proposal.error);
  if (chosen === undefined) {
    return (
      <Typography.Paragraph size="sm" color="muted">
        Choose the call that answers with the page&apos;s record: &quot;Use as main call&quot; on
        its group.
      </Typography.Paragraph>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <Typography.Paragraph className="text-code break-all">
        {endpointText(chosen)}
      </Typography.Paragraph>
      <FieldMatches label="Main call fields" matches={chosen.fields} samples={samples} />
      {samples < MIN_SAMPLES ? (
        <Typography.Paragraph size="sm" color="muted">
          Capture a second sample to draft the recipe.
        </Typography.Paragraph>
      ) : null}
      {proposal.isFetching ? <LoadingView label="Drafting the recipe" /> : null}
      <ProblemAlert problem={problem} />
    </div>
  );
}

/** Step 3 and 4: the samples' calls by endpoint, the main call chosen from them, and extra calls added. */
function CallSteps({
  ids,
  main,
  proposal,
  onPick,
  onAdd,
}: Readonly<{
  ids: readonly string[];
  main: Endpoint | null;
  proposal: ReturnType<typeof useProposal>;
  onPick: (endpoint: Endpoint) => void;
  /** Adds a group as an extra call; null while none can be added. */
  onAdd: ((endpoint: Endpoint) => void) | null;
}>) {
  const calls = useCallList(ids);
  const problem = problemOf(calls.error);
  const sampleOf: SampleNumber = (captureId) => ids.indexOf(captureId) + 1;
  const endpoints = calls.data === undefined ? [] : ownSiteFirst(calls.data.endpoints);
  const mainKey = main === null ? null : endpointKey(main);
  const chosen = endpoints.find((endpoint) => endpointKey(endpoint) === mainKey);
  return (
    <>
      <Section title="3. Calls">
        <Typography.Paragraph size="sm" color="muted">
          Every call the sample pages made, grouped by endpoint, the page&apos;s own site first,
          with the paths that read each field of the schema.
        </Typography.Paragraph>
        {calls.isPending ? <LoadingView label="Loading the calls" /> : null}
        <ProblemAlert problem={problem} />
        {calls.isSuccess && endpoints.length === 0 ? (
          <EmptyView title="No calls">
            The pages made no call the capture keeps (JSON, Flight or HTML fragments).
          </EmptyView>
        ) : null}
        <CallList
          endpoints={endpoints}
          samples={ids.length}
          sampleOf={sampleOf}
          main={mainKey}
          onPick={onPick}
          onAdd={onAdd}
        />
      </Section>
      <Section title="4. Main call">
        <MainCall chosen={chosen} samples={ids.length} proposal={proposal} />
      </Section>
    </>
  );
}

/** The first `call_<n>` the recipe has no call of. */
function newCallName(recipe: CustomRecipe): string {
  const taken = new Set(Object.keys(recipe.calls ?? {}));
  let n = 1;
  while (taken.has(`call_${String(n)}`)) n += 1;
  return `call_${String(n)}`;
}

const nothingToAdd: Problem = {
  title: "The call fills no field the recipe leaves unfilled.",
  detail: "An extra call reads the fields no other source reads; this one reads none of them.",
  errors: [],
  retryAfter: null,
};

/** The extra call a draft of another endpoint makes: its request, reading only the fields `recipe` has no source
 * for; null when it reads none of them. */
function extraCall(drafted: Proposal, recipe: CustomRecipe) {
  const filled = fieldSources(recipe);
  const map = Object.fromEntries(
    Object.entries(drafted.recipe.main.map).filter(([name]) => !filled.has(name)),
  );
  return Object.keys(map).length === 0 ? null : { ...drafted.recipe.main, map };
}

/** Steps 5 to 8 on the recipe as it stands: the proposal (or the stored recipe) with the engineer's edits over it. */
function RecipeSteps({
  host,
  recipe,
  proposal,
  fieldTypes,
  sampleUrls,
  addProblem,
  replaces,
  isDrafting,
  handlers,
}: Readonly<{
  host: string;
  recipe: CustomRecipe;
  proposal: Proposal | undefined;
  fieldTypes: FieldTypes;
  sampleUrls: readonly string[];
  addProblem: Problem | null;
  replaces: string | null;
  /** A new draft is being read: the recipe shown is the last one's, so it is neither tested nor saved. */
  isDrafting: boolean;
  handlers: EditHandlers;
}>) {
  const test = useTestRecipe();
  const fields = Object.keys(fieldTypes);
  return (
    <>
      <ReviewStep
        recipe={recipe}
        proposal={proposal}
        fieldTypes={fieldTypes}
        tested={test.data}
        handlers={handlers}
      />
      <ExtrasStep recipe={recipe} fields={fields} problem={addProblem} handlers={handlers} />
      <TestStep
        recipe={recipe}
        sampleUrls={sampleUrls}
        test={test}
        isDrafting={isDrafting}
        fieldTypes={fieldTypes}
      />
      <SaveStep
        host={host}
        recipe={recipe}
        replaces={replaces}
        tested={test.data}
        isDrafting={isDrafting}
        handlers={handlers}
      />
    </>
  );
}

/** The builder's steps for one schema, from the samples to saving. `stored` is the recipe edited (saved as the next
 * version of `replaces`), null for a new one; a main call chosen drafts the recipe anew, and the engineer's edits lie
 * over the draft, so a later draft never overwrites one. */
function Builder({
  host,
  schema,
  stored,
  replaces,
}: Readonly<{
  host: string;
  schema: SchemaDef;
  stored: WrittenRecipe | null;
  replaces: string | null;
}>) {
  const [samples, setSamples] = useState<readonly Sample[]>([]);
  const [main, setMain] = useState<Endpoint | null>(null);
  const [edits, setEdits] = useState<Edits>(NO_EDITS);
  const [addProblem, setAddProblem] = useState<Problem | null>(null);
  const ids = useMemo(() => samples.map(({ captureId }) => captureId), [samples]);
  const fieldTypes = useMemo(() => parseFieldTypes(schema.fields, schema.field_order), [schema]);
  const body = useMemo<ProposeBody | null>(
    () =>
      main === null || ids.length < MIN_SAMPLES
        ? null
        : { capture_ids: [...ids], main: endpointRef(main), schema_id: schema.id },
    [ids, main, schema.id],
  );
  const proposal = useProposal(body);
  const addCall = useProposeCall();
  const base = proposal.data?.recipe ?? stored;
  const recipe = base === null ? null : applied(base, edits);
  const isDrafting = proposal.isPlaceholderData || proposal.isFetching;
  // One extra call at a time, each on the draft it was asked for: two at once would read the same fields and name.
  const canAdd = recipe !== null && ids.length >= MIN_SAMPLES && !isDrafting && !addCall.isPending;
  const handlers: EditHandlers = {
    edits,
    onEdit: (part, value) => {
      setEdits((kept) => withEdit(kept, part, value));
    },
    onReset: (part) => {
      setEdits((kept) => withoutEdit(kept, part));
    },
  };
  const onAdd = (endpoint: Endpoint) => {
    if (!canAdd) return;
    setAddProblem(null);
    addCall.mutate(
      { capture_ids: [...ids], main: endpointRef(endpoint), schema_id: schema.id },
      {
        onSuccess: (drafted) => {
          const call = extraCall(drafted, recipe);
          if (call === null) setAddProblem(nothingToAdd);
          else handlers.onEdit(["calls", newCallName(recipe)], call);
        },
        onError: (error) => {
          setAddProblem(problemOf(error));
        },
      },
    );
  };
  return (
    <>
      <Section title="2. Sample pages">
        <Typography.Paragraph size="sm" color="muted">
          {samplesHelp(samples.length)}
        </Typography.Paragraph>
        <SampleList
          samples={samples}
          fieldTypes={fieldTypes}
          onRemove={(captureId) => {
            setSamples((kept) => kept.filter((sample) => sample.captureId !== captureId));
          }}
        />
        <SampleForm
          schema={schema}
          isFull={samples.length >= MAX_SAMPLES}
          onCaptured={(sample) => {
            setSamples((kept) => [...kept, sample]);
          }}
        />
      </Section>
      {samples.length === 0 ? null : (
        <CallSteps
          ids={ids}
          main={main}
          proposal={proposal}
          onPick={setMain}
          onAdd={canAdd ? onAdd : null}
        />
      )}
      {recipe === null ? null : (
        <RecipeSteps
          host={host}
          recipe={recipe}
          proposal={proposal.data}
          fieldTypes={fieldTypes}
          sampleUrls={samples.map(({ result }) => result.url)}
          addProblem={addProblem}
          replaces={replaces}
          isDrafting={isDrafting}
          handlers={handlers}
        />
      )}
    </>
  );
}

/** Building a custom recipe for the site, step by step: 1. the schema, 2. two to five sample pages captured in the
 * browser with their page steps and request context, each with its records, 3. their calls by endpoint, with where
 * each carries the schema's fields, a call opening in a drawer with its headers, body, answer and the HAR, 4. the
 * main call, which the server drafts a recipe from, 5. the draft reviewed and edited, 6. extra calls, a warm-up and
 * page fields, 7. a test on any URLs beside the normal extraction, and 8. saving. Calls, paths and headers are shown
 * here only (a stated exception to hiding internals). Changing the schema starts over: the samples were read for
 * the one chosen. */
export function RecipeBuilder() {
  const { host = "" } = useParams();
  const [schema, setSchema] = useState<SchemaDef | null>(null);
  return (
    <Screen
      title={`New recipe for ${host}`}
      actions={<Link href={recipesPath(host)}>All recipes</Link>}
    >
      <Section title="1. Schema">
        <SchemaSelect
          value={schema?.id ?? ""}
          onChange={(_id, chosen) => {
            setSchema(chosen);
          }}
        />
      </Section>
      {schema === null ? null : (
        <Builder key={schema.id} host={host} schema={schema} stored={null} replaces={null} />
      )}
    </Screen>
  );
}

/** An active custom recipe in the same builder: its stored values in place of a draft, saved as its next version.
 * Samples may be captured to choose another main call or add extra calls. */
export function RecipeEditor() {
  const { id = "" } = useParams();
  const config = useConfig(id);
  const schema = useSchema(config.data?.schema_id ?? null);
  const problem = problemOf(config.error) ?? problemOf(schema.error);
  if (problem !== null) return <ProblemAlert problem={problem} />;
  if (!config.isSuccess || !schema.isSuccess) return <LoadingView label="Loading the recipe" />;
  const { recipe, site } = config.data;
  return (
    <Screen
      title={`Edit recipe of ${site}`}
      actions={<Link href={recipePath(id)}>Back to the recipe</Link>}
    >
      {recipe === null ? (
        <EmptyView title="Not a custom recipe">
          A learned config is corrected on its config screen.
        </EmptyView>
      ) : (
        <Builder host={site} schema={schema.data} stored={recipe} replaces={id} />
      )}
    </Screen>
  );
}
