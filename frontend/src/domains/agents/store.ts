import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  AgentOut,
  AgentCreate,
  ExecutionResult,
  SSEEvent,
  UUID,
} from "@/shared/api/types";
import * as api from "./api";

export const useAgentStore = defineStore("agents", () => {
  const agents = ref<AgentOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<UUID | null>(null);
  const lastResult = ref<ExecutionResult | null>(null);
  const executing = ref(false);

  // SSE 流式执行状态
  const streaming = ref(false);
  // 逐 token 累积的答案文本（流式渲染用）
  const streamText = ref("");
  // 流式过程中的工具调用与观察（展示执行过程）
  const streamTraces = ref<Array<{ type: "tool" | "observation"; content: string }>>([]);
  // 当前流式执行的 AbortController（用于取消）
  let streamController: AbortController | null = null;

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

  async function create(data: AgentCreate) {
    error.value = null;
    try {
      const agent = await api.createAgent(data);
      agents.value = [agent, ...agents.value];
      return agent;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to create agent";
      throw e;
    }
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

  /**
   * 流式执行 Agent：逐 token 累积到 streamText，工具/观察追加到 streamTraces，
   * done 事件写入 lastResult。调用方在执行前清空 streamText/streamTraces。
   *
   * @returns ExecutionResult（done 事件的 result）
   */
  async function executeStream(agentId: UUID, input: string): Promise<ExecutionResult> {
    streaming.value = true;
    error.value = null;
    streamText.value = "";
    streamTraces.value = [];
    streamController = new AbortController();
    try {
      let result: ExecutionResult | null = null;
      await api.executeAgentStream(
        agentId,
        { input },
        (event: SSEEvent) => {
          if (event.type === "token") {
            streamText.value += event.content;
          } else if (event.type === "tool") {
            streamTraces.value.push({
              type: "tool",
              content: `${event.name}(${JSON.stringify(event.args)})`,
            });
          } else if (event.type === "observation") {
            streamTraces.value.push({
              type: "observation",
              content: event.content,
            });
          } else if (event.type === "done") {
            result = event.result;
            // done 时同步 lastResult + streamText（兜底，若 token 事件未覆盖完整）
            lastResult.value = event.result;
            if (result && typeof result.final_answer === "string") {
              streamText.value = result.final_answer;
            }
          }
        },
        streamController.signal,
      );
      if (!result) {
        throw new Error("流式执行未收到 done 事件");
      }
      return result;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Stream execution failed";
      throw e;
    } finally {
      streaming.value = false;
      streamController = null;
    }
  }

  /** 取消正在进行的流式执行。 */
  function cancelStream(): void {
    if (streamController) {
      streamController.abort();
      streamController = null;
      streaming.value = false;
    }
  }

  function select(id: UUID | null) {
    selectedId.value = id;
  }

  return {
    agents,
    loading,
    error,
    selectedId,
    selected,
    lastResult,
    executing,
    // SSE 流式
    streaming,
    streamText,
    streamTraces,
    fetchAgents,
    create,
    execute,
    executeStream,
    cancelStream,
    select,
  };
});
