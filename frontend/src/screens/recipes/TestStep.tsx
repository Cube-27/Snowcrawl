import { Alert, Button, Card, Typography } from "@heroui/react";
import { useState } from "react";
import type { FieldTypes } from "../../api/field";
import { problemOf } from "../../api/hooks/query";
import type { useTestRecipe } from "../../api/hooks/recipes";
import { MAX_TEST_URLS, type CustomRecipe, type TestRun, type UrlTest } from "../../api/recipes";
import { Collapsible } from "../../components/molecules/Collapsible";
import { ExtractedRecords } from "../../components/organisms/ExtractedRecords";
import { FetchTrail } from "../../components/organisms/FetchAttempts";
import { ProblemAlert } from "../../components/molecules/ProblemAlert";
import { Section } from "../../components/templates/Section";
import { Lines } from "./Fields";

// RFC 9110 §15.5, §15.6: an error status.
const ERROR_STATUS = 400;

/** The recipe's own attempts that got no answer or an error status. A call the browser sends from the site's page
 * that its endpoint refuses is one the site's own code signs, which no recipe replays. */
function refusedCalls(run: TestRun): number[] {
  return (run.fetch?.attempts ?? [])
    .filter((attempt) => attempt.recipe != null && (attempt.status ?? 0) >= ERROR_STATUS)
    .map((attempt) => attempt.status ?? 0);
}

function RunView({
  title,
  run,
  fieldTypes,
}: Readonly<{ title: string; run: TestRun; fieldTypes: FieldTypes }>) {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2">
      <Typography.Heading level={4}>{title}</Typography.Heading>
      {run.reason === null ? null : (
        <Typography.Paragraph size="sm">{run.reason}</Typography.Paragraph>
      )}
      <ExtractedRecords records={run.records} fieldTypes={fieldTypes} />
      {run.fetch === null ? null : (
        <Collapsible title="Fetch trail">
          <FetchTrail fetch={run.fetch} />
        </Collapsible>
      )}
    </div>
  );
}

function UrlResult({
  result,
  sentByBrowser,
  fieldTypes,
}: Readonly<{ result: UrlTest; sentByBrowser: boolean; fieldTypes: FieldTypes }>) {
  const refused = sentByBrowser ? refusedCalls(result.recipe) : [];
  return (
    <Card>
      <Card.Header>
        <Card.Title className="font-mono text-sm break-all">{result.url}</Card.Title>
      </Card.Header>
      <Card.Content className="flex flex-col gap-4">
        {refused.length === 0 ? null : (
          <Alert status="warning">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>Cannot replay: the site refused the browser-sent call</Alert.Title>
              <Alert.Description>
                HTTP {refused.join(", ")}. The site signs its requests in its own code, which a
                recipe does not reproduce.
              </Alert.Description>
            </Alert.Content>
          </Alert>
        )}
        <div className="flex flex-col gap-4 lg:flex-row">
          <RunView title="With the recipe" run={result.recipe} fieldTypes={fieldTypes} />
          <RunView title="Normal extraction" run={result.normal} fieldTypes={fieldTypes} />
        </div>
      </Card.Content>
    </Card>
  );
}

function urlsOf(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** Step 7: the recipe tried on up to five URLs of its patterns (the samples' unless others are given): per URL, the
 * records it serves beside the normal extraction's, with both fetch trails; nothing is stored. */
export function TestStep({
  recipe,
  sampleUrls,
  test,
  isDrafting,
  fieldTypes,
}: Readonly<{
  recipe: CustomRecipe;
  sampleUrls: readonly string[];
  test: ReturnType<typeof useTestRecipe>;
  isDrafting: boolean;
  fieldTypes: FieldTypes;
}>) {
  // The samples' URLs until the engineer types others.
  const [typed, setTyped] = useState<string | null>(null);
  const text = typed ?? sampleUrls.join("\n");
  const urls = urlsOf(text);
  const problem = problemOf(test.error);
  // The results are of the recipe and URLs the last test sent, which later edits may have changed.
  const sent = test.variables;
  const isStale = sent !== undefined && JSON.stringify(sent) !== JSON.stringify({ recipe, urls });
  return (
    <Section title="7. Test">
      <Lines
        label={`URLs to test (one per line, at most ${String(MAX_TEST_URLS)})`}
        value={text}
        placeholder="https://example.com/item/3"
        onChange={setTyped}
      />
      <div>
        <Button
          isPending={test.isPending}
          isDisabled={isDrafting || urls.length === 0 || urls.length > MAX_TEST_URLS}
          onPress={() => {
            test.mutate({ recipe, urls });
          }}
        >
          Test
        </Button>
      </div>
      {problem === null ? null : <ProblemAlert problem={problem} />}
      {isStale && test.data !== undefined ? (
        <Typography.Paragraph size="sm" color="muted">
          The recipe or the URLs changed since this test: test again to see what they give.
        </Typography.Paragraph>
      ) : null}
      {test.data?.urls.map((result) => (
        <UrlResult
          key={result.url}
          result={result}
          sentByBrowser={sent?.recipe.sent_by === "browser"}
          fieldTypes={fieldTypes}
        />
      ))}
    </Section>
  );
}
