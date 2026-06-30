import { api } from "@/shared/api/client";
import type {
  AgentOut,
  AgentCreate,
  AgentExecuteRequest,
  ExecutionResult,
  WorkflowOut,
  WorkflowDef,
  ExecutionTrace,
  ListResponse,
} from "@/shared/api/types";

export function fetchAgents() {
  return api.get<ListResponse<AgentOut>>("/agents");
}

export function createAgent(data: AgentCreate) {
  return api.post<AgentOut>("/agents", data);
}

export function executeAgent(agentId: number, data: AgentExecuteRequest) {
  return api.post<ExecutionResult>(`/agents/${agentId}/execute`, data);
}

export function fetchWorkflows() {
  return api.get<ListResponse<WorkflowOut>>("/workflows");
}

export function createWorkflow(data: WorkflowDef) {
  return api.post<WorkflowOut>("/workflows", data);
}

export function executeWorkflow(workflowId: number, input: Record<string, unknown>) {
  return api.post<ExecutionTrace>(`/workflows/${workflowId}/execute`, {
    input,
    stream: false,
  });
}
