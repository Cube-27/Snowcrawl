import { FieldError, Label, ListBox, Select } from "@heroui/react";
import type { Key } from "react";

/** The guard `accepts` takes for `options`: a key of its own, not one it inherits (such as `toString`). */
export function isOptionOf<K extends string>(options: Readonly<Record<K, string>>) {
  return (key: unknown): key is K => typeof key === "string" && Object.hasOwn(options, key);
}

/** The classes of a toolbar select's error line: it hangs under the select without taking room (so showing one moves
 * nothing), on one line from `sm` and in a column of `w-64` at phone width, where a long message would pass the
 * screen's edge. The select is `relative`. */
export const HANGING_ERROR = "absolute top-full w-64 sm:w-max sm:whitespace-nowrap";

/** A labelled HeroUI Select over a `Record` of option labels. With `hideLabel`, the label is its accessible name only,
 * for a toolbar, whose error line hangs under it without taking room, so showing one moves nothing around it.
 * `placeholder` is what it shows while nothing is chosen; `name` is the request member whose errors it
 * shows. */
export function OptionSelect<K extends string>({
  label,
  name,
  options,
  value,
  isRequired = false,
  isDisabled = false,
  hideLabel = false,
  placeholder,
  accepts,
  onChange,
}: Readonly<{
  label: string;
  name?: string;
  options: Readonly<Record<K, string>>;
  value: K | null;
  isRequired?: boolean;
  isDisabled?: boolean;
  hideLabel?: boolean;
  placeholder?: string;
  accepts: (key: unknown) => key is K;
  onChange: (value: K) => void;
}>) {
  return (
    <Select
      {...(hideLabel ? { className: "relative" } : {})}
      isRequired={isRequired}
      isDisabled={isDisabled}
      value={value}
      {...(hideLabel ? { "aria-label": label } : {})}
      {...(placeholder === undefined ? {} : { placeholder })}
      {...(name === undefined ? {} : { name })}
      onChange={(key: Key | null) => {
        if (accepts(key)) onChange(key);
      }}
    >
      {hideLabel ? null : <Label>{label}</Label>}
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {Object.entries<string>(options).map(([option, text]) => (
            <ListBox.Item key={option} id={option} textValue={text}>
              {text}
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
      <FieldError {...(hideLabel ? { className: HANGING_ERROR } : {})} />
    </Select>
  );
}
