import { Button, Drawer, Tabs, Typography } from "@heroui/react";
import { Code, Copy } from "@gravity-ui/icons";
import { useMemo, useState, type Key } from "react";
import { copyText } from "../../../app/clipboard";
import { Collapsible } from "../../../components/molecules/Collapsible";
import type { ModeId } from "../modes";
import { ENDPOINTS, type Endpoint } from "./endpoints";
import { agentPrompts } from "./prompts";
import { curlSnippet, KEY_VARIABLE, mcpSnippet, pythonSnippet } from "./snippets";

type Language = "curl" | "python" | "mcp";

const LANGUAGES: readonly { id: Language; label: string }[] = [
  { id: "curl", label: "cURL" },
  { id: "python", label: "Python" },
  { id: "mcp", label: "MCP" },
];

function isLanguage(key: Key): key is Language {
  return LANGUAGES.some((language) => language.id === key);
}

const NO_REQUEST = { note: "Complete the request above to see its code." };

/** The code of `request` in `language`, or why there is none: the form can't be sent yet, or no MCP tool takes it. */
function snippet(
  language: Language,
  endpoint: Endpoint,
  request: object | null,
): { code: string } | { note: string } {
  if (request === null) return NO_REQUEST;
  const origin = window.location.origin;
  if (language === "curl") return { code: curlSnippet(origin, endpoint, request) };
  if (language === "python") return { code: pythonSnippet(origin, endpoint, request) };
  if (endpoint.mcpTool === null)
    return { note: "The MCP server has no tool for this endpoint; call `scrape` once per URL." };
  return { code: mcpSnippet(endpoint.mcpTool, request) };
}

/** `text` with each `code span` (CommonMark §6.1) in monospace, as the OpenAPI descriptions write member names. */
function WithCode({ text }: Readonly<{ text: string }>) {
  return text
    .split("`")
    .map((part, i) =>
      i % 2 === 1 ? <Typography.Code key={part + String(i)}>{part}</Typography.Code> : part,
    );
}

/** Each line of `code` with its offset, which keys it. */
function codeLines(code: string): { start: number; line: string }[] {
  let start = 0;
  return code.split("\n").map((line) => {
    const entry = { start, line };
    start += line.length + 1;
    return entry;
  });
}

/** A code block with line numbers (drawn by theme.css, so copying the text leaves them out), in a fixed-height box
 * that scrolls; a note takes the same box. */
function CodeBlock({
  label,
  content,
}: Readonly<{ label: string; content: { code: string } | { note: string } }>) {
  return (
    <section aria-label={label}>
      {"note" in content ? (
        <div className="code-view h-code-view">
          <Typography.Paragraph size="sm" color="muted">
            <WithCode text={content.note} />
          </Typography.Paragraph>
        </div>
      ) : (
        <pre className="code-view h-code-view">
          <code>
            {codeLines(content.code).map(({ start, line }) => (
              <span key={start} className="code-line">
                {line}
                {"\n"}
              </span>
            ))}
          </code>
        </pre>
      )}
    </section>
  );
}

/** The prompts for coding agents, collapsed under the code: most readers want the code itself. */
function Prompts({ mode }: Readonly<{ mode: ModeId }>) {
  return (
    <Collapsible title="Prompts for coding agents">
      {agentPrompts(mode).map((prompt) => (
        <div key={prompt.name} className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <Typography.Paragraph>{prompt.title}</Typography.Paragraph>
            <Typography.Paragraph size="sm" color="muted" className="truncate">
              {prompt.name}
            </Typography.Paragraph>
          </div>
          <Button
            size="sm"
            variant="secondary"
            aria-label={`Copy ${prompt.title}`}
            onPress={() => {
              copyText(prompt.text, "the prompt");
            }}
          >
            <Copy aria-hidden="true" />
            Copy
          </Button>
        </div>
      ))}
    </Collapsible>
  );
}

/** Where the snippets' key comes from: the UI holds none, so this says how to make one. */
function ApiKey() {
  return (
    <section aria-label="API key">
      <Typography.Paragraph size="sm" color="muted">
        The snippets read your API key from <Typography.Code>${KEY_VARIABLE}</Typography.Code>.
        Create one with <Typography.Code>snowcrawl apikey create</Typography.Code>; this page never
        holds or shows a key.
      </Typography.Paragraph>
    </section>
  );
}

/** What the code calls: the endpoint's method and path, then its one-sentence summary. */
function EndpointSummary({ endpoint }: Readonly<{ endpoint: Endpoint }>) {
  return (
    <section aria-label="Endpoint" className="flex flex-col gap-1">
      <Typography.Paragraph size="sm">
        <Typography.Code>POST {endpoint.path}</Typography.Code>
      </Typography.Paragraph>
      <Typography.Paragraph size="sm" color="muted">
        <WithCode text={endpoint.sentence} />
      </Typography.Paragraph>
    </section>
  );
}

function CodeTabs({ endpoint, request }: Readonly<{ endpoint: Endpoint; request: object | null }>) {
  const [language, setLanguage] = useState<Language>("curl");
  const snippets = useMemo(
    () => new Map(LANGUAGES.map(({ id }) => [id, snippet(id, endpoint, request)])),
    [endpoint, request],
  );
  const current = snippets.get(language);
  return (
    <Tabs
      selectedKey={language}
      onSelectionChange={(key) => {
        if (isLanguage(key)) setLanguage(key);
      }}
    >
      <div className="flex items-center justify-between gap-3">
        <Tabs.ListContainer>
          <Tabs.List aria-label="Language">
            {LANGUAGES.map(({ id, label }) => (
              <Tabs.Tab key={id} id={id}>
                {label}
                <Tabs.Indicator />
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs.ListContainer>
        <Button
          size="sm"
          variant="secondary"
          isDisabled={current === undefined || !("code" in current)}
          onPress={() => {
            if (current !== undefined && "code" in current) copyText(current.code, "the code");
          }}
        >
          <Copy aria-hidden="true" />
          Copy code
        </Button>
      </div>
      {LANGUAGES.map(({ id, label }) => (
        <Tabs.Panel key={id} id={id}>
          <CodeBlock label={`${label} code`} content={snippets.get(id) ?? NO_REQUEST} />
        </Tabs.Panel>
      ))}
    </Tabs>
  );
}

/** The toolbar's Get code button and its drawer from the right: the endpoint, then `request`
 * (the body the primary action sends; null while the form can't be sent) as cURL, Python and an MCP tool call, where
 * the API key comes from, and the prompts for coding agents, collapsed. */
export function GetCode({ mode, request }: Readonly<{ mode: ModeId; request: object | null }>) {
  const endpoint = ENDPOINTS[mode];
  return (
    <Drawer>
      <Button variant="secondary">
        <Code aria-hidden="true" />
        Get code
      </Button>
      <Drawer.Backdrop>
        <Drawer.Content placement="right">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>Get code: {endpoint.title}</Drawer.Heading>
              <Typography.Paragraph size="sm" color="muted">
                This request as code, to run from your own program or agent.
              </Typography.Paragraph>
            </Drawer.Header>
            <Drawer.Body className="flex flex-col gap-6">
              <EndpointSummary endpoint={endpoint} />
              <div className="flex flex-col gap-3">
                <CodeTabs endpoint={endpoint} request={request} />
                <ApiKey />
              </div>
              <Prompts mode={mode} />
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}
