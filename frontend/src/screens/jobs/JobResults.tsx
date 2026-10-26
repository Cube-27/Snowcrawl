import { Table } from "@heroui/react";
import { useCallback, useMemo, useState } from "react";
import { parseFieldTypes } from "../../api/field";
import { useJobResults } from "../../api/hooks/jobs";
import { problemOf } from "../../api/hooks/query";
import { useSchema } from "../../api/hooks/schemas";
import type { PageOutput } from "../../api/jobs";
import type { StoredRecord } from "../../api/records";
import { useCursorPages } from "../../components/lib/cursors";
import { EmptyView, LoadingView, ProblemAlert } from "../../components/molecules/ViewStates";
import { OutcomeLabel, TaskStatusLabel } from "../../components/molecules/StatusLabels";
import { RecordTable, type TableRecord } from "../../components/organisms/RecordTable";
import { Timestamp } from "../../components/atoms/Timestamp";
import { PagedTable } from "../../components/organisms/PagedTable";
import { TruncatedText } from "../../components/atoms/TruncatedText";
import { cleanFields } from "../playground/output/cleanJson";
import { RecordInspector } from "../records/RecordInspector";

/** The job's records in the columns of their schema, each opening the record inspector. */
function JobRecords({ records }: Readonly<{ records: readonly StoredRecord[] }>) {
  const [open, setOpen] = useState<StoredRecord | null>(null);
  // A job extracts with one schema.
  const schema = useSchema(records[0]?.schema_id ?? null);
  const fieldTypes = useMemo(
    () => parseFieldTypes(schema.data?.fields, schema.data?.field_order),
    [schema.data],
  );
  const json = useCallback(
    (record: TableRecord) => cleanFields(record.fields, fieldTypes),
    [fieldTypes],
  );
  const problem = problemOf(schema.error);
  if (records.length === 0) return <EmptyView title="No records yet" />;
  if (problem !== null) return <ProblemAlert problem={problem} />;
  if (!schema.isSuccess) return <LoadingView label="Loading the schema" />;
  return (
    <>
      <RecordTable
        label="Records"
        records={records}
        fieldTypes={fieldTypes}
        onOpen={setOpen}
        json={json}
      />
      {open === null ? null : (
        <RecordInspector
          key={open.id}
          record={open}
          fieldTypes={fieldTypes}
          onClose={() => {
            setOpen(null);
          }}
        />
      )}
    </>
  );
}

/** A job without a schema: each of its pages with its status, its outcome, and why it failed or ended as it did when
 * the outcome alone does not say (`last_error`). */
function PageOutputs({ pages }: Readonly<{ pages: readonly PageOutput[] }>) {
  return (
    <Table>
      <Table.ScrollContainer>
        {/* Fixed column widths: a page of long URLs truncates rather than moving the columns. */}
        <Table.Content aria-label="Pages" className="table-fixed">
          <Table.Header>
            <Table.Column isRowHeader className="w-1/3">
              URL
            </Table.Column>
            <Table.Column className="w-1/8">Status</Table.Column>
            <Table.Column className="w-1/8">Outcome</Table.Column>
            <Table.Column className="w-1/4">Reason</Table.Column>
            <Table.Column className="w-1/6">Fetched</Table.Column>
          </Table.Header>
          <Table.Body items={pages.map((page) => ({ id: page.task_id, page }))}>
            {({ id, page }) => (
              <Table.Row id={id}>
                <Table.Cell className="truncate">{page.final_url ?? page.url}</Table.Cell>
                <Table.Cell>
                  <TaskStatusLabel status={page.status} />
                </Table.Cell>
                <Table.Cell>
                  {page.outcome === null ? "—" : <OutcomeLabel outcome={page.outcome} />}
                </Table.Cell>
                <Table.Cell>
                  {page.last_error === null ? "—" : <TruncatedText text={page.last_error} />}
                </Table.Cell>
                <Table.Cell className="truncate">
                  <Timestamp value={page.fetched_at} />
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** One page of the job's results (`GET /v1/jobs/{id}/results`) with previous and next: its records, or the pages of a
 * job without a schema. */
export function JobResults({ jobId }: Readonly<{ jobId: string }>) {
  const { cursor, page, pageSize, setPageSize, previous, nextTo } = useCursorPages();
  const results = useJobResults(jobId, { cursor, limit: pageSize });
  const data = results.data;
  return (
    <PagedTable
      problem={problemOf(results.error)}
      loading={data === undefined ? "Loading results" : null}
      page={page}
      onPrevious={previous}
      onNext={data === undefined ? null : nextTo(data.page.next_cursor)}
      rows={{
        total: data?.page.total ?? null,
        count: data?.page.items.length ?? 0,
        pageSize,
        onPageSize: setPageSize,
      }}
    >
      {data?.kind === "records" ? <JobRecords records={data.page.items} /> : null}
      {data?.kind === "pages" ? <PageOutputs pages={data.page.items} /> : null}
    </PagedTable>
  );
}
