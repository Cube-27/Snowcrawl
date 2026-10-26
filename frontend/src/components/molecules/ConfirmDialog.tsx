import { AlertDialog, Button } from "@heroui/react";
import type { ReactNode } from "react";

/** Asks before an action that cannot be undone. `trigger` is the HeroUI Button that opens it; Cancel closes it, and
 * the confirm button runs `onConfirm` and closes it. */
export function ConfirmDialog({
  trigger,
  title,
  children,
  confirmLabel,
  onConfirm,
}: Readonly<{
  trigger: ReactNode;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
}>) {
  return (
    <AlertDialog>
      {trigger}
      <AlertDialog.Backdrop>
        <AlertDialog.Container>
          <AlertDialog.Dialog>
            {({ close }) => (
              <>
                <AlertDialog.Header>
                  <AlertDialog.Icon status="danger" />
                  <AlertDialog.Heading>{title}</AlertDialog.Heading>
                </AlertDialog.Header>
                <AlertDialog.Body>{children}</AlertDialog.Body>
                <AlertDialog.Footer>
                  <Button variant="tertiary" onPress={close}>
                    Cancel
                  </Button>
                  <Button
                    variant="danger"
                    onPress={() => {
                      onConfirm();
                      close();
                    }}
                  >
                    {confirmLabel}
                  </Button>
                </AlertDialog.Footer>
              </>
            )}
          </AlertDialog.Dialog>
        </AlertDialog.Container>
      </AlertDialog.Backdrop>
    </AlertDialog>
  );
}
