import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type { ModelConfigOut, ModelConfigCreate } from "@/shared/api/types";
import * as api from "./api";

export const useModelStore = defineStore("models", () => {
  const models = ref<ModelConfigOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedAlias = ref<string | null>(null);

  const selected = computed(
    () => models.value.find((m) => m.alias === selectedAlias.value) ?? null,
  );

  async function fetchList() {
    loading.value = true;
    error.value = null;
    try {
      models.value = await api.fetchModels();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load models";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: ModelConfigCreate) {
    error.value = null;
    try {
      const model = await api.createModel(data);
      models.value = [...models.value, model];
      return model;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to create model";
      throw e;
    }
  }

  async function remove(alias: string) {
    error.value = null;
    try {
      await api.deleteModel(alias);
      models.value = models.value.filter((m) => m.alias !== alias);
      if (selectedAlias.value === alias) selectedAlias.value = null;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to delete model";
      throw e;
    }
  }

  function select(alias: string | null) {
    selectedAlias.value = alias;
  }

  return {
    models,
    loading,
    error,
    selectedAlias,
    selected,
    fetchList,
    create,
    remove,
    select,
  };
});
