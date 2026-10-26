import type { ReactNode } from "react";
import type { Problem } from "../../api/problem";
import { LoadingView } from "../../components/molecules/LoadingView";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";

/** What a run shows: on its way (the loading state's name), refused or failed, or its output. */
export type OutputState =
  | { kind: "pending"; label: string }
  | { kind: "failed"; problem: Problem }
  | { kind: "done"; content: ReactNode };

/** The playground's output area: mounted from the first run, it holds every state of the run in a box
 * of at least `min-h-playground-output`, so starting a run, its failure and its output move nothing around it (owner's
 * rule: no layout shift on interaction). */
export function OutputArea({ state }: Readonly<{ state: OutputState }>) {
  let content: ReactNode;
  if (state.kind === "pending") content = <LoadingView label={state.label} />;
  else if (state.kind === "failed") content = <ProblemAlert problem={state.problem} />;
  else content = state.content;
  return (
    <section aria-label="Output" className="flex min-h-playground-output flex-col gap-4">
      {content}
    </section>
  );
}
