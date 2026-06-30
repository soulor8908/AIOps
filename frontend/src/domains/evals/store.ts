import { defineStore } from "pinia";
import { ref } from "vue";
import type { EvalRunOut, EvalRunCreate } from "@/shared/api/types";
import * as api from "./api";

export const useEvalStore = defineStore("evals", () => {
  const runs = ref<EvalRunOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const running = ref(false);

  async function fetchList() {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.fetchEvals();
      runs.value = res.items;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load evals";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: EvalRunCreate) {
    const run = await api.createEval(data);
    runs.value = [run, ...runs.value];
    return run;
  }

  async function execute(evalId: string) {
    running.value = true;
    try {
      const updated = await api.executeEval(evalId);
      const idx = runs.value.findIndex((r) => r.id === evalId);
      if (idx >= 0) runs.value[idx] = updated;
      return updated;
    } finally {
      running.value = false;
    }
  }

  return { runs, loading, error, running, fetchList, create, execute };
});
