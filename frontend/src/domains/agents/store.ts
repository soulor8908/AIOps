import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  AgentOut,
  AgentCreate,
  WorkflowOut,
  ExecutionResult,
  UUID,
} from "@/shared/api/types";
import * as api from "./api";

export const useAgentStore = defineStore("agents", () => {
  const agents = ref<AgentOut[]>([]);
  const workflows = ref<WorkflowOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<UUID | null>(null);
  const lastResult = ref<ExecutionResult | null>(null);
  const executing = ref(false);

  const selected = computed(
    () => agents.value.find((a) => a.id === selectedId.value) ?? null,
  );

  async function fetchAgents() {
    loading.value = true;
    error.value = null;
    try {
      agents.value = await api.fetchAgents();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load agents";
    } finally {
      loading.value = false;
    }
  }

  async function fetchWorkflows() {
    workflows.value = await api.fetchWorkflows();
  }

  async function create(data: AgentCreate) {
    const agent = await api.createAgent(data);
    agents.value = [agent, ...agents.value];
    return agent;
  }

  // 后端 ExecuteRequest.input 为 str（min 1）。
  async function execute(agentId: UUID, input: string) {
    executing.value = true;
    error.value = null;
    try {
      lastResult.value = await api.executeAgent(agentId, { input });
      return lastResult.value;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Execution failed";
      throw e;
    } finally {
      executing.value = false;
    }
  }

  function select(id: UUID | null) {
    selectedId.value = id;
  }

  return {
    agents,
    workflows,
    loading,
    error,
    selectedId,
    selected,
    lastResult,
    executing,
    fetchAgents,
    fetchWorkflows,
    create,
    execute,
    select,
  };
});
