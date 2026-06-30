"""Knowledge Base — 业务逻辑纯函数。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import LLMError, NotFoundError, ValidationError
from app.core.llm_client import LLMClient, LLMConfig, Message
from app.domains.knowledge.chunker import chunk_text
from app.domains.knowledge.embedder import embed_batch
from app.domains.knowledge.models import (
    Chunk,
    Document,
    KnowledgeBase,
    KnowledgeBaseCreate,
    RAGQuery,
    SearchQuery,
    SearchResult,
)

MAX_DOC_BYTES = 50 * 1024 * 1024  # 50MB


async def create_kb(session: AsyncSession, payload: KnowledgeBaseCreate) -> KnowledgeBase:
    """创建知识库。"""
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
        raise ValidationError(f"文档超 {MAX_DOC_BYTES // 1024 // 1024}MB 上限")
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
    """RAG：检索 + LLM 生成。"""
    results = await search_kb(
        session,
        kb_id,
        SearchQuery(query=query.question, top_k=query.top_k),
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
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 返回非法 JSON: {exc}") from exc
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
