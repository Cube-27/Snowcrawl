import { FieldError, ListBox, Select } from "@heroui/react";
import type { Key } from "react";
import type { ScrapeFormat } from "../../api/scrape";
import { FORMATS, orderedFormats } from "../../components/lib/formats";
import { HANGING_ERROR } from "../../components/molecules/OptionSelect";

/** A page format: every format but `records`, which a chosen schema asks for. */
export type PageFormat = Exclude<ScrapeFormat, "records">;

const PAGE_FORMATS = FORMATS.filter(
  (entry): entry is readonly [PageFormat, string] => entry[0] !== "records",
);

function pageFormats(keys: Key | Key[] | null): PageFormat[] {
  const chosen = (Array.isArray(keys) ? keys : []).map(String);
  return orderedFormats(chosen).filter((format): format is PageFormat => format !== "records");
}

/** The toolbar's Format: the page formats a scrape, batch or crawl asks for, several at once. Its name
 * is the request member a server error names. Its error line hangs under it without taking room, like the Schema
 * select's (`OptionSelect`), so a refused send moves nothing. */
export function FormatSelect({
  value,
  isDisabled = false,
  onChange,
}: Readonly<{
  value: readonly PageFormat[];
  isDisabled?: boolean;
  onChange: (formats: PageFormat[]) => void;
}>) {
  return (
    <Select
      className="relative"
      aria-label="Format"
      name="formats"
      selectionMode="multiple"
      placeholder="No page format"
      isDisabled={isDisabled}
      value={[...value]}
      onChange={(keys) => {
        onChange(pageFormats(keys));
      }}
    >
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox selectionMode="multiple">
          {PAGE_FORMATS.map(([format, text]) => (
            <ListBox.Item key={format} id={format} textValue={text}>
              {text}
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
      <FieldError className={HANGING_ERROR} />
    </Select>
  );
}
