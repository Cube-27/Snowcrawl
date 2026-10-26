import { describe, expect, it } from "vite-plus/test";
import { problemOf } from "./problem";

function problemResponse(contentType = "application/problem+json"): Response {
  return new Response(null, {
    status: 401,
    headers: { "content-type": contentType },
  });
}

describe("problemOf", () => {
  it("reads title and detail from an RFC 9457 body", () => {
    const body = {
      type: "unauthorized",
      title: "Unauthorized",
      status: 401,
      detail: "The API key is revoked.",
    };

    expect(problemOf(problemResponse(), body)).toEqual({
      title: "Unauthorized",
      detail: "The API key is revoked.",
      errors: [],
      retryAfter: null,
      status: 401,
    });
  });

  it("reads the per-field errors and skips malformed entries", () => {
    const body = {
      type: "validation",
      title: "Invalid request",
      status: 422,
      errors: [{ field: "url", reason: "not an absolute URL" }, { field: 3 }, "page_kind"],
    };

    expect(problemOf(problemResponse(), body)).toEqual({
      title: "Invalid request",
      detail: null,
      errors: [{ field: "url", reason: "not an absolute URL" }],
      retryAfter: null,
      status: 401,
    });
  });

  it("returns null for a body that is not a problem", () => {
    expect(problemOf(problemResponse("application/json"), { title: "x" })).toBeNull();
    expect(problemOf(problemResponse(), { detail: "no title" })).toBeNull();
    expect(problemOf(problemResponse(), null)).toBeNull();
  });
});
