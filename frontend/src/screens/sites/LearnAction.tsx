import { Button, Fieldset, Form, Link, toast, Typography } from "@heroui/react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { useJobEvents } from "../../api/hooks/events";
import { useJob } from "../../api/hooks/jobs";
import { useLearnSite } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { FormActions } from "../../components/molecules/FormActions";
import { JobStatusLabel } from "../../components/molecules/StatusLabels";
import { ProblemAlert } from "../../components/molecules/ViewStates";
import { SchemaSelect } from "../../components/molecules/SchemaSelect";
import { jobPath } from "../jobs/paths";

/** "Learn now" for a site and schema (`POST /v1/sites/{host}/learn`). The server refuses while a
 * config is active or a run is in flight (409); after a refusal, "Learn anyway" asks again with `force`. The screen
 * shows `learn.error`. */
export function useLearn({ site, schemaId }: Readonly<{ site: string; schemaId: string }>) {
  const learn = useLearnSite(site);
  const navigate = useNavigate();
  // The learning job this screen started, shown by `LearnRun` until the screen is left.
  const [jobId, setJobId] = useState<string | null>(null);
  const start = (force: boolean) => {
    learn.mutate(
      { schema_id: schemaId, force },
      {
        onSuccess: (id) => {
          setJobId(id);
          toast.success(`Learning started as job ${id}.`, {
            actionProps: {
              children: "Open job",
              onPress: () => void navigate(jobPath(id)),
            },
          });
        },
      },
    );
  };
  return { learn, start, jobId };
}

/** The learning job started here: its status as it runs (its events read the job again) and a link to its
 * page, so a run that fails says so where it was started. */
export function LearnRun({ jobId }: Readonly<{ jobId: string }>) {
  const job = useJob(jobId);
  useJobEvents(jobId);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Typography.Paragraph size="sm">Learning job</Typography.Paragraph>
      {job.data === undefined ? null : <JobStatusLabel status={job.data.status} />}
      <Link href={jobPath(jobId)}>Open the job</Link>
    </div>
  );
}

/** "Learn anyway" after a refusal, then "Learn now"; `submits` makes "Learn now" its form's submit button. */
export function LearnButtons({
  learn,
  start,
  submits = false,
}: Readonly<ReturnType<typeof useLearn> & { submits?: boolean }>) {
  return (
    <>
      {learn.isError ? (
        <Button
          variant="secondary"
          isPending={learn.isPending}
          onPress={() => {
            start(true);
          }}
        >
          Learn anyway
        </Button>
      ) : null}
      <Button
        isPending={learn.isPending}
        {...(submits
          ? { type: "submit" as const }
          : {
              onPress: () => {
                start(false);
              },
            })}
      >
        Learn now
      </Button>
    </>
  );
}

/** Learning a config for the site from a schema chosen here: the way to a site's first config. */
export function SiteLearnForm({ site }: Readonly<{ site: string }>) {
  const [schemaId, setSchemaId] = useState("");
  const learning = useLearn({ site, schemaId });
  const problem = problemOf(learning.learn.error);
  return (
    <Form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        learning.start(false);
      }}
    >
      <Fieldset>
        <Fieldset.Legend>Learn a config</Fieldset.Legend>
        <SchemaSelect value={schemaId} onChange={setSchemaId} />
      </Fieldset>
      <FormActions>
        <LearnButtons {...learning} submits />
      </FormActions>
      {/* Under the buttons: a refusal or a run moves nothing above them. */}
      <ProblemAlert problem={problem} />
      {learning.jobId === null ? null : <LearnRun jobId={learning.jobId} />}
    </Form>
  );
}
