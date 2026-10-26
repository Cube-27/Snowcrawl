import { Typography } from "@heroui/react";
import type { ReactNode } from "react";

/** A screen's compact title row, the same on every screen: the page's one h1 with its badges on the left (`children`),
 * an optional one-line `description` under it, and its primary actions on the right (`actions`). */
export function PageHeader({
  title,
  description,
  children,
  actions,
}: Readonly<{
  title: string;
  description?: string | undefined;
  children?: ReactNode;
  actions?: ReactNode;
}>) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <Typography.Heading level={1}>{title}</Typography.Heading>
          {children}
        </div>
        {description === undefined ? null : (
          <Typography.Paragraph size="sm" color="muted">
            {description}
          </Typography.Paragraph>
        )}
      </div>
      {actions === undefined ? null : (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  );
}
