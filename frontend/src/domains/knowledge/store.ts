import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  KnowledgeBaseOut,
  KnowledgeBaseCreate,
  SearchResult,
} from "@/shared/api/types";
import * as api from "./api";

export const useKnowledgeStore = defineStore("knowledge", () => {
  const knowledgeBases = ref<KnowledgeBaseOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const selectedId = ref<number | null>(null);
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
      const res = await api.fetchKnowledgeBases();
      knowledgeBases.value = res.items;
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

  async function uploadDocument(file: File) {
    if (selectedId.value === null) return;
    uploading.value = true;
    try {
      await api.uploadDocument(selectedId.value, file);
      await fetchList();
    } finally {
      uploading.value = false;
    }
  }

  async function search(query: string, topK = 5) {
    if (selectedId.value === null) return;
    searching.value = true;
    try {
      const res = await api.searchKnowledge(selectedId.value, {
        query,
        top_k: topK,
      });
      searchResults.value = res.results;
    } finally {
      searching.value = false;
    }
  }

  function select(id: number | null) {
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
