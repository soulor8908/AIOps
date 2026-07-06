import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  EvalRunOut,
  EvalRunCreate,
  EvalSampleOut,
  EvalSampleCreate,
  EvalSampleQuery,
  OnlineEvalRequest,
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

// ===================== E2: Online eval 闭环 =====================

/** 列生产采样样本，支持按 judged / agent_id / priority_min 过滤。
 * 后端按 priority DESC, sampled_at DESC 排序。 */
export function fetchSamples(query: EvalSampleQuery = {}) {
  return api.get<EvalSampleOut[]>(
    `/evals/samples${buildQuery(query as Record<string, string | number | boolean | undefined>)}`,
  );
}

/** 手动录入采样样本（admin-only）。 */
export function createSample(data: EvalSampleCreate) {
  return api.post<EvalSampleOut>("/evals/samples", data);
}

/** 触发 online eval 闭环（admin-only）。
 * 取样本 → 匹配离线 golden → LLM judge → 写 EvalRun（含回归检测）。 */
export function runOnlineEval(data: OnlineEvalRequest) {
  return api.post<EvalRunOut>("/evals/online-eval", data);
}
