import { api, upload } from "@/shared/api/client";
import { buildQuery } from "@/shared/utils";
import type {
  KnowledgeBaseOut,
  KnowledgeBaseCreate,
  DocumentOut,
  SearchResult,
  SearchQuery,
  UUID,
} from "@/shared/api/types";

// 列表端点返回裸数组（response_model=list[<Out>]），无 {items,total} 包装。
export function fetchKnowledgeBases(limit = 50, offset = 0) {
  return api.get<KnowledgeBaseOut[]>(
    `/knowledge-bases${buildQuery({ limit, offset })}`,
  );
}

export function createKnowledgeBase(data: KnowledgeBaseCreate) {
  return api.post<KnowledgeBaseOut>("/knowledge-bases", data);
}

export function getKnowledgeBase(kbId: UUID) {
  return api.get<KnowledgeBaseOut>(`/knowledge-bases/${kbId}`);
}

// 后端要求 Form 字段 title + file。
export function uploadDocument(kbId: UUID, file: File, title: string) {
  return upload<DocumentOut>(`/knowledge-bases/${kbId}/documents`, file, title);
}

// 后端 search 返回裸数组 SearchResult[]（非 {results: [...]}）。
export function searchKnowledge(kbId: UUID, data: SearchQuery) {
  return api.post<SearchResult[]>(`/knowledge-bases/${kbId}/search`, data);
}

export type { SearchResult };
