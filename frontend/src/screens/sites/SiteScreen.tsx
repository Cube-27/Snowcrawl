import { Link } from "@heroui/react";
import { useParams } from "react-router";
import { useSiteConfigs } from "../../api/hooks/configs";
import { problemOf } from "../../api/hooks/query";
import { EmptyView } from "../../components/molecules/EmptyView";
import { LoadingView } from "../../components/molecules/LoadingView";
import { PageHeader } from "../../components/templates/PageHeader";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { recipesPath } from "../recipes/paths";
import { ConfigVersionsTable, useSchemaLabel } from "./ConfigTables";
import { SiteLearnForm } from "./LearnAction";

/** One site's config versions: status and field status counts per version; a row opens one. "Learn a
 * config" starts learning for a schema chosen here, so a site without a config can get its first; "Recipes" opens
 * the site's custom recipes. */
export function SiteScreen() {
  const { host = "" } = useParams();
  const configs = useSiteConfigs(host);
  const schemaLabel = useSchemaLabel();
  const problem = problemOf(configs.error);

  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title={host} actions={<Link href={recipesPath(host)}>Recipes</Link>} />
      {configs.isPending ? <LoadingView label="Loading the site's configs" /> : null}
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {configs.isSuccess && configs.data.length === 0 ? (
        <EmptyView title="No configs yet">
          A config appears here once learning has run for this site. Learn one below.
        </EmptyView>
      ) : null}
      {configs.isSuccess && configs.data.length > 0 ? (
        <ConfigVersionsTable
          label="Config versions"
          configs={configs.data}
          schemaLabel={schemaLabel}
        />
      ) : null}
      {configs.isSuccess ? <SiteLearnForm site={host} /> : null}
    </section>
  );
}
