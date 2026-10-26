import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { parseFieldTypes } from "../../api/field";
import { problemOf } from "../../api/hooks/query";
import { useRecords } from "../../api/hooks/records";
import { useSchema } from "../../api/hooks/schemas";
import { activeFilters, type RecordFilters, type StoredRecord } from "../../api/records";
import { useCursorPages } from "../../components/lib/cursors";
import { EmptyView } from "../../components/molecules/EmptyView";
import { PageHeader } from "../../components/templates/PageHeader";
import { PagedTable } from "../../components/organisms/PagedTable";
import { RecordTable } from "../../components/organisms/RecordTable";
import { RecordFiltersForm } from "./RecordFilters";
import { RecordInspector } from "./RecordInspector";

// The filters live in the URL, so a filtered list can be reloaded, shared and returned to.
function filtersOf(params: URLSearchParams): RecordFilters {
  return {
    schema_id: params.get("schema_id") ?? "",
    job_id: params.get("job_id") ?? "",
    site: params.get("site") ?? "",
    changed_since: params.get("changed_since") ?? "",
  };
}

/** One page of the filtered records with previous and next. */
function RecordResults({ filters }: Readonly<{ filters: RecordFilters }>) {
  const { cursor, page, pageSize, setPageSize, previous, nextTo } = useCursorPages();
  const [open, setOpen] = useState<StoredRecord | null>(null);
  const records = useRecords({ filters, cursor, limit: pageSize });
  const schema = useSchema(filters.schema_id);
  // Stable while the schema is, so opening a record leaves the table's columns and cells as they are.
  const fieldTypes = useMemo(
    () => parseFieldTypes(schema.data?.fields, schema.data?.field_order),
    [schema.data],
  );
  const ready = records.isSuccess && schema.isSuccess;
  const items = records.data?.items ?? [];

  return (
    <>
      <PagedTable
        problem={problemOf(records.error ?? schema.error)}
        loading={ready ? null : "Loading records"}
        page={page}
        onPrevious={previous}
        onNext={records.isSuccess ? nextTo(records.data.next_cursor) : null}
        rows={{
          total: records.data?.total ?? null,
          count: items.length,
          pageSize,
          onPageSize: setPageSize,
        }}
      >
        {items.length === 0 ? (
          <EmptyView title="No records">No record matches these filters.</EmptyView>
        ) : (
          <RecordTable label="Records" records={items} fieldTypes={fieldTypes} onOpen={setOpen} />
        )}
      </PagedTable>
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

/** Stored records by schema, job, site and change time: a column per schema field, the fields each
 * record changed, and an inspector per record. */
export function RecordsScreen() {
  const [params, setParams] = useSearchParams();
  const filters = filtersOf(params);
  const key = new URLSearchParams(activeFilters(filters)).toString();
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title="Records" />
      <RecordFiltersForm
        key={key}
        filters={filters}
        onSubmit={(next) => {
          setParams(activeFilters(next));
        }}
      />
      {filters.schema_id === "" ? (
        <EmptyView title="No schema chosen yet">
          Records are listed by schema, with a column for each of its fields.
        </EmptyView>
      ) : (
        <RecordResults key={key} filters={filters} />
      )}
    </section>
  );
}
