import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  PromptOut,
  PromptCreate,
  PromptUpdate,
  PromptVersionOut,
  PromptVersionCreate,
  PromptDiff,
  ListResponse,
} from "@/shared/api/types";

export function fetchPrompts(q = "", limit = 20, offset = 0) {
  return api.get<ListResponse<PromptOut>>(
    `/prompts${buildQuery({ q, limit, offset })}`,
  );
}

export function createPrompt(data: PromptCreate) {
  return api.post<PromptOut>("/prompts", data);
}

export function getPrompt(promptId: number) {
  return api.get<PromptOut>(`/prompts/${promptId}`);
}

export function updatePrompt(promptId: number, data: PromptUpdate) {
  return api.put<PromptOut>(`/prompts/${promptId}`, data);
}

export function deletePrompt(promptId: number) {
  return api.del<void>(`/prompts/${promptId}`);
}

export function listVersions(promptId: number) {
  return api.get<ListResponse<PromptVersionOut>>(
    `/prompts/${promptId}/versions`,
  );
}

export function createVersion(promptId: number, data: PromptVersionCreate) {
  return api.post<PromptVersionOut>(`/prompts/${promptId}/versions`, data);
}

export function rollbackVersion(promptId: number, versionId: number) {
  return api.post<PromptOut>(
    `/prompts/${promptId}/versions/${versionId}/rollback`,
    {},
  );
}

export function diffVersions(
  promptId: number,
  fromVersion: number,
  toVersion: number,
) {
  return api.get<PromptDiff>(
    `/prompts/${promptId}/diff${buildQuery({
      from_version: fromVersion,
      to_version: toVersion,
    })}`,
  );
}
