import { Button } from "@heroui/react";
import { ArrowDownToLine, Copy } from "@gravity-ui/icons";
import { memo, useMemo } from "react";
import { copyText } from "../../app/clipboard";
import { saveText } from "../../app/download";
import { jsonLines, type JsonTokenKind } from "../lib/jsonTokens";

// Each token kind's class in theme.css, which holds its color.
const TOKEN_CLASSES: Readonly<Record<JsonTokenKind, string | undefined>> = {
  key: "json-key",
  string: "json-string",
  number: "json-number",
  literal: "json-literal",
  plain: undefined,
};

/** `value` as JSON, laid out as a code editor shows it: pretty-printed with a two-space indent, a line
 * number per line and a guide per indent level (both drawn by theme.css, so copied text has neither), colored by
 * token, in a fixed-height box that scrolls, with Copy and Download (`filename`, a `.json` file) at its top right.
 * Memoized: the value can be large and the form above re-renders on every keystroke. */
export const JsonView = memo(function JsonView({
  label,
  value,
  filename,
}: Readonly<{ label: string; value: unknown; filename: string }>) {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  const lines = useMemo(() => jsonLines(text), [text]);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-end gap-2">
        <Button
          size="sm"
          variant="secondary"
          onPress={() => {
            copyText(text, "the JSON");
          }}
        >
          <Copy aria-hidden="true" />
          Copy
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onPress={() => {
            saveText(filename, text, "application/json");
          }}
        >
          <ArrowDownToLine aria-hidden="true" />
          Download
        </Button>
      </div>
      <section aria-label={label}>
        <pre className="json-view h-json-view">
          {lines.map((line, index) => (
            <span key={line.start} className="json-line">
              {line.indent === "" ? null : <span className="json-indent">{line.indent}</span>}
              {line.tokens.map((token) => (
                <span key={token.start} className={TOKEN_CLASSES[token.kind]}>
                  {token.text}
                </span>
              ))}
              {index < lines.length - 1 ? "\n" : null}
            </span>
          ))}
        </pre>
      </section>
    </div>
  );
});
