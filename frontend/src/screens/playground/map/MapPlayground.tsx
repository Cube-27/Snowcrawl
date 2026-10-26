import { Button } from "@heroui/react";
import { useMapUrls } from "../../../api/hooks/jobs";
import { problemOf } from "../../../api/hooks/query";
import { EmptyView } from "../../../components/molecules/EmptyView";
import { OutputArea, type OutputState } from "../OutputArea";
import { NO_ERRORS, optionsChanged } from "../pageRequest";
import { PlaygroundLayout } from "../PlaygroundLayout";
import { RecentRuns } from "../run/RecentRuns";
import { useShownJob } from "../run/useShownJob";
import { requestUrl, UrlInput } from "../UrlInput";
import { usePageForm } from "../usePageForm";
import { buildMap, DEFAULT_MAP_OPTIONS, INITIAL_MAP, mapForm } from "./form";
import { MapOptions } from "./MapOptions";
import { MapOutput } from "./MapOutput";
import { useMapRun } from "./mapRun";

/** A map's request always can be sent: the server validates its URL and globs. */
const sendable = () => NO_ERRORS;

/** A past map's output opened from its job: `POST /v1/map` answers its URLs at once, its job keeps only the request,
 * and the URLs stay only in the history entry of the tab that ran it. */
const NOT_KEPT: OutputState = {
  kind: "done",
  content: (
    <EmptyView title="A map's URLs are not kept">
      They stay only in the browser tab that ran the map. This run's request is above. Choose Edit
      and re-run to map the site again.
    </EmptyView>
  ),
};

/** The Map playground at `/map`: a URL and its scope (`POST /v1/map`, answered at once), then the URLs
 * found as a table of links, kept in the run's history entry (`useMapRun`); before any run, the recent maps. Map takes
 * no format or schema, so the toolbar has Options only. */
export function MapPlayground() {
  const map = useMapUrls();
  const problem = problemOf(map.error);
  const { form, setForm, optionsOpen, setOptionsOpen, validationErrors } = usePageForm(
    INITIAL_MAP,
    problem,
    sendable,
  );
  const { jobId, onEdit } = useShownJob(mapForm, setForm);
  const { run, key, keep } = useMapRun(setForm);
  const request = buildMap({ ...form, url: requestUrl(form.url) });

  let state: OutputState | null = null;
  if (map.isPending) state = { kind: "pending", label: "Mapping the site" };
  else if (problem !== null) state = { kind: "failed", problem };
  else if (run !== null)
    state = { kind: "done", content: <MapOutput key={key} result={run.result} /> };
  else if (jobId !== null) state = NOT_KEPT;

  return (
    <PlaygroundLayout
      mode="map"
      validationErrors={validationErrors}
      onSubmit={() => {
        map.mutate(request, {
          onSuccess: (result) => {
            keep(form, result);
            // The answer lives in its entry now; the entry it was asked from shows what it showed before.
            map.reset();
          },
        });
      }}
      onEdit={onEdit}
      request={request}
      input={
        <UrlInput
          value={form.url}
          onChange={(url) => {
            setForm({ ...form, url });
          }}
        />
      }
      optionsOpen={optionsOpen}
      onOptionsOpen={setOptionsOpen}
      optionsChanged={optionsChanged(form.options, DEFAULT_MAP_OPTIONS)}
      action={
        <Button type="submit" isPending={map.isPending}>
          Map
        </Button>
      }
      options={
        <MapOptions
          options={form.options}
          onChange={(options) => {
            setForm({ ...form, options });
          }}
        />
      }
    >
      {state === null ? <RecentRuns kind="map" /> : <OutputArea state={state} />}
    </PlaygroundLayout>
  );
}
