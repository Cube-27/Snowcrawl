import { Button, Disclosure } from "@heroui/react";
import { useState, type ReactNode } from "react";

/** A part of a screen that starts collapsed: a quiet trigger named `title` with `aside` beside
 * it (a status), then `children` once opened. The children are rendered only while open, so a collapsed live list
 * costs nothing. */
export function Collapsible({
  title,
  aside = null,
  children,
}: Readonly<{ title: string; aside?: ReactNode; children: ReactNode }>) {
  const [open, setOpen] = useState(false);
  return (
    <Disclosure isExpanded={open} onExpandedChange={setOpen}>
      <div className="flex flex-wrap items-center gap-3">
        <Disclosure.Heading>
          {/* The padding hangs into the margin, so the label lines up with the content around it. */}
          <Button slot="trigger" variant="ghost" size="sm" className="-ms-3">
            {title}
            <Disclosure.Indicator />
          </Button>
        </Disclosure.Heading>
        {aside}
      </div>
      <Disclosure.Content>
        <Disclosure.Body className="flex flex-col gap-3 pt-3">
          {open ? children : null}
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  );
}
