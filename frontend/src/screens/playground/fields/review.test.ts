import { describe, expect, it } from "vite-plus/test";
import type { SchemaDef } from "../../../api/schemas";
import { formOf } from "../../../components/lib/schemaDocument";
import { reviewOf } from "./review";
import { fieldSpec } from "../../../test/schemas";

const draft: SchemaDef = {
  id: "0192a000-0000-7000-8000-0000000000d1",
  name: "draft_0a1b2c3d4e5f",
  origin: "prompt_draft",
  status: "active",
  page_kind: "detail",
  required_groups: [["title"]],
  field_order: ["title", "price"],
  labels: null,
  fields: {
    title: fieldSpec({ type: "text", required: true, scope: "row" }),
    price: fieldSpec({ type: "money", required: false, scope: "row" }),
  },
};

describe("reviewOf", () => {
  it("runs an unchanged draft as its field names, so the request is the one Get code shows", () => {
    expect(reviewOf(draft, formOf(draft), ["title", "price"])).toEqual({
      kind: "run",
      schema: { fields: ["title", "price"] },
    });
  });

  it("runs an edited draft under a name of its own as an inline schema", () => {
    const form = { ...formOf(draft), name: "my_items", requiredGroups: "" };

    expect(reviewOf(draft, form, ["title", "price"])).toEqual({
      kind: "run",
      schema: {
        inline: {
          name: "my_items",
          page_kind: "detail",
          required_groups: [],
          fields: {
            title: fieldSpec({ type: "text", required: true, scope: "row" }),
            price: fieldSpec({ type: "money", required: false, scope: "row" }),
          },
        },
      },
    });
  });
});
