import { Button, Description, Input, Label, TextField } from "@heroui/react";
import { Plus, TrashBin } from "@gravity-ui/icons";
import type { PageKind } from "../../../api/scrape";
import { isOptionOf, OptionSelect } from "../../../components/molecules/OptionSelect";
import { PAGE_KIND_LABELS } from "../../../components/lib/requestOptions";
import type { RuleRow } from "./form";

const isPageKind = isOptionOf(PAGE_KIND_LABELS);

/** A crawl's `page_kind_rules` as rows of a URL glob and a page kind, across the panel's width.
 * Without rules every page in scope is extracted; with rules, a page no glob matches is read for its links only. With
 * a schema every rule is for its page kind, `fixed`. `error` is why the rules can't be sent. */
export function RuleRows({
  rules,
  fixed,
  error,
  onChange,
}: Readonly<{
  rules: readonly RuleRow[];
  fixed: PageKind | null;
  error: string | undefined;
  onChange: (rules: RuleRow[]) => void;
}>) {
  const replace = (key: string, next: RuleRow) => {
    onChange(rules.map((rule) => (rule.key === key ? next : rule)));
  };
  return (
    <fieldset className="col-span-full flex flex-col gap-2">
      <legend className="contents">
        <Label>Page-kind rules</Label>
      </legend>
      {rules.map((rule, index) => {
        const n = String(index + 1);
        return (
          <div key={rule.key} className="flex items-center gap-2">
            <TextField
              aria-label={`Pattern ${n}`}
              className="flex-1"
              value={rule.pattern}
              onChange={(pattern) => {
                replace(rule.key, { ...rule, pattern });
              }}
            >
              <Input
                autoComplete="off"
                spellCheck={false}
                placeholder="https://example.com/items/*"
              />
            </TextField>
            <OptionSelect
              label={`Page kind ${n}`}
              hideLabel
              options={PAGE_KIND_LABELS}
              value={fixed ?? rule.pageKind}
              isDisabled={fixed !== null}
              accepts={isPageKind}
              onChange={(pageKind) => {
                replace(rule.key, { ...rule, pageKind });
              }}
            />
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label={`Remove rule ${n}`}
              onPress={() => {
                onChange(rules.filter(({ key }) => key !== rule.key));
              }}
            >
              <TrashBin aria-hidden="true" />
            </Button>
          </div>
        );
      })}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Description>
          A URL glob per rule; a page no rule matches is read for its links only.
        </Description>
        <Button
          size="sm"
          variant="secondary"
          onPress={() => {
            onChange([
              ...rules,
              { key: crypto.randomUUID(), pattern: "", pageKind: fixed ?? "detail" },
            ]);
          }}
        >
          <Plus aria-hidden="true" />
          Add rule
        </Button>
      </div>
      {/* Not one field's error, so React Aria's FieldError has no field to follow: the message is shown as an alert. */}
      {error === undefined ? null : (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </fieldset>
  );
}
