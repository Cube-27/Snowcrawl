import type { StreamState } from "../../api/events";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

// One tone per stream state: receiving, waiting to reconnect, closed after the job's end, or refused.
const STATE_TONES: Record<StreamState, StatusTone> = {
  connecting: "neutral",
  live: "accent",
  reconnecting: "warning",
  ended: "success",
  failed: "danger",
};

/** Where a job's event stream is, as a status label. */
export function StreamStateLabel({ state }: Readonly<{ state: StreamState }>) {
  return <StatusLabel text={state} tone={STATE_TONES[state]} />;
}
