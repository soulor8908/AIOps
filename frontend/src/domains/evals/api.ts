import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  EvalRunOut,
  EvalRunCreate,
  UUID,
} from "@/shared/api/types";

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchEvals(limit = 50, offset = 0) {
  return api.get<EvalRunOut[]>(`/evals${buildQuery({ limit, offset })}`);
}

export function createEval(data: EvalRunCreate) {
  return api.post<EvalRunOut>("/evals", data);
}

export function getEval(evalId: UUID) {
  return api.get<EvalRunOut>(`/evals/${evalId}`);
}

export function executeEval(evalId: UUID) {
  return api.post<EvalRunOut>(`/evals/${evalId}/run`, {});
}
