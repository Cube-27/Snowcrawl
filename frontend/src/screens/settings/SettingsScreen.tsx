import { Screen } from "../../components/templates/Screen";
import { LimitsSection } from "./LimitsSection";
import { LlmCostSection } from "./LlmCostSection";

/** Settings and usage of this deployment: the project's limits and the LLM cost history. */
export function SettingsScreen() {
  return (
    <Screen title="Settings">
      <LimitsSection />
      <LlmCostSection />
    </Screen>
  );
}
