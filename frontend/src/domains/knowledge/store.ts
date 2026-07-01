import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  KnowledgeBaseOut,
  KnowledgeBaseCreate,
  SearchResult,
  UUID,
} from "@/shared/api/types";
import * as api from "./api";

export const useKnowledgeStore = defineStore("knowledge", () => {
  const knowledgeBases = ref<KnowledgeBaseOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<UUID | null>(null);
  const searchResults = ref<SearchResult[]>([]);
  const searching = ref(false);
  const uploading = ref(false);

  const selected = computed(
    () => knowledgeBases.value.find((kb) => kb.id === selectedId.value) ?? null,
  );

  async function fetchList() {
    loading.value = true;
    error.value = null;
    try {
      knowledgeBases.value = await api.fetchKnowledgeBases();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load knowledge bases";
    } finally {
      loading.value = false;
    }
  }

  async function create(data: KnowledgeBaseCreate) {
    const kb = await api.createKnowledgeBase(data);
    knowledgeBases.value = [kb, ...knowledgeBases.value];
    return kb;
  }

  // 后端要求 Form 字段 title；文件名去除扩展名作为标题。
  async function uploadDocument(file: File) {
    if (selectedId.value === null) return;
    uploading.value = true;
    try {
      const title = file.name.replace(/\.[^.]+$/, "") || file.name;
      await api.uploadDocument(selectedId.value, file, title);
      await fetchList();
    } finally {
      uploading.value = false;
    }
  }

  async function search(query: string, topK = 5) {
    if (selectedId.value === null) return;
    searching.value = true;
    try {
      // 后端 search 返回裸数组 SearchResult[]。
      searchResults.value = await api.searchKnowledge(selectedId.value, {
        query,
        top_k: topK,
      });
    } finally {
      searching.value = false;
    }
  }

  function select(id: UUID | null) {
    selectedId.value = id;
    searchResults.value = [];
  }

  return {
    knowledgeBases,
    loading,
    error,
    selectedId,
    selected,
    searchResults,
    searching,
    uploading,
    fetchList,
    create,
    uploadDocument,
    search,
    select,
  };
});
