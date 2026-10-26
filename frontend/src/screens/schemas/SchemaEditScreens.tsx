import { toast } from "@heroui/react";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { problemOf } from "../../api/hooks/query";
import { useCreateSchema, useSchema, useUpdateSchema } from "../../api/hooks/schemas";
import type { Problem } from "../../api/problem";
import type { SchemaDef } from "../../api/schemas";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { SchemaForm } from "../../components/organisms/SchemaForm";
import {
  contentOf,
  EMPTY_FORM,
  formOf,
  newSchemaOf,
  type FormMode,
  type SchemaFormState,
} from "../../components/lib/schemaDocument";
import { schemaPath } from "./paths";

/** Opens the schema the server stored, with a confirmation; `note` follows the name. */
function useOpenSaved(note = "") {
  const navigate = useNavigate();
  return ({ id, name }: SchemaDef) => {
    toast.success(`Saved ${name}.${note}`);
    void navigate(schemaPath(id));
  };
}

/** A schema form whose submit goes to `onSave`, with the save's state shown. */
function SchemaEditor({
  title,
  description,
  mode,
  initial,
  isPending,
  problem,
  onSave,
  cancelTo,
}: Readonly<{
  title: string;
  description?: string;
  mode: FormMode;
  /** The schema the form starts from; null for a blank one. */
  initial: SchemaDef | null;
  isPending: boolean;
  problem: Problem | null;
  onSave: (form: SchemaFormState) => void;
  cancelTo: string;
}>) {
  const navigate = useNavigate();
  const [form, setForm] = useState(() => (initial === null ? EMPTY_FORM : formOf(initial)));
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title={title} description={description} />
      <SchemaForm
        form={form}
        mode={mode}
        problem={problem}
        isPending={isPending}
        onChange={setForm}
        onSubmit={onSave}
        onCancel={() => void navigate(cancelTo)}
      />
    </section>
  );
}

/** A new `user` schema, blank or, with `?from=<id>`, prefilled from that schema's document: adopting a
 * prompt draft creates a user schema from it. The name starts empty, since a schema's name is taken by that
 * schema and the server refuses it with 409 (UAT F32). */
export function NewSchemaScreen() {
  const from = useSearchParams()[0].get("from");
  const source = useSchema(from);
  const create = useCreateSchema();
  const openSaved = useOpenSaved();
  const loadProblem = problemOf(source.error);

  if (loadProblem !== null) return <ProblemAlert problem={loadProblem} />;
  if (from !== null && source.data === undefined) {
    return <LoadingView label="Loading the schema" />;
  }
  const initial = source.data ?? null;
  return (
    <SchemaEditor
      title="New schema"
      mode="create"
      initial={initial === null ? null : { ...initial, name: "" }}
      isPending={create.isPending}
      problem={problemOf(create.error)}
      onSave={(form) => {
        const body = newSchemaOf(form);
        if (body !== null) create.mutate(body, { onSuccess: openSaved });
      }}
      cancelTo={initial === null ? "/schemas" : schemaPath(initial.id)}
    />
  );
}

/** A schema's whole document, edited in place; the name stays. Saving suspends the schema's site
 * configs, so their sites learn again. */
export function EditSchemaScreen() {
  const { id = "" } = useParams();
  const schema = useSchema(id);
  const update = useUpdateSchema(id);
  const openSaved = useOpenSaved(" Its site configs are learning again.");
  const loadProblem = problemOf(schema.error);

  if (loadProblem !== null) return <ProblemAlert problem={loadProblem} />;
  if (schema.data === undefined) return <LoadingView label="Loading the schema" />;
  const { name } = schema.data;
  return (
    <SchemaEditor
      title={`Edit ${name}`}
      description="Saving updates this schema in place. Its learned site configs go back to learning."
      mode="edit"
      initial={schema.data}
      isPending={update.isPending}
      problem={problemOf(update.error)}
      onSave={(form) => {
        const body = contentOf(form);
        if (body !== null) update.mutate(body, { onSuccess: openSaved });
      }}
      cancelTo={schemaPath(id)}
    />
  );
}
