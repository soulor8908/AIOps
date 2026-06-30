import { api, upload } from "@/shared/api/client";
import type {
  KnowledgeBaseOut,
  KnowledgeBaseCreate,
  DocumentOut,
  SearchResult,
  SearchRequest,
  SearchResponse,
  ListResponse,
} from "@/shared/api/types";

export function fetchKnowledgeBases() {
  return api.get<ListResponse<KnowledgeBaseOut>>("/knowledge-bases");
}

export function createKnowledgeBase(data: KnowledgeBaseCreate) {
  return api.post<KnowledgeBaseOut>("/knowledge-bases", data);
}

export function uploadDocument(kbId: number, file: File) {
  return upload<DocumentOut>(`/knowledge-bases/${kbId}/documents`, file);
}

export function searchKnowledge(kbId: number, data: SearchRequest) {
  return api.post<SearchResponse>(`/knowledge-bases/${kbId}/search`, data);
}

export type { SearchResult };
