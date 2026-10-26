import { Button, toast } from "@heroui/react";
import { ArrowDownToLine } from "@gravity-ui/icons";
import { useExportJob, useExportJobJson } from "../../../api/hooks/jobs";
import { problemOf } from "../../../api/hooks/query";
import { saveExportPages, saveJson } from "../../../app/download";
import { MoreExports } from "../../../components/molecules/MoreExports";

// RFC 4180 §3: CSV's media type.
const CSV = { extension: "csv", type: "text/csv" };

/** A refused export, as a toast: it is the outcome of the button just pressed, and an alert in the run's header moved
 * the page under it. */
function notifyRefused(error: Error) {
  const problem = problemOf(error);
  toast.danger(problem?.title ?? "The export failed.", {
    description: problem?.detail ?? undefined,
  });
}

/** A run's downloads (UI): JSON is every page's `json_values` item in task order, saved as one array, so
 * it equals the drawers' items; CSV is the server's `csv_records` export (RFC 4180), one file per page of it. With
 * `outputs` (a job without a schema), JSON is every page's outputs, as its drawers show them, and CSV is disabled:
 * there are no records to put in columns. */
export function RunExports({
  jobId,
  withMetadata,
  outputs,
}: Readonly<{ jobId: string; withMetadata: boolean; outputs: boolean }>) {
  const json = useExportJobJson(jobId, outputs);
  const csv = useExportJob(jobId);
  const busy = json.isPending || csv.isPending;
  return (
    <div className="flex flex-wrap gap-2">
      <Button
        variant="secondary"
        isPending={json.isPending}
        isDisabled={busy}
        onPress={() => {
          json.mutate(withMetadata, {
            onSuccess: (items) => {
              const filename = `job-${jobId}.json`;
              saveJson(filename, items);
              toast.success(`Saved ${filename}.`);
            },
            onError: notifyRefused,
          });
        }}
      >
        <ArrowDownToLine aria-hidden="true" />
        Export JSON
      </Button>
      <Button
        variant="secondary"
        isPending={csv.isPending}
        isDisabled={busy || outputs}
        onPress={() => {
          csv.mutate("csv_records", {
            onSuccess: (pages) => {
              const files = saveExportPages(jobId, CSV, pages);
              toast.success(`Saved ${files.join(", ")}.`);
            },
            onError: notifyRefused,
          });
        }}
      >
        <ArrowDownToLine aria-hidden="true" />
        Export CSV
      </Button>
      <MoreExports jobId={jobId} withoutRecords={outputs} isDisabled={busy} />
    </div>
  );
}
