import { Button, Form } from "@heroui/react";
import { Pencil } from "@gravity-ui/icons";
import type { ReactNode } from "react";
import { PageHeader } from "../../components/templates/PageHeader";
import { CommandBar } from "./CommandBar";
import { GetCode } from "./getCode/GetCode";
import type { ModeId } from "./modes";
import { ModeSwitch } from "./ModeSwitch";
import { OptionsPopover } from "./OptionsPanel";
import { ReadOnlyContext } from "./readOnly";
import { ScheduleThis } from "./ScheduleThis";

/** Every mode's page, top to bottom: the mode switch, centred; the command bar (`input`, the Options
 * button, `tools`, `action`), centred in a narrower column, in a form whose errors are `validationErrors`; then the
 * output (`children`) across the content width. Options opens `options` in a popover while `optionsOpen`, and shows a
 * dot when `optionsChanged`. Get code, before the action, shows `request`: the body the action sends, built once for
 * both (null while the form can't be sent); Schedule this stores the same body as a recurring schedule.
 * With `onEdit`, the page shows a run's request read-only, and the action is "Edit and re-run", which calls it
 */
export function PlaygroundLayout({
  mode,
  validationErrors,
  onSubmit,
  onEdit,
  request,
  input,
  optionsOpen,
  onOptionsOpen,
  optionsChanged,
  tools = null,
  action,
  options,
  children,
}: Readonly<{
  mode: ModeId;
  validationErrors: Readonly<Record<string, string>>;
  onSubmit: () => void;
  onEdit: (() => void) | null;
  request: object | null;
  input: ReactNode;
  optionsOpen: boolean;
  onOptionsOpen: (isOpen: boolean) => void;
  optionsChanged: boolean;
  tools?: ReactNode;
  action: ReactNode;
  options: ReactNode;
  children: ReactNode;
}>) {
  const readOnly = onEdit !== null;
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title="Playground" />
      <ModeSwitch current={mode} />
      <ReadOnlyContext value={readOnly}>
        <Form
          className="mx-auto w-full max-w-playground"
          validationErrors={validationErrors}
          onSubmit={(event) => {
            event.preventDefault();
            if (!readOnly) onSubmit();
          }}
        >
          <CommandBar
            input={input}
            options={
              <OptionsPopover
                isOpen={optionsOpen}
                changed={optionsChanged}
                onOpenChange={onOptionsOpen}
              >
                {options}
              </OptionsPopover>
            }
            tools={tools}
            action={
              <>
                <ScheduleThis mode={mode} request={request} />
                <GetCode mode={mode} request={request} />
                {onEdit === null ? (
                  action
                ) : (
                  <Button variant="secondary" onPress={onEdit}>
                    <Pencil aria-hidden="true" />
                    Edit and re-run
                  </Button>
                )}
              </>
            }
          />
        </Form>
      </ReadOnlyContext>
      {children}
    </section>
  );
}
