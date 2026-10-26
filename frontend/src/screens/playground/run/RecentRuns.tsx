import { Link } from "@heroui/react";
import { useJobs } from "../../../api/hooks/jobs";
import { problemOf } from "../../../api/hooks/query";
import type { JobKind } from "../../../api/jobs";
import { EmptyView } from "../../../components/molecules/EmptyView";
import { LoadingView } from "../../../components/molecules/LoadingView";
import { ProblemAlert } from "../../../components/molecules/ProblemAlert";
import { Section } from "../../../components/templates/Section";
import { JobRows } from "../../jobs/JobRows";

/** How many runs the list shows. */
const RECENT = 10;

/** Recent runs' columns and their widths: what the run was started on, its status, when, how long, and the
 * cue that the row opens it. */
const RUN_COLUMNS = [
  ["target", "w-5/12"],
  ["status", "w-1/6"],
  ["started", "w-1/6"],
  ["duration", "w-1/8"],
  ["open", "w-1/8"],
] as const;

/** The last runs of a mode's `kind`, newest first, shown before any run on the page; a row opens its
 * run here (`?job=`), and "All runs" opens the Jobs screen on this kind. Every state fills the output area's box, so a
 * run that replaces the list moves nothing. */
export function RecentRuns({ kind }: Readonly<{ kind: JobKind }>) {
  const jobs = useJobs({ filters: { kind, status: null }, cursor: null, limit: RECENT });
  const problem = problemOf(jobs.error);
  let content;
  if (problem !== null) content = <ProblemAlert problem={problem} />;
  else if (!jobs.isSuccess) content = <LoadingView label="Loading recent runs" />;
  else if (jobs.data.items.length === 0)
    content = (
      <EmptyView title="No runs yet">
        Each run you start here is listed here, and opens again with its results.
      </EmptyView>
    );
  else content = <JobRows label="Recent runs" jobs={jobs.data.items} columns={RUN_COLUMNS} />;
  return (
    <div className="min-h-playground-output">
      <Section title="Recent runs" action={<Link href={`/jobs?kind=${kind}`}>All runs</Link>}>
        {content}
      </Section>
    </div>
  );
}
