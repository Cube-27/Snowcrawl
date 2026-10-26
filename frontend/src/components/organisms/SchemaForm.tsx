import {
  Button,
  Description,
  FieldError,
  Fieldset,
  Form,
  Input,
  Label,
  Tabs,
  TextArea,
  TextField,
} from "@heroui/react";
import { useMemo, useState, type Key } from "react";
import type { Problem } from "../../api/problem";
import { Choices } from "../molecules/Choices";
import { FormActions } from "../molecules/FormActions";
import { ProblemAlert } from "../molecules/ViewStates";
import { SchemaFieldEditor } from "./SchemaFieldEditor";
import {
  inputErrors,
  isDuplicateName,
  jsonOf,
  newRow,
  PAGE_KIND_OPTIONS,
  parseForm,
  type FieldRow,
  type FormMode,
  type SchemaFormState,
} from "../lib/schemaDocument";

type View = "form" | "json";

interface FormProps {
  form: SchemaFormState;
  mode: FormMode;
  onChange: (form: SchemaFormState) => void;
}

function DocumentInputs({ form, mode, onChange }: Readonly<FormProps>) {
  return (
    <>
      {mode === "create" ? (
        <TextField
          isRequired
          name="name"
          value={form.name}
          onChange={(name) => {
            onChange({ ...form, name });
          }}
        >
          <Label>Name</Label>
          <Input autoComplete="off" />
          <Description>A name no other schema has.</Description>
          <FieldError />
        </TextField>
      ) : null}
      <Choices
        label="Page kind"
        name="page_kind"
        options={PAGE_KIND_OPTIONS}
        value={form.pageKind}
        isRequired
        onChange={(pageKind) => {
          onChange({ ...form, pageKind });
        }}
      />
      <TextField
        name="required_groups"
        value={form.requiredGroups}
        onChange={(requiredGroups) => {
          onChange({ ...form, requiredGroups });
        }}
      >
        <Label>Required groups</Label>
        <TextArea rows={3} />
        <Description>One group per line, its field names separated by commas.</Description>
        <FieldError />
      </TextField>
    </>
  );
}

function FieldRows({ form, onChange }: Readonly<Omit<FormProps, "mode">>) {
  const setFields = (fields: readonly FieldRow[]) => {
    onChange({ ...form, fields });
  };
  return (
    <Fieldset>
      <Fieldset.Legend>Fields</Fieldset.Legend>
      {form.fields.map((row, index) => (
        <SchemaFieldEditor
          key={row.key}
          row={row}
          index={index}
          isDuplicate={(name) => isDuplicateName(form, row.key, name)}
          onChange={(next) => {
            setFields(form.fields.map((other) => (other.key === row.key ? next : other)));
          }}
          onRemove={() => {
            setFields(form.fields.filter((other) => other.key !== row.key));
          }}
        />
      ))}
      <div>
        <Button
          variant="secondary"
          onPress={() => {
            setFields([...form.fields, newRow()]);
          }}
        >
          Add field
        </Button>
      </div>
    </Fieldset>
  );
}

/** The JSON tab's text and why it can't be read into the form, if it can't. */
interface JsonText {
  text: string;
  error: string | null;
}

/** A schema document edited as a form or as JSON. The two tabs edit one document: leaving the JSON tab
 * reads it back into the form, and stays when it can't. A problem from the API shows above the inputs, and each of
 * its errors next to the input it names. `onSubmit` gets the document; the server validates it. The submit button
 * reads `submitLabel`. */
export function SchemaForm({
  form,
  mode,
  problem,
  isPending,
  submitLabel = "Save",
  onChange,
  onSubmit,
  onCancel,
}: Readonly<
  FormProps & {
    problem: Problem | null;
    isPending: boolean;
    submitLabel?: string;
    onSubmit: (form: SchemaFormState) => void;
    onCancel: () => void;
  }
>) {
  const [view, setView] = useState<View>("form");
  const [json, setJson] = useState<JsonText>({ text: "", error: null });
  const validationErrors = useMemo(() => inputErrors(problem), [problem]);

  /** The form the JSON tab holds, or null after showing why it can't be read. */
  const readJson = (): SchemaFormState | null => {
    const parsed = parseForm(json.text, mode, form);
    if (parsed.kind === "invalid") {
      setJson({ ...json, error: parsed.reason });
      return null;
    }
    onChange(parsed.form);
    return parsed.form;
  };

  const switchView = (key: Key) => {
    if (key === view) return;
    if (key === "json") {
      setJson({ text: jsonOf(form, mode), error: null });
      setView("json");
    } else if (readJson() !== null) {
      setView("form");
    }
  };

  const submit = () => {
    if (view === "form") {
      onSubmit(form);
      return;
    }
    const current = readJson();
    if (current === null) return;
    // The form tab requires a page kind; the JSON tab can hold null, which has no request yet.
    if (current.pageKind === null) {
      setJson({ ...json, error: "`page_kind` must be detail or listing." });
      return;
    }
    onSubmit(current);
  };

  return (
    <Form
      className="flex flex-col gap-4"
      validationErrors={validationErrors}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <ProblemAlert problem={problem} />
      <Tabs selectedKey={view} onSelectionChange={switchView}>
        <Tabs.ListContainer>
          <Tabs.List aria-label="Schema editor">
            <Tabs.Tab id="form">
              Form
              <Tabs.Indicator />
            </Tabs.Tab>
            <Tabs.Tab id="json">
              JSON
              <Tabs.Indicator />
            </Tabs.Tab>
          </Tabs.List>
        </Tabs.ListContainer>
        <Tabs.Panel id="form" className="flex flex-col gap-4">
          <DocumentInputs form={form} mode={mode} onChange={onChange} />
          <FieldRows form={form} onChange={onChange} />
        </Tabs.Panel>
        <Tabs.Panel id="json">
          <TextField
            value={json.text}
            isInvalid={json.error !== null}
            onChange={(text) => {
              setJson({ text, error: null });
            }}
          >
            <Label>Schema JSON</Label>
            <TextArea rows={20} />
            <FieldError>{json.error}</FieldError>
          </TextField>
        </Tabs.Panel>
      </Tabs>
      <FormActions>
        <Button variant="secondary" onPress={onCancel}>
          Cancel
        </Button>
        <Button type="submit" isPending={isPending}>
          {submitLabel}
        </Button>
      </FormActions>
    </Form>
  );
}
