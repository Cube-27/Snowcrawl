import { Xmark } from "@gravity-ui/icons";
import { Button, DateField, Description, Form, Input, Label, TextField } from "@heroui/react";
import { parseAbsolute, toZoned, today, type ZonedDateTime } from "@internationalized/date";
import { useMemo, useState } from "react";
import type { RecordFilters } from "../../api/records";
import { FormActions } from "../../components/molecules/FormActions";
import { SchemaSelect } from "../../components/molecules/SchemaSelect";

function OptionalText({
  label,
  description,
  value,
  onChange,
}: Readonly<{
  label: string;
  description: string;
  value: string;
  onChange: (value: string) => void;
}>) {
  return (
    <TextField value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input autoComplete="off" />
      <Description>{description}</Description>
    </TextField>
  );
}

// The time is entered and shown in UTC, so the instant sent is the one on screen whatever the viewer's zone.
const ZONE = "UTC";

// An instant from the address that is not ISO 8601 with an offset shows an empty field; the filter is still sent,
// and the API's problem says what is wrong with it.
function zonedOf(instant: string): ZonedDateTime | null {
  try {
    return parseAbsolute(instant, ZONE);
  } catch {
    return null;
  }
}

/** The start of the change feed: records stored from this instant on, as ISO 8601; "" for none. */
function ChangedSinceField({
  value,
  onChange,
}: Readonly<{ value: string; onChange: (instant: string) => void }>) {
  // The field compares its value by identity: a new one each render would reset a date half typed in.
  const zoned = useMemo(() => zonedOf(value), [value]);
  const [placeholder] = useState(() => toZoned(today(ZONE), ZONE));
  return (
    <DateField
      value={zoned}
      onChange={(next) => {
        onChange(next === null ? "" : next.toAbsoluteString());
      }}
      granularity="minute"
      hourCycle={24}
      placeholderValue={placeholder}
    >
      <Label>Changed since</Label>
      <DateField.Group>
        <DateField.Input>{(segment) => <DateField.Segment segment={segment} />}</DateField.Input>
        {value === "" ? null : (
          <DateField.Suffix>
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label="Clear changed since"
              onPress={() => {
                onChange("");
              }}
            >
              <Xmark aria-hidden="true" />
            </Button>
          </DateField.Suffix>
        )}
      </DateField.Group>
      <Description>Only new records and new versions stored from this time on, in UTC.</Description>
    </DateField>
  );
}

/** The `GET /v1/records` filters. The schema is required; the others narrow the list when given. */
export function RecordFiltersForm({
  filters,
  onSubmit,
}: Readonly<{ filters: RecordFilters; onSubmit: (filters: RecordFilters) => void }>) {
  const [draft, setDraft] = useState(filters);
  const set = (name: keyof RecordFilters) => (value: string) => {
    setDraft({ ...draft, [name]: value });
  };
  return (
    <Form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit(draft);
      }}
    >
      {/* A list's filters: two columns from `md`, so the list starts higher on the page. */}
      <div className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2">
        <SchemaSelect value={draft.schema_id} onChange={set("schema_id")} />
        <OptionalText
          label="Site"
          description="Only the records of this host."
          value={draft.site}
          onChange={set("site")}
        />
        <OptionalText
          label="Job ID"
          description="Only the records this job extracted."
          value={draft.job_id}
          onChange={set("job_id")}
        />
        <ChangedSinceField value={draft.changed_since} onChange={set("changed_since")} />
      </div>
      <FormActions>
        <Button type="submit">Show records</Button>
      </FormActions>
    </Form>
  );
}
