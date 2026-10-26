import { Link } from "@heroui/react";
import { useParams } from "react-router";
import { useSiteConfigs } from "../../api/hooks/configs";
import { EmptyView, QueryView } from "../../components/molecules/ViewStates";
import { Screen } from "../../components/templates/Screen";
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

  return (
    <Screen title={host} actions={<Link href={recipesPath(host)}>Recipes</Link>}>
      <QueryView
        query={configs}
        loading="Loading the site's configs"
        isEmpty={(data) => data.length === 0}
        empty={
          <EmptyView title="No configs yet">
            A config appears here once learning has run for this site. Learn one below.
          </EmptyView>
        }
      >
        {(data) => (
          <ConfigVersionsTable label="Config versions" configs={data} schemaLabel={schemaLabel} />
        )}
      </QueryView>
      {configs.isSuccess ? <SiteLearnForm site={host} /> : null}
    </Screen>
  );
}
