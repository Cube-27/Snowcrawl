import { Pagination } from "@heroui/react";
import { OptionSelect } from "./OptionSelect";
import { PAGE_SIZES } from "../lib/cursors";

/** The page size choice of a results table, and the rows it shows of the total. */
export interface PageSizing {
  pageSize: number;
  onPageSize: (size: number) => void;
}

const SIZE_OPTIONS: Readonly<Record<string, string>> = Object.fromEntries(
  PAGE_SIZES.map((size) => [String(size), `${String(size)} per page`]),
);

function isSize(key: unknown): key is string {
  return typeof key === "string" && Object.hasOwn(SIZE_OPTIONS, key);
}

/** Previous and next under a table, with the page size when `sizing` is given. The API pages with a cursor, so there are no page numbers: a direction is enabled when the API returned a cursor for it. */
export function TablePagination({
  summary,
  sizing,
  onPrevious,
  onNext,
}: Readonly<{
  summary: string;
  sizing?: PageSizing | undefined;
  onPrevious: (() => void) | null;
  onNext: (() => void) | null;
}>) {
  return (
    <Pagination className="justify-end">
      <Pagination.Summary>{summary}</Pagination.Summary>
      {sizing === undefined ? null : (
        <div className="w-40">
          <OptionSelect
            label="Rows per page"
            hideLabel
            options={SIZE_OPTIONS}
            value={String(sizing.pageSize)}
            accepts={isSize}
            onChange={(size) => {
              sizing.onPageSize(Number(size));
            }}
          />
        </div>
      )}
      <Pagination.Content>
        <Pagination.Item>
          <Pagination.Previous isDisabled={onPrevious === null} onPress={() => onPrevious?.()}>
            <Pagination.PreviousIcon />
            Previous
          </Pagination.Previous>
        </Pagination.Item>
        <Pagination.Item>
          <Pagination.Next isDisabled={onNext === null} onPress={() => onNext?.()}>
            Next
            <Pagination.NextIcon />
          </Pagination.Next>
        </Pagination.Item>
      </Pagination.Content>
    </Pagination>
  );
}
