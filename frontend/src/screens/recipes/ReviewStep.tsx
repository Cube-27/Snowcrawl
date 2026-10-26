import { Chip, Typography } from "@heroui/react";
import type { FieldTypes } from "../../api/field";
import type { CustomRecipe, Proposal, RecipeTestResult } from "../../api/recipes";
import { FieldValue } from "../../components/molecules/FieldValue";
import { Section } from "../../components/templates/Screen";
import { CallEditor, type EditHandlers } from "./CallEditor";
import { fieldSources, isEdited, sourceText } from "./draft";
import { EditedMark, Lines } from "./Fields";

/** The URL patterns, one per line: `example.com/item/{id}` (`config_ir.url_pattern`). */
function PatternsEditor({
  patterns,
  handlers,
}: Readonly<{ patterns: readonly string[]; handlers: EditHandlers }>) {
  return (
    <div className="flex flex-col gap-2">
      <Lines
        label="URL patterns (one per line)"
        value={patterns.join("\n")}
        placeholder="example.com/item/{id}"
        onChange={(text) => {
          handlers.onEdit(
            ["patterns"],
            text
              .split("\n")
              .map((line) => line.trim())
              .filter((line) => line !== ""),
          );
        }}
      />
      <EditedMark
        label="the patterns"
        isEdited={isEdited(handlers.edits, ["patterns"])}
        onReset={() => {
          handlers.onReset(["patterns"]);
        }}
      />
    </div>
  );
}

/** What the proposal could not settle: request values nothing on the pages binds, and the headers it left out. */
function ProposalNotes({ proposal }: Readonly<{ proposal: Proposal }>) {
  const notes = [
    ...proposal.unbound.map((value) => `Not bound: ${value}`),
    ...proposal.withheld_headers.map((name) => `Header left out: ${name}`),
  ];
  if (notes.length === 0) return null;
  return (
    <ul aria-label="Proposal notes" className="flex flex-col gap-1 text-sm">
      {notes.map((note) => (
        <li key={note}>{note}</li>
      ))}
    </ul>
  );
}

/** One field's value on each URL the recipe was tested on: the first record's. */
function TestedValues({
  field,
  tested,
  fieldTypes,
}: Readonly<{ field: string; tested: RecipeTestResult | undefined; fieldTypes: FieldTypes }>) {
  if (tested === undefined) return null;
  return (
    <ul className="flex flex-col gap-1 text-sm" aria-label={`Values of ${field}`}>
      {tested.urls.map(({ url, recipe }) => {
        const value = recipe.records[0]?.fields[field];
        return (
          <li key={url} className="flex flex-wrap gap-2">
            <span className="min-w-0 truncate text-muted">{url}</span>
            {value === undefined ? (
              <span>—</span>
            ) : (
              <FieldValue field={value} spec={fieldTypes[field] ?? null} />
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Every field of the schema: where the recipe reads it (one source; two are refused when saved, none leaves it
 * unfilled, and a page field that needs a render is flagged) and, once tested, its value on each URL. */
function FieldSources({
  recipe,
  fieldTypes,
  tested,
}: Readonly<{
  recipe: CustomRecipe;
  fieldTypes: FieldTypes;
  tested: RecipeTestResult | undefined;
}>) {
  const sources = fieldSources(recipe);
  return (
    <ul aria-label="Field sources" className="flex flex-col gap-3">
      {Object.keys(fieldTypes).map((field) => {
        const found = sources.get(field) ?? [];
        return (
          <li key={field} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{field}</span>
              {found.length === 0 ? <Chip size="sm">Unfilled</Chip> : null}
              {found.length > 1 ? (
                <Chip size="sm" color="danger">
                  More than one source
                </Chip>
              ) : null}
              {found.some(
                (source) => source.kind === "page" && source.field.needs === "browser",
              ) ? (
                <Chip size="sm" color="warning">
                  Needs a render
                </Chip>
              ) : null}
            </div>
            {found.map((source) => (
              <span key={sourceText(source)} className="text-code break-all">
                {sourceText(source)}
              </span>
            ))}
            <TestedValues field={field} tested={tested} fieldTypes={fieldTypes} />
          </li>
        );
      })}
    </ul>
  );
}

/** Step 5: the draft reviewed, every value editable: the URL patterns, the main call's request, binders, headers,
 * pagination and field map; each field's source and value. An edit is the engineer's and outlasts a new proposal. */
export function ReviewStep({
  recipe,
  proposal,
  fieldTypes,
  tested,
  handlers,
}: Readonly<{
  recipe: CustomRecipe;
  proposal: Proposal | undefined;
  fieldTypes: FieldTypes;
  tested: RecipeTestResult | undefined;
  handlers: EditHandlers;
}>) {
  return (
    <Section title="5. Review">
      {proposal === undefined ? null : <ProposalNotes proposal={proposal} />}
      <PatternsEditor patterns={recipe.patterns} handlers={handlers} />
      <Typography.Heading level={3}>Main call</Typography.Heading>
      <CallEditor
        call={recipe.main}
        part={["main"]}
        isExtra={false}
        hasWarmUp={recipe.warm_up != null}
        fields={Object.keys(fieldTypes)}
        handlers={handlers}
      />
      <Typography.Heading level={3}>Fields</Typography.Heading>
      <FieldSources recipe={recipe} fieldTypes={fieldTypes} tested={tested} />
    </Section>
  );
}
