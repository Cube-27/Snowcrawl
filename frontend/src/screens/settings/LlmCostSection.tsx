import { Typography } from "@heroui/react";
import { useState, type ReactNode } from "react";
import { useLlmCostReport } from "../../api/hooks/usage";
import type { CostReport } from "../../api/usage";
import { EmptyView, QueryView } from "../../components/molecules/ViewStates";
import { OptionSelect } from "../../components/molecules/OptionSelect";
import { isRange, rangeStart, RANGES, type RangeKey } from "./costs";
import { BreakdownTable, DayTable, TopJobsTable, TotalTable } from "./LlmCostTables";
import { Section } from "../../components/templates/Screen";

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
  const [range, setRange] = useState<RangeKey>("30");
  const report = useLlmCostReport(rangeStart(range));
  return (
    <Section
      title="LLM usage & cost"
      description="Estimated from the providers' list prices, for visibility; it is not a bill. A cost is unknown when its model has no price. Days are UTC. Calls made without a job (a schema drafted before its run) are not recorded, so they are not included."
    >
      <div className="sm:w-64">
        <OptionSelect
          label="Range"
          options={RANGES}
          value={range}
          accepts={isRange}
          onChange={setRange}
        />
      </div>
      <QueryView query={report} loading="Loading the LLM cost history">
        {(data) => <Report report={data} />}
      </QueryView>
    </Section>
  );
}
