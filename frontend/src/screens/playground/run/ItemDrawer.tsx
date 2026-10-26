import { Typography } from "@heroui/react";
import type { Artifact, FetchSummary } from "../../../api/scrape";
import { FetchTrail } from "../../../components/organisms/FetchAttempts";
import { JsonView } from "../../../components/molecules/JsonView";
import { ArtifactViews } from "../output/FormatViews";
import { SideDrawer } from "../../../components/molecules/SideDrawer";

/** One page's JSON in a drawer from the right, over the results table: its clean records, or its
 * page output for a job without a schema, then how it was fetched (`fetch`, once read).
 * `position` is the page's place in the job (from 1), which names its file; `onClose` closes the drawer. */
export function ItemDrawer({
  url,
  value,
  fetch,
  artifacts,
  position,
  onClose,
}: Readonly<{
  url: string;
  value: unknown;
  fetch: FetchSummary | undefined;
  artifacts?: { screenshot?: Artifact | undefined; pdf?: Artifact | undefined } | undefined;
  position: number;
  onClose: () => void;
}>) {
  return (
    <SideDrawer
      title={<>Page {position}</>}
      header={
        <Typography.Paragraph size="sm" color="muted" className="truncate">
          {url}
        </Typography.Paragraph>
      }
      onClose={onClose}
    >
      {artifacts?.screenshot === undefined && artifacts?.pdf === undefined ? null : (
        <ArtifactViews screenshot={artifacts.screenshot} pdf={artifacts.pdf} />
      )}
      <JsonView label="Page JSON" value={value} filename={`page-${String(position)}.json`} />
      {fetch === undefined ? null : <FetchTrail fetch={fetch} />}
    </SideDrawer>
  );
}
