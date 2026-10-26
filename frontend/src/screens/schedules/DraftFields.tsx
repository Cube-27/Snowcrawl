import { FieldError, Input, Label, NumberField, TextField } from "@heroui/react";
import { Choices } from "../../components/molecules/Choices";
import { MIN_MINUTES, type ScheduleDraft } from "./cadence";

const EVERY = [
  ["interval", "Interval"],
  ["cron", "Cron expression"],
] as const;

/** The viewer's IANA time zone (ECMA-402 `resolvedOptions().timeZone`), the cron expression's default zone. */
export function viewerZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The fields of a schedule's name and cadence, shared by Schedule this and Edit. */
export function DraftFields({
  draft,
  errors,
  onChange,
}: Readonly<{
  draft: ScheduleDraft;
  errors: Readonly<Record<string, string>>;
  onChange: (draft: ScheduleDraft) => void;
}>) {
  return (
    <>
      <TextField
        name="name"
        isInvalid={"name" in errors}
        value={draft.name}
        onChange={(name) => {
          onChange({ ...draft, name });
        }}
      >
        <Label>Name</Label>
        <Input autoComplete="off" />
        <FieldError>{errors["name"]}</FieldError>
      </TextField>
      <Choices
        label="Runs"
        options={EVERY}
        value={draft.every}
        onChange={(every) => {
          onChange({ ...draft, every });
        }}
      />
      {draft.every === "interval" ? (
        <NumberField
          name="interval_s"
          isInvalid={"interval_s" in errors}
          value={draft.minutes}
          minValue={MIN_MINUTES}
          formatOptions={{ maximumFractionDigits: 0, useGrouping: false }}
          onChange={(minutes) => {
            onChange({ ...draft, minutes });
          }}
        >
          <Label>Every (minutes)</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input />
            <NumberField.IncrementButton />
          </NumberField.Group>
          <FieldError>{errors["interval_s"]}</FieldError>
        </NumberField>
      ) : (
        <TextField
          name="cron"
          isInvalid={"cron" in errors}
          value={draft.cron}
          onChange={(cron) => {
            onChange({ ...draft, cron });
          }}
        >
          <Label>Cron expression (minute hour day month weekday)</Label>
          <Input autoComplete="off" spellCheck={false} placeholder="0 6 * * 1-5" />
          <FieldError>{errors["cron"]}</FieldError>
        </TextField>
      )}
      <TextField
        name="timezone"
        isInvalid={"timezone" in errors}
        value={draft.timezone}
        onChange={(timezone) => {
          onChange({ ...draft, timezone });
        }}
      >
        <Label>Time zone</Label>
        <Input autoComplete="off" spellCheck={false} />
        <FieldError>{errors["timezone"]}</FieldError>
      </TextField>
    </>
  );
}
