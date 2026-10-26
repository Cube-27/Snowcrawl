import { Button, Chip, Input, Label, TextArea, TextField } from "@heroui/react";
import { ArrowRotateLeft } from "@gravity-ui/icons";

/** Says a value is the engineer's, not the proposal's; "Reset" shows the proposal's again. Nothing without an edit. */
export function EditedMark({
  label,
  isEdited,
  onReset,
}: Readonly<{ label: string; isEdited: boolean; onReset: () => void }>) {
  if (!isEdited) return null;
  return (
    <span className="inline-flex items-center gap-1">
      <Chip size="sm" color="accent">
        Edited
      </Chip>
      <Button size="sm" variant="ghost" aria-label={`Reset ${label}`} onPress={onReset}>
        <ArrowRotateLeft aria-hidden="true" />
        Reset
      </Button>
    </span>
  );
}

/** One line of free text with its label, in a builder row. */
export function Line({
  label,
  value,
  placeholder = "",
  isMono = true,
  onChange,
}: Readonly<{
  label: string;
  value: string;
  placeholder?: string;
  isMono?: boolean;
  onChange: (value: string) => void;
}>) {
  return (
    <TextField className="flex min-w-0 flex-1 flex-col gap-1" value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        className={isMono ? "text-code" : ""}
      />
    </TextField>
  );
}

/** Free text over several lines with its label. */
export function Lines({
  label,
  value,
  placeholder = "",
  onChange,
  onBlur,
}: Readonly<{
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
}>) {
  return (
    <TextField
      className="flex min-w-0 flex-col gap-1"
      value={value}
      onChange={onChange}
      {...(onBlur === undefined ? {} : { onBlur })}
    >
      <Label>{label}</Label>
      <TextArea rows={4} spellCheck={false} placeholder={placeholder} className="text-code" />
    </TextField>
  );
}
