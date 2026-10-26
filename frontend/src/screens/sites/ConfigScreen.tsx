import { Button, Chip, Link, Tabs } from "@heroui/react";
import { useParams } from "react-router";
import type { ConfigDetail } from "../../api/configs";
import {
  useApproveConfig,
  useConfig,
  useRejectConfig,
  useSiteConfigs,
} from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { useSchema } from "../../api/hooks/schemas";
import { ConfigStatusLabel } from "../../components/molecules/StatusLabels";
import { LoadingView, ProblemAlert, QueryView } from "../../components/molecules/ViewStates";
import { Screen } from "../../components/templates/Screen";
import { ConfigFieldsTable, ConfigVersionsTable, useSchemaLabel } from "./ConfigTables";
import { CorrectionForm } from "./CorrectionForm";
import { ExportPanel } from "./ExportPanel";
import { LearnButtons, LearnRun, useLearn } from "./LearnAction";
import { sitePath } from "./paths";
import { ValidationReportView } from "./ValidationReportView";

/** The other versions of this config: the same site, schema and page kind. */
function SiblingVersions({ config }: Readonly<{ config: ConfigDetail }>) {
  const configs = useSiteConfigs(config.site);
  const schemaLabel = useSchemaLabel();
  return (
    <QueryView query={configs} loading="Loading the versions">
      {(data) => (
        <ConfigVersionsTable
          label="Versions"
          configs={data.filter(
            (other) => other.schema_id === config.schema_id && other.page_kind === config.page_kind,
          )}
          schemaLabel={schemaLabel}
        />
      )}
    </QueryView>
  );
}

/** The correction form needs the schema's fields: a correction may give steps to a field the config lacks. */
function Correction({ config }: Readonly<{ config: ConfigDetail }>) {
  const schema = useSchema(config.schema_id);
  return (
    <QueryView query={schema} loading="Loading the schema">
      {(data) => <CorrectionForm config={config} schemaFields={Object.keys(data.fields)} />}
    </QueryView>
  );
}

const TABS = ["Fields", "Validation", "Correct", "Export", "Versions"] as const;

function ConfigTabs({ config }: Readonly<{ config: ConfigDetail }>) {
  return (
    <Tabs>
      <Tabs.ListContainer>
        <Tabs.List aria-label="Config">
          {TABS.map((tab) => (
            <Tabs.Tab key={tab} id={tab}>
              {tab}
              <Tabs.Indicator />
            </Tabs.Tab>
          ))}
        </Tabs.List>
      </Tabs.ListContainer>
      <Tabs.Panel id="Fields">
        <ConfigFieldsTable config={config} />
      </Tabs.Panel>
      <Tabs.Panel id="Validation">
        <ValidationReportView report={config.validation_report} />
      </Tabs.Panel>
      <Tabs.Panel id="Correct">
        <Correction config={config} />
      </Tabs.Panel>
      <Tabs.Panel id="Export">
        <ExportPanel config={config} />
      </Tabs.Panel>
      <Tabs.Panel id="Versions">
        <SiblingVersions config={config} />
      </Tabs.Panel>
    </Tabs>
  );
}

/** Approves or rejects a learned config that waits for approval; shown only on such a config. */
function DecisionButtons({
  approve,
  reject,
}: Readonly<{
  approve: ReturnType<typeof useApproveConfig>;
  reject: ReturnType<typeof useRejectConfig>;
}>) {
  const busy = approve.isPending || reject.isPending;
  return (
    <>
      <Button
        variant="secondary"
        isPending={reject.isPending}
        isDisabled={busy}
        onPress={() => {
          reject.mutate();
        }}
      >
        Reject
      </Button>
      <Button
        isPending={approve.isPending}
        isDisabled={busy}
        onPress={() => {
          approve.mutate();
        }}
      >
        Approve
      </Button>
    </>
  );
}

function ConfigView({ config }: Readonly<{ config: ConfigDetail }>) {
  const learning = useLearn({ site: config.site, schemaId: config.schema_id });
  const approve = useApproveConfig(config.id);
  const reject = useRejectConfig(config.id);
  const learnProblem =
    problemOf(learning.learn.error) ?? problemOf(approve.error) ?? problemOf(reject.error);
  const schemaLabel = useSchemaLabel();
  const actions = (
    <>
      {config.status === "pending_approval" ? (
        <DecisionButtons approve={approve} reject={reject} />
      ) : null}
      <LearnButtons {...learning} />
    </>
  );
  return (
    <Screen
      title={config.site}
      actions={actions}
      badges={
        <>
          <Chip>v{config.version}</Chip>
          <Chip>{config.origin}</Chip>
          <ConfigStatusLabel status={config.status} />
          {/* In the header's badge row, beside the button that started it: nothing below moves. */}
          {learning.jobId === null ? null : <LearnRun jobId={learning.jobId} />}
        </>
      }
    >
      <ProblemAlert problem={learnProblem} />
      <div className="flex flex-wrap gap-4">
        <Link href={sitePath(config.site)}>Every config of {config.site}</Link>
        <span>
          {schemaLabel(config)}, {config.page_kind} pages
        </span>
      </div>
      <ConfigTabs config={config} />
    </Screen>
  );
}

/** One config version: its fields' steps, status, yield and validity, its validation report, the
 * correction form, export with the release report, and the other versions; "Learn now" in the header, and "Reject" and "Approve"
 * on a learned config waiting for approval. */
export function ConfigScreen() {
  const { id = "" } = useParams();
  const config = useConfig(id);
  const problem = problemOf(config.error);
  if (problem !== null) return <ProblemAlert problem={problem} />;
  if (!config.isSuccess) return <LoadingView label="Loading the config" />;
  // Keyed by id: a correction's link to the new version starts its form afresh.
  return <ConfigView key={config.data.id} config={config.data} />;
}
