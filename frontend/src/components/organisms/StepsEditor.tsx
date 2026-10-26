import { Button, Fieldset, Input, Label, TextField } from "@heroui/react";
import { FIELD_TYPES, isFieldType } from "../lib/schemaDocument";
import { OptionSelect } from "../molecules/OptionSelect";
import {
  emptyPostOp,
  emptyStep,
  isJsonPathSource,
  isPostOpKind,
  isStepKind,
  JSONPATH_SOURCES,
  POST_OP_KINDS,
  STEP_KINDS,
  type DraftStep,
  type PostOp,
} from "../lib/steps";

/** `list` with the item at `index` replaced by `item`. */
function replaced<T>(list: readonly T[], index: number, item: T): T[] {
  return list.map((current, at) => (at === index ? item : current));
}

/** `list` with the items at `index` and `index + by` swapped; unchanged when that leaves the list. */
function moved<T>(list: readonly T[], index: number, by: -1 | 1): T[] {
  const other = index + by;
  const a = list[index];
  const b = list[other];
  if (a === undefined || b === undefined) return [...list];
  return replaced(replaced(list, index, b), other, a);
}

function Text({
  label,
  value,
  onChange,
}: Readonly<{ label: string; value: string; onChange: (value: string) => void }>) {
  return (
    <TextField value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input autoComplete="off" spellCheck={false} />
    </TextField>
  );
}

function PostOpFields({ op, onChange }: Readonly<{ op: PostOp; onChange: (op: PostOp) => void }>) {
  switch (op.op) {
    case "normalize":
      return (
        <OptionSelect
          label="Field type"
          options={FIELD_TYPES}
          value={op.type}
          accepts={isFieldType}
          onChange={(type) => {
            onChange({ ...op, type });
          }}
        />
      );
    case "regex_replace":
      return (
        <>
          <Text
            label="Pattern"
            value={op.pattern}
            onChange={(pattern) => {
              onChange({ ...op, pattern });
            }}
          />
          <Text
            label="Replacement"
            value={op.repl}
            onChange={(repl) => {
              onChange({ ...op, repl });
            }}
          />
        </>
      );
    default:
      return null;
  }
}

/** A step's post operations, in the order they run. */
function PostOpsEditor({
  ops,
  onChange,
}: Readonly<{ ops: readonly PostOp[]; onChange: (ops: PostOp[]) => void }>) {
  return (
    <Fieldset>
      <Fieldset.Legend>Post operations</Fieldset.Legend>
      {ops.map((op, index) => (
        <div key={op.id} className="flex flex-wrap items-end gap-3">
          <OptionSelect
            label={`Operation ${String(index + 1)}`}
            options={POST_OP_KINDS}
            value={op.op}
            accepts={isPostOpKind}
            onChange={(kind) => {
              // A new kind keeps the operation's row.
              onChange(replaced(ops, index, { ...emptyPostOp(kind), id: op.id }));
            }}
          />
          <PostOpFields
            op={op}
            onChange={(next) => {
              onChange(replaced(ops, index, next));
            }}
          />
          <Button
            variant="ghost"
            onPress={() => {
              onChange(ops.filter((_, at) => at !== index));
            }}
          >
            Remove operation {index + 1}
          </Button>
        </div>
      ))}
      <div>
        <Button
          variant="secondary"
          onPress={() => {
            onChange([...ops, emptyPostOp("trim")]);
          }}
        >
          Add operation
        </Button>
      </div>
    </Fieldset>
  );
}

function KindFields({
  step,
  onChange,
}: Readonly<{ step: DraftStep; onChange: (step: DraftStep) => void }>) {
  if (step.kind === "jsonld") {
    return (
      <Text
        label="schema.org type (optional)"
        value={step.whenType}
        onChange={(whenType) => {
          onChange({ ...step, whenType });
        }}
      />
    );
  }
  if (step.kind === "jsonpath") {
    return (
      <OptionSelect
        label="Source"
        options={JSONPATH_SOURCES}
        value={step.source}
        accepts={isJsonPathSource}
        onChange={(source) => {
          onChange({ ...step, source });
        }}
      />
    );
  }
  return null;
}

function StepRow({
  step,
  position,
  count,
  onChange,
  onMove,
  onRemove,
}: Readonly<{
  step: DraftStep;
  position: number;
  count: number;
  onChange: (step: DraftStep) => void;
  onMove: (by: -1 | 1) => void;
  onRemove: () => void;
}>) {
  const name = `step ${String(position)}`;
  return (
    <Fieldset>
      <Fieldset.Legend>Step {position}</Fieldset.Legend>
      <OptionSelect
        label="Kind"
        options={STEP_KINDS}
        value={step.kind}
        accepts={isStepKind}
        onChange={(kind) => {
          onChange({ ...step, kind });
        }}
      />
      <Text
        label={STEP_KINDS[step.kind]}
        value={step.expression}
        onChange={(expression) => {
          onChange({ ...step, expression });
        }}
      />
      <KindFields step={step} onChange={onChange} />
      <PostOpsEditor
        ops={step.post}
        onChange={(post) => {
          onChange({ ...step, post });
        }}
      />
      <Fieldset.Actions>
        <Button
          variant="ghost"
          isDisabled={position === 1}
          aria-label={`Move ${name} up`}
          onPress={() => {
            onMove(-1);
          }}
        >
          Move up
        </Button>
        <Button
          variant="ghost"
          isDisabled={position === count}
          aria-label={`Move ${name} down`}
          onPress={() => {
            onMove(1);
          }}
        >
          Move down
        </Button>
        <Button variant="danger-soft" aria-label={`Remove ${name}`} onPress={onRemove}>
          Remove
        </Button>
      </Fieldset.Actions>
    </Fieldset>
  );
}

/** A field's config steps as an ordered list: the first step with a valid value wins. It checks nothing itself; the server validates the steps when the correction is submitted. */
export function StepsEditor({
  steps,
  onChange,
}: Readonly<{ steps: readonly DraftStep[]; onChange: (steps: DraftStep[]) => void }>) {
  return (
    <div className="flex flex-col gap-4">
      {steps.map((step, index) => (
        <StepRow
          key={step.id}
          step={step}
          position={index + 1}
          count={steps.length}
          onChange={(next) => {
            onChange(replaced(steps, index, next));
          }}
          onMove={(by) => {
            onChange(moved(steps, index, by));
          }}
          onRemove={() => {
            onChange(steps.filter((_, at) => at !== index));
          }}
        />
      ))}
      <div>
        <Button
          variant="secondary"
          onPress={() => {
            onChange([...steps, emptyStep()]);
          }}
        >
          Add step
        </Button>
      </div>
    </div>
  );
}
