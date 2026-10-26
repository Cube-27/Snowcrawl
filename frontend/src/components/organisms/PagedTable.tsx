import { useEffect, useRef, type ReactNode } from "react";
import type { Problem } from "../../api/problem";
import { LoadingView } from "../molecules/LoadingView";
import { ProblemAlert } from "../molecules/ProblemAlert";
import { PAGE_SIZES } from "../lib/cursors";
import { TablePagination, type PageSizing } from "../molecules/TablePagination";

/** Rows per page of a table that is not a results table (`useCursorPages` sizes the others): the box (`h-table-page` in theme.css) holds this many rows and the header. */
export const PAGE_SIZE = 20;

/** The rows a page shows of the total, "rows 51–100 of 1,000" (every results page carries `total`). */
export interface PageRows extends PageSizing {
  /** The rows in all, or null until the first page is here. */
  total: number | null;
  /** The rows on the page shown. */
  count: number;
}

function summaryOf(page: number, rows: PageRows | undefined): string {
  if (rows?.total == null) return `Page ${String(page)}`;
  if (rows.count === 0) return `${rows.total.toLocaleString("en-US")} rows`;
  const first = (page - 1) * rows.pageSize + 1;
  const range = `${first.toLocaleString("en-US")}–${(first + rows.count - 1).toLocaleString("en-US")}`;
  return `Rows ${range} of ${rows.total.toLocaleString("en-US")}`;
}

/** One page of a paged table and the pager under it. Every state (problem, loading, the page) fills the same box and
 * the pager stays under it, so paging moves nothing (owner's rule: no layout shift on interaction). `onNext` is null
 * until the page is known. */
export function PagedTable({
  problem,
  loading,
  page,
  onPrevious,
  onNext,
  rows,
  children,
}: Readonly<{
  problem: Problem | null;
  /** The loading state's name, or null once the page is here. */
  loading: string | null;
  page: number;
  onPrevious: (() => void) | null;
  onNext: (() => void) | null;
  /** The total, the page size and its choice, for a table of results; the pager says "Page n" without it. */
  rows?: PageRows | undefined;
  children: ReactNode;
}>) {
  // A new page starts at its first row, not where the last one was scrolled to.
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current !== null) box.current.scrollTop = 0;
  }, [page]);
  // A list that fits one page needs no fixed box (it would be a screen of empty space under a few rows), and a short
  // one needs no pager either: the smallest page size is already more than it has.
  const total = rows?.total ?? null;
  const fits = total !== null && onPrevious === null && onNext === null;
  const short = fits && total <= Math.min(...PAGE_SIZES);
  let content = children;
  if (problem !== null) content = <ProblemAlert problem={problem} />;
  else if (loading !== null) content = <LoadingView label={loading} />;
  return (
    <div className="flex flex-col gap-3">
      <div
        ref={box}
        className={
          fits ? "max-h-(--height-table-page) overflow-auto" : "h-table-page overflow-auto"
        }
      >
        {content}
      </div>
      {short ? null : (
        <TablePagination
          summary={summaryOf(page, rows)}
          sizing={rows}
          onPrevious={onPrevious}
          onNext={onNext}
        />
      )}
    </div>
  );
}
