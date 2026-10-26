import type { RecordField } from "../../api/scrape";

type Evidence = NonNullable<RecordField["evidence"]>;
type SourceKind = NonNullable<RecordField["source_kind"]>;

// Words for the server's codes. Each map covers every code the API can send, so a new
// code is a type error here rather than a raw code on screen.
const EVIDENCE_LABELS: Readonly<Record<Evidence, string>> = {
  adapter: "Platform API",
  structured: "Structured data",
  agreed: "Sources agree",
  dom: "Page text",
  network: "Page network call",
  state: "Page script state",
  config_corroborated: "Config, corroborated",
  config_uncorroborated: "Config, uncorroborated",
  llm_grounded: "LLM, grounded in the page",
  derived: "Derived from the URL",
};

const SOURCE_LABELS: Readonly<Record<SourceKind, string>> = {
  adapter: "Platform API",
  api: "API recipe",
  jsonld: "JSON-LD",
  microdata: "Microdata",
  og: "Open Graph",
  js_state: "Page script state",
  xhr: "Network call",
  config: "Config steps",
  llm: "LLM",
  derived: "Derived",
  dom: "Page text",
};

function Coded({ code, label }: Readonly<{ code: string | null; label: Record<string, string> }>) {
  if (code === null) return <span>—</span>;
  // The server's code is the cell's hover text, for whoever reads the API's answer beside the table.
  return <span title={code}>{label[code] ?? code}</span>;
}

/** A field's evidence bucket in words, its code on hover; "—" for none. */
export function EvidenceText({ evidence }: Readonly<{ evidence: Evidence | null }>) {
  return <Coded code={evidence} label={EVIDENCE_LABELS} />;
}

/** A field's source kind in words, its code on hover; "—" for none. */
export function SourceKindText({ kind }: Readonly<{ kind: SourceKind | null }>) {
  return <Coded code={kind} label={SOURCE_LABELS} />;
}
