import { describe, expect, it } from "vite-plus/test";
import {
  EMPTY_FORM,
  inputErrors,
  jsonOf,
  newSchemaOf,
  parseForm,
  type SchemaFormState,
} from "./schemaDocument";

const form: SchemaFormState = {
  name: " item_detail ",
  pageKind: "detail",
  requiredGroups: "title, price\n\n  sku ,  \n",
  fields: [
    { key: "a", name: " title ", spec: { type: "text", required: true, scope: "row" } },
    { key: "b", name: "price", spec: { type: "money", required: false, scope: "row" } },
    {
      key: "c",
      name: "tags",
      spec: {
        type: "list",
        required: false,
        scope: "row",
        item: { type: "text", required: false, scope: "row" },
      },
    },
  ],
};

describe("schema document: the request body", () => {
  it("is the document with trimmed names and one required group per non-empty line", () => {
    expect(newSchemaOf(form)).toEqual({
      name: "item_detail",
      page_kind: "detail",
      required_groups: [["title", "price"], ["sku"]],
      fields: {
        title: { type: "text", required: true, scope: "row" },
        price: { type: "money", required: false, scope: "row" },
        tags: {
          type: "list",
          required: false,
          scope: "row",
          item: { type: "text", required: false, scope: "row" },
        },
      },
    });
  });
});

describe("schema document: the JSON tab", () => {
  it("round-trips the form through its JSON", () => {
    const parsed = parseForm(jsonOf(form, "create"), "create", EMPTY_FORM);

    expect(parsed.kind === "ok" ? newSchemaOf(parsed.form) : parsed).toEqual(newSchemaOf(form));
  });

  it.each([
    ["not JSON", "{", "Enter the schema as a JSON object."],
    ["an array", "[]", "Enter the schema as a JSON object."],
    ["no name", '{"page_kind":"detail","fields":{}}', "`name` must be a string."],
    [
      "an unknown page kind",
      '{"name":"x","page_kind":"grid","fields":{}}',
      "`page_kind` must be detail or listing.",
    ],
  ])("refuses %s with the reason", (_, text, reason) => {
    expect(parseForm(text, "create", EMPTY_FORM)).toEqual({ kind: "invalid", reason });
  });
});

describe("schema document: server errors", () => {
  it("puts each error under the field row or input its path names", () => {
    const problem = {
      title: "The schema document is invalid",
      detail: null,
      retryAfter: null,
      errors: [
        { field: "fields.price.type", reason: "Input should be a valid type." },
        { field: "fields.price", reason: "money needs a currency." },
        { field: "name", reason: "String should match pattern." },
      ],
    };

    expect(inputErrors(problem)).toEqual({
      "fields.price": "Input should be a valid type. money needs a currency.",
      name: "String should match pattern.",
    });
    expect(inputErrors(null)).toEqual({});
  });
});
