import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type { ConversationOut, DashboardMetrics } from "@/shared/api/types";
import * as api from "./api";
import type { ConversationQuery } from "./api";

export const useAnalyticsStore = defineStore("analytics", () => {
  const metrics = ref<DashboardMetrics | null>(null);
  const conversations = ref<ConversationOut[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 后端列表返回裸数组，无 total 字段；本地以 conversations.length 近似。
  const total = computed(() => conversations.value.length);

  async function fetchMetrics() {
    loading.value = true;
    error.value = null;
    try {
      metrics.value = await api.getDashboardMetrics();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load metrics";
    } finally {
      loading.value = false;
    }
  }

  async function fetchConversations(params: ConversationQuery = {}) {
    loading.value = true;
    error.value = null;
    try {
      conversations.value = await api.fetchConversations(params);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load conversations";
    } finally {
      loading.value = false;
    }
  }

  return {
    metrics,
    conversations,
    total,
    loading,
    error,
    fetchMetrics,
    fetchConversations,
  };
});
