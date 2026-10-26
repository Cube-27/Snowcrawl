import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vite-plus/test";
import type { FieldTypes } from "../../api/field";
import type { ExtractedRecord } from "../../api/records";
import { recordField } from "../../test/records";
import { ExtractedRecords } from "./ExtractedRecords";

const fieldTypes: FieldTypes = {
  title: { type: "text" },
  price: { type: "money" },
  variants: { type: "list", item: { type: "text" } },
};

function product(n: number): ExtractedRecord {
  return {
    complete: true,
    confidence: 0.9,
    mode: "adhoc",
    fields: {
      variants: recordField({ value: ["S", "M", "L"] }),
      price: recordField({ value: { amount: "9.90", currency: "EUR" } }),
      title: recordField({ value: `Product ${String(n)}` }),
    },
  };
}

describe("Records table", () => {
  it("has the schema's fields as columns in its order, one row per record, and nested values as a count", async () => {
    render(<ExtractedRecords records={[product(1), product(2)]} fieldTypes={fieldTypes} />);

    const grid = screen.getByRole("grid", { name: "Records" });
    const headers = within(grid)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers.slice(0, 3)).toEqual(["title", "price", "variants"]);
    expect(within(grid).getAllByRole("row")).toHaveLength(3);
    expect(within(grid).getAllByText("3 items")).toHaveLength(2);

    await userEvent.click(
      screen.getAllByRole("button", { name: "View variants" })[0] as HTMLElement,
    );
    expect(await screen.findByRole("heading", { name: "variants of row 1" })).toBeInTheDocument();
  });

  it("shows one page of a long result at a time with the total", async () => {
    const records = Array.from({ length: 120 }, (_, index) => product(index + 1));
    render(<ExtractedRecords records={records} fieldTypes={fieldTypes} />);

    expect(screen.getByText("Rows 1–50 of 120")).toBeInTheDocument();
    expect(within(screen.getByRole("grid", { name: "Records" })).getAllByRole("row")).toHaveLength(
      51,
    );

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("Rows 101–120 of 120")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});
