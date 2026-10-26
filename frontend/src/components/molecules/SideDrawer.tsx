import { Drawer } from "@heroui/react";
import type { ReactNode } from "react";

/** A drawer over the screen, open while mounted: its heading (`title`), what it is about under it (`header`), then
 * its content down the body. `onClose` runs when the viewer closes it (the close button, Escape, the backdrop). It
 * slides in from the right, or from the bottom for content as wide as the screen. */
export function SideDrawer({
  title,
  header,
  placement = "right",
  onClose,
  children,
}: Readonly<{
  title: ReactNode;
  header?: ReactNode;
  placement?: "right" | "bottom";
  onClose: () => void;
  children: ReactNode;
}>) {
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement={placement}>
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>{title}</Drawer.Heading>
              {header}
            </Drawer.Header>
            <Drawer.Body className="flex flex-col gap-6">{children}</Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}
