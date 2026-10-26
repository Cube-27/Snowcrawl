import { Button, Switch, Typography } from "@heroui/react";
import { Plus, TrashBin } from "@gravity-ui/icons";
import { useState } from "react";
import type { RecipeCall, RecipeHeader } from "../../api/recipes";
import { isOptionOf, OptionSelect } from "../../components/molecules/OptionSelect";
import { isEdited, type Edits, type Part } from "./draft";
import { EditedMark, Line, Lines } from "./Fields";

/** A value of the call changed at `part`, or reset to the proposal's. */
export interface EditHandlers {
  edits: Edits;
  onEdit: (part: Part, value: unknown) => void;
  onReset: (part: Part) => void;
}

const METHODS = { GET: "GET", POST: "POST" } as const;
const ANSWERS = { json: "JSON (read by JSONPath)", html: "HTML (read by XPath 1.0)" } as const;
const ENCODINGS = { json: "JSON", form: "Form (urlencoded)" } as const;

/** Where a parameter's value comes from (`api_recipe.model.BinderKind`): the page URL, the page's JSON documents, the
 * warm-up's answer, or, in an extra call, the main call's answer. */
const BINDERS = {
  pattern: "URL pattern parameter",
  path: "URL path segment (from 0)",
  query: "URL query parameter",
  regex: "URL regex (one group)",
  jsonld: "Page JSON-LD (JSONPath)",
  js_state: "Page script state (JSONPath)",
  warm_up: "Warm-up answer (JSONPath)",
  warm_up_xpath: "Warm-up answer (XPath)",
  warm_up_regex: "Warm-up answer (regex)",
  main: "Main call's answer (JSONPath)",
} as const;
type BinderKind = keyof typeof BINDERS;
const isBinderKind = isOptionOf(BINDERS);

/** A binder's text, `kind:arg`, split at its first colon. */
function binderParts(text: string): { kind: BinderKind | null; arg: string } {
  const at = text.indexOf(":");
  const kind = at < 0 ? text : text.slice(0, at);
  return { kind: isBinderKind(kind) ? kind : null, arg: at < 0 ? "" : text.slice(at + 1) };
}

/** The binders a call may use: a main binder only in an extra call, a warm-up one only with a warm-up. */
function binderOptions(isExtra: boolean, hasWarmUp: boolean): Partial<Record<BinderKind, string>> {
  return Object.fromEntries(
    Object.entries(BINDERS).filter(
      ([kind]) => (isExtra || kind !== "main") && (hasWarmUp || !kind.startsWith("warm_up")),
    ),
  );
}

function ParamRow({
  name,
  text,
  options,
  onChange,
}: Readonly<{
  name: string;
  text: string;
  options: Partial<Record<BinderKind, string>>;
  onChange: (text: string | undefined) => void;
}>) {
  const { kind, arg } = binderParts(text);
  return (
    <li className="flex flex-wrap items-end gap-2">
      <span className="w-32 text-code">{`{${name}}`}</span>
      <OptionSelect
        label={`Binder of ${name}`}
        options={options as Record<BinderKind, string>}
        value={kind}
        accepts={isBinderKind}
        onChange={(next) => {
          onChange(`${next}:${arg}`);
        }}
      />
      <Line
        label={`Argument of ${name}`}
        value={arg}
        onChange={(next) => {
          onChange(`${kind ?? "pattern"}:${next}`);
        }}
      />
      <Button
        size="sm"
        variant="ghost"
        aria-label={`Remove parameter ${name}`}
        onPress={() => {
          onChange(undefined);
        }}
      >
        <TrashBin aria-hidden="true" />
      </Button>
    </li>
  );
}

/** The call's `{name}` parameters, each with where its value comes from. */
function ParamsEditor({
  call,
  part,
  isExtra,
  hasWarmUp,
  handlers,
}: Readonly<{
  call: RecipeCall;
  part: Part;
  isExtra: boolean;
  hasWarmUp: boolean;
  handlers: EditHandlers;
}>) {
  const [added, setAdded] = useState("");
  const params = call.params_from_url ?? {};
  const options = binderOptions(isExtra, hasWarmUp);
  const at = [...part, "params_from_url"];
  return (
    <div className="flex flex-col gap-2">
      <Typography.Heading level={4}>
        Parameters{" "}
        <EditedMark
          label="the parameters"
          isEdited={isEdited(handlers.edits, at)}
          onReset={() => {
            handlers.onReset(at);
          }}
        />
      </Typography.Heading>
      <ul aria-label="Parameters" className="flex flex-col gap-2">
        {Object.entries(params).map(([name, text]) => (
          <ParamRow
            key={name}
            name={name}
            text={text}
            options={options}
            onChange={(next) => {
              handlers.onEdit([...at, name], next);
            }}
          />
        ))}
      </ul>
      <div className="flex items-end gap-2">
        <Line label="New parameter" value={added} placeholder="id" onChange={setAdded} />
        <Button
          size="sm"
          variant="secondary"
          isDisabled={added.trim() === ""}
          onPress={() => {
            handlers.onEdit([...at, added.trim()], "pattern:");
            setAdded("");
          }}
        >
          <Plus aria-hidden="true" />
          Add parameter
        </Button>
      </div>
    </div>
  );
}

function headerValue(header: RecipeHeader): string {
  return "value" in header ? header.value : "";
}

function HeaderRow({
  index,
  header,
  onChange,
}: Readonly<{
  index: number;
  header: RecipeHeader;
  onChange: (next: RecipeHeader | null) => void;
}>) {
  const fromContext = "from_context" in header;
  const label = `header ${String(index + 1)}`;
  return (
    <li className="flex flex-wrap items-end gap-2">
      <Line
        label={`Name of ${label}`}
        value={header.name}
        onChange={(name) => {
          onChange(
            fromContext ? { name, from_context: true } : { name, value: headerValue(header) },
          );
        }}
      />
      {fromContext ? (
        <Typography.Paragraph size="sm" className="flex-1">
          From the request context: the caller&apos;s value, never stored.
        </Typography.Paragraph>
      ) : (
        <Line
          label={`Value of ${label}`}
          value={headerValue(header)}
          onChange={(value) => {
            onChange({ name: header.name, value });
          }}
        />
      )}
      <Switch
        isSelected={fromContext}
        onChange={(isSelected) => {
          onChange(
            isSelected
              ? { name: header.name, from_context: true }
              : { name: header.name, value: "" },
          );
        }}
      >
        <Switch.Content>
          <Switch.Control>
            <Switch.Thumb />
          </Switch.Control>
          From context
        </Switch.Content>
      </Switch>
      <Button
        size="sm"
        variant="ghost"
        aria-label={`Remove ${label}`}
        onPress={() => {
          onChange(null);
        }}
      >
        <TrashBin aria-hidden="true" />
      </Button>
    </li>
  );
}

/** The headers the call sends, in order (RFC 9110 §5.3): a constant value, or the request context's. */
function HeadersEditor({
  call,
  part,
  handlers,
}: Readonly<{ call: RecipeCall; part: Part; handlers: EditHandlers }>) {
  const headers = call.headers ?? [];
  // The rows are the list's positions: a header has no identity of its own.
  const rows = headers.map((header, index) => ({ id: `header-${String(index)}`, index, header }));
  const at = [...part, "headers"];
  const set = (next: RecipeHeader[]) => {
    handlers.onEdit(at, next);
  };
  return (
    <div className="flex flex-col gap-2">
      <Typography.Heading level={4}>
        Headers{" "}
        <EditedMark
          label="the headers"
          isEdited={isEdited(handlers.edits, at)}
          onReset={() => {
            handlers.onReset(at);
          }}
        />
      </Typography.Heading>
      <ul aria-label="Headers" className="flex flex-col gap-2">
        {rows.map(({ id, index, header }) => (
          <HeaderRow
            key={id}
            index={index}
            header={header}
            onChange={(next) => {
              set(
                next === null
                  ? headers.filter((_, other) => other !== index)
                  : headers.map((kept, other) => (other === index ? next : kept)),
              );
            }}
          />
        ))}
      </ul>
      <div>
        <Button
          size="sm"
          variant="secondary"
          onPress={() => {
            set([...headers, { name: "", value: "" }]);
          }}
        >
          <Plus aria-hidden="true" />
          Add header
        </Button>
      </div>
    </div>
  );
}

/** The pagination as JSON (RFC 8259); text that is not JSON is not applied and says so. */
function PaginationEditor({
  call,
  part,
  handlers,
}: Readonly<{ call: RecipeCall; part: Part; handlers: EditHandlers }>) {
  const at = [...part, "pagination"];
  const shown = JSON.stringify(call.pagination ?? null, null, 2);
  // The text typed, while the pagination is the engineer's; otherwise the draft's, which a new proposal replaces.
  const [typed, setTyped] = useState<string | null>(null);
  const [isValid, setIsValid] = useState(true);
  const text = typed !== null && isEdited(handlers.edits, at) ? typed : shown;
  return (
    <div className="flex flex-col gap-2">
      <Lines
        label="Pagination (JSON; null for none)"
        value={text}
        onChange={(next) => {
          setTyped(next);
          try {
            handlers.onEdit(at, JSON.parse(next) as unknown);
            setIsValid(true);
          } catch {
            setIsValid(false);
          }
        }}
      />
      {isValid ? null : (
        <Typography.Paragraph size="sm" className="text-danger">
          Not JSON: the pagination is left as it was.
        </Typography.Paragraph>
      )}
      <EditedMark
        label="the pagination"
        isEdited={isEdited(handlers.edits, at)}
        onReset={() => {
          handlers.onReset(at);
          setTyped(null);
          setIsValid(true);
        }}
      />
    </div>
  );
}

/** Per schema field, where the call's answer holds it: a JSONPath (RFC 9535), or an XPath 1.0 for an HTML answer; empty
 * when the call does not read it. */
function MapEditor({
  call,
  part,
  fields,
  handlers,
}: Readonly<{ call: RecipeCall; part: Part; fields: readonly string[]; handlers: EditHandlers }>) {
  const at = [...part, "map"];
  return (
    <ul aria-label="Field map" className="flex flex-col gap-2">
      {fields.map((field) => (
        <li key={field} className="flex items-end gap-2">
          <Line
            label={`Path of ${field}`}
            value={call.map[field] ?? ""}
            placeholder={call.answer === "html" ? "//h1" : "$.data.title"}
            onChange={(path) => {
              handlers.onEdit([...at, field], path === "" ? undefined : path);
            }}
          />
          <EditedMark
            label={`the path of ${field}`}
            isEdited={isEdited(handlers.edits, [...at, field])}
            onReset={() => {
              handlers.onReset([...at, field]);
            }}
          />
        </li>
      ))}
    </ul>
  );
}

/** Every value of one call, editable: its method, URL template, answer kind, parameters, headers, body, pagination (the
 * main call) and field map. */
export function CallEditor({
  call,
  part,
  isExtra,
  hasWarmUp,
  fields,
  handlers,
}: Readonly<{
  call: RecipeCall;
  part: Part;
  isExtra: boolean;
  hasWarmUp: boolean;
  fields: readonly string[];
  handlers: EditHandlers;
}>) {
  const edit = (name: string) => (value: unknown) => {
    handlers.onEdit([...part, name], value);
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-2">
        <OptionSelect
          label="Method"
          options={METHODS}
          value={call.method}
          accepts={isOptionOf(METHODS)}
          onChange={edit("method")}
        />
        <Line label="URL" value={call.url} onChange={edit("url")} />
        <OptionSelect
          label="Answer"
          options={ANSWERS}
          value={call.answer ?? "json"}
          accepts={isOptionOf(ANSWERS)}
          onChange={edit("answer")}
        />
        <EditedMark
          label="the request"
          isEdited={["method", "url", "answer"].some((name) =>
            isEdited(handlers.edits, [...part, name]),
          )}
          onReset={() => {
            for (const name of ["method", "url", "answer"]) handlers.onReset([...part, name]);
          }}
        />
      </div>
      <ParamsEditor
        call={call}
        part={part}
        isExtra={isExtra}
        hasWarmUp={hasWarmUp}
        handlers={handlers}
      />
      <HeadersEditor call={call} part={part} handlers={handlers} />
      <div className="flex flex-wrap items-end gap-2">
        <Lines
          label="Body template (empty: no body)"
          value={call.body?.template ?? ""}
          onChange={(template) => {
            handlers.onEdit(
              [...part, "body"],
              template === "" ? null : { template, encoding: call.body?.encoding ?? "json" },
            );
          }}
        />
        {call.body == null ? null : (
          <OptionSelect
            label="Body encoding"
            options={ENCODINGS}
            value={call.body.encoding ?? "json"}
            accepts={isOptionOf(ENCODINGS)}
            onChange={(encoding) => {
              handlers.onEdit([...part, "body"], { template: call.body?.template ?? "", encoding });
            }}
          />
        )}
      </div>
      {isExtra ? null : <PaginationEditor call={call} part={part} handlers={handlers} />}
      <Typography.Heading level={4}>Field map</Typography.Heading>
      <MapEditor call={call} part={part} fields={fields} handlers={handlers} />
    </div>
  );
}
