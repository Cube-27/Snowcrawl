import type { ReactNode } from "react";
import { useSearchParams } from "react-router";
import { useJobs } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import type { JobFilters, JobKind, JobStatus, JobSummary } from "../../api/jobs";
import { useCursorPages } from "../../components/lib/cursors";
import { EmptyView } from "../../components/molecules/EmptyView";
import { JOB_STATUS_TEXT } from "../../components/molecules/JobStatusLabel";
import { isOptionOf, OptionSelect } from "../../components/molecules/OptionSelect";
import { PagedTable } from "../../components/organisms/PagedTable";
import { JobRows } from "./JobRows";
import { KIND_LABELS } from "./kinds";

/** The filter choice that sends no filter. */
const ANY = "any";

const KIND_OPTIONS: Readonly<Record<JobKind | typeof ANY, string>> = {
  [ANY]: "Any kind",
  ...KIND_LABELS,
};

const STATUS_OPTIONS: Readonly<Record<JobStatus | typeof ANY, string>> = {
  [ANY]: "Any status",
  ...JOB_STATUS_TEXT,
};

const isKindOption = isOptionOf(KIND_OPTIONS);
const isStatusOption = isOptionOf(STATUS_OPTIONS);
const isKind = isOptionOf(KIND_LABELS);
const isStatus = isOptionOf(JOB_STATUS_TEXT);

// The filters live in the address, as the records screen's do: going back to the list keeps them.
function filtersOf(params: URLSearchParams): JobFilters {
  const kind = params.get("kind");
  const status = params.get("status");
  return { kind: isKind(kind) ? kind : null, status: isStatus(status) ? status : null };
}

function paramsOf({ kind, status }: JobFilters): Record<string, string> {
  return { ...(kind === null ? {} : { kind }), ...(status === null ? {} : { status }) };
}

function JobFilterFields({
  filters,
  onChange,
}: Readonly<{ filters: JobFilters; onChange: (filters: JobFilters) => void }>) {
  return (
    <div className="flex flex-wrap gap-4">
      <OptionSelect
        label="Kind"
        options={KIND_OPTIONS}
        value={filters.kind ?? ANY}
        accepts={isKindOption}
        onChange={(kind) => {
          onChange({ ...filters, kind: kind === ANY ? null : kind });
        }}
      />
      <OptionSelect
        label="Status"
        options={STATUS_OPTIONS}
        value={filters.status ?? ANY}
        accepts={isStatusOption}
        onChange={(status) => {
          onChange({ ...filters, status: status === ANY ? null : status });
        }}
      />
    </div>
  );
}

/** The Jobs screen's columns and their widths. */
const JOB_COLUMNS = [
  // Wide enough for the longest kind, "Batch scrape", untruncated.
  ["kind", "w-1/8"],
  ["target", "w-11/40"],
  ["status", "w-3/20"],
  ["tasks", "w-1/5"],
  ["created", "w-1/6"],
  ["open", "w-1/12"],
] as const;

/** The page's rows; an empty first page says why there are none. */
function JobPageContent({
  jobs,
  filters,
  first,
}: Readonly<{ jobs: readonly JobSummary[]; filters: JobFilters; first: boolean }>) {
  if (jobs.length > 0 || !first) return <JobRows label="Jobs" jobs={jobs} columns={JOB_COLUMNS} />;
  if (filters.scheduleId != null) {
    return (
      <EmptyView title="No runs yet">The schedule's runs are listed here as they start.</EmptyView>
    );
  }
  if (filters.kind !== null || filters.status !== null) {
    return (
      <EmptyView title="No job matches these filters">Choose another kind or status.</EmptyView>
    );
  }
  return <EmptyView title="No jobs yet">Runs started in the playground are listed here.</EmptyView>;
}

/** One page of the jobs that pass `filters`. */
export function JobPages({ filters }: Readonly<{ filters: JobFilters }>) {
  const { cursor, page, pageSize, setPageSize, previous, nextTo } = useCursorPages();
  const jobs = useJobs({ filters, cursor, limit: pageSize });
  return (
    <PagedTable
      problem={problemOf(jobs.error)}
      loading={jobs.isSuccess ? null : "Loading jobs"}
      page={page}
      onPrevious={previous}
      onNext={jobs.isSuccess ? nextTo(jobs.data.next_cursor) : null}
      rows={{
        total: jobs.data?.total ?? null,
        count: jobs.data?.items.length ?? 0,
        pageSize,
        onPageSize: setPageSize,
      }}
    >
      {jobs.isSuccess ? (
        <JobPageContent jobs={jobs.data.items} filters={filters} first={previous === null} />
      ) : null}
    </PagedTable>
  );
}

/** The jobs, newest first (`GET /v1/jobs`), filtered by kind and status, a page at a time; a row opens its job.
 * `tools` sit at the right of the filters' row. */
export function JobTable({ tools }: Readonly<{ tools: ReactNode }>) {
  const [params, setParams] = useSearchParams();
  const filters = filtersOf(params);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <JobFilterFields
          filters={filters}
          onChange={(next) => {
            setParams(paramsOf(next), { replace: true });
          }}
        />
        {tools}
      </div>
      {/* Keyed by the filters: other filters page from their own first page. */}
      <JobPages key={`${String(filters.kind)}/${String(filters.status)}`} filters={filters} />
    </div>
  );
}
