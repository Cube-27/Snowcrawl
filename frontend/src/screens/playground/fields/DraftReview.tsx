import { Typography } from "@heroui/react";
import { useState } from "react";
import { useFieldsDraft } from "../../../api/hooks/schemas";
import type { Problem } from "../../../api/problem";
import type { PageKind } from "../../../api/scrape";
import type { SchemaDef } from "../../../api/schemas";
import { QueryView } from "../../../components/molecules/ViewStates";
import { SchemaForm } from "../../../components/organisms/SchemaForm";
import { formOf } from "../../../components/lib/schemaDocument";
import { reviewOf, type ReviewedSchema } from "./review";
import { SideDrawer } from "../../../components/molecules/SideDrawer";

/** The drafted schema in the schema editor; Run sends what `reviewOf` makes of it. */
function ReviewForm({
  draft,
  fields,
  onRun,
  onCancel,
}: Readonly<{
  draft: SchemaDef;
  fields: readonly string[];
  onRun: (schema: ReviewedSchema) => void;
  onCancel: () => void;
}>) {
  const [form, setForm] = useState(() => formOf(draft));
  const [refusal, setRefusal] = useState<Problem | null>(null);
  return (
    <SchemaForm
      form={form}
      mode="create"
      problem={refusal}
      isPending={false}
      submitLabel="Run"
      onChange={(next) => {
        setForm(next);
        setRefusal(null);
      }}
      onSubmit={(submitted) => {
        const review = reviewOf(draft, submitted, fields);
        if (review.kind === "refused") setRefusal(review.problem);
        else onRun(review.schema);
      }}
      onCancel={onCancel}
    />
  );
}

/** The schema the server drafts from `fields` for `pageKind` pages, in a drawer from the right, for review before the
 * run: Run sends it as drafted, or as edited; closing the drawer sends nothing. */
export function DraftReview({
  fields,
  pageKind,
  onRun,
  onClose,
}: Readonly<{
  fields: readonly string[];
  pageKind: PageKind;
  onRun: (schema: ReviewedSchema) => void;
  onClose: () => void;
}>) {
  const draft = useFieldsDraft(fields, pageKind);
  const body = (
    <QueryView query={draft} loading="Drafting the schema">
      {(data) => <ReviewForm draft={data} fields={fields} onRun={onRun} onCancel={onClose} />}
    </QueryView>
  );
  return (
    <SideDrawer
      title="Review the drafted schema"
      header={
        <Typography.Paragraph size="sm" color="muted">
          Drafted from {fields.length} field {fields.length === 1 ? "name" : "names"} for {pageKind}{" "}
          pages. Run it as drafted, or edit it first: an edited schema runs inline under a name of
          its own.
        </Typography.Paragraph>
      }
      onClose={onClose}
    >
      {body}
    </SideDrawer>
  );
}
