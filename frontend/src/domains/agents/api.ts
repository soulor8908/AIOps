import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  AgentOut,
  AgentCreate,
  ExecuteRequest,
  ExecutionResult,
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
