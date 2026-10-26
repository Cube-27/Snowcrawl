import { Typography } from "@heroui/react";
import { useId, type ReactNode } from "react";

/* A screen's layout, the same on every screen: `Screen` stacks its `PageHeader` and parts with the `stack` spacing,
 * and each titled part is a `Section` under it, so the outline reads h1, then h2 (WCAG 2.2 SC 1.3.1). */

interface HeaderProps {
  title: string;
  description?: string | undefined;
  /** Badges beside the title (a status). */
  badges?: ReactNode;
  /** The screen's primary actions, on the right. */
  actions?: ReactNode;
}

/** A screen's compact title row: the page's one h1 with its `badges` on the left, an optional one-line `description`
 * under it, and its primary `actions` on the right. */
export function PageHeader({ title, description, badges, actions }: Readonly<HeaderProps>) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <Typography.Heading level={1} className="break-words">
            {title}
          </Typography.Heading>
          {badges}
        </div>
        {description === undefined ? null : (
          <Typography.Paragraph size="sm" color="muted" className="max-w-prose">
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

/** A whole screen: its header, then its parts (`children`) down the page. */
export function Screen({ children, ...header }: Readonly<HeaderProps & { children: ReactNode }>) {
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader {...header} />
      {children}
    </section>
  );
}

/** A titled part of a screen, named by its h2 (`aria-labelledby`): an optional `description` under the heading, an
 * optional `action` on its right (a "View all" link), then the content. */
export function Section({
  title,
  description,
  action,
  children,
}: Readonly<{
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}>) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Typography.Heading level={2} id={headingId}>
          {title}
        </Typography.Heading>
        {action}
      </div>
      {description === undefined ? null : (
        <Typography.Paragraph size="sm" color="muted" className="max-w-prose">
          {description}
        </Typography.Paragraph>
      )}
      {children}
    </section>
  );
}
