import { Button, Dropdown, toast } from "@heroui/react";
import { useExportJob, useExportJobFile } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import type { JobFile } from "../../api/jobs";
import { saveBlob, saveExportPages } from "../../app/download";

// JSON Lines is a text export by cursor like CSV; the others are made whole by the server: a workbook
// (ECMA-376 Office Open XML), an Apache Parquet file, and the `llms.txt` pair (llmstxt.org).
const LINES = { extension: "jsonl", type: "application/x-ndjson" };
const FILES: readonly { file: JobFile; label: string; records: boolean }[] = [
  { file: "xlsx", label: "Excel workbook (.xlsx)", records: true },
  { file: "parquet", label: "Parquet (.parquet)", records: true },
  { file: "llms.txt", label: "llms.txt", records: false },
  { file: "llms-full.txt", label: "llms-full.txt", records: false },
];

function notifyRefused(error: Error) {
  const problem = problemOf(error);
  toast.danger(problem?.title ?? "The export failed.", {
    description: problem?.detail ?? undefined,
  });
}

/** The job's other exports in a menu: JSON Lines, Excel, Parquet and `llms.txt` / `llms-full.txt`. `withoutRecords`
 * (a job without a schema) leaves out the ones made of records. */
export function MoreExports({
  jobId,
  withoutRecords,
  isDisabled,
}: Readonly<{ jobId: string; withoutRecords: boolean; isDisabled: boolean }>) {
  const lines = useExportJob(jobId);
  const made = useExportJobFile(jobId);
  const busy = lines.isPending || made.isPending;
  return (
    <Dropdown>
      <Button variant="secondary" isPending={busy} isDisabled={isDisabled || busy}>
        More exports
      </Button>
      <Dropdown.Popover>
        <Dropdown.Menu
          aria-label="More exports"
          onAction={(key) => {
            if (key === "jsonl") {
              lines.mutate("jsonl", {
                onSuccess: (pages) => {
                  toast.success(`Saved ${saveExportPages(jobId, LINES, pages).join(", ")}.`);
                },
                onError: notifyRefused,
              });
              return;
            }
            const chosen = FILES.find(({ file }) => file === key);
            if (chosen === undefined) return;
            made.mutate(chosen.file, {
              onSuccess: (blob) => {
                const filename = chosen.records
                  ? `job-${jobId}.${chosen.file}`
                  : `job-${jobId}-${chosen.file}`;
                saveBlob(filename, blob);
                toast.success(`Saved ${filename}.`);
              },
              onError: notifyRefused,
            });
          }}
        >
          <Dropdown.Item id="jsonl" textValue="JSON Lines" isDisabled={withoutRecords}>
            JSON Lines (.jsonl)
          </Dropdown.Item>
          {FILES.map(({ file, label, records }) => (
            <Dropdown.Item
              key={file}
              id={file}
              textValue={label}
              isDisabled={records && withoutRecords}
            >
              {label}
            </Dropdown.Item>
          ))}
        </Dropdown.Menu>
      </Dropdown.Popover>
    </Dropdown>
  );
}
