import { Link, Table } from "@heroui/react";
import { useState } from "react";
import { DEFAULT_PAGE_SIZE } from "../../../components/lib/cursors";
import { PagedTable } from "../../../components/organisms/PagedTable";
import { SelectionCheckbox, selectOnPage } from "../run/scrapeAsBatch";

/** The URLs a map found, a page at a time in the order the server returned them, with row selection
 * (`selected`, kept across pages). The whole list is in the map's answer, so the pages are slices of it. */
export function LinksTable({
  urls,
  selected,
  onSelect,
}: Readonly<{
  urls: readonly string[];
  selected: ReadonlySet<string>;
  onSelect: (selected: Set<string>) => void;
}>) {
  const [page, setPage] = useState(1);
  const first = (page - 1) * DEFAULT_PAGE_SIZE;
  const rows = urls.slice(first, first + DEFAULT_PAGE_SIZE).map((url) => ({ id: url, url }));
  return (
    <PagedTable
      problem={null}
      loading={null}
      page={page}
      onPrevious={
        page > 1
          ? () => {
              setPage(page - 1);
            }
          : null
      }
      onNext={
        first + DEFAULT_PAGE_SIZE < urls.length
          ? () => {
              setPage(page + 1);
            }
          : null
      }
    >
      <Table>
        <Table.ScrollContainer>
          {/* Fixed column widths: long URLs truncate rather than widening the table. */}
          <Table.Content
            aria-label="Links"
            className="table-fixed"
            selectionMode="multiple"
            selectedKeys={selected}
            onSelectionChange={(selection) => {
              onSelect(
                selectOnPage(
                  selected,
                  rows.map(({ url }) => ({ key: url, url })),
                  selection,
                ),
              );
            }}
          >
            <Table.Header>
              <Table.Column className="w-12">
                <SelectionCheckbox label="Select this page" />
              </Table.Column>
              <Table.Column isRowHeader>URL</Table.Column>
            </Table.Header>
            <Table.Body items={rows}>
              {(row) => (
                <Table.Row id={row.id}>
                  <Table.Cell>
                    <SelectionCheckbox label="Select" />
                  </Table.Cell>
                  <Table.Cell className="truncate">
                    {/* The page itself, in a new tab: it is another site, not a screen of this app. */}
                    <Link href={row.url} target="_blank" rel="noreferrer">
                      {row.url}
                    </Link>
                  </Table.Cell>
                </Table.Row>
              )}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
      </Table>
    </PagedTable>
  );
}
