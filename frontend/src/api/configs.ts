import type { ApiClient } from "./client";
import { apiResult, type ApiResult } from "./result";
import type { components } from "./schema";

/** The site config endpoints, in the generated shapes. */
export type ConfigSummary = components["schemas"]["ConfigSummary"];
export type ConfigDetail = components["schemas"]["ConfigDetail"];
export type ConfigStatus = ConfigSummary["status"];
export type FieldConfigStatus = components["schemas"]["FieldSteps"]["status"];
export type FieldSteps = components["schemas"]["FieldSteps"];
export type ValidationReport = components["schemas"]["ValidationReport"];
export type FieldReport = components["schemas"]["FieldReport"];
export type Correction = components["schemas"]["Correction"];
export type CorrectedStep = components["schemas"]["CorrectedSteps"]["steps"][number];
/** A step as a config stores it. */
export type IrStep = FieldSteps["steps"][number];
export type ExportFormat = components["schemas"]["ExportRequest"]["format"];
export type ConfigExport = components["schemas"]["ConfigExport"];
export type ReleaseReport = components["schemas"]["ReleaseReport"];
export type ReleaseField = components["schemas"]["ReleaseField"];

/** A config version as people name it: "version <n>". */
export function configVersionLabel({ version }: Pick<ConfigSummary, "version">): string {
  return `version ${String(version)}`;
}

/** Every version of the site's configs. */
export async function listSiteConfigs(
  client: ApiClient,
  host: string,
): Promise<ApiResult<ConfigSummary[]>> {
  const result = await apiResult(
    () => client.GET("/v1/sites/{host}/configs", { params: { path: { host } } }),
    "Listing the site's configs",
  );
  return result.kind === "done" ? { kind: "done", value: result.value.items } : result;
}

export function getConfig(client: ApiClient, id: string): Promise<ApiResult<ConfigDetail>> {
  return apiResult(
    () => client.GET("/v1/configs/{config_id}", { params: { path: { config_id: id } } }),
    "Loading the config",
  );
}

/** Stores the version after `id` with the correction applied; the answer is that version, with its validation. */
export function correctConfig(
  client: ApiClient,
  id: string,
  body: Correction,
): Promise<ApiResult<ConfigDetail>> {
  return apiResult(
    () => client.PUT("/v1/configs/{config_id}", { params: { path: { config_id: id } }, body }),
    "Saving the correction",
  );
}

/** Approves a learned config waiting as `pending_approval`: it becomes the active version. */
export function approveConfig(client: ApiClient, id: string): Promise<ApiResult<ConfigDetail>> {
  return apiResult(
    () => client.POST("/v1/configs/{config_id}/approve", { params: { path: { config_id: id } } }),
    "Approving the config",
  );
}

/** Refuses a learned config waiting as `pending_approval`: it becomes `suspended`, and what served before goes
 * on serving. */
export function rejectConfig(client: ApiClient, id: string): Promise<ApiResult<ConfigDetail>> {
  return apiResult(
    () => client.POST("/v1/configs/{config_id}/reject", { params: { path: { config_id: id } } }),
    "Rejecting the config",
  );
}

/** Retires an active custom recipe: it becomes `suspended`, with any repair of it waiting for approval, and the learned
 * config serves its URLs again. */
export function retireRecipe(client: ApiClient, id: string): Promise<ApiResult<ConfigDetail>> {
  return apiResult(
    () => client.POST("/v1/configs/{config_id}/retire", { params: { path: { config_id: id } } }),
    "Retiring the recipe",
  );
}

export function exportConfig(
  client: ApiClient,
  id: string,
  format: ExportFormat,
): Promise<ApiResult<ConfigExport>> {
  return apiResult(
    () =>
      client.POST("/v1/configs/{config_id}/export", {
        params: { path: { config_id: id } },
        body: { format },
      }),
    "Exporting the config",
  );
}

/** Starts learning the site's config for one schema version; `force` re-learns while a config is active. */
export async function learnSite(
  client: ApiClient,
  host: string,
  body: { schema_id: string; force: boolean },
): Promise<ApiResult<string>> {
  const result = await apiResult(
    () => client.POST("/v1/sites/{host}/learn", { params: { path: { host } }, body }),
    "Starting to learn",
  );
  return result.kind === "done" ? { kind: "done", value: result.value.job_id } : result;
}
