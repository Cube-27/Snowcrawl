import { Button, Drawer, Table, Typography } from "@heroui/react";
import { ArrowDownToLine } from "@gravity-ui/icons";
import type { CallDetail, HeaderLines } from "../../api/captures";
import { useCall, useHarExport } from "../../api/hooks/recipes";
import { problemOf } from "../../api/hooks/query";
import { saveJson } from "../../app/download";
import { JsonView } from "../../components/molecules/JsonView";
import { LoadingView } from "../../components/molecules/LoadingView";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { Section } from "../../components/templates/Section";
import { sizeText } from "./endpoints";

/** Header field lines in the order sent (RFC 9110 §5.3: a repeated name is not merged); "None" without any. */
function HeaderTable({ label, headers }: Readonly<{ label: string; headers: HeaderLines | null }>) {
  if (headers === null || headers.length === 0) {
    return <Typography.Paragraph size="sm">No headers recorded.</Typography.Paragraph>;
  }
  const rows = headers.map(([name = "", value = ""], index) => ({ id: index, name, value }));
  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label={label}>
          <Table.Header>
            <Table.Column isRowHeader>Name</Table.Column>
            <Table.Column>Value</Table.Column>
          </Table.Header>
          <Table.Body items={rows}>
            {(row) => (
              <Table.Row id={row.id}>
                <Table.Cell>{row.name}</Table.Cell>
                <Table.Cell className="break-all">{row.value}</Table.Cell>
              </Table.Row>
            )}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}

/** The answer's JSON value (RFC 8259) once its anti-hijacking guard prefix is cut off; undefined when the text is not
 * JSON. */
function answerJson(call: CallDetail): unknown {
  const text =
    call.guard_prefix !== null && call.answer.startsWith(call.guard_prefix)
      ? call.answer.slice(call.guard_prefix.length)
      : call.answer;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function Answer({ call, filename }: Readonly<{ call: CallDetail; filename: string }>) {
  const json = answerJson(call);
  if (json !== undefined) return <JsonView label="Answer" value={json} filename={filename} />;
  return <pre className="code-view h-code-view">{call.answer}</pre>;
}

function CallBody({ call, filename }: Readonly<{ call: CallDetail; filename: string }>) {
  return (
    <div className="flex flex-col gap-6">
      <Typography.Paragraph size="sm">
        {call.status}, {call.content_type ?? "no content type"}, {sizeText(call.size)}
        {call.step === null || call.step === 0
          ? ", on load"
          : `, after page step ${String(call.step)}`}
      </Typography.Paragraph>
      <Section title="Request headers">
        <HeaderTable label="Request headers" headers={call.request_headers} />
      </Section>
      {call.request_body === null ? null : (
        <Section title="Request body">
          <pre className="code-view h-code-view">{call.request_body}</pre>
        </Section>
      )}
      <Section title="Response headers">
        <HeaderTable label="Response headers" headers={call.response_headers} />
      </Section>
      <Section title="Answer">
        <Answer call={call} filename={filename} />
      </Section>
    </div>
  );
}

/** Saves the capture's HAR (every call it kept, without cookies) as `capture-<id>.har`. */
function HarDownload({ captureId }: Readonly<{ captureId: string }>) {
  const har = useHarExport();
  const problem = problemOf(har.error);
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Button
          size="sm"
          variant="secondary"
          isPending={har.isPending}
          onPress={() => {
            har.mutate(captureId, {
              onSuccess: (document) => {
                saveJson(`capture-${captureId}.har`, document);
              },
            });
          }}
        >
          <ArrowDownToLine aria-hidden="true" />
          Download HAR
        </Button>
      </div>
      {problem === null ? null : <ProblemAlert problem={problem} />}
    </div>
  );
}

/** One call of a sample in a drawer from the right: the request as sent (headers as recorded, no cookies), the answer's
 * headers and text, and the sample's HAR. */
export function CallDrawer({
  captureId,
  index,
  sample,
  onClose,
}: Readonly<{ captureId: string; index: number; sample: number; onClose: () => void }>) {
  const call = useCall(captureId, index);
  const problem = problemOf(call.error);
  return (
    <Drawer
      isOpen
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Drawer.Backdrop>
        <Drawer.Content placement="right">
          <Drawer.Dialog>
            <Drawer.CloseTrigger />
            <Drawer.Header>
              <Drawer.Heading>
                Call {index} of sample {sample}
              </Drawer.Heading>
              {call.data === undefined ? null : (
                <Typography.Paragraph size="sm" color="muted" className="break-all">
                  {call.data.method ?? "GET"} {call.data.url}
                </Typography.Paragraph>
              )}
            </Drawer.Header>
            <Drawer.Body className="flex flex-col gap-6">
              <HarDownload captureId={captureId} />
              {call.isPending ? <LoadingView label="Loading the call" /> : null}
              {problem === null ? null : <ProblemAlert problem={problem} />}
              {call.data === undefined ? null : (
                <CallBody call={call.data} filename={`call-${captureId}-${String(index)}.json`} />
              )}
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  );
}
