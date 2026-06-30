import { api } from "@/shared/api/client";
import type {
  EvalRunOut,
  EvalRunCreate,
  ListResponse,
} from "@/shared/api/types";

export function fetchEvals() {
  return api.get<ListResponse<EvalRunOut>>("/evals");
}

export function createEval(data: EvalRunCreate) {
  return api.post<EvalRunOut>("/evals", data);
}

export function executeEval(evalId: string) {
  return api.post<EvalRunOut>(`/evals/${evalId}/run`, {});
}
