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

  // P1-5：请求序号守卫，防止快速切换路由时旧请求覆盖新数据。
  // 每次 fetch 递增 seq，resolve 时比对——若不是最新请求则丢弃结果。
  let _metricsSeq = 0;
  let _convSeq = 0;

  async function fetchMetrics() {
    const seq = ++_metricsSeq;
    loading.value = true;
    error.value = null;
    try {
      const result = await api.getDashboardMetrics();
      if (seq !== _metricsSeq) return; // 已被新请求取代
      metrics.value = result;
    } catch (e) {
      if (seq !== _metricsSeq) return;
      error.value = e instanceof Error ? e.message : "Failed to load metrics";
    } finally {
      if (seq === _metricsSeq) loading.value = false;
    }
  }

  async function fetchConversations(params: ConversationQuery = {}) {
    const seq = ++_convSeq;
    loading.value = true;
    error.value = null;
    try {
      const result = await api.fetchConversations(params);
      if (seq !== _convSeq) return; // 已被新请求取代
      conversations.value = result;
    } catch (e) {
      if (seq !== _convSeq) return;
      error.value = e instanceof Error ? e.message : "Failed to load conversations";
    } finally {
      if (seq === _convSeq) loading.value = false;
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
