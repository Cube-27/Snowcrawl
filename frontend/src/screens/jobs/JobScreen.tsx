import { Typography } from "@heroui/react";
import { Navigate, useParams } from "react-router";
import { useJob } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import { cancelState, type JobSummary } from "../../api/jobs";
import { CancelJobAction } from "../../components/molecules/CancelJobAction";
import { EventLog } from "../../components/organisms/EventLog";
import { JobStatusLabel } from "../../components/molecules/JobStatusLabel";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { RunProgress } from "../../components/molecules/RunProgress";
import { Section } from "../../components/templates/Section";
import { StatusLabel } from "../../components/atoms/StatusLabel";
import { Timestamp } from "../../components/atoms/Timestamp";
import { WebhookDeliveries } from "../../components/organisms/WebhookDeliveries";
import { UsageSummary } from "../../components/molecules/UsageSummary";
import { formatUsd } from "../../components/lib/usd";
import { ExportActions } from "./ExportActions";
import { JobResults } from "./JobResults";
import { runPath } from "../playground/modes";
import { KIND_LABELS } from "./kinds";

function JobTimes({ job }: Readonly<{ job: JobSummary }>) {
  return (
    <Typography.Paragraph size="sm" color="muted">
      Job <Typography.Code>{job.id}</Typography.Code> · created <Timestamp value={job.created_at} />{" "}
      · started <Timestamp value={job.started_at} /> · finished{" "}
      <Timestamp value={job.finished_at} />
    </Typography.Paragraph>
  );
}

function JobView({ job }: Readonly<{ job: JobSummary }>) {
  // A cancel asked for ends the job once its claimed tasks end, which a dead worker's lease delays.
  const cancel = cancelState(job);
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader
        title={`${KIND_LABELS[job.kind]} job`}
        actions={cancel === "cancellable" ? <CancelJobAction id={job.id} /> : undefined}
      >
        <JobStatusLabel status={job.status} />
        {cancel === "requested" ? <StatusLabel text="cancel requested" tone="warning" /> : null}
      </PageHeader>
      <JobTimes job={job} />
      <Section title="Progress">
        <RunProgress job={job} />
      </Section>
      <Section title="Usage">
        <UsageSummary usage={job.usage} />
        <Typography.Paragraph size="sm">
          Estimated LLM cost:{" "}
          {job.llm_cost_usd === null
            ? "unknown, some of its tokens have no price"
            : formatUsd(job.llm_cost_usd)}
        </Typography.Paragraph>
      </Section>
      <EventLog jobId={job.id} />
      <WebhookDeliveries jobId={job.id} webhookUrl={job.webhook_url} />
      <Section title="Results">
        <ExportActions jobId={job.id} />
        <JobResults jobId={job.id} />
      </Section>
    </section>
  );
}

/** One job. A job a playground mode runs redirects to its run there; a `learn` job keeps this page:
 * Its status and progress, usage, live events, results with exports, and a cancel. */
export function JobScreen() {
  const { id = "" } = useParams();
  const job = useJob(id);
  const problem = problemOf(job.error);
  if (problem !== null) return <ProblemAlert problem={problem} />;
  if (!job.isSuccess) return <LoadingView label="Loading the job" />;
  const run = runPath(job.data.kind, job.data.id);
  if (run !== null) return <Navigate replace to={run} />;
  return <JobView key={id} job={job.data} />;
}
