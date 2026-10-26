import { useSyncExternalStore } from "react";

const SECOND = 1000;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** A duration as "12s", "4m 05s" or "1h 02m 03s"; negative counts as zero. */
export function elapsedText(ms: number): string {
  const total = Math.max(0, Math.floor(ms / SECOND));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${String(hours)}h ${pad(minutes)}m ${pad(seconds)}s`;
  if (minutes > 0) return `${String(minutes)}m ${pad(seconds)}s`;
  return `${String(seconds)}s`;
}

// One clock for every running duration on the page: it ticks once a second while any is shown, so ten running rows
// re-render together on one timer rather than on ten.
let clockNow = Date.now();
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;

function subscribeClock(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === undefined) {
    clockNow = Date.now();
    timer = setInterval(() => {
      clockNow = Date.now();
      listeners.forEach((notify) => {
        notify();
      });
    }, SECOND);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size > 0) return;
    clearInterval(timer);
    timer = undefined;
  };
}

function stillClock(): () => void {
  // no-op: a duration that has ended, or not started, needs no ticks
  return () => undefined;
}

/** The time now, read again every second while `ticking`. */
function useNow(ticking: boolean): number {
  return useSyncExternalStore(ticking ? subscribeClock : stillClock, () => clockNow);
}

/** How long a job has run, as text: from its start to its end, or to now while it runs; a dash before it starts. */
export function DurationText({
  startedAt,
  finishedAt,
}: Readonly<{ startedAt: string | null; finishedAt: string | null }>) {
  const now = useNow(startedAt !== null && finishedAt === null);
  const end = finishedAt === null ? now : Date.parse(finishedAt);
  return <>{startedAt === null ? "—" : elapsedText(end - Date.parse(startedAt))}</>;
}
