import { useNavigate } from "react-router";
import { LookupForm } from "../../components/molecules/LookupForm";
import { Screen } from "../../components/templates/Screen";
import { JobTable } from "./JobTable";
import { jobPath } from "./paths";

/** The run history: every job, newest first, a row opening its run in its playground
 * mode; a job also opens by its id. Runs start in the playground. */
export function JobsScreen() {
  const navigate = useNavigate();
  return (
    <Screen
      title="Jobs"
      description="Every run, newest first. Open a row to see its results in the playground."
    >
      <JobTable
        tools={
          <LookupForm
            label="Job ID"
            placeholder="Paste a job id"
            submitLabel="Open job"
            onOpen={(id) => void navigate(jobPath(id))}
          />
        }
      />
    </Screen>
  );
}
