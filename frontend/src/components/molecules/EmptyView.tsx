import { EmptyState, Typography } from "@heroui/react";
import type { ReactNode } from "react";

/** A screen or list with nothing to show yet: what is missing, and how to get it (`children`, `action`). */
export function EmptyView({
  title,
  children,
  action,
}: Readonly<{ title: string; children?: ReactNode; action?: ReactNode }>) {
  return (
    <EmptyState>
      <Typography.Paragraph weight="medium">{title}</Typography.Paragraph>
      {children === undefined ? null : (
        <Typography.Paragraph size="sm" color="muted">
          {children}
        </Typography.Paragraph>
      )}
      {action}
    </EmptyState>
  );
}
