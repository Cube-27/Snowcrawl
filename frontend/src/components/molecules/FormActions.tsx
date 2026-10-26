import type { ReactNode } from "react";

/** A form's buttons, after its fields: right-aligned, secondary actions (Cancel) first and the primary one last. */
export function FormActions({ children }: Readonly<{ children: ReactNode }>) {
  return <div className="flex flex-wrap justify-end gap-2">{children}</div>;
}
