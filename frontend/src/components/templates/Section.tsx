import { Typography } from "@heroui/react";
import type { ReactNode } from "react";

/** A titled part of a screen: its h2, an optional action on the right of the heading (a "View all" link), then the
 * content. */
export function Section({
  title,
  action,
  children,
}: Readonly<{ title: string; action?: ReactNode; children: ReactNode }>) {
  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <Typography.Heading level={2}>{title}</Typography.Heading>
        {action}
      </div>
      {children}
    </section>
  );
}
