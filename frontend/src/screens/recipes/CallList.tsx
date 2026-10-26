import { Button, Card, Chip, Table, Typography } from "@heroui/react";
import { useState } from "react";
import type { CallSummary, Endpoint, FieldMatch } from "../../api/captures";
import { keyedTexts } from "../../components/lib/keys";
import { Collapsible } from "../../components/molecules/Collapsible";
import { CallDrawer } from "./CallDrawer";
import { endpointKey, endpointText, isOwnSite, matchLines, sizeText } from "./endpoints";

/** Which sample (from 1) a capture is, by its place among the samples. */
export type SampleNumber = (captureId: string) => number;

function CallsTable({
  calls,
  sampleOf,
  onOpen,
}: Readonly<{
  calls: readonly CallSummary[];
  sampleOf: SampleNumber;
  onOpen: (call: CallSummary) => void;
}>) {
  const rows = calls.map((call) => ({ ...call, id: `${call.capture_id}/${String(call.index)}` }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Calls">
          <Table.Header>
            <Table.Column isRowHeader>Sample</Table.Column>
            <Table.Column>Status</Table.Column>
            <Table.Column>Content type</Table.Column>
            <Table.Column className="numeric">Size</Table.Column>
            <Table.Column>After</Table.Column>
            <Table.Column>Call</Table.Column>
          </Table.Header>
          <Table.Body items={rows}>
            {(call) => (
              <Table.Row id={call.id}>
                <Table.Cell>{sampleOf(call.capture_id)}</Table.Cell>
                <Table.Cell>{call.status}</Table.Cell>
                <Table.Cell>{call.content_type ?? "—"}</Table.Cell>
                <Table.Cell className="numeric">{call.omitted ?? sizeText(call.size)}</Table.Cell>
                <Table.Cell>
                  {call.step === null || call.step === 0 ? "Load" : `Step ${String(call.step)}`}
                </Table.Cell>
                <Table.Cell>
                  <Button
                    size="sm"
                    variant="ghost"
                    onPress={() => {
                      onOpen(call);
                    }}
                  >
                    Open call {call.index}
                  </Button>
                </Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** Where a group carries each field, a line per path (`matchLines`); the paths read by type beyond the first few of a
 * field are behind a disclosure. */
export function FieldMatches({
  label,
  matches,
  samples,
}: Readonly<{ label: string; matches: readonly FieldMatch[]; samples: number }>) {
  const { lines, more } = matchLines(matches, samples);
  const items = (texts: string[], name: string) => (
    <ul aria-label={name} className="flex flex-col gap-1 text-code">
      {keyedTexts(texts).map(({ key, text }) => (
        <li key={key}>{text}</li>
      ))}
    </ul>
  );
  return (
    <div className="flex flex-col gap-1">
      {items(lines, label)}
      {more.length === 0 ? null : (
        <Collapsible title={`${String(more.length)} more paths by type`}>
          {items(more, `More ${label.toLowerCase()}`)}
        </Collapsible>
      )}
    </div>
  );
}

/** One endpoint group: its URL shape, on how many samples it was called, where it carries each field, its calls, and
 * the choice of it as the recipe's main call. */
function EndpointCard({
  endpoint,
  samples,
  isMain,
  sampleOf,
  onOpen,
  onPick,
  onAdd,
}: Readonly<{
  endpoint: Endpoint;
  samples: number;
  isMain: boolean;
  sampleOf: SampleNumber;
  onOpen: (call: CallSummary) => void;
  onPick: () => void;
  /** Adds the group as an extra call; none before a main call is chosen. */
  onAdd: (() => void) | null;
}>) {
  const title = endpointText(endpoint);
  return (
    <Card>
      <Card.Header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Card.Title className="min-w-0 text-code break-all">{title}</Card.Title>
          {isMain ? (
            <Chip color="success">Main call</Chip>
          ) : (
            <div className="flex flex-wrap gap-2">
              {onAdd === null ? null : (
                <Button
                  size="sm"
                  variant="secondary"
                  aria-label={`Add ${title} as an extra call`}
                  onPress={onAdd}
                >
                  Add as extra call
                </Button>
              )}
              <Button
                size="sm"
                variant="secondary"
                aria-label={`Use ${title} as the main call`}
                onPress={onPick}
              >
                Use as main call
              </Button>
            </div>
          )}
        </div>
        <Card.Description>
          {isOwnSite(endpoint) ? "The page's own site" : "Another site"}, called on{" "}
          {endpoint.samples.length} of {samples} samples
          {endpoint.rows === null ? "" : `, rows at ${endpoint.rows}`}
        </Card.Description>
      </Card.Header>
      <Card.Content className="flex flex-col gap-3">
        {endpoint.fields.length === 0 ? (
          <Typography.Paragraph size="sm">Carries no field of the schema.</Typography.Paragraph>
        ) : (
          <FieldMatches label="Field matches" matches={endpoint.fields} samples={samples} />
        )}
        <CallsTable calls={endpoint.calls} sampleOf={sampleOf} onOpen={onOpen} />
      </Card.Content>
    </Card>
  );
}

/** The samples' calls by endpoint, the page's own site first; a call opens in a drawer. `main` is the chosen group's
 * key (`endpointKey`), `onPick` chooses one and `onAdd` adds one as an extra call. */
export function CallList({
  endpoints,
  samples,
  sampleOf,
  main,
  onPick,
  onAdd,
}: Readonly<{
  endpoints: readonly Endpoint[];
  samples: number;
  sampleOf: SampleNumber;
  main: string | null;
  onPick: (endpoint: Endpoint) => void;
  /** Null while no extra call can be added: before a main call, while a draft or another addition is read. */
  onAdd: ((endpoint: Endpoint) => void) | null;
}>) {
  const [open, setOpen] = useState<CallSummary | null>(null);
  return (
    <div className="flex flex-col gap-4">
      {endpoints.map((endpoint) => {
        const key = endpointKey(endpoint);
        return (
          <EndpointCard
            key={key}
            endpoint={endpoint}
            samples={samples}
            isMain={key === main}
            sampleOf={sampleOf}
            onOpen={setOpen}
            onPick={() => {
              onPick(endpoint);
            }}
            onAdd={
              main === null || onAdd === null
                ? null
                : () => {
                    onAdd(endpoint);
                  }
            }
          />
        );
      })}
      {open === null ? null : (
        <CallDrawer
          captureId={open.capture_id}
          index={open.index}
          sample={sampleOf(open.capture_id)}
          onClose={() => {
            setOpen(null);
          }}
        />
      )}
    </div>
  );
}
