import type { CustomRecipe, PageField, RecipeCall, WrittenRecipe } from "../../api/recipes";

/** Where a value sits in a recipe: its members' names from the top, `["main", "map", "title"]`. */
export type Part = readonly string[];

/** The engineer's edits of a recipe, by part, in the order made; `undefined` removes the part. They lie over the
 * draft the server proposed and are applied anew to every later proposal, so a proposal never overwrites one. */
export type Edits = ReadonlyMap<string, { part: Part; value: unknown }>;

export const NO_EDITS: Edits = new Map();

const keyOf = (part: Part) => JSON.stringify(part);

function isUnder(part: Part, prefix: Part): boolean {
  return prefix.length <= part.length && prefix.every((name, index) => part[index] === name);
}

/** `edits` with `part` set to `value`; an edit inside `part` is replaced by this one. */
export function withEdit(edits: Edits, part: Part, value: unknown): Edits {
  const kept = [...edits].filter(([, edit]) => !isUnder(edit.part, part));
  return new Map([...kept, [keyOf(part), { part, value }]]);
}

/** `edits` without the edits of `part` or inside it: the proposal's value shows again. */
export function withoutEdit(edits: Edits, part: Part): Edits {
  return new Map([...edits].filter(([, edit]) => !isUnder(edit.part, part)));
}

export function isEdited(edits: Edits, part: Part): boolean {
  return [...edits.values()].some((edit) => isUnder(edit.part, part));
}

type Tree = Record<string, unknown>;

function isTree(value: unknown): value is Tree {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function setIn(tree: Tree, part: Part, value: unknown): Tree {
  const [name, ...rest] = part;
  if (name === undefined) return tree;
  if (rest.length === 0) {
    const { [name]: _removed, ...others } = tree;
    return value === undefined ? others : { ...others, [name]: value };
  }
  const child = tree[name];
  return { ...tree, [name]: setIn(isTree(child) ? child : {}, rest, value) };
}

/** `base` with every edit applied in the order made, as the request body it is sent as. */
export function applied(base: CustomRecipe | WrittenRecipe, edits: Edits): CustomRecipe {
  let tree: Tree = { ...base };
  for (const { part, value } of edits.values()) tree = setIn(tree, part, value);
  return tree as unknown as CustomRecipe;
}

/** Where a recipe reads one field: the main call's answer, an extra call's, or the page itself. */
export type Source =
  | { kind: "main"; path: string }
  | { kind: "call"; callName: string; path: string }
  | { kind: "page"; field: PageField };

function callMap(call: RecipeCall | undefined): Readonly<Record<string, string>> {
  return call?.map ?? {};
}

/** Each field's sources in the recipe; the server refuses a field with more than one. */
export function fieldSources(recipe: CustomRecipe): ReadonlyMap<string, readonly Source[]> {
  const found = new Map<string, Source[]>();
  const add = (name: string, source: Source) => {
    found.set(name, [...(found.get(name) ?? []), source]);
  };
  for (const [name, path] of Object.entries(callMap(recipe.main)))
    add(name, { kind: "main", path });
  for (const [call, recipeCall] of Object.entries(recipe.calls ?? {})) {
    for (const [name, path] of Object.entries(callMap(recipeCall)))
      add(name, { kind: "call", callName: call, path });
  }
  for (const [name, field] of Object.entries(recipe.fields ?? {}))
    add(name, { kind: "page", field });
  return found;
}

/** A page step as people read it: an XPath as written, a JSON-LD path with its type, any other step as JSON. */
function stepText(step: PageField["steps"][number]): string {
  if ("xpath" in step) return step.xpath;
  if ("jsonld" in step) {
    const type = step.when_type == null ? "" : ` of type ${step.when_type}`;
    return `JSON-LD ${step.jsonld}${type}`;
  }
  return JSON.stringify(step);
}

/** A source as people read it: "main call at $.data.price", "call stock at $.qty", "page: //h1 (needs a render)". */
export function sourceText(source: Source): string {
  if (source.kind === "main") return `main call at ${source.path}`;
  if (source.kind === "call") return `call ${source.callName} at ${source.path}`;
  const steps = source.field.steps.map(stepText);
  const render = source.field.needs === "browser" ? " (needs a render)" : "";
  return `page: ${steps.join(", then ")}${render}`;
}
