import { PageHeader } from "../../components/templates/PageHeader";
import { LimitsSection } from "./LimitsSection";
import { LlmCostSection } from "./LlmCostSection";

/** Settings and usage of this deployment: the project's limits and the LLM cost history. */
export function SettingsScreen() {
  return (
    <section className="flex flex-col gap-stack">
      <PageHeader title="Settings" />
      <LimitsSection />
      <LlmCostSection />
    </section>
  );
}
