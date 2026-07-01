import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  ModelConfigOut,
  ModelConfigCreate,
  ModelConfigUpdate,
  ChatRequest,
  ChatResponse,
} from "@/shared/api/types";

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchModels(activeOnly = false, limit = 50, offset = 0) {
  return api.get<ModelConfigOut[]>(
    `/models${buildQuery({ active_only: activeOnly, limit, offset })}`,
  );
}

export function createModel(data: ModelConfigCreate) {
  return api.post<ModelConfigOut>("/models", data);
}

export function getModel(alias: string) {
  return api.get<ModelConfigOut>(`/models/${alias}`);
}

export function updateModel(modelAlias: string, data: ModelConfigUpdate) {
  return api.put<ModelConfigOut>(`/models/${modelAlias}`, data);
}

export function deleteModel(modelAlias: string) {
  return api.del<void>(`/models/${modelAlias}`);
}

export function chatCompletion(modelAlias: string, data: ChatRequest) {
  return api.post<ChatResponse>(`/models/${modelAlias}/chat`, data);
}
