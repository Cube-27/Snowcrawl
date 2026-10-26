import { Description, FieldError, Label, Radio, RadioGroup } from "@heroui/react";

/** The option of `options` whose value is `value`: RadioGroup reports a plain string. */
function pick<T extends string>(
  options: readonly (readonly [T, string])[],
  value: string,
): T | null {
  return options.find(([option]) => option === value)?.[0] ?? null;
}

/** One choice among `options` as a HeroUI RadioGroup; `name` is the request member a server error names. The
 * options in `disabled` can't be chosen, and `description` says why. */
export function Choices<T extends string>({
  label,
  options,
  value,
  isRequired = false,
  name,
  disabled = [],
  description,
  onChange,
}: Readonly<{
  label: string;
  options: readonly (readonly [T, string])[];
  value: T | null;
  isRequired?: boolean;
  name?: string;
  disabled?: readonly T[];
  description?: string;
  onChange: (value: T) => void;
}>) {
  return (
    <RadioGroup
      value={value}
      isRequired={isRequired}
      {...(name === undefined ? {} : { name })}
      orientation="horizontal"
      onChange={(next) => {
        const chosen = pick(options, next);
        if (chosen !== null) onChange(chosen);
      }}
    >
      <Label>{label}</Label>
      {options.map(([option, text]) => (
        <Radio key={option} value={option} isDisabled={disabled.includes(option)}>
          <Radio.Content>
            <Radio.Control>
              <Radio.Indicator />
            </Radio.Control>
            <Label>{text}</Label>
          </Radio.Content>
        </Radio>
      ))}
      {description === undefined ? null : <Description>{description}</Description>}
      <FieldError />
    </RadioGroup>
  );
}
