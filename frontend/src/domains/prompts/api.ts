import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  PromptOut,
  PromptCreate,
  PromptUpdate,
  PromptVersionOut,
  PromptVersionCreate,
  DiffResult,
  UUID,
} from "@/shared/api/types";

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchPrompts(q = "", limit = 20, offset = 0) {
  return api.get<PromptOut[]>(
    `/prompts${buildQuery({ q, limit, offset })}`,
  );
}

export function createPrompt(data: PromptCreate) {
  return api.post<PromptOut>("/prompts", data);
}

export function getPrompt(promptId: UUID) {
  return api.get<PromptOut>(`/prompts/${promptId}`);
}

export function updatePrompt(promptId: UUID, data: PromptUpdate) {
  return api.put<PromptOut>(`/prompts/${promptId}`, data);
}

export function deletePrompt(promptId: UUID) {
  return api.del<void>(`/prompts/${promptId}`);
}

export function listVersions(promptId: UUID) {
  return api.get<PromptVersionOut[]>(`/prompts/${promptId}/versions`);
}

export function createVersion(promptId: UUID, data: PromptVersionCreate) {
  return api.post<PromptVersionOut>(`/prompts/${promptId}/versions`, data);
}

export function rollbackVersion(promptId: UUID, versionId: UUID) {
  return api.post<PromptVersionOut>(
    `/prompts/${promptId}/versions/${versionId}/rollback`,
    {},
  );
}

// 后端 diff 查询参数为 from/to（int 版本号）。
export function diffVersions(
  promptId: UUID,
  fromVersion: number,
  toVersion: number,
) {
  return api.get<DiffResult>(
    `/prompts/${promptId}/diff${buildQuery({
      from: fromVersion,
      to: toVersion,
    })}`,
  );
}
