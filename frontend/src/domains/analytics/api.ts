import { api } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  ConversationOut,
  DashboardMetrics,
  ListResponse,
} from "@/shared/api/types";

export interface ConversationQuery {
  start_date?: string;
  end_date?: string;
  agent_id?: number;
  limit?: number;
  offset?: number;
}

export function fetchConversations(params: ConversationQuery = {}) {
  return api.get<ListResponse<ConversationOut>>(
    `/analytics/conversations${buildQuery(params as Record<string, string | number | undefined>)}`,
  );
}

export function getDashboardMetrics() {
  return api.get<DashboardMetrics>("/analytics/dashboard");
}
