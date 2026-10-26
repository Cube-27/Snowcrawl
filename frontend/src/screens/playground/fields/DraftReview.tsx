import { Drawer, Typography } from "@heroui/react";
import { useState } from "react";
import { problemOf } from "../../../api/hooks/query";
import { useFieldsDraft } from "../../../api/hooks/schemas";
import type { Problem } from "../../../api/problem";
import type { PageKind } from "../../../api/scrape";
import type { SchemaDef } from "../../../api/schemas";
import { LoadingView } from "../../../components/molecules/LoadingView";
import { ProblemAlert } from "../../../components/molecules/ProblemAlert";
import { SchemaForm } from "../../../components/organisms/SchemaForm";
import { formOf } from "../../../components/lib/schemaDocument";
import { reviewOf, type ReviewedSchema } from "./review";

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
  const problem = problemOf(draft.error);
  let body = <LoadingView label="Drafting the schema" />;
  if (problem !== null) body = <ProblemAlert problem={problem} />;
  else if (draft.data !== undefined)
    body = <ReviewForm draft={draft.data} fields={fields} onRun={onRun} onCancel={onClose} />;
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement="right">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>Review the drafted schema</Drawer.Heading>
              <Typography.Paragraph size="sm" color="muted">
                Drafted from {fields.length} field {fields.length === 1 ? "name" : "names"} for{" "}
                {pageKind} pages. Run it as drafted, or edit it first: an edited schema runs inline
                under a name of its own.
              </Typography.Paragraph>
            </Drawer.Header>
            <Drawer.Body>{body}</Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}
