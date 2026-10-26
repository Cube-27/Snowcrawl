import { Typography } from "@heroui/react";
import { useId, useState, type ReactNode } from "react";
import { problemOf } from "../../api/hooks/query";
import { useLlmCostReport } from "../../api/hooks/usage";
import type { CostReport } from "../../api/usage";
import { EmptyView } from "../../components/molecules/EmptyView";
import { LoadingView } from "../../components/molecules/LoadingView";
import { OptionSelect } from "../../components/molecules/OptionSelect";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { isRange, rangeStart, RANGES, type RangeKey } from "./costs";
import { BreakdownTable, DayTable, TopJobsTable, TotalTable } from "./LlmCostTables";

function Part({ title, children }: Readonly<{ title: string; children: ReactNode }>) {
  return (
    <div className="flex flex-col gap-3">
      <Typography.Heading level={3}>{title}</Typography.Heading>
      {children}
    </div>
  );
}

function Report({ report }: Readonly<{ report: CostReport }>) {
  if (report.total.calls === 0) {
    return (
      <EmptyView title="No LLM calls in this range">
        Jobs that use an LLM (a scrape or extract with a prompt, a learning run) show up here.
      </EmptyView>
    );
  }
  return (
    <div className="flex flex-col gap-6">
      <TotalTable total={report.total} />
      <Part title="By model and role">
        <BreakdownTable rows={report.breakdown} />
      </Part>
      <Part title="Top jobs by cost">
        <TopJobsTable jobs={report.top_jobs} />
      </Part>
      <Part title="Per day">
        <DayTable days={report.days} />
      </Part>
    </div>
  );
}

/** The LLM cost history (`GET /v1/usage/llm`) for a range of days through today. */
export function LlmCostSection() {
  const headingId = useId();
  const [range, setRange] = useState<RangeKey>("30");
  const report = useLlmCostReport(rangeStart(range));
  const problem = problemOf(report.error);
  let content: ReactNode;
  if (problem !== null) content = <ProblemAlert problem={problem} />;
  else if (report.isSuccess) content = <Report report={report.data} />;
  else content = <LoadingView label="Loading the LLM cost history" />;
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <Typography.Heading level={2} id={headingId}>
        LLM usage &amp; cost
      </Typography.Heading>
      <Typography.Paragraph size="sm" color="muted">
        Estimated from the providers&apos; list prices, for visibility; it is not a bill. A cost is
        unknown when its model has no price. Days are UTC. Calls made without a job (a schema
        drafted before its run) are not recorded, so they are not included.
      </Typography.Paragraph>
      <div className="sm:w-64">
        <OptionSelect
          label="Range"
          options={RANGES}
          value={range}
          accepts={isRange}
          onChange={setRange}
        />
      </div>
      {content}
    </section>
  );
}
