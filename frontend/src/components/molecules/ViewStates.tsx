import { Alert, EmptyState, Spinner, Typography } from "@heroui/react";
import type { ReactNode } from "react";
import { problemOf } from "../../api/hooks/query";
import type { Problem, ProblemSummary } from "../../api/problem";

/* The states a view of server data goes through, shown the same way on every screen: on its way (`LoadingView`),
 * refused or failed (`ProblemAlert`), with nothing to show (`EmptyView`), and `QueryView`, which picks one. */

/** Data on its way: one spinner with a name, in place of the content it loads. */
export function LoadingView({ label = "Loading" }: Readonly<{ label?: string }>) {
  return (
    <div className="flex justify-center py-6">
      <Spinner aria-label={label} />
    </div>
  );
}

/** A screen or list with nothing to show yet: what is missing, and how to get it (`children`, `action`). */
export function EmptyView({
  title,
  children,
  action,
}: Readonly<{ title: string; children?: ReactNode; action?: ReactNode }>) {
  return (
    <EmptyState>
      <Typography.Paragraph weight="medium">{title}</Typography.Paragraph>
      {children === undefined ? null : (
        <Typography.Paragraph size="sm" color="muted">
          {children}
        </Typography.Paragraph>
      )}
      {action}
    </EmptyState>
  );
}

// RFC 9110 §10.2.3: Retry-After is delay-seconds (digits only) or an HTTP date.
function retryText(retryAfter: string): string {
  return /^\d+$/.test(retryAfter)
    ? `Retry after ${retryAfter} seconds.`
    : `Retry after ${retryAfter}.`;
}

/** A full problem's per-field lines and retry time, after its detail. */
function ProblemLines({ problem }: Readonly<{ problem: Problem }>) {
  return (
    <>
      {problem.errors.map((error) => (
        <Alert.Description key={`${error.field}:${error.reason}`}>
          {/* An error about the whole request names no field (RFC 9457 extension `errors`). */}
          {error.field === "" ? error.reason : `${error.field}: ${error.reason}`}
        </Alert.Description>
      ))}
      {problem.retryAfter === null ? null : (
        <Alert.Description>{retryText(problem.retryAfter)}</Alert.Description>
      )}
    </>
  );
}

/** An RFC 9457 problem from the API: its title, its detail, then any per-field lines and retry time; nothing while
 * there is none (`null`). `children` are more lines, as `Alert.Description`s. */
export function ProblemAlert({
  problem,
  children,
}: Readonly<{ problem: ProblemSummary | Problem | null; children?: ReactNode }>) {
  if (problem === null) return null;
  return (
    <Alert status="danger" role="alert">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{problem.title}</Alert.Title>
        {problem.detail === null ? null : <Alert.Description>{problem.detail}</Alert.Description>}
        {"errors" in problem ? <ProblemLines problem={problem} /> : null}
        {children}
      </Alert.Content>
    </Alert>
  );
}

/** The part of a TanStack Query result `QueryView` reads. */
interface QueryLike<T> {
  data: T | undefined;
  error: Error | null;
}

/** One query's view in one place: its problem once it failed, the loading state (`loading`, its name) until its data
 * is here, `empty` when `isEmpty` holds for the data, else `children` of the data. */
export function QueryView<T>({
  query,
  loading,
  isEmpty,
  empty,
  children,
}: Readonly<{
  query: QueryLike<T>;
  loading: string;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}>) {
  const problem = problemOf(query.error);
  if (problem !== null) return <ProblemAlert problem={problem} />;
  if (query.data === undefined) return <LoadingView label={loading} />;
  if (isEmpty?.(query.data) === true) return empty;
  return children(query.data);
}
