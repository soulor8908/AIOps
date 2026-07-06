import { api, streamSSE } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  AgentOut,
  AgentCreate,
  ExecuteRequest,
  ExecutionResult,
  SSEEvent,
  WorkflowOut,
  WorkflowDef,
  UUID,
} from "@/shared/api/types";

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchAgents(limit = 50, offset = 0) {
  return api.get<AgentOut[]>(
    `/agents${buildQuery({ limit, offset })}`,
  );
}

export function createAgent(data: AgentCreate) {
  return api.post<AgentOut>("/agents", data);
}

export function executeAgent(agentId: UUID, data: ExecuteRequest) {
  return api.post<ExecutionResult>(`/agents/${agentId}/execute`, data);
}

/**
 * 流式执行 Agent（POST /agents/{id}/execute/stream）。
 * EventSource 不支持 POST，故用 fetch + ReadableStream 解析 SSE。
 *
 * @param agentId Agent ID
 * @param data ExecuteRequest（input / max_turns / context）
 * @param onEvent 每个 SSE 事件的回调（token / tool / observation / done）
 * @param signal 可选 AbortSignal，用于取消流
 */
export function executeAgentStream(
  agentId: UUID,
  data: ExecuteRequest,
  onEvent: (event: SSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSSE(
    `/agents/${agentId}/execute/stream`,
    data,
    onEvent,
    signal,
  );
}

export function fetchWorkflows(limit = 50, offset = 0) {
  return api.get<WorkflowOut[]>(
    `/workflows${buildQuery({ limit, offset })}`,
  );
}

export function createWorkflow(data: WorkflowDef) {
  return api.post<WorkflowOut>("/workflows", data);
}

export function executeWorkflow(workflowId: UUID, input: string) {
  return api.post<ExecutionResult>(`/workflows/${workflowId}/execute`, {
    input,
  });
}
