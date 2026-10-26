import { Drawer, Typography } from "@heroui/react";
import { useState } from "react";
import type { FieldTypes } from "../../api/field";
import type { ExtractedRecord } from "../../api/records";
import { DEFAULT_PAGE_SIZE, PAGE_SIZES } from "../lib/cursors";
import { EmptyView } from "../molecules/EmptyView";
import { PagedTable } from "./PagedTable";
import { RecordFieldsTable, RecordSummary } from "./RecordFields";
import { RecordTable } from "./RecordTable";

/** One record's every field attribute in a drawer over the table. */
function RecordDrawer({
  record,
  fieldTypes,
  onClose,
}: Readonly<{ record: ExtractedRecord; fieldTypes: FieldTypes; onClose: () => void }>) {
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement="right">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>Record</Drawer.Heading>
              <RecordSummary record={record} />
            </Drawer.Header>
            <Drawer.Body>
              <RecordFieldsTable label="Record fields" record={record} fieldTypes={fieldTypes} />
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}

/** The records one answer holds (a scrape of a listing page, a replay) as a table of one row per record. The answer
 * is one response already in memory, so its pages are slices of it, and the table shows one page of at most 100 rows.
 * A row opens its record's fields with their status, evidence and source in a drawer. A few records need no pager. Without a record, why there are none. */
export function ExtractedRecords({
  records,
  fieldTypes,
}: Readonly<{ records: readonly ExtractedRecord[]; fieldTypes: FieldTypes }>) {
  const [{ page, pageSize }, setPosition] = useState({ page: 1, pageSize: DEFAULT_PAGE_SIZE });
  const [open, setOpen] = useState<ExtractedRecord | null>(null);
  const drawer =
    open === null ? null : (
      <RecordDrawer
        record={open}
        fieldTypes={fieldTypes}
        onClose={() => {
          setOpen(null);
        }}
      />
    );
  if (records.length === 0) {
    return <EmptyView title="No records">The page yielded no record for this schema.</EmptyView>;
  }
  if (records.length <= Math.min(...PAGE_SIZES)) {
    return (
      <>
        <div className="flex flex-col gap-2">
          <Typography.Paragraph size="sm" color="muted">
            {records.length === 1 ? "1 record" : `${String(records.length)} records`}
          </Typography.Paragraph>
          <RecordTable label="Records" records={records} fieldTypes={fieldTypes} onOpen={setOpen} />
        </div>
        {drawer}
      </>
    );
  }
  const first = (page - 1) * pageSize;
  const shown = records.slice(first, first + pageSize);
  return (
    <>
      <PagedTable
        problem={null}
        loading={null}
        page={page}
        onPrevious={
          page > 1
            ? () => {
                setPosition({ page: page - 1, pageSize });
              }
            : null
        }
        onNext={
          first + pageSize < records.length
            ? () => {
                setPosition({ page: page + 1, pageSize });
              }
            : null
        }
        rows={{
          total: records.length,
          count: shown.length,
          pageSize,
          onPageSize: (size) => {
            setPosition({ page: 1, pageSize: size });
          },
        }}
      >
        <RecordTable label="Records" records={shown} fieldTypes={fieldTypes} onOpen={setOpen} />
      </PagedTable>
      {drawer}
    </>
  );
}
