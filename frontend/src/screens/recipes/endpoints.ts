import type { Endpoint, FieldMatch } from "../../api/captures";
import type { EndpointRef } from "../../api/recipes";

/** One endpoint group as text, stable while samples are added: method, origin, path shape (with its format suffix),
 * query names and body shape, as the server groups calls by them. */
export function endpointKey(endpoint: Endpoint): string {
  return JSON.stringify([
    endpoint.method,
    endpoint.origin,
    endpoint.has_path,
    endpoint.segments,
    endpoint.suffix,
    endpoint.query,
    endpoint.body_shape,
  ]);
}

/** The endpoint as a draft names it: by the members of its key. */
export function endpointRef(endpoint: Endpoint): EndpointRef {
  const {
    method,
    origin,
    has_path: hasPath,
    segments,
    suffix,
    query,
    body_shape: bodyShape,
  } = endpoint;
  return { method, origin, has_path: hasPath, segments, suffix, query, body_shape: bodyShape };
}

/** The request shape people read: `GET https://example.test/{3 segments}.json?page&size`, and the body's shape after
 * it when the calls send one (`POST https://example.test/{1 segment} body {$.operationName, $.variables['id']}`), so
 * groups of one URL that differ by their body read apart, as the server tells them apart. */
export function endpointText(endpoint: Endpoint): string {
  const path = endpoint.has_path ? `/{${segmentsText(endpoint.segments)}}${endpoint.suffix}` : "/";
  const query = endpoint.query.length === 0 ? "" : `?${endpoint.query.join("&")}`;
  return `${endpoint.method ?? "GET"} ${endpoint.origin}${path}${query}${bodyText(endpoint.body_shape)}`;
}

/** A JSON body's shape is the paths of its leaves (RFC 9535); any other body's is its media type, then a form body's
 * names (`body application/x-www-form-urlencoded {q, page}`). */
function bodyText(shape: Endpoint["body_shape"]): string {
  if (shape == null) return "";
  const [first, ...names] = shape;
  if (first !== undefined && !first.startsWith("$")) {
    return names.length === 0 ? ` body ${first}` : ` body ${first} {${names.join(", ")}}`;
  }
  return ` body {${shape.join(", ")}}`;
}

function segmentsText(segments: number): string {
  return segments === 1 ? "1 segment" : `${String(segments)} segments`;
}

/** Whether any call of the group went to the page's own site (the server's `same_site`, RFC 6265bis §5.2). */
export function isOwnSite(endpoint: Endpoint): boolean {
  return endpoint.calls.some((call) => call.same_site === true);
}

/** The groups with the page's own site first; otherwise in the server's order (first called first). */
export function ownSiteFirst(endpoints: readonly Endpoint[]): Endpoint[] {
  return [...endpoints.filter(isOwnSite), ...endpoints.filter((endpoint) => !isOwnSite(endpoint))];
}

// The paths read by type only shown per field; the rest wait behind "more paths".
const TYPED_SHOWN = 3;

/** Where a group carries its fields, a line per path: "price at $.data.price on 3 of 3", then "in the text on 2 of
 * 3" for an HTML answer that holds the published value. A path read by type only, not by a published value, says so,
 * and a field's such paths after the first few are `more`, for a disclosure. */
export function matchLines(
  matches: readonly FieldMatch[],
  samples: number,
): { lines: string[]; more: string[] } {
  const of = (count: number) => `on ${String(count)} of ${String(samples)}`;
  const lines: string[] = [];
  const more: string[] = [];
  for (const match of matches) {
    const { field, in_text: inText } = match;
    const kind = match.published ? "" : " (by type)";
    const texts = match.paths.map(
      ({ path, samples: count }) => `${field} at ${path} ${of(count)}${kind}`,
    );
    const shown = match.published ? texts.length : TYPED_SHOWN;
    lines.push(...texts.slice(0, shown));
    more.push(...texts.slice(shown));
    if (inText > 0) lines.push(`${field} in the text ${of(inText)}`);
  }
  return { lines, more };
}

/** A byte count as people read it: "512 B", "3.4 kB" (SI, ISO/IEC 80000-13). */
export function sizeText(bytes: number): string {
  if (bytes < 1000) return `${String(bytes)} B`;
  if (bytes < 1_000_000) return `${(bytes / 1000).toFixed(1)} kB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}
