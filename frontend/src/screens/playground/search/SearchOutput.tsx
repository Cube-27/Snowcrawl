import { Typography } from "@heroui/react";
import { NavLink } from "react-router";
import type { SearchResult } from "../../../api/jobs";
import { EmptyView } from "../../../components/molecules/ViewStates";
import { runPath } from "../modes";

/** What a search found: its results in the provider's order, each a link with its snippet. Titles and snippets are the
 * web's text, shown as text. A scrape's batch job opens in Batch. */
export function SearchOutput({ result }: Readonly<{ result: SearchResult }>) {
  if (result.results.length === 0)
    return <EmptyView title="No results">The provider found nothing for this query.</EmptyView>;
  const jobId = result.job_id ?? null;
  const job = jobId === null ? null : runPath("batch_scrape", jobId);
  const count = result.results.length;
  return (
    <>
      <Typography.Paragraph>
        {count} {count === 1 ? "result" : "results"}
        {result.skipped > 0 ? `, ${String(result.skipped)} the API may not fetch` : ""}.
        {job === null ? null : (
          <>
            {" "}
            <NavLink to={job}>Open the scrape of the results</NavLink>
          </>
        )}
      </Typography.Paragraph>
      <ol className="flex flex-col gap-4">
        {result.results.map((hit) => (
          <li key={hit.url} className="flex flex-col gap-1">
            {/* A plain anchor: the router's link handling would resolve an external URL against the page. */}
            <a className="link" href={hit.url} target="_blank" rel="noreferrer noopener">
              {hit.title === "" ? hit.url : hit.title}
            </a>
            <Typography.Code>{hit.url}</Typography.Code>
            {hit.snippet === "" ? null : <Typography.Paragraph>{hit.snippet}</Typography.Paragraph>}
          </li>
        ))}
      </ol>
    </>
  );
}
