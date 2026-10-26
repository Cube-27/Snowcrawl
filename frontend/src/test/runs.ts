import type {
  JobKind,
  JobSummary,
  JsonValuesItem,
  JsonValuesPage,
  StoredJobRequest,
} from "../api/jobs";
import type { RecordPage, StoredRecord } from "../api/records";
import { stubApi, type Call } from "./api";
import { jobSummary } from "./jobs";

/** The API stubs the job-running playground modes share (Batch, Crawl, Extract): the toolbar's schema list, the
 * submit, and the run's job, request, events and `json_values` results. */

/** The stored schema the toolbar offers: a detail-page schema with one text field. */
export const SCHEMA_ID = "0192a000-0000-7000-8000-0000000000s1";

/** `GET /v1/schemas`: the one stored schema. */
export function schemaList(): Response {
  return Response.json({
    items: [
      {
        id: SCHEMA_ID,
        name: "item_detail",
        origin: "user",
        status: "active",
        page_kind: "detail",
        fields: { title: { type: "text" } },
      },
    ],
  });
}

/** `GET /v1/schemas/{id}`: the stored schema the toolbar offers, whole. */
export function storedSchema(): Response {
  return Response.json({
    id: SCHEMA_ID,
    name: "item_detail",
    origin: "user",
    status: "active",
    page_kind: "detail",
    required_groups: [],
    field_order: ["title"],
    labels: null,
    fields: { title: { type: "text", required: false, scope: "row" } },
  });
}

/** An event stream that stays open and sends nothing. */
export function quietEvents(): Response {
  return new Response(new ReadableStream(), { headers: { "content-type": "text/event-stream" } });
}

/** A page of `json_values` results. */
export function valuesPage(items: JsonValuesItem[], next_cursor: string | null = null): Response {
  return Response.json({
    format: "json_values",
    items,
    next_cursor,
    total: items.length,
  } satisfies JsonValuesPage);
}

/** A page of a job's stored records (`results` without a format). */
export function recordsPage(items: StoredRecord[]): Response {
  return Response.json({ items, next_cursor: null, total: items.length } satisfies RecordPage);
}

export interface RunStub {
  /** The submit route, such as `POST /v1/crawl`; it answers `202` with the job's id. */
  submit: string;
  jobId: string;
  kind: JobKind;
  /** The job's summary; by default `jobSummary` of `kind`. */
  job?: JobSummary;
  /** The job's stored request (`GET /v1/jobs/{id}/request`). */
  request: StoredJobRequest;
  /** The job's results, one page. */
  items?: JsonValuesItem[];
  /** The job's stored records, one page (a job with a schema). */
  records?: StoredRecord[];
}

/** Stubs a mode's run as `run` describes, with `replies` over these defaults; returns the calls recorded. */
export function runApi(run: RunStub, replies: Parameters<typeof stubApi>[0] = {}): Call[] {
  const job = `/v1/jobs/${run.jobId}`;
  const summary = run.job ?? jobSummary({ id: run.jobId, kind: run.kind });
  return stubApi({
    "GET /v1/schemas": schemaList,
    [run.submit]: () => Response.json({ job_id: run.jobId }, { status: 202 }),
    [`GET ${job}`]: () => Response.json(summary),
    [`GET ${job}/request`]: () => Response.json(run.request),
    [`GET ${job}/events`]: quietEvents,
    [`GET /v1/schemas/${SCHEMA_ID}`]: storedSchema,
    [`GET ${job}/results`]: ({ query }) =>
      query.includes("format=json_values")
        ? valuesPage(run.items ?? [])
        : recordsPage(run.records ?? []),
    ...replies,
  });
}
