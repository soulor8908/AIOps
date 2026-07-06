import { defineStore } from "pinia";
import { ref } from "vue";
import type {
  EvalRunOut,
  EvalRunCreate,
  EvalSampleOut,
  EvalSampleQuery,
  OnlineEvalRequest,
} from "@/shared/api/types";
import * as api from "./api";

export const useEvalStore = defineStore("evals", () => {
  const runs = ref<EvalRunOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const running = ref(false);

  // E2：online eval 闭环状态
  const samples = ref<EvalSampleOut[]>([]);
  const samplesLoading = ref(false);
  const onlineEvalRunning = ref(false);
  // E2：跨组件共享的选中 sample ids（EvalSamples 选择 → OnlineEvalRunner 触发）
  const selectedSampleIds = ref<Set<string>>(new Set());

  async function fetchList() {
    loading.value = true;
    error.value = null;
    try {
      runs.value = await api.fetchEvals();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load evals";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: EvalRunCreate) {
    error.value = null;
    try {
      const run = await api.createEval(data);
      runs.value = [run, ...runs.value];
      return run;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to create eval";
      throw e;
    }
  }

  async function execute(evalId: string) {
    running.value = true;
    error.value = null;
    try {
      const updated = await api.executeEval(evalId);
      const idx = runs.value.findIndex((r) => r.id === evalId);
      if (idx >= 0) runs.value[idx] = updated;
      return updated;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Eval execution failed";
      throw e;
    } finally {
      running.value = false;
    }
  }

  // ===================== E2: Online eval 闭环 =====================

  /** 拉取生产采样样本列表。
   * 默认查询未 judged 样本（online eval 的待评估队列）。
   * @param query 查询参数，默认 judged=false（待评估队列） */
  async function fetchSamples(query: EvalSampleQuery = { judged: false }) {
    samplesLoading.value = true;
    error.value = null;
    try {
      samples.value = await api.fetchSamples(query);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load samples";
    } finally {
      samplesLoading.value = false;
    }
  }

  /** 触发 online eval 闭环（admin-only）。
   * 成功后把产出的 EvalRun 插入 runs 头部，便于在 EvalList 中查看回归结果。 */
  async function runOnlineEval(data: OnlineEvalRequest) {
    onlineEvalRunning.value = true;
    error.value = null;
    try {
      const run = await api.runOnlineEval(data);
      runs.value = [run, ...runs.value];
      // 触发后这些样本已被 judge，刷新待评估队列 + 清空选择
      selectedSampleIds.value = new Set();
      await fetchSamples({ judged: false });
      return run;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Online eval failed";
      throw e;
    } finally {
      onlineEvalRunning.value = false;
    }
  }

  function toggleSampleSelection(id: string) {
    if (selectedSampleIds.value.has(id)) {
      selectedSampleIds.value.delete(id);
    } else {
      selectedSampleIds.value.add(id);
    }
    selectedSampleIds.value = new Set(selectedSampleIds.value);
  }

  function selectAllPendingSamples() {
    const unjudged = samples.value.filter((s) => !s.judged);
    selectedSampleIds.value = new Set(unjudged.map((s) => s.id));
  }

  function clearSampleSelection() {
    selectedSampleIds.value = new Set();
  }

  return {
    runs,
    loading,
    error,
    running,
    // E2
    samples,
    samplesLoading,
    onlineEvalRunning,
    selectedSampleIds,
    fetchList,
    create,
    execute,
    fetchSamples,
    runOnlineEval,
    toggleSampleSelection,
    selectAllPendingSamples,
    clearSampleSelection,
  };
});
