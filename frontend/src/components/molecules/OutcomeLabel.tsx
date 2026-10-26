import type { TaskOutcome } from "../../api/jobs";
import { StatusLabel, type StatusTone } from "../atoms/StatusLabel";

// One tone per `url_task.outcome`. `partial` and `shell` returned a page without all it should have;
// `blocked`, `timeout` and `failed` returned none. `not_found` is the page's own answer;
// `links_only` (a page read for its links) and `no_match` (a discovered page without its required fields) are
// not failures. `auth_wall` (a 401), `soft_404`, `geo_redirect` and `redirected_to_listing` (where a redirect led
// instead of the page) and `unsupported_media` (an answer that is no HTML page) are the site's answers, as `not_found`
// is. `geo_blocked` (a 451, withheld where the request came from) returned none.
const OUTCOME_TONES: Record<TaskOutcome, StatusTone> = {
  extracted: "success",
  partial: "warning",
  shell: "warning",
  not_found: "neutral",
  links_only: "neutral",
  no_match: "neutral",
  auth_wall: "neutral",
  soft_404: "neutral",
  geo_redirect: "neutral",
  redirected_to_listing: "neutral",
  unsupported_media: "neutral",
  geo_blocked: "danger",
  blocked: "danger",
  timeout: "danger",
  failed: "danger",
};

/** A URL's outcome as its text and icon. */
export function OutcomeLabel({ outcome }: Readonly<{ outcome: TaskOutcome }>) {
  return <StatusLabel text={outcome} tone={OUTCOME_TONES[outcome]} />;
}
