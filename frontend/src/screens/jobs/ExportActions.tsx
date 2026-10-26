import { Button, toast } from "@heroui/react";
import { useExportJob } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import type { ExportFormat } from "../../api/jobs";
import { saveExportPages } from "../../app/download";
import { MoreExports } from "../../components/molecules/MoreExports";
import { ProblemAlert } from "../../components/molecules/ViewStates";

// Each export format's label and file: RFC 8259 JSON, RFC 4180 CSV, RFC 7763 Markdown.
interface FileFormat {
  format: ExportFormat;
  label: string;
  extension: string;
  type: string;
}

const FORMATS: readonly FileFormat[] = [
  { format: "json_records", label: "Export JSON", extension: "json", type: "application/json" },
  { format: "csv_records", label: "Export CSV", extension: "csv", type: "text/csv" },
  { format: "markdown", label: "Export Markdown", extension: "md", type: "text/markdown" },
];

/** The job's records exported by the server and saved as files. A job without a schema has no records
 * to export; the server's problem says so. */
export function ExportActions({ jobId }: Readonly<{ jobId: string }>) {
  const exported = useExportJob(jobId);
  const problem = problemOf(exported.error);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap justify-end gap-2">
        {FORMATS.map((file) => (
          <Button
            key={file.format}
            variant="secondary"
            isPending={exported.isPending && exported.variables === file.format}
            isDisabled={exported.isPending}
            onPress={() => {
              exported.mutate(file.format, {
                onSuccess: (pages) => {
                  const files = saveExportPages(jobId, file, pages);
                  toast.success(`Saved ${files.join(", ")}.`);
                },
              });
            }}
          >
            {file.label}
          </Button>
        ))}
        <MoreExports jobId={jobId} withoutRecords={false} isDisabled={exported.isPending} />
      </div>
      <ProblemAlert problem={problem} />
    </div>
  );
}
