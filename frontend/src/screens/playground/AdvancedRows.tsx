import { Button, Description, Input, Label, TextField, Typography } from "@heroui/react";
import { ArrowDown, ArrowUp, Plus, TrashBin } from "@gravity-ui/icons";
import { useId } from "react";
import type { CacheMode } from "../../api/scrape";
import { isOptionOf, OptionSelect } from "../../components/molecules/OptionSelect";
import {
  ACTION_MODES,
  MAX_ACTIONS,
  newAction,
  type ActionMode,
  type ActionRow,
  type ActionType,
  type AdvancedFetch,
} from "./advanced";
import { SelectRow, TextAreaRow, TextRow } from "./OptionsPanel";

const CACHE_MODES: readonly (readonly [CacheMode, string])[] = [
  ["default", "Reuse within max age"],
  ["bypass", "Always fetch"],
  ["only", "Stored capture only"],
];

const ACTION_TYPES: Readonly<Record<ActionType, string>> = {
  wait: "Wait",
  click: "Click",
  scroll: "Scroll",
  press: "Press key",
  type: "Type",
};
const isActionType = isOptionOf(ACTION_TYPES);

const MODE_LABELS: Readonly<Record<ActionMode, string>> = {
  ms: "Milliseconds",
  selector: "CSS selector",
  network_idle: "Network idle",
  text: "Visible text",
  bottom: "To the bottom",
  by: "By pixels",
  stable: "Until no growth",
  key: "Key",
};
const isMode = isOptionOf(MODE_LABELS);

/** What a mode's value field holds; null for a mode that takes no value. */
const VALUE_PLACEHOLDERS: Readonly<Record<ActionMode, string | null>> = {
  ms: "1000",
  selector: "button.more",
  network_idle: null,
  text: "Show more",
  bottom: null,
  by: "800",
  stable: null,
  key: "Enter",
};

/** The Advanced options: a section across the panel after the common settings, shown at once like
 * every setting, with the caller's request context, the cache mode and the
 * actions editor, each sent as its `fetch` member while set. */
export function AdvancedRows({
  value,
  onChange,
}: Readonly<{ value: AdvancedFetch; onChange: (value: AdvancedFetch) => void }>) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      className="col-span-full flex flex-col gap-4 border-t border-border pt-4"
    >
      <Typography.Heading level={3} id={headingId}>
        Advanced
      </Typography.Heading>
      <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-4">
        <TextAreaRow
          label="Headers"
          help="One Name: value per line, sent over HTTP to the page's own host only (never in the browser). Values are secrets: never stored or shown again."
          name="fetch.headers"
          value={value.headers}
          placeholder="Authorization: Bearer …"
          onChange={(headers) => {
            onChange({ ...value, headers });
          }}
        />
        <TextAreaRow
          label="Cookies"
          help="One name=value per line (or separated by ;), sent to the page's own host over HTTP and in the browser. Values are secrets: never stored or shown again."
          name="fetch.cookies"
          value={value.cookies}
          placeholder="session=…"
          onChange={(cookies) => {
            onChange({ ...value, cookies });
          }}
        />
        <TextRow
          label="Proxy pool"
          help="The name of a proxy pool the server is configured with. Empty: the site's pool, if any."
          name="fetch.proxy_pool"
          value={value.proxyPool}
          placeholder="Pool name"
          onChange={(proxyPool) => {
            onChange({ ...value, proxyPool });
          }}
        />
        <TextRow
          label="Wait for"
          help="Before the page is read in the browser: milliseconds, a CSS selector to appear, or network_idle."
          name="fetch.wait_for"
          value={value.waitFor}
          placeholder="2000, .price or network_idle"
          onChange={(waitFor) => {
            onChange({ ...value, waitFor });
          }}
        />
        <SelectRow<CacheMode>
          label="Cache"
          help="Reuse a stored capture within max age, always fetch, or only serve a stored capture (fails when there is none)."
          name="fetch.cache"
          options={CACHE_MODES}
          value={value.cache}
          onChange={(cache) => {
            onChange({ ...value, cache });
          }}
        />
        <ActionRows
          rows={value.actions}
          onChange={(actions) => {
            onChange({ ...value, actions });
          }}
        />
      </div>
    </section>
  );
}

/** The actions editor: the page steps in order, each a kind, how it names its target and the target; added up to the
 * server's cap, moved up or down, removed. A row missing its value is not sent. */
export function ActionRows({
  rows,
  onChange,
}: Readonly<{ rows: readonly ActionRow[]; onChange: (rows: ActionRow[]) => void }>) {
  const replace = (key: string, next: ActionRow) => {
    onChange(rows.map((row) => (row.key === key ? next : row)));
  };
  const move = (index: number, by: number) => {
    const next = [...rows];
    const [row] = next.splice(index, 1);
    if (row !== undefined) next.splice(index + by, 0, row);
    onChange(next);
  };
  return (
    <fieldset className="col-span-full flex flex-col gap-2">
      <legend className="contents">
        <Label>Actions</Label>
      </legend>
      {rows.map((row, index) => (
        <ActionLine
          key={row.key}
          row={row}
          n={index + 1}
          isFirst={index === 0}
          isLast={index === rows.length - 1}
          onChange={(next) => {
            replace(row.key, next);
          }}
          onMove={(by) => {
            move(index, by);
          }}
          onRemove={() => {
            onChange(rows.filter(({ key }) => key !== row.key));
          }}
        />
      ))}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Description>
          Run in the browser, in order, before the page is read. Never logins: typing into a
          password field is refused.
        </Description>
        <Button
          size="sm"
          variant="secondary"
          isDisabled={rows.length >= MAX_ACTIONS}
          onPress={() => {
            onChange([...rows, newAction("click")]);
          }}
        >
          <Plus aria-hidden="true" />
          Add step
        </Button>
      </div>
    </fieldset>
  );
}

function ActionLine({
  row,
  n,
  isFirst,
  isLast,
  onChange,
  onMove,
  onRemove,
}: Readonly<{
  row: ActionRow;
  n: number;
  isFirst: boolean;
  isLast: boolean;
  onChange: (row: ActionRow) => void;
  onMove: (by: number) => void;
  onRemove: () => void;
}>) {
  const step = String(n);
  const modes = ACTION_MODES[row.type];
  const placeholder = VALUE_PLACEHOLDERS[row.mode];
  const modeOptions = Object.fromEntries(modes.map((mode) => [mode, MODE_LABELS[mode]])) as Record<
    ActionMode,
    string
  >;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <OptionSelect
        label={`Step ${step} kind`}
        hideLabel
        options={ACTION_TYPES}
        value={row.type}
        accepts={isActionType}
        onChange={(type) => {
          onChange({ ...newAction(type), key: row.key });
        }}
      />
      {modes.length > 1 ? (
        <OptionSelect
          label={`Step ${step} target`}
          hideLabel
          options={modeOptions}
          value={row.mode}
          accepts={isMode}
          onChange={(mode) => {
            onChange({ ...row, mode, value: "" });
          }}
        />
      ) : null}
      {placeholder === null ? null : (
        <TextField
          aria-label={`Step ${step} ${MODE_LABELS[row.mode]}`}
          className="min-w-0 flex-1"
          value={row.value}
          onChange={(value) => {
            onChange({ ...row, value });
          }}
        >
          <Input autoComplete="off" spellCheck={false} placeholder={placeholder} />
        </TextField>
      )}
      {row.type === "type" ? (
        <TextField
          aria-label={`Step ${step} text`}
          className="min-w-0 flex-1"
          value={row.text}
          onChange={(text) => {
            onChange({ ...row, text });
          }}
        >
          <Input autoComplete="off" spellCheck={false} placeholder="Text to type" />
        </TextField>
      ) : null}
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label={`Move step ${step} up`}
        isDisabled={isFirst}
        onPress={() => {
          onMove(-1);
        }}
      >
        <ArrowUp aria-hidden="true" />
      </Button>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label={`Move step ${step} down`}
        isDisabled={isLast}
        onPress={() => {
          onMove(1);
        }}
      >
        <ArrowDown aria-hidden="true" />
      </Button>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label={`Remove step ${step}`}
        onPress={onRemove}
      >
        <TrashBin aria-hidden="true" />
      </Button>
    </div>
  );
}
