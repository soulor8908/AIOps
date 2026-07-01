import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  PromptOut,
  PromptCreate,
  PromptVersionOut,
  PromptVersionCreate,
  UUID,
} from "@/shared/api/types";
import * as api from "./api";

export const usePromptStore = defineStore("prompts", () => {
  const items = ref<PromptOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<UUID | null>(null);
  const versions = ref<PromptVersionOut[]>([]);
  const versionsLoading = ref(false);

  // 后端列表返回裸数组，无 total 字段；本地以 items.length 近似。
  const total = computed(() => items.value.length);

  const selected = computed(() =>
    items.value.find((p) => p.id === selectedId.value) ?? null,
  );

  async function fetchList(q = "", limit = 20, offset = 0) {
    loading.value = true;
    error.value = null;
    try {
      items.value = await api.fetchPrompts(q, limit, offset);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load prompts";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: PromptCreate) {
    const prompt = await api.createPrompt(data);
    items.value = [prompt, ...items.value];
    return prompt;
  }

  async function remove(promptId: UUID) {
    await api.deletePrompt(promptId);
    items.value = items.value.filter((p) => p.id !== promptId);
    if (selectedId.value === promptId) selectedId.value = null;
  }

  async function fetchVersions(promptId: UUID) {
    versionsLoading.value = true;
    try {
      versions.value = await api.listVersions(promptId);
    } finally {
      versionsLoading.value = false;
    }
  }

  async function createVersion(promptId: UUID, data: PromptVersionCreate) {
    const version = await api.createVersion(promptId, data);
    versions.value = [version, ...versions.value];
    const idx = items.value.findIndex((p) => p.id === promptId);
    if (idx >= 0) {
      // 后端 PromptOut 字段为 current_version_id（UUID）。
      items.value[idx] = {
        ...items.value[idx],
        current_version_id: version.id,
        versions: [version, ...items.value[idx].versions],
      };
    }
    return version;
  }

  async function rollback(promptId: UUID, versionId: UUID) {
    const version = await api.rollbackVersion(promptId, versionId);
    const idx = items.value.findIndex((p) => p.id === promptId);
    if (idx >= 0) {
      items.value[idx] = {
        ...items.value[idx],
        current_version_id: version.id,
      };
    }
    return version;
  }

  function select(promptId: UUID | null) {
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
