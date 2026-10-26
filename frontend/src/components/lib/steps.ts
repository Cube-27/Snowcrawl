import type { CorrectedStep, IrStep } from "../../api/configs";
import { isJsonObject } from "../../api/json";
import type { FieldType } from "../../api/schemas";
import { isFieldType } from "./schemaDocument";

/** A config IR step as the StepsEditor edits it (config learning steps). The editor keeps the
 * members of every kind, so switching kinds loses nothing typed; `toCorrectedStep` sends only the chosen kind's. The
 * UI never checks a selector: the server validates the steps when they are submitted. */
export type StepKind = "xpath" | "css" | "jsonld" | "jsonpath" | "regex" | "attr";

/** Where a `jsonpath` step reads. */
export type JsonPathSource = Extract<CorrectedStep, { jsonpath: string }>["source"];

/** An editor-only identity, the React key of a step or post operation: it keeps its row when another one moves or is
 * removed. It is never sent (`toCorrectedStep`). */
let lastId = 0;
function newId(): string {
  lastId += 1;
  return `draft-${String(lastId)}`;
}

/** A post operation, run on the step's value in order. */
export type PostOp = { id: string } & (
  | { op: "trim" }
  | { op: "absolutize_url" }
  | { op: "normalize"; type: FieldType }
  | { op: "regex_replace"; pattern: string; repl: string }
);

export type PostOpKind = PostOp["op"];

export interface DraftStep {
  id: string;
  kind: StepKind;
  /** The selector, path, pattern or attribute name of the kind. */
  expression: string;
  /** `jsonld` only: the schema.org type the node must have; empty for any. */
  whenType: string;
  /** `jsonpath` only. */
  source: JsonPathSource;
  post: PostOp[];
}

export const STEP_KINDS: Readonly<Record<StepKind, string>> = {
  xpath: "XPath",
  css: "CSS selector",
  jsonld: "JSON-LD path",
  jsonpath: "JSONPath",
  regex: "Regex",
  attr: "Attribute",
};

export const JSONPATH_SOURCES: Readonly<Record<JsonPathSource, string>> = {
  jsonld: "JSON-LD",
  js_state: "Script state",
  attr: "JSON attribute",
  rsc: "Flight payload",
  xhr: "XHR response",
  api: "API response",
};

export const POST_OP_KINDS: Readonly<Record<PostOpKind, string>> = {
  trim: "Trim",
  absolutize_url: "Absolutize URL",
  normalize: "Normalize",
  regex_replace: "Regex replace",
};

export function isStepKind(value: unknown): value is StepKind {
  return typeof value === "string" && Object.hasOwn(STEP_KINDS, value);
}

export function isJsonPathSource(value: unknown): value is JsonPathSource {
  return typeof value === "string" && Object.hasOwn(JSONPATH_SOURCES, value);
}

export function isPostOpKind(value: unknown): value is PostOpKind {
  return typeof value === "string" && Object.hasOwn(POST_OP_KINDS, value);
}

/** A new step of `kind` with nothing typed yet. */
export function emptyStep(kind: StepKind = "xpath"): DraftStep {
  return { id: newId(), kind, expression: "", whenType: "", source: "jsonld", post: [] };
}

/** A new post operation of `op`. */
export function emptyPostOp(op: PostOpKind): PostOp {
  const id = newId();
  switch (op) {
    case "normalize":
      return { id, op, type: "text" };
    case "regex_replace":
      return { id, op, pattern: "", repl: "" };
    default:
      return { id, op };
  }
}

type IrPostOp = IrStep["post"][number];

function regexReplaceOf(op: IrPostOp): { pattern: string; repl: string } | null {
  const params: unknown = "regex_replace" in op ? op.regex_replace : null;
  if (!isJsonObject(params)) return null;
  const { pattern, repl } = params;
  return typeof pattern === "string" && typeof repl === "string" ? { pattern, repl } : null;
}

/** The editor's form of a stored post operation. The IR's operations are a closed set (`config_ir.model`): a normalize
 * operation is stored as `{"<field type>": {}}`. */
function postOpOf(op: IrPostOp): PostOp | null {
  const replace = regexReplaceOf(op);
  if (replace !== null) return { id: newId(), op: "regex_replace", ...replace };
  const [name] = Object.keys(op);
  if (name === "trim" || name === "absolutize_url") return { id: newId(), op: name };
  return isFieldType(name) ? { id: newId(), op: "normalize", type: name } : null;
}

function kindAndExpression(step: IrStep): Pick<DraftStep, "kind" | "expression"> {
  // A CSS step also carries its XPath translation, so it is told apart first.
  if ("css" in step) return { kind: "css", expression: step.css };
  if ("xpath" in step) return { kind: "xpath", expression: step.xpath };
  if ("jsonld" in step) return { kind: "jsonld", expression: step.jsonld };
  if ("jsonpath" in step) return { kind: "jsonpath", expression: step.jsonpath };
  if ("regex" in step) return { kind: "regex", expression: step.regex };
  return { kind: "attr", expression: step.attr };
}

/** The editor's form of a step a config stores. */
export function draftOf(step: IrStep): DraftStep {
  return {
    ...emptyStep(),
    ...kindAndExpression(step),
    whenType: "when_type" in step ? (step.when_type ?? "") : "",
    source: "source" in step ? step.source : "jsonld",
    post: step.post.map(postOpOf).filter((op) => op !== null),
  };
}

type CorrectedPostOp = NonNullable<CorrectedStep["post"]>[number];

function correctedPostOp(op: PostOp): CorrectedPostOp {
  switch (op.op) {
    case "trim":
      return { trim: {} };
    case "absolutize_url":
      return { absolutize_url: {} };
    case "normalize":
      // The server tells post operations apart by their one key (`_post_op_kind` in `config_ir/model.py`), a field
      // type for a normalize operation; `{type}`, the generated input type's form, is refused there.
      return { [op.type]: {} } as CorrectedPostOp;
    case "regex_replace":
      return { regex_replace: { pattern: op.pattern, repl: op.repl } };
  }
}

/** The step as `PUT /v1/configs/{id}` takes it: only the members of its kind. */
export function toCorrectedStep(step: DraftStep): CorrectedStep {
  const post = step.post.map(correctedPostOp);
  const { expression } = step;
  switch (step.kind) {
    case "xpath":
      return { xpath: expression, post };
    case "css":
      // The server translates `css` to the XPath the executors run (`CssStep`'s validator); the generated input type
      // lists `xpath` as required only because the stored model has it.
      return { css: expression, post } as CorrectedStep;
    case "jsonld":
      return { jsonld: expression, when_type: step.whenType === "" ? null : step.whenType, post };
    case "jsonpath":
      return { jsonpath: expression, source: step.source, post };
    case "regex":
      return { regex: expression, post };
    case "attr":
      return { attr: expression, post };
  }
}

/** Whether two step lists send the same steps. */
export function sameSteps(a: readonly DraftStep[], b: readonly DraftStep[]): boolean {
  return JSON.stringify(a.map(toCorrectedStep)) === JSON.stringify(b.map(toCorrectedStep));
}

/** A step in one line, as a field's steps are listed: its kind and its expression. */
export function stepText(step: IrStep): string {
  const { kind, expression } = kindAndExpression(step);
  return `${kind}: ${expression}`;
}
