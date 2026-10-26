import { Copy } from "@gravity-ui/icons";
import { Button, Typography, toast } from "@heroui/react";

async function copyLocator(locator: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(locator);
    toast.success("Locator copied");
  } catch {
    // The Clipboard API rejects without permission or focus; the locator stays on screen to select by hand.
    toast.warning("Couldn't copy the locator");
  }
}

/** Where a value came from (locator), in monospace with a copy button. */
export function LocatorText({ locator }: Readonly<{ locator: string | null }>) {
  if (locator === null) return <span>—</span>;
  return (
    <span className="inline-flex items-center gap-1">
      <Typography.Code>{locator}</Typography.Code>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label="Copy locator"
        onPress={() => void copyLocator(locator)}
      >
        <Copy aria-hidden="true" />
      </Button>
    </span>
  );
}
