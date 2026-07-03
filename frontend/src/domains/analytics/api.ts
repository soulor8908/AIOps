import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  ConversationOut,
  DashboardMetrics,
  UUID,
} from "@/shared/api/types";

// 后端 /analytics/conversations 支持 user_id（UUID）、limit、offset、
// start_date / end_date（YYYY-MM-DD，按 created_at 闭区间过滤）查询参数。
export interface ConversationQuery {
  user_id?: UUID;
  limit?: number;
  offset?: number;
  start_date?: string;
  end_date?: string;
}

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchConversations(params: ConversationQuery = {}) {
  return api.get<ConversationOut[]>(
    `/analytics/conversations${buildQuery(params as Record<string, string | number | undefined>)}`,
  );
}

export function getConversation(conversationId: UUID) {
  return api.get<ConversationOut>(`/analytics/conversations/${conversationId}`);
}

export function getDashboardMetrics(days = 7) {
  return api.get<DashboardMetrics>(
    `/analytics/dashboard${buildQuery({ days })}`,
  );
}
