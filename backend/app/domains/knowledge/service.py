"""Knowledge Base — 业务逻辑纯函数。"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError, ValidationError
from app.core.llm_client import LLMClient, LLMConfig, Message
from app.domains.knowledge.chunker import chunk_text
from app.domains.knowledge.embedder import EMBEDDING_MODEL_REGISTRY, embed_batch
from app.domains.knowledge.models import (
    EMBEDDING_DIM,
    Chunk,
    Document,
    KnowledgeBase,
    KnowledgeBaseCreate,
    RAGQuery,
    SearchQuery,
    SearchResult,
)

logger = logging.getLogger(__name__)

MAX_DOC_BYTES = 50 * 1024 * 1024  # 50MB

# RRF 常数（Reciprocal Rank Fusion 标准值，参考 Cormack et al. 2009）
RRF_K = 60
# hybrid 检索每路候选放大倍数：fetch_k = top_k * RRF_FETCH_MULT，让 RRF 有更多候选
RRF_FETCH_MULT = 2
# LLM reranker 单候选内容截断长度，避免 prompt 过长
_RERANK_CONTENT_LIMIT = 500


def _is_postgresql(session: AsyncSession) -> bool:
    """判断当前 session 绑定的方言是否为 PostgreSQL。

    SQLite 测试环境返回 False，触发 BM25 检索降级为 LIKE、向量检索跳过。
    """
    bind = session.bind
    if bind is None:
        return False
    return bind.dialect.name == "postgresql"


# ===================== hybrid search 内部组件 =====================


async def _vector_search(
    session: AsyncSession,
    kb_id: uuid.UUID,
    q_vec: list[float],
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """向量检索 top_k（pgvector 余弦距离）。

    SQLite 上 pgvector 算子不可用，返回空列表（hybrid 退化为纯 BM25）。
    """
    if not _is_postgresql(session):
        logger.debug("SQLite 环境，跳过向量检索（cosine_distance 不可用）")
        return []
    # pgvector: <=> 余弦距离，1 - distance = 相似度
    stmt = (
        select(
            Chunk,
            (1.0 - Chunk.embedding.cosine_distance(q_vec)).label("score"),
        )
        .where(Chunk.knowledge_base_id == kb_id)
        .order_by(Chunk.embedding.cosine_distance(q_vec))
        .limit(top_k)
    )
    rows = (await session.execute(stmt)).all()
    return [(chunk, float(score) if score is not None else 0.0) for chunk, score in rows]


async def _bm25_search(
    session: AsyncSession,
    kb_id: uuid.UUID,
    query_text: str,
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """BM25 全文检索 top_k。

    PG: ts_rank_cd(search_vector, plainto_tsquery('simple', q)) 排序 + @@ 过滤。
    SQLite: 无 tsvector，降级为 content LIKE 模糊匹配（score 固定 1.0，RRF 只关心 rank）。
    """
    if _is_postgresql(session):
        ts_query = func.plainto_tsquery("simple", query_text)
        score_expr = func.ts_rank_cd(Chunk.search_vector, ts_query).label("score")
        stmt = (
            select(Chunk, score_expr)
            .where(
                Chunk.knowledge_base_id == kb_id,
                Chunk.search_vector.op("@@")(ts_query),
            )
            .order_by(score_expr.desc())
            .limit(top_k)
        )
    else:
        # SQLite 降级：LIKE 模糊匹配，score 固定 1.0（RRF 只关心 rank）
        stmt = (
            select(Chunk, literal(1.0).label("score"))
            .where(
                Chunk.knowledge_base_id == kb_id,
                Chunk.content.like(f"%{query_text}%"),
            )
            .order_by(Chunk.chunk_index)
            .limit(top_k)
        )
    rows = (await session.execute(stmt)).all()
    return [(chunk, float(score) if score is not None else 0.0) for chunk, score in rows]


def _rrf_fuse(
    vector_rows: list[tuple[Chunk, float]],
    bm25_rows: list[tuple[Chunk, float]],
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """RRF 融合：score = sum(1/(RRF_K + rank_i))，按融合分数降序取 top_k。

    rank 从 1 起算（rank=1 为各路最优），与 Cormack et al. 2009 一致。
    """
    scores: defaultdict[uuid.UUID, float] = defaultdict(float)
    chunks: dict[uuid.UUID, Chunk] = {}
    for rank, (chunk, _) in enumerate(vector_rows, start=1):
        scores[chunk.id] += 1.0 / (RRF_K + rank)
        chunks[chunk.id] = chunk
    for rank, (chunk, _) in enumerate(bm25_rows, start=1):
        scores[chunk.id] += 1.0 / (RRF_K + rank)
        chunks[chunk.id] = chunk
    sorted_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [(chunks[cid], scores[cid]) for cid in sorted_ids[:top_k]]


async def _llm_rerank(
    question: str,
    candidates: list[tuple[Chunk, float]],
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """LLM reranker：单次调用让 LLM 对候选按相关度降序排序。

    prompt 列出所有候选（内容截断），LLM 输出按相关度降序的索引列表。
    解析失败或返回不全时兜底按原 RRF 顺序补全，保证不丢候选。

    注意：此为 LLM rerank 的回退路径。优先使用 ``_cross_encoder_rerank``
    （本地 cross-encoder，无 API 成本、低延迟）。仅当 sentence-transformers
    未安装时才走到此分支。
    """
    if not candidates:
        return []
    from app.core.config import settings

    docs_block = "\n".join(
        f"[{i}] {c.content[:_RERANK_CONTENT_LIMIT]}" for i, (c, _) in enumerate(candidates)
    )
    prompt = (
        "你是相关度判官。根据问题对候选文档按相关度降序排序，只输出 JSON。\n"
        f"问题：{question}\n候选文档：\n{docs_block}\n"
        '输出格式：{"order": [索引列表]}'
    )
    client = LLMClient(
        LLMConfig(
            provider="openai",
            model=settings.default_llm_model,
            api_key=settings.openai_api_key,
        )
    )
    try:
        resp = await client.chat([Message(role="user", content=prompt)])
    finally:
        await client.close()

    try:
        data = json.loads(resp.content.strip().strip("`").strip())
        order = data.get("order", [])
        if not isinstance(order, list):
            order = []
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        logger.warning("LLM reranker 输出无法解析，保持原 RRF 顺序")
        return candidates[:top_k]

    reranked: list[tuple[Chunk, float]] = []
    seen: set[int] = set()
    for idx in order:
        if isinstance(idx, int) and 0 <= idx < len(candidates) and idx not in seen:
            reranked.append(candidates[idx])
            seen.add(idx)
        if len(reranked) >= top_k:
            break
    # 兜底：LLM 返回不全则按原 RRF 顺序补全
    for i, candidate in enumerate(candidates):
        if len(reranked) >= top_k:
            break
        if i not in seen:
            reranked.append(candidate)
            seen.add(i)
    return reranked[:top_k]


# P1-4：cross-encoder 本地 reranker。sentence-transformers 为可选重依赖，
# 仅在显式安装后启用；未安装时 _cross_encoder_available 为 False，rerank
# 回退到 LLM rerank。模型首次加载后缓存到 _CROSS_ENCODER_MODEL 避免重复加载。
_CROSS_ENCODER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_CROSS_ENCODER_MODEL: Any = None
_CROSS_ENCODER_AVAILABLE: bool | None = None


def _is_cross_encoder_available() -> bool:
    """检测 sentence-transformers 是否可导入（惰性，结果缓存）。"""
    global _CROSS_ENCODER_AVAILABLE
    if _CROSS_ENCODER_AVAILABLE is None:
        try:
            import sentence_transformers  # noqa: F401

            _CROSS_ENCODER_AVAILABLE = True
        except ImportError:
            _CROSS_ENCODER_AVAILABLE = False
            logger.info(
                "sentence-transformers 未安装，rerank 回退到 LLM rerank。"
                "安装：pip install sentence-transformers"
            )
    return _CROSS_ENCODER_AVAILABLE


def _get_cross_encoder() -> Any:
    """惰性加载 cross-encoder 模型（首次调用加载，后续复用缓存）。"""
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    from sentence_transformers import CrossEncoder

    _CROSS_ENCODER_MODEL = CrossEncoder(_CROSS_ENCODER_MODEL_NAME)
    return _CROSS_ENCODER_MODEL


async def _cross_encoder_rerank(
    question: str,
    candidates: list[tuple[Chunk, float]],
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """P1-4：cross-encoder 本地 reranker。

    用 ``cross-encoder/ms-marco-MiniLM-L-6-v2`` 对 (question, doc) 对打分，
    按分数降序取 top_k。本地 CPU 推理，无 API 成本、低延迟（~10ms/candidate）。
    ``predict`` 是 CPU 密集型，放线程池避免阻塞事件循环。
    """
    if not candidates:
        return []
    import asyncio

    model = _get_cross_encoder()
    pairs = [(question, c.content[:_RERANK_CONTENT_LIMIT]) for c, _ in candidates]
    # CPU 密集 → 线程池执行，避免阻塞 async 事件循环
    scores = await asyncio.to_thread(model.predict, pairs)
    ranked = sorted(
        zip(candidates, scores, strict=True), key=lambda x: x[1], reverse=True
    )
    return [(c, float(s)) for (c, _), s in ranked[:top_k]]


async def _rerank(
    question: str,
    candidates: list[tuple[Chunk, float]],
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """P1-4：rerank 调度。优先 cross-encoder 本地推理，不可用时回退 LLM rerank。"""
    if _is_cross_encoder_available():
        try:
            return await _cross_encoder_rerank(question, candidates, top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross-encoder rerank 失败，回退 LLM rerank: %s", exc)
    return await _llm_rerank(question, candidates, top_k)


async def _hybrid_search(
    session: AsyncSession,
    kb: KnowledgeBase,
    question: str,
    top_k: int,
    rerank: bool = False,
) -> list[SearchResult]:
    """hybrid 检索：向量 + BM25 + RRF + 可选 reranker。

    默认不开 rerank（避免额外成本），RRF 融合已显著优于纯向量。
    开启 rerank 时优先用 cross-encoder 本地推理（无 API 成本），
    sentence-transformers 未安装则回退 LLM rerank。
    """
    from app.domains.knowledge.embedder import embed_text

    q_vec = await embed_text(question, kb.embedding_model)
    fetch_k = min(top_k * RRF_FETCH_MULT, 50)

    vec_rows = await _vector_search(session, kb.id, q_vec, fetch_k)
    bm25_rows = await _bm25_search(session, kb.id, question, fetch_k)
    fused = _rrf_fuse(vec_rows, bm25_rows, top_k)

    if rerank and fused:
        fused = await _rerank(question, fused, top_k)

    return [
        SearchResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            content=chunk.content,
            score=score,
            metadata=dict(chunk.metadata_) if chunk.metadata_ else {},
        )
        for chunk, score in fused
    ]


async def create_kb(session: AsyncSession, payload: KnowledgeBaseCreate) -> KnowledgeBase:
    """创建知识库。

    P1-7：校验 ``embedding_model`` 已在注册表登记，并校验其维度与 chunks.embedding
    列维度（``EMBEDDING_DIM``）一致。维度不匹配会导致 pgvector 写入/检索崩溃，
    在创建阶段拦截给出明确错误，优于上传时才暴露。
    """
    if payload.embedding_model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(
            f"embedding_model '{payload.embedding_model}' 未登记，"
            f"可选: {sorted(EMBEDDING_MODEL_REGISTRY)}"
        )
    model_dim = EMBEDDING_MODEL_REGISTRY[payload.embedding_model]
    if model_dim != EMBEDDING_DIM:
        raise ValidationError(
            f"embedding_model '{payload.embedding_model}' 维度 {model_dim} "
            f"与 chunks.embedding 列维度 {EMBEDDING_DIM} 不匹配，"
            f"请选择维度为 {EMBEDDING_DIM} 的模型"
        )
    kb = KnowledgeBase(
        name=payload.name,
        description=payload.description,
        embedding_model=payload.embedding_model,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    session.add(kb)
    await session.flush()
    return kb


async def get_kb(session: AsyncSession, kb_id: uuid.UUID) -> KnowledgeBase:
    """获取知识库（含 documents 关系）。"""
    stmt = (
        select(KnowledgeBase)
        .options(selectinload(KnowledgeBase.documents))
        .where(KnowledgeBase.id == kb_id)
    )
    kb = (await session.execute(stmt)).scalar_one_or_none()
    if kb is None:
        raise NotFoundError(f"知识库 {kb_id} 不存在")
    return kb


async def list_kbs(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[KnowledgeBase]:
    """列出知识库。"""
    stmt = (
        select(KnowledgeBase)
        .order_by(KnowledgeBase.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def upload_document(
    session: AsyncSession,
    kb_id: uuid.UUID,
    title: str,
    content: str,
    mime_type: str | None = None,
    source_uri: str | None = None,
) -> Document:
    """上传文档：分块 + 向量化 + 入库。"""
    kb = await get_kb(session, kb_id)
    size = len(content.encode("utf-8"))
    if size > MAX_DOC_BYTES:
        raise ValidationError(
            f"文档超 {MAX_DOC_BYTES // 1024 // 1024}MB 上限"
            f"（实际 {size} bytes, 上限 {MAX_DOC_BYTES} bytes）"
        )
    doc = Document(
        knowledge_base_id=kb_id,
        title=title,
        source_uri=source_uri,
        mime_type=mime_type,
        size_bytes=size,
        status="processing",
    )
    session.add(doc)
    await session.flush()
    chunks = chunk_text(content, chunk_size=kb.chunk_size, overlap=kb.chunk_overlap)
    embeddings = await embed_batch([c.content for c in chunks], kb.embedding_model)
    # P1-7：防御性校验返回向量维度与列维度一致（API 异常或模型配置漂移时兜底）
    for emb in embeddings:
        if len(emb) != EMBEDDING_DIM:
            raise ValidationError(
                f"embedding 返回维度 {len(emb)} 与 chunks.embedding 列维度 "
                f"{EMBEDDING_DIM} 不匹配（model={kb.embedding_model}），"
                f"已中止上传避免脏数据"
            )
    for chunk, emb in zip(chunks, embeddings, strict=True):
        session.add(
            Chunk(
                document_id=doc.id,
                knowledge_base_id=kb_id,
                chunk_index=chunk.index,
                content=chunk.content,
                embedding=emb,
                token_count=chunk.token_count,
                metadata_={"title": title},
            )
        )
    doc.chunk_count = len(chunks)
    doc.status = "ready"
    await session.flush()
    # PG 上同步更新 search_vector（to_tsvector('simple', content)）用于 BM25 检索；
    # SQLite 上跳过（search_vector 列渲染为 TEXT，BM25 降级为 content LIKE）
    if _is_postgresql(session):
        await session.execute(
            update(Chunk)
            .where(Chunk.document_id == doc.id)
            .values(search_vector=func.to_tsvector("simple", Chunk.content))
        )
    return doc


async def search_kb(
    session: AsyncSession, kb_id: uuid.UUID, query: SearchQuery
) -> list[SearchResult]:
    """向量检索 top_k。使用 pgvector 余弦距离算子。"""
    kb = await get_kb(session, kb_id)
    from app.domains.knowledge.embedder import embed_text

    q_vec = await embed_text(query.query, kb.embedding_model)
    # pgvector: <=> 余弦距离，1 - distance = 相似度
    stmt = (
        select(
            Chunk,
            (1.0 - Chunk.embedding.cosine_distance(q_vec)).label("score"),
        )
        .where(Chunk.knowledge_base_id == kb_id)
        .order_by(Chunk.embedding.cosine_distance(q_vec))
        .limit(query.top_k)
    )
    rows = (await session.execute(stmt)).all()
    results: list[SearchResult] = []
    for chunk, score in rows:
        score_f = float(score) if score is not None else 0.0
        if score_f < query.score_threshold:
            continue
        results.append(
            SearchResult(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                content=chunk.content,
                score=score_f,
                metadata=dict(chunk.metadata_) if chunk.metadata_ else {},
            )
        )
    return results


async def rag_query(
    session: AsyncSession, kb_id: uuid.UUID, query: RAGQuery
) -> dict[str, Any]:
    """RAG：hybrid 检索 + LLM 生成。

    检索流程：向量 + BM25 双路 → RRF 融合 → 可选 LLM reranker → LLM 生成答案。
    默认 ``rerank=False``（无额外 LLM 成本），hybrid + RRF 已显著优于纯向量。
    """
    kb = await get_kb(session, kb_id)
    results = await _hybrid_search(
        session, kb, query.question, query.top_k, query.rerank
    )
    context = "\n---\n".join(r.content for r in results)
    from app.core.config import settings

    client = LLMClient(
        LLMConfig(
            provider="openai",
            model=settings.default_llm_model,
            api_key=settings.openai_api_key,
        )
    )
    messages = [
        Message(role="system", content="根据以下上下文回答问题。\n上下文:\n" + context),
        Message(role="user", content=query.question),
    ]
    try:
        resp = await client.chat(messages)
    finally:
        await client.close()
    return {
        "answer": resp.content,
        "sources": [r.model_dump() for r in results],
        "usage": resp.usage,
    }


__all__ = [
    "MAX_DOC_BYTES",
    "create_kb",
    "get_kb",
    "list_kbs",
    "rag_query",
    "search_kb",
    "upload_document",
]
