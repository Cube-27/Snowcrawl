import { Button, Link, Table, Typography } from "@heroui/react";
import { useState } from "react";
import { useJobResults, useJobValues } from "../../../api/hooks/jobs";
import { problemOf } from "../../../api/hooks/query";
import type { JsonValuesItem, PageOutput } from "../../../api/jobs";
import { useCursorPages } from "../../../components/lib/cursors";
import { pageJson } from "../output/cleanJson";
import { OutcomeLabel } from "../../../components/molecules/StatusLabels";
import { PagedTable } from "../../../components/organisms/PagedTable";
import { ItemDrawer } from "./ItemDrawer";
import { ScrapeAsBatchBar, SelectionCheckbox, selectOnPage, type Selection } from "./scrapeAsBatch";

/** A page of the job and its place in the job (from 1): tasks keep their order (oldest first), so the place is the
 * row's identity. */
interface Row {
  id: number;
  item: JsonValuesItem;
}

/** The name of a row's JSON button: the page's place in the job. */
function pageLabel({ id }: Row): string {
  return `JSON of page ${String(id)}`;
}

/** The selected URLs of a table that has row selection, and how they change. */
interface UrlSelection {
  selected: ReadonlySet<string>;
  onSelect: (selected: Set<string>) => void;
}

/** React Aria's selection props for `rows`, whose keys are their places: a row is selected when its URL is. */
function selectionProps(rows: readonly Row[], { selected, onSelect }: UrlSelection) {
  return {
    selectionMode: "multiple" as const,
    selectedKeys: new Set(rows.filter(({ item }) => selected.has(item.url)).map(({ id }) => id)),
    onSelectionChange: (selection: Selection) => {
      onSelect(
        selectOnPage(
          selected,
          rows.map(({ id, item }) => ({ key: id, url: item.url })),
          selection,
        ),
      );
    },
  };
}

function PagesTable({
  rows,
  selection,
  onOpen,
}: Readonly<{
  rows: readonly Row[];
  selection: UrlSelection | null;
  onOpen: (row: Row) => void;
}>) {
  return (
    <Table>
      <Table.ScrollContainer>
        {/* Fixed column widths: long URLs truncate rather than moving the columns. */}
        <Table.Content
          aria-label="Pages"
          className="table-fixed"
          {...(selection === null ? {} : selectionProps(rows, selection))}
        >
          <Table.Header>
            {selection === null ? null : (
              <Table.Column className="w-12">
                <SelectionCheckbox label="Select this page" />
              </Table.Column>
            )}
            <Table.Column isRowHeader>URL</Table.Column>
            <Table.Column className="w-1/6">Outcome</Table.Column>
            <Table.Column className="w-1/6 numeric">Records</Table.Column>
            <Table.Column className="w-1/6">JSON</Table.Column>
          </Table.Header>
          <Table.Body
            items={rows}
            renderEmptyState={() => (
              <Typography.Paragraph color="muted">No page has a result yet.</Typography.Paragraph>
            )}
          >
            {(row) => (
              <Table.Row id={row.id}>
                {selection === null ? null : (
                  <Table.Cell>
                    <SelectionCheckbox label="Select" />
                  </Table.Cell>
                )}
                <Table.Cell className="truncate">
                  {/* The page itself, in a new tab: it is another site, not a screen of this app. */}
                  <Link href={row.item.url} target="_blank" rel="noreferrer">
                    {row.item.url}
                  </Link>
                </Table.Cell>
                <Table.Cell>
                  {row.item.outcome === null ? "—" : <OutcomeLabel outcome={row.item.outcome} />}
                </Table.Cell>
                <Table.Cell className="numeric">{row.item.records.length}</Table.Cell>
                <Table.Cell>
                  <Button
                    size="sm"
                    variant="secondary"
                    aria-label={pageLabel(row)}
                    onPress={() => {
                      onOpen(row);
                    }}
                  >
                    JSON
                  </Button>
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The page output at `offset` in a read of `format=json`, once it holds page outputs; undefined
 * until then, so the drawer keeps showing the item. */
function pageOutput(
  results: ReturnType<typeof useJobResults>["data"],
  offset: number,
): PageOutput | undefined {
  return results?.kind === "pages" ? results.page.items[offset] : undefined;
}

/** The drawer of one page, mounted only while it is open, so that what it reads is read only then. A job without a
 * schema (`outputs`) shows the page's outputs, read from `format=json` at the same cursor as the table's page, whose
 * pages hold the same tasks in the same order; any other shows the item's records. How the page was
 * fetched is on the page output, or on `json_values` asked for it. `offset` is the row's index in the page. */
function PageDrawer({
  jobId,
  page,
  offset,
  row,
  outputs,
  onClose,
}: Readonly<{
  jobId: string;
  page: { cursor: string | null; limit: number };
  offset: number;
  row: Row;
  outputs: boolean;
  onClose: () => void;
}>) {
  // Page outputs can be large: they are read only for a job that shows them.
  const pageOutputs = useJobResults(jobId, page, outputs).data;
  const fetched = useJobValues(
    jobId,
    { ...page, withMetadata: false, withFetch: true },
    !outputs,
  ).data;
  const output = pageOutput(pageOutputs, offset);
  const { item } = row;
  return (
    <ItemDrawer
      url={item.url}
      value={outputs ? (output ?? item) : pageJson(item.records, item.metadata)}
      fetch={(output ?? fetched?.items[offset])?.fetch}
      artifacts={output ?? fetched?.items[offset]}
      position={row.id}
      onClose={onClose}
    />
  );
}

/** A batch's or crawl's pages, one row each in task order, from `results?format=json_values`; each
 * row's JSON and fetch open in a drawer (`PageDrawer`). The job's events read the page shown again as tasks end. With
 * `selectable` (a crawl's pages), rows are selected by URL across pages, and the selected URLs open as a batch
 */
export function ResultsTable({
  jobId,
  withMetadata,
  selectable,
  outputs,
}: Readonly<{ jobId: string; withMetadata: boolean; selectable: boolean; outputs: boolean }>) {
  const { cursor, page, pageSize, setPageSize, previous, nextTo } = useCursorPages();
  const values = useJobValues(jobId, {
    cursor,
    limit: pageSize,
    withMetadata,
    withFetch: false,
  });
  // The open drawer's row, by its place: its item is read from the page shown, so a refresh updates it.
  const [openId, setOpenId] = useState<number | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const data = values.data;
  const first = (page - 1) * pageSize + 1;
  const rows = (data?.items ?? []).map((item, offset) => ({ id: first + offset, item }));
  const open = rows.find(({ id }) => id === openId);
  return (
    <>
      {selectable ? <ScrapeAsBatchBar selected={selected} /> : null}
      <PagedTable
        problem={problemOf(values.error)}
        loading={data === undefined ? "Loading the pages" : null}
        page={page}
        onPrevious={previous}
        onNext={data === undefined ? null : nextTo(data.next_cursor)}
        rows={{ total: data?.total ?? null, count: rows.length, pageSize, onPageSize: setPageSize }}
      >
        <PagesTable
          rows={rows}
          selection={selectable ? { selected, onSelect: setSelected } : null}
          onOpen={({ id }) => {
            setOpenId(id);
          }}
        />
      </PagedTable>
      {open === undefined ? null : (
        <PageDrawer
          jobId={jobId}
          page={{ cursor, limit: pageSize }}
          offset={open.id - first}
          row={open}
          outputs={outputs}
          onClose={() => {
            setOpenId(null);
          }}
        />
      )}
    </>
  );
}
