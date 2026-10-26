import {
  Alert,
  Button,
  Checkbox,
  ErrorMessage,
  Fieldset,
  Form,
  Label,
  Link,
  toast,
} from "@heroui/react";
import { useState } from "react";
import { configVersionLabel, type ConfigDetail } from "../../api/configs";
import { useCorrectConfig } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import type { FieldProblem } from "../../api/problem";
import { FieldConfigStatusLabel } from "../../components/molecules/ConfigStatusLabel";
import { FormActions } from "../../components/molecules/FormActions";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { StepsEditor } from "../../components/organisms/StepsEditor";
import { correctionOf, fieldDrafts, type FieldDraft } from "./correction";
import { configPath } from "./paths";
import { ValidationReportView } from "./ValidationReportView";

/** The server's reasons for one field: the problem names request members as `fields.<name>…` or `confirm.<name>`. */
function reasonsFor(name: string, errors: readonly FieldProblem[]): string[] {
  const members = [`fields.${name}`, `confirm.${name}`];
  return errors
    .filter(({ field }) =>
      members.some((member) => field === member || field.startsWith(`${member}.`)),
    )
    .map(({ field, reason }) => `${field}: ${reason}`);
}

function FieldCorrection({
  draft,
  reasons,
  onChange,
}: Readonly<{
  draft: FieldDraft;
  reasons: readonly string[];
  onChange: (draft: FieldDraft) => void;
}>) {
  return (
    <Fieldset>
      <Fieldset.Legend>{draft.name}</Fieldset.Legend>
      {draft.stored === null ? null : (
        <div className="flex flex-wrap items-center gap-3">
          <FieldConfigStatusLabel status={draft.stored.status} />
          <Checkbox
            isSelected={draft.confirm}
            onChange={(confirm) => {
              onChange({ ...draft, confirm });
            }}
          >
            <Checkbox.Content>
              <Checkbox.Control>
                <Checkbox.Indicator />
              </Checkbox.Control>
              <Label>Confirm the steps of {draft.name}</Label>
            </Checkbox.Content>
          </Checkbox>
        </div>
      )}
      <StepsEditor
        steps={draft.steps}
        onChange={(steps) => {
          onChange({ ...draft, steps });
        }}
      />
      {reasons.map((reason) => (
        <ErrorMessage key={reason}>{reason}</ErrorMessage>
      ))}
    </Fieldset>
  );
}

/** The version a correction stored, with the server's validation of it. */
function CorrectionResult({ saved }: Readonly<{ saved: ConfigDetail }>) {
  return (
    <section className="flex flex-col gap-4" aria-label="Correction result">
      <Alert status={saved.status === "active" ? "success" : "warning"}>
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Title>
            Saved as {configVersionLabel(saved)}, {saved.status}.
          </Alert.Title>
          <Alert.Description>
            <Link href={configPath(saved.id)}>Open {configVersionLabel(saved)}</Link>
          </Alert.Description>
        </Alert.Content>
      </Alert>
      <ValidationReportView report={saved.validation_report} />
    </section>
  );
}

/** The operator's correction of a config (C5): steps per schema field and confirmations, sent as `PUT
 * /v1/configs/{id}`. The server validates it on the held-out pages and answers with the new version. */
export function CorrectionForm({
  config,
  schemaFields,
}: Readonly<{ config: ConfigDetail; schemaFields: readonly string[] }>) {
  const [drafts, setDrafts] = useState(() => fieldDrafts(config, schemaFields));
  const correct = useCorrectConfig(config.id);
  const problem = problemOf(correct.error);
  const errors = problem?.errors ?? [];

  return (
    <div className="flex flex-col gap-6">
      <Form
        className="flex flex-col gap-6"
        onSubmit={(event) => {
          event.preventDefault();
          correct.mutate(correctionOf(drafts), {
            onSuccess: (saved) => {
              toast.success(`Saved ${configVersionLabel(saved)}.`);
            },
          });
        }}
      >
        {problem === null ? null : <ProblemAlert problem={problem} />}
        {drafts.map((draft, index) => (
          <FieldCorrection
            key={draft.name}
            draft={draft}
            reasons={reasonsFor(draft.name, errors)}
            onChange={(next) => {
              setDrafts(drafts.map((current, at) => (at === index ? next : current)));
            }}
          />
        ))}
        <FormActions>
          <Button type="submit" isPending={correct.isPending}>
            Submit correction
          </Button>
        </FormActions>
      </Form>
      {correct.isSuccess ? <CorrectionResult saved={correct.data} /> : null}
    </div>
  );
}
