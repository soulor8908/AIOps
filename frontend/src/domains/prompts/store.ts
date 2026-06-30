import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  PromptOut,
  PromptCreate,
  PromptVersionOut,
  PromptVersionCreate,
} from "@/shared/api/types";
import * as api from "./api";

export const usePromptStore = defineStore("prompts", () => {
  const items = ref<PromptOut[]>([]);
  const total = ref(0);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<number | null>(null);
  const versions = ref<PromptVersionOut[]>([]);
  const versionsLoading = ref(false);

  const selected = computed(() =>
    items.value.find((p) => p.id === selectedId.value) ?? null,
  );

  async function fetchList(q = "", limit = 20, offset = 0) {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.fetchPrompts(q, limit, offset);
      items.value = res.items;
      total.value = res.total;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load prompts";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: PromptCreate) {
    const prompt = await api.createPrompt(data);
    items.value = [prompt, ...items.value];
    total.value++;
    return prompt;
  }

  async function remove(promptId: number) {
    await api.deletePrompt(promptId);
    items.value = items.value.filter((p) => p.id !== promptId);
    if (selectedId.value === promptId) selectedId.value = null;
  }

  async function fetchVersions(promptId: number) {
    versionsLoading.value = true;
    try {
      const res = await api.listVersions(promptId);
      versions.value = res.items;
    } finally {
      versionsLoading.value = false;
    }
  }

  async function createVersion(promptId: number, data: PromptVersionCreate) {
    const version = await api.createVersion(promptId, data);
    versions.value = [version, ...versions.value];
    const idx = items.value.findIndex((p) => p.id === promptId);
    if (idx >= 0) {
      items.value[idx] = { ...items.value[idx], current_version: version, version_count: items.value[idx].version_count + 1 };
    }
    return version;
  }

  async function rollback(promptId: number, versionId: number) {
    const prompt = await api.rollbackVersion(promptId, versionId);
    const idx = items.value.findIndex((p) => p.id === promptId);
    if (idx >= 0) items.value[idx] = prompt;
    return prompt;
  }

  function select(promptId: number | null) {
    selectedId.value = promptId;
  }

  return {
    items,
    total,
    loading,
    error,
    selectedId,
    selected,
    versions,
    versionsLoading,
    fetchList,
    create,
    remove,
    fetchVersions,
    createVersion,
    rollback,
    select,
  };
});
