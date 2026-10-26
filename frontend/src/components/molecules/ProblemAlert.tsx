import { Alert } from "@heroui/react";
import type { ReactNode } from "react";
import type { Problem, ProblemSummary } from "../../api/problem";

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

/** An RFC 9457 problem from the API, shown the same way on every screen: its title, its detail, then
 * any per-field lines and retry time. `children` are more lines, as `Alert.Description`s. */
export function ProblemAlert({
  problem,
  children,
}: Readonly<{ problem: ProblemSummary | Problem; children?: ReactNode }>) {
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
