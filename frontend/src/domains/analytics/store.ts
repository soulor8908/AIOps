import { defineStore } from "pinia";
import { ref } from "vue";
import type { ConversationOut, DashboardMetrics } from "@/shared/api/types";
import * as api from "./api";
import type { ConversationQuery } from "./api";

export const useAnalyticsStore = defineStore("analytics", () => {
  const metrics = ref<DashboardMetrics | null>(null);
  const conversations = ref<ConversationOut[]>([]);
  const total = ref(0);
  const loading = ref(false);
  const error = ref<string | null>(null);

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
      const res = await api.fetchConversations(params);
      conversations.value = res.items;
      total.value = res.total;
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
