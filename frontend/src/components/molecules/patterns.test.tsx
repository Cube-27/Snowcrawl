import { Button } from "@heroui/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vite-plus/test";
import { ConfirmDialog } from "./ConfirmDialog";
import { ProblemAlert } from "./ProblemAlert";

describe("ConfirmDialog", () => {
  function renderDialog(onConfirm: () => void) {
    render(
      <ConfirmDialog
        trigger={<Button>Delete</Button>}
        title="Delete the job?"
        confirmLabel="Delete job"
        onConfirm={onConfirm}
      >
        Its records stay.
      </ConfirmDialog>,
    );
  }

  it("runs the action once when confirmed, then closes", async () => {
    const onConfirm = vi.fn<() => void>();
    renderDialog(onConfirm);

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete job" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

describe("ProblemAlert", () => {
  it("names each refused member, and gives a whole-request reason without one", () => {
    render(
      <ProblemAlert
        problem={{
          title: "Invalid request",
          detail: null,
          retryAfter: null,
          errors: [
            { field: "urls", reason: "at most 1000 URLs" },
            { field: "", reason: "records are asked alone" },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Invalid requesturls: at most 1000 URLsrecords are asked alone",
    );
  });
});
