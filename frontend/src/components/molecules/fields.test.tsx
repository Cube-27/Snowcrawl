import { Toast } from "@heroui/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vite-plus/test";
import { FieldValue } from "./FieldValue";
import { LocatorText } from "../atoms/LocatorText";
import { RecordFieldsTable, RecordSummary } from "../organisms/RecordFields";
import { recordField } from "../../test/records";

describe("FieldValue", () => {
  it("renders a conflict as an em dash with its reason and status, and no value", async () => {
    render(
      <FieldValue
        field={{ status: "conflict", reason: "disagree", value: "a value the server withheld" }}
        spec={{ type: "text" }}
      />,
    );

    const dash = screen.getByRole("button", { name: "No value: disagree" });
    expect(dash).toHaveTextContent("—");
    expect(screen.getByText("conflict")).toBeInTheDocument();
    expect(screen.queryByText("a value the server withheld")).not.toBeInTheDocument();

    await userEvent.tab();
    expect(dash).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("disagree");
  });

  it.each([
    ["money", { type: "money" }, { amount: "19.90", currency: "EUR" }, "19.90 EUR"],
    ["money", { type: "money" }, { amount: "19.90", currency: null }, "19.90 (currency unknown)"],
    [
      "location",
      { type: "location" },
      {
        text: "Springfield Central",
        locality: "Springfield",
        region: null,
        country: "ZZ",
        remote_flag: false,
      },
      "Springfield Central",
    ],
  ] as const)("renders a published %s value", (_name, spec, value, text) => {
    const { container } = render(
      <FieldValue field={{ status: "published", reason: null, value }} spec={spec} />,
    );

    expect(container).toHaveTextContent(text);
  });
});

describe("one uncalibrated badge per record", () => {
  const uncalibrated = { calibrated: false, confidence: 0.4 };

  it("shows it once for a record with several uncalibrated published fields", () => {
    const record = {
      complete: true,
      mode: "adhoc" as const,
      confidence: null,
      fields: {
        title: recordField(uncalibrated),
        price: recordField(uncalibrated),
        brand: recordField(uncalibrated),
      },
    };
    render(
      <>
        <RecordSummary record={record} />
        <RecordFieldsTable label="Fields" record={record} fieldTypes={{}} />
      </>,
    );

    expect(screen.getAllByText("uncalibrated")).toHaveLength(1);
    expect(screen.getAllByText("0.40")).toHaveLength(3);
  });
});

describe("LocatorText", () => {
  it("copies the locator and says so", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(
      <>
        <Toast.Provider />
        <LocatorText locator="jsonld:0:$.offers.price" />
      </>,
    );

    expect(screen.getByText("jsonld:0:$.offers.price")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy locator" }));

    expect(writeText).toHaveBeenCalledWith("jsonld:0:$.offers.price");
    expect(await screen.findByText("Locator copied")).toBeInTheDocument();
  });
});
