import {
  Button,
  Description,
  FieldError,
  Form,
  Label,
  NumberField,
  Switch,
  toast,
} from "@heroui/react";
import { useState } from "react";
import { problemOf } from "../../api/hooks/query";
import { useProjectSettings, useSaveProjectSettings } from "../../api/hooks/project";
import type { Problem } from "../../api/problem";
import { FormActions } from "../../components/molecules/FormActions";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { Section } from "../../components/templates/Section";

/** What the form edits: the LLM call cap of a run whose request sets none, the draft call cap of an API key,
 * and whether a learned config waits for approval before it serves. */
type Edited = { llmCallCap: number | null; draftCallCap: number | null; requireApproval: boolean };

/** The project's limits (`/v1/project/settings`): the LLM call cap of a run whose request sets none, the uncharged
 * field-name drafts one API key may make, and the approval of learned configs. While the settings load, the same form
 * shows disabled, so loading moves nothing. */
export function LimitsSection() {
  const settings = useProjectSettings();
  const save = useSaveProjectSettings();
  return (
    <Section title="Limits">
      <LimitsForm
        // The form starts from the loaded values: it mounts again once the settings arrive.
        key={settings.isSuccess ? "loaded" : "loading"}
        saved={{
          llmCallCap: settings.data?.llm_call_cap ?? null,
          draftCallCap: settings.data?.draft_call_cap ?? null,
          requireApproval: settings.data?.require_config_approval ?? false,
        }}
        isLoading={!settings.isSuccess}
        problem={problemOf(settings.error) ?? problemOf(save.error)}
        isPending={save.isPending}
        onSave={({ llmCallCap, draftCallCap, requireApproval }) => {
          save.mutate(
            {
              llm_call_cap: llmCallCap,
              draft_call_cap: draftCallCap,
              require_config_approval: requireApproval,
            },
            {
              onSuccess: () => {
                toast.success("Saved the limits.");
              },
            },
          );
        }}
      />
    </Section>
  );
}

/** The largest cap the server stores: a PostgreSQL `integer` column, a 4-byte signed integer (PostgreSQL docs
 * §8.1.1). */
const MAX_CAP = 2_147_483_647;

/** A cap as a whole number of calls, at most `MAX_CAP`; empty (NaN) is no limit. */
function CapField({
  name,
  label,
  description,
  value,
  isDisabled,
  onChange,
}: Readonly<{
  name: string;
  label: string;
  description: string;
  value: number;
  isDisabled: boolean;
  onChange: (value: number) => void;
}>) {
  return (
    <NumberField
      name={name}
      value={value}
      minValue={0}
      maxValue={MAX_CAP}
      isDisabled={isDisabled}
      formatOptions={{ maximumFractionDigits: 0, useGrouping: false }}
      onChange={onChange}
    >
      <Label>{label}</Label>
      <NumberField.Group>
        <NumberField.DecrementButton />
        <NumberField.Input placeholder="No limit" />
        <NumberField.IncrementButton />
      </NumberField.Group>
      <Description>{description}</Description>
      <FieldError />
    </NumberField>
  );
}

function LimitsForm({
  saved,
  isLoading,
  problem,
  isPending,
  onSave,
}: Readonly<{
  saved: Edited;
  isLoading: boolean;
  problem: Problem | null;
  isPending: boolean;
  onSave: (edited: Edited) => void;
}>) {
  // NaN is an empty NumberField: no limit.
  const [llmCallCap, setLlmCallCap] = useState(saved.llmCallCap ?? Number.NaN);
  const [draftCallCap, setDraftCallCap] = useState(saved.draftCallCap ?? Number.NaN);
  const [requireApproval, setRequireApproval] = useState(saved.requireApproval);
  return (
    <Form
      className="flex flex-col gap-4 sm:max-w-md"
      onSubmit={(event) => {
        event.preventDefault();
        onSave({
          llmCallCap: Number.isNaN(llmCallCap) ? null : llmCallCap,
          draftCallCap: Number.isNaN(draftCallCap) ? null : draftCallCap,
          requireApproval,
        });
      }}
    >
      {problem === null ? null : <ProblemAlert problem={problem} />}
      <CapField
        name="llm_call_cap"
        label="LLM call limit per run"
        description="The most LLM calls a run may make when it sets no limit of its own. Empty: no limit."
        value={llmCallCap}
        isDisabled={isLoading}
        onChange={setLlmCallCap}
      />
      <CapField
        name="draft_call_cap"
        label="Draft call limit per key"
        description="The most field-name drafts one API key may make, counted in all, with no window. Empty: no limit."
        value={draftCallCap}
        isDisabled={isLoading}
        onChange={setDraftCallCap}
      />
      <div className="flex flex-col gap-1">
        <Switch
          name="require_config_approval"
          isSelected={requireApproval}
          isDisabled={isLoading}
          onChange={setRequireApproval}
        >
          <Switch.Content>
            <Switch.Control>
              <Switch.Thumb />
            </Switch.Control>
            <Label>Approve learned configs</Label>
          </Switch.Content>
        </Switch>
        <Description>
          A config learned for a site waits for your approval on its config page before it is used.
        </Description>
      </div>
      <FormActions>
        <Button type="submit" isPending={isPending} isDisabled={isLoading}>
          Save
        </Button>
      </FormActions>
    </Form>
  );
}
