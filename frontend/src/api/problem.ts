import { isJsonObject } from "./json";

/** The parts of an RFC 9457 problem the UI shows. The API defines the full body; this narrows it until the
 * generated `schema.d.ts` carries the problem schema. */
export interface ProblemSummary {
  title: string;
  detail: string | null;
}

/** One entry of a problem's `errors`: the request member and why it was refused. */
export interface FieldProblem {
  field: string;
  reason: string;
}

export interface Problem extends ProblemSummary {
  errors: FieldProblem[];
  /** RFC 9110 §10.2.3, sent with 429: delay-seconds or an HTTP date, as sent. */
  retryAfter: string | null;
  /** RFC 9110 §15 status of the answer the problem came with; absent for a failure that had no answer. */
  status?: number;
}

// RFC 9457 §3: problem bodies are `application/problem+json`; RFC 9110 §8.3.1 allows parameters after `;`.
function isProblemJson(response: Response): boolean {
  const mediaType = response.headers.get("content-type")?.split(";")[0]?.trim().toLowerCase();
  return mediaType === "application/problem+json";
}

function isFieldProblem(entry: unknown): entry is FieldProblem {
  return isJsonObject(entry) && typeof entry.field === "string" && typeof entry.reason === "string";
}

/** The problem `response` answered with, given its already parsed `body` (openapi-fetch reads error bodies itself);
 * null when it is not a problem with a title. Malformed `errors` entries are skipped. */
export function problemOf(response: Response, body: unknown): Problem | null {
  if (!isProblemJson(response) || !isJsonObject(body) || typeof body.title !== "string") {
    return null;
  }
  const errors = Array.isArray(body.errors) ? body.errors : [];
  return {
    title: body.title,
    detail: typeof body.detail === "string" ? body.detail : null,
    errors: errors.filter(isFieldProblem).map(({ field, reason }) => ({ field, reason })),
    retryAfter: response.headers.get("Retry-After"),
    status: response.status,
  };
}

/** A problem's per-field errors keyed by request member, as a React Aria `Form`'s `validationErrors` takes them. */
export function fieldErrors(problem: Problem): Readonly<Record<string, string>> {
  return Object.fromEntries(problem.errors.map(({ field, reason }) => [field, reason]));
}
