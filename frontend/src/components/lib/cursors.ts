import { useState } from "react";

/** The page sizes a results table offers (the API caps a page at 100 rows, so a page is never large). */
export const PAGE_SIZES = [10, 25, 50, 100] as const;

/** Rows per page of every paged table: a table without a size choice keeps it, and one with a choice starts at it. */
export const DEFAULT_PAGE_SIZE = 25;

interface Position {
  cursors: (string | null)[];
  pageSize: number;
}

/** Paging back and forth over an API that pages forward by cursor: the cursors of the pages before
 * are kept here, and the first page has none. Another page size starts again from the first page, as a cursor
 * stands for the rows after it, not for a page number. */
export function useCursorPages() {
  const [{ cursors, pageSize }, setPosition] = useState<Position>({
    cursors: [null],
    pageSize: DEFAULT_PAGE_SIZE,
  });
  return {
    /** The cursor of the page shown; null for the first. */
    cursor: cursors.at(-1) ?? null,
    /** The page shown, from 1. */
    page: cursors.length,
    /** Rows per page. */
    pageSize,
    /** Shows the first page at another size. */
    setPageSize: (size: number) => {
      setPosition({ cursors: [null], pageSize: size });
    },
    /** Goes back a page; null on the first. */
    previous:
      cursors.length > 1
        ? () => {
            setPosition({ cursors: cursors.slice(0, -1), pageSize });
          }
        : null,
    /** Goes on to the page at `next` (a page's `next_cursor`); null on the last page. */
    nextTo: (next: string | null) =>
      next === null
        ? null
        : () => {
            setPosition({ cursors: [...cursors, next], pageSize });
          },
  };
}
