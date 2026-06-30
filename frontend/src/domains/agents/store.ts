import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  AgentOut,
  AgentCreate,
  WorkflowOut,
  ExecutionResult,
} from "@/shared/api/types";
import * as api from "./api";

export const useAgentStore = defineStore("agents", () => {
  const agents = ref<AgentOut[]>([]);
  const workflows = ref<WorkflowOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<number | null>(null);
  const lastResult = ref<ExecutionResult | null>(null);
  const executing = ref(false);

  const selected = computed(
    () => agents.value.find((a) => a.id === selectedId.value) ?? null,
  );

  async function fetchAgents() {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.fetchAgents();
      agents.value = res.items;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load agents";
    } finally {
      loading.value = false;
    }
  }

  async function fetchWorkflows() {
    const res = await api.fetchWorkflows();
    workflows.value = res.items;
  }

  async function create(data: AgentCreate) {
    const agent = await api.createAgent(data);
    agents.value = [agent, ...agents.value];
    return agent;
  }

  async function execute(agentId: number, input: Record<string, unknown>) {
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

  function select(id: number | null) {
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
