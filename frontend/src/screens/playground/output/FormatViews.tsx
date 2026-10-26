import { Label, Link, Table, TextArea, TextField, Typography } from "@heroui/react";
import type { Artifact, Chunk, PageMetadata, ScrapeResponse } from "../../../api/scrape";
import { FetchAttemptsTable } from "../../../components/organisms/FetchAttempts";

/** A text format (markdown, html, raw_html) in a read-only text area, to read, scroll and copy as sent. */
export function TextFormat({ label, text }: Readonly<{ label: string; text: string }>) {
  return (
    <TextField isReadOnly value={text}>
      <Label>{label}</Label>
      <TextArea rows={20} />
    </TextField>
  );
}

/** Pairs of a name and the value the server sent, in a two-column table. */
export function NameValueTable({
  label,
  rows,
}: Readonly<{ label: string; rows: readonly (readonly [string, string])[] }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label={label}>
          <Table.Header>
            <Table.Column isRowHeader>Name</Table.Column>
            <Table.Column>Value</Table.Column>
          </Table.Header>
          <Table.Body items={rows.map(([name, value]) => ({ id: name, name, value }))}>
            {(row) => (
              <Table.Row id={row.id}>
                <Table.Cell>{row.name}</Table.Cell>
                <Table.Cell>{row.value}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The page's links, as text: they lead to other sites and are not followed from here. */
export function LinksTable({ links }: Readonly<{ links: readonly string[] }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Links">
          <Table.Header>
            <Table.Column isRowHeader>URL</Table.Column>
          </Table.Header>
          <Table.Body items={links.map((url) => ({ id: url }))}>
            {(link) => (
              <Table.Row id={link.id}>
                <Table.Cell>{link.id}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

function text(value: string | null): string {
  return value ?? "—";
}

export function MetadataTable({ metadata }: Readonly<{ metadata: PageMetadata }>) {
  return (
    <NameValueTable
      label="Metadata"
      rows={[
        ["Title", text(metadata.title)],
        ["Description", text(metadata.description)],
        ["Language", text(metadata.language)],
        ["Canonical", text(metadata.canonical)],
        ["Structured data types", metadata.structured_data_types.join(", ") || "—"],
        ...Object.entries(metadata.og),
      ]}
    />
  );
}

export function FetchView({ fetch }: Readonly<{ fetch: NonNullable<ScrapeResponse["fetch"]> }>) {
  return (
    <div className="flex flex-col gap-4">
      <NameValueTable
        label="Fetch"
        rows={[
          ["Method", fetch.method],
          ["HTTP status", fetch.status_code === null ? "—" : String(fetch.status_code)],
          ["Block class", fetch.block_class],
          // The platform adapter that read the page's API capture; other fetches have none.
          ...(fetch.adapter === null ? [] : [["Adapter", fetch.adapter] as const]),
        ]}
      />
      <FetchAttemptsTable attempts={fetch.attempts} />
    </div>
  );
}

export function UsageTable({ usage }: Readonly<{ usage: ScrapeResponse["usage"] }>) {
  return (
    <NameValueTable
      label="Usage"
      rows={[
        ["LLM calls", String(usage.llm_calls)],
        ["LLM tokens in", String(usage.llm_tokens_in)],
        ["LLM tokens out", String(usage.llm_tokens_out)],
        ["Browser seconds", String(usage.browser_seconds)],
        ["HTTP requests", String(usage.http_requests)],
      ]}
    />
  );
}

/** A page's screenshot, shown as the API serves it: the UI's server attaches the API key to `/v1`, so the
 * image loads from the artifact's URL. */
export function ScreenshotView({ artifact }: Readonly<{ artifact: Artifact }>) {
  return (
    <a href={artifact.url} target="_blank" rel="noreferrer">
      <img
        src={artifact.url}
        alt="Screenshot of the page"
        className="max-w-full rounded-medium border border-divider object-contain"
      />
    </a>
  );
}

/** A page's PDF print: opened by its URL in the browser's viewer. */
export function PdfLink({ artifact }: Readonly<{ artifact: Artifact }>) {
  return (
    <Link href={artifact.url} target="_blank" rel="noreferrer">
      Open the PDF
    </Link>
  );
}

/** The page's artifacts (screenshot, PDF) of a scrape or a job's page output. */
export function ArtifactViews({
  screenshot,
  pdf,
}: Readonly<{ screenshot?: Artifact | undefined; pdf?: Artifact | undefined }>) {
  return (
    <div className="flex flex-col gap-3">
      {screenshot === undefined ? null : <ScreenshotView artifact={screenshot} />}
      {pdf === undefined ? null : <PdfLink artifact={pdf} />}
    </div>
  );
}

/** The page's chunks (`chunks`), each with the headings above it and its size in words. */
export function ChunksView({ chunks }: Readonly<{ chunks: readonly Chunk[] }>) {
  return (
    <div className="flex flex-col gap-3">
      {chunks.map((chunk) => (
        <div key={chunk.index} className="rounded-medium border border-divider p-3">
          <Typography.Paragraph size="sm" color="muted">
            #{chunk.index + 1} · {chunk.words} words
            {chunk.heading_path.length === 0 ? "" : ` · ${chunk.heading_path.join(" > ")}`}
          </Typography.Paragraph>
          <Typography.Paragraph className="whitespace-pre-wrap">{chunk.text}</Typography.Paragraph>
        </div>
      ))}
    </div>
  );
}
