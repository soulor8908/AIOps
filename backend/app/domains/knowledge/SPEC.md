# Feature: Knowledge Base

> 对齐实际实现：`models.py` / `router.py` / `service.py` / `chunker.py` / `embedder.py`（前缀 `/api/v1/knowledge-bases`）

## Goals
- 知识库管理：每个 KB 独立配置 embedding 模型与分块策略（`chunk_size` / `chunk_overlap`）
- 文档上传：自动分块 → 向量化 → 入库（同步处理，status 流转 processing→ready）
- 向量检索：基于 pgvector 余弦距离算子（`cosine_distance`）的 top_k 检索
- RAG：检索 + LLM 生成，返回 `answer` / `sources` / `usage`

## Constraints
- 单文档最大 50MB（`service.MAX_DOC_BYTES = 50*1024*1024`）
- 向量维度 1536（`EMBEDDING_DIM = 1536`，对齐 `text-embedding-3-small`）
- 分块参数：`chunk_size` 100-8000（默认 800）、`chunk_overlap` 0-500（默认 100），且 `0 ≤ overlap < chunk_size`
- 检索 `top_k`：search 1-50 / RAG 1-20；`score_threshold` 0.0-1.0
- embedding HTTP 超时 30s；调用失败回退零向量（不中断上传，零向量自然排末位）
- KB `name` ≤ 128 字符
- 列表分页 `limit` 1-200

## Non-Goals
- OCR（仅处理可解码为 UTF-8 的文本）
- 多语言自动检测
- 语义分块（当前为固定长度按字符分块）
- 增量更新 / 单 chunk 重新嵌入
- 向量索引调参（HNSW 等）

## Success Criteria (Eval)
- [ ] 文档上传后 status 由 processing → ready，`chunk_count` 与实际分块数一致
- [ ] 检索结果按余弦相似度降序，低于 `score_threshold` 的被过滤
- [ ] embedding 失败时回退零向量，文档仍可入库
- [ ] RAG 返回的 sources 与检索结果一致，并附带 LLM usage
- [ ] 文档超 50MB 或内容为空时上传被拒绝
- [ ] `chunk_text` 在 overlap ≥ chunk_size 时抛 `ValueError`

## Data Models
- ORM `KnowledgeBase`（`knowledge_bases` 表）：`id`(UUID)、`name`、`description`、`embedding_model`(默认 text-embedding-3-small)、`chunk_size`(默认 800)、`chunk_overlap`(默认 100)、`created_at`、`updated_at`；`documents` 关系（cascade all, delete-orphan）
- ORM `Document`（`documents` 表）：`id`(UUID)、`knowledge_base_id`(FK, CASCADE)、`title`、`source_uri`、`mime_type`、`size_bytes`、`chunk_count`、`status`(pending/processing/ready/failed)、`created_at`、`updated_at`
- ORM `Chunk`（`chunks` 表）：`id`(UUID)、`document_id`(FK, CASCADE)、`knowledge_base_id`(FK, CASCADE)、`chunk_index`、`content`、`embedding`(Vector(1536))、`token_count`、`metadata`(JSONB)、`created_at`
- Schemas：`KnowledgeBaseCreate` / `KnowledgeBaseOut` / `DocumentOut` / `SearchQuery`(query/top_k/score_threshold) / `SearchResult`(chunk_id/document_id/content/score/metadata) / `RAGQuery`(question/top_k) / `RAGResponse`(answer/sources/usage)
- `chunker.py`：`chunk_text` 固定长度按字符切分（中文友好），`step = chunk_size - overlap`；`ChunkResult`(index/content/token_count)；`_estimate_tokens` 粗估（CJK 按字 + ASCII 按词）
- `embedder.py`：`embed_text` / `embed_batch` 调 OpenAI Embeddings API，失败回退 `_zero_vector`；`OPENAI_API_KEY` 未配置时直接返回零向量

## API Endpoints
前缀 `/api/v1/knowledge-bases`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/knowledge-bases` | 知识库列表（limit/offset） |
| POST | `/knowledge-bases` | 创建知识库 |
| GET | `/knowledge-bases/{kb_id}` | 知识库详情（含 documents） |
| POST | `/knowledge-bases/{kb_id}/documents` | 上传文档（multipart: `title` + `file`） |
| POST | `/knowledge-bases/{kb_id}/search` | 向量检索 |
| POST | `/knowledge-bases/{kb_id}/rag` | RAG 检索 + LLM 生成 |

## Error Cases
- 知识库不存在 → `NotFoundError` (404)
- 文档内容为空 → `ValidationError` (422)
- 文档超 50MB → `ValidationError` (422)
- embedding API 失败 / KEY 未配置 → 回退零向量（不抛错，日志记录）
- RAG 的 LLM 调用失败 → 异常上抛 `LLMError` (502)
- 分块参数非法（overlap ≥ chunk_size）→ `ValueError`
