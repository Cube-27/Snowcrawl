import { Typography } from "@heroui/react";
import { useMemo } from "react";
import type { JobEvents } from "../../api/hooks/events";
import { activityEntry } from "../lib/activityText";
import { Collapsible } from "../molecules/Collapsible";
import { ProblemAlert } from "../molecules/ViewStates";
import { StreamStateLabel } from "../molecules/StatusLabels";

/** One line, keyed by its event's place in the stream: events only ever arrive after it. */
interface Line {
  key: number;
  text: string;
}

/** The lines about one task URL, or about the whole run (`url` null). */
interface Group {
  url: string | null;
  lines: Line[];
}

/** The events' lines grouped by task URL, the group with the latest event first; lines keep their order. */
export function activityGroups(events: JobEvents["events"]): Group[] {
  const groups = new Map<string | null, Group>();
  events.forEach((event, key) => {
    const entry = activityEntry(event);
    if (entry === null) return;
    const { text, url } = entry;
    const group = groups.get(url) ?? { url, lines: [] };
    group.lines.push({ key, text });
    // Moved to the end of the map's order, which is newest last.
    groups.delete(url);
    groups.set(url, group);
  });
  return [...groups.values()].reverse();
}

/** The activity's lines, newest group first, in a fixed-height box that scrolls. */
function ActivityLines({ events }: Readonly<{ events: JobEvents["events"] }>) {
  const groups = useMemo(() => activityGroups(events), [events]);
  return (
    <ol aria-label="Activity" className="flex h-activity-feed flex-col gap-3 overflow-y-auto">
      {groups.length === 0 ? (
        <li>
          <Typography.Paragraph size="sm" color="muted">
            No activity yet.
          </Typography.Paragraph>
        </li>
      ) : null}
      {groups.map((group) => (
        <li key={group.url ?? ""} className="flex flex-col gap-1">
          <Typography.Paragraph size="sm" weight="medium" className="truncate">
            {group.url ?? "This run"}
          </Typography.Paragraph>
          <ul aria-label={group.url ?? "This run"} className="flex flex-col gap-0.5">
            {group.lines.map((line) => (
              <li key={line.key}>
                <Typography.Paragraph size="sm" color="muted">
                  {line.text}
                </Typography.Paragraph>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ol>
  );
}

/** A run's live activity: its events as plain-language lines grouped by task URL, collapsed
 * under the results with the stream's state beside it. The run's progress is in its header, so the lines are detail a
 * reader opens when they want it. */
export function ActivityFeed({ activity }: Readonly<{ activity: JobEvents }>) {
  return (
    <Collapsible title="Activity" aside={<StreamStateLabel state={activity.state} />}>
      <ProblemAlert problem={activity.problem} />
      <ActivityLines events={activity.events} />
    </Collapsible>
  );
}
