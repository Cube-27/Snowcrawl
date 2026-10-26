import { Button, Form, InputGroup, Label, TextField } from "@heroui/react";
import { Magnifier } from "@gravity-ui/icons";
import { useState } from "react";

/** One field and its button on a single row, to open something by its key (a job id, a site host). The submitted
 * value is trimmed; the button is off until there is one, so the field needs no required mark. */
export function LookupForm({
  label,
  placeholder,
  submitLabel,
  onOpen,
}: Readonly<{
  label: string;
  placeholder: string;
  submitLabel: string;
  onOpen: (value: string) => void;
}>) {
  const [value, setValue] = useState("");
  return (
    <Form
      className="flex w-full max-w-md items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        // Enter in an empty field sends the form too.
        if (value.trim() !== "") onOpen(value.trim());
      }}
    >
      <TextField className="min-w-0 flex-1" value={value} onChange={setValue}>
        <Label>{label}</Label>
        <InputGroup>
          <InputGroup.Prefix>
            <Magnifier aria-hidden="true" />
          </InputGroup.Prefix>
          <InputGroup.Input placeholder={placeholder} autoComplete="off" spellCheck={false} />
        </InputGroup>
      </TextField>
      <Button type="submit" variant="secondary" isDisabled={value.trim() === ""}>
        {submitLabel}
      </Button>
    </Form>
  );
}
