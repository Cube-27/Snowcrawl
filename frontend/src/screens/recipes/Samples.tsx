import { Button, Form, Typography } from "@heroui/react";
import { TrashBin } from "@gravity-ui/icons";
import { useState } from "react";
import type { CaptureRequest, CaptureResult } from "../../api/captures";
import type { FieldTypes } from "../../api/field";
import { useCaptureSample } from "../../api/hooks/recipes";
import { problemOf } from "../../api/hooks/query";
import type { SchemaDef } from "../../api/schemas";
import { Collapsible } from "../../components/molecules/Collapsible";
import { ExtractedRecords } from "../../components/organisms/ExtractedRecords";
import { FormActions } from "../../components/molecules/FormActions";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { pairs, toAction, waitFor, type ActionRow } from "../playground/advanced";
import { ActionRows } from "../playground/AdvancedRows";
import { TextAreaRow, TextRow } from "../playground/OptionsPanel";

/** A sample page the browser captured: the stored capture and what its page gave. */
export interface Sample {
  captureId: string;
  result: CaptureResult;
}

/** What the sample form holds: the page and, as `fetch` names them, its page steps and request context. */
interface SampleDraft {
  url: string;
  actions: readonly ActionRow[];
  waitFor: string;
  /** One `name=value` per line, or several separated by `;`. */
  cookies: string;
  proxyPool: string;
}

const EMPTY_DRAFT: SampleDraft = { url: "", actions: [], waitFor: "", cookies: "", proxyPool: "" };

/** The capture request of a draft, for the schema's page kind; a step missing its value is not sent. */
function captureRequest(draft: SampleDraft, schema: SchemaDef): CaptureRequest {
  const actions = draft.actions.map(toAction).filter((action) => action !== null);
  const cookies = pairs(draft.cookies, "=");
  const wait = waitFor(draft.waitFor);
  const proxyPool = draft.proxyPool.trim();
  return {
    url: draft.url.trim(),
    schema_id: schema.id,
    page_kind: schema.page_kind,
    ...(actions.length === 0 ? {} : { actions }),
    ...(wait === null ? {} : { wait_for: wait }),
    ...(Object.keys(cookies).length === 0 ? {} : { cookies }),
    ...(proxyPool === "" ? {} : { proxy_pool: proxyPool }),
  };
}

/** A capture that stored nothing: the server's reason, as a scrape states it. */
function NotStored({ result }: Readonly<{ result: CaptureResult }>) {
  return (
    <ProblemAlert
      problem={{
        title: `The page was not captured (${result.outcome}).`,
        detail: result.reason,
        errors: [],
        retryAfter: null,
      }}
    />
  );
}

/** Adds a sample page: its URL, page steps and request context, captured in the browser on "Capture". The page
 * steps run before the page's calls are kept, so the calls a click makes are among them. A capture that stored
 * nothing is not added; the form keeps what was typed. */
export function SampleForm({
  schema,
  isFull,
  onCaptured,
}: Readonly<{ schema: SchemaDef; isFull: boolean; onCaptured: (sample: Sample) => void }>) {
  const [draft, setDraft] = useState<SampleDraft>(EMPTY_DRAFT);
  const capture = useCaptureSample();
  const problem = problemOf(capture.error);
  const kept = capture.data;
  const kind = schema.page_kind;
  return (
    <Form
      aria-label="Add a sample page"
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        capture.mutate(captureRequest(draft, schema), {
          onSuccess: (result) => {
            if (result.capture_id === null) return;
            onCaptured({ captureId: result.capture_id, result });
            setDraft(EMPTY_DRAFT);
          },
        });
      }}
    >
      <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-4">
        <TextRow
          label="Sample URL"
          help={`A ${kind} page of the site the recipe will serve.`}
          name="url"
          value={draft.url}
          placeholder="https://example.com/item/1"
          onChange={(url) => {
            setDraft({ ...draft, url });
          }}
        />
        <TextRow
          label="Wait for"
          help="Before the page is read: milliseconds, a CSS selector to appear, or network_idle."
          name="wait_for"
          value={draft.waitFor}
          placeholder="2000, .price or network_idle"
          onChange={(value) => {
            setDraft({ ...draft, waitFor: value });
          }}
        />
        <TextRow
          label="Proxy pool"
          help="The name of a proxy pool the server is configured with. Empty: the site's pool, if any."
          name="proxy_pool"
          value={draft.proxyPool}
          placeholder="Pool name"
          onChange={(proxyPool) => {
            setDraft({ ...draft, proxyPool });
          }}
        />
        <TextAreaRow
          label="Cookies"
          help="One name=value per line (or separated by ;), sent to the page's own host. Values are secrets: never stored or shown again."
          name="cookies"
          value={draft.cookies}
          placeholder="session=…"
          onChange={(cookies) => {
            setDraft({ ...draft, cookies });
          }}
        />
        <ActionRows
          rows={draft.actions}
          onChange={(actions) => {
            setDraft({ ...draft, actions });
          }}
        />
      </div>
      <FormActions>
        <Button
          type="submit"
          isPending={capture.isPending}
          isDisabled={isFull || draft.url.trim() === ""}
        >
          Capture
        </Button>
      </FormActions>
      {/* Under the button: a refusal moves nothing above it. */}
      <ProblemAlert problem={problem} />
      {kept?.capture_id === null ? <NotStored result={kept} /> : null}
    </Form>
  );
}

function recordsText(count: number): string {
  return count === 1 ? "1 record" : `${String(count)} records`;
}

/** The samples captured so far, in order, each with how its page ended and its records (collapsed); "Remove" drops
 * one from the call list. */
export function SampleList({
  samples,
  fieldTypes,
  onRemove,
}: Readonly<{
  samples: readonly Sample[];
  fieldTypes: FieldTypes;
  onRemove: (captureId: string) => void;
}>) {
  return (
    <ol aria-label="Samples" className="flex flex-col gap-3">
      {samples.map(({ captureId, result }, index) => (
        <li key={captureId} className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Typography.Paragraph className="min-w-0 truncate">
              {index + 1}. {result.url}
            </Typography.Paragraph>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Remove sample ${String(index + 1)}`}
              onPress={() => {
                onRemove(captureId);
              }}
            >
              <TrashBin aria-hidden="true" />
              Remove
            </Button>
          </div>
          <Collapsible title={`${result.outcome}, ${recordsText(result.records.length)}`}>
            <ExtractedRecords records={result.records} fieldTypes={fieldTypes} />
          </Collapsible>
        </li>
      ))}
    </ol>
  );
}
