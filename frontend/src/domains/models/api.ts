import { api } from "@/shared/api/client";
import type {
  ModelConfigOut,
  ModelConfigCreate,
  ChatRequest,
  ChatResponse,
  ListResponse,
} from "@/shared/api/types";

export function fetchModels() {
  return api.get<ListResponse<ModelConfigOut>>("/models");
}

export function createModel(data: ModelConfigCreate) {
  return api.post<ModelConfigOut>("/models", data);
}

export function updateModel(modelAlias: string, data: Partial<ModelConfigCreate>) {
  return api.put<ModelConfigOut>(`/models/${modelAlias}`, data);
}

export function deleteModel(modelAlias: string) {
  return api.del<void>(`/models/${modelAlias}`);
}

export function chatCompletion(modelAlias: string, data: ChatRequest) {
  return api.post<ChatResponse>(`/models/${modelAlias}/chat`, data);
}
