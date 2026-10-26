import { describe, expect, it } from "vite-plus/test";
import type { Endpoint, FieldMatch } from "../../api/captures";
import { endpointText, matchLines } from "./endpoints";

function endpoint(bodyShape: string[] | null): Endpoint {
  return {
    method: "POST",
    origin: "https://api.example.test",
    has_path: true,
    segments: 1,
    query: [],
    body_shape: bodyShape,
    samples: [],
    calls: [],
    fields: [],
    rows: null,
  };
}

describe("endpointText", () => {
  it("tells apart groups of one URL by their body's shape", () => {
    const titles = [
      endpoint(["$.operationName", "$.variables['id']"]),
      endpoint(["$.operationName", "$.variables['slug']"]),
      endpoint(null),
      endpoint([`q=${"a".repeat(70)}&page=1`]),
      endpoint([`q=${"a".repeat(70)}&page=2`]),
    ].map(endpointText);

    expect(new Set(titles).size).toBe(5);
    expect(titles[0]).toBe(
      "POST https://api.example.test/{1 segment} body {$.operationName, $.variables['id']}",
    );
  });
});

describe("matchLines", () => {
  it("keeps a field's paths by type beyond the first few for a disclosure, and every published path", () => {
    const paths = ["$.a", "$.b", "$.c", "$.d", "$.e"].map((path) => ({ path, samples: 2 }));
    const matches: FieldMatch[] = [
      { field: "price", published: false, paths, in_text: 0 },
      { field: "title", published: true, paths, in_text: 1 },
    ];

    const { lines, more } = matchLines(matches, 2);

    expect(lines).toHaveLength(3 + 5 + 1);
    expect(more).toEqual(["price at $.d on 2 of 2 (by type)", "price at $.e on 2 of 2 (by type)"]);
  });
});
