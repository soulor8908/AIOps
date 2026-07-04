"""Agent 记忆层（P1-4）— 向量化历史检索替换 LLM 摘要压缩。

设计要点：
- 复用 ``knowledge/embedder.py`` 的 ``embed_text``（1536 维 text-embedding-3-small），
  不重复造 embedder。
- ``upsert_memory``：对 content 向量化后持久化到 ``agent_memory_chunks`` 表。
  embedding 失败回退零向量（embedder 内部处理），不阻塞主流程。
- ``search_memory``：对 query 向量化后用 pgvector ``cosine_distance`` 检索 top-k。
  SQLite 上 pgvector 算子不可用，返回空列表（降级为无记忆注入）。
- ``PgMemoryBackend``：实现 executor 的 ``MemoryBackend`` 协议，用独立的
  ``AsyncSessionLocal`` 会话执行 DB 操作（executor 在请求 session commit 后运行）。
  ``session_factory`` 可注入以便测试替换。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domains.agents.models import AgentMemoryChunk
from app.domains.knowledge.embedder import embed_text

logger = logging.getLogger("app.agents.memory")

# 默认检索 top-k。可通过 ``settings.agent_memory_top_k`` 覆盖。
_DEFAULT_TOP_K = 3


async def upsert_memory(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    turn: int,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> AgentMemoryChunk:
    """向量化 content 并持久化为一条记忆分块。

    embedding 失败时 embed_text 返回零向量（不抛错），写入仍成功——零向量在
    检索时 cosine 相似度最低，自然排末位，不影响检索质量。
    """
    embedding = await embed_text(content)
    chunk = AgentMemoryChunk(
        agent_id=agent_id,
        session_id=session_id,
        turn=turn,
        content=content,
        embedding=embedding,
        metadata_=metadata or {},
    )
    session.add(chunk)
    await session.commit()
    await session.refresh(chunk)
    return chunk


async def search_memory(
    session: AsyncSession,
    agent_id: uuid.UUID,
    query: str,
    top_k: int = _DEFAULT_TOP_K,
) -> list[AgentMemoryChunk]:
    """按 query 向量检索 agent 的 top-k 相关历史记忆。

    SQLite 上 pgvector ``cosine_distance`` 算子不可用，返回空列表（降级为无
    记忆注入）。生产 PG 上用 HNSW 索引加速。
    """
    if not _is_postgresql(session):
        logger.debug("SQLite 环境，跳过记忆检索（cosine_distance 不可用）")
        return []
    q_vec = await embed_text(query)
    stmt = (
        select(AgentMemoryChunk)
        .where(AgentMemoryChunk.agent_id == agent_id)
        .order_by(AgentMemoryChunk.embedding.cosine_distance(q_vec))
        .limit(top_k)
    )
    return list((await session.execute(stmt)).scalars().all())


def _is_postgresql(session: AsyncSession) -> bool:
    """判断当前 session 绑定的方言是否为 PostgreSQL。

    与 knowledge/service.py 同模式：SQLite 测试环境返回 False。
    """
    bind = session.bind
    if bind is None:
        return False
    return bind.dialect.name == "postgresql"


class MemoryBackend(Protocol):
    """Agent 记忆后端协议。executor 依赖此协议，具体实现由调用方注入。

    ``search`` 返回相关历史 content 列表（已去 embedding），供 executor 注入
    为 system 消息。``upsert`` 持久化单轮 content（observation / final_answer）。
    两个方法均不应抛异常出协议边界——失败时实现应记日志并降级（search 返回 []，
    upsert 静默跳过），绝不阻塞主请求路径。
    """

    async def search(
        self, agent_id: uuid.UUID, query: str, top_k: int = _DEFAULT_TOP_K
    ) -> list[str]: ...

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        turn: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class PgMemoryBackend:
    """pgvector + AsyncSessionLocal 实现的记忆后端。

    executor 在请求 session ``commit`` 后运行（LLM 调用不在事务内），故记忆
    DB 操作需用独立会话。``session_factory`` 默认为应用级 ``AsyncSessionLocal``，
    测试可注入测试引擎的 session factory。

    所有方法捕获异常并降级（search→[]、upsert→no-op），不阻塞主请求路径
    （observability.spec.md§5：记忆/采样不阻塞请求）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        top_k: int = _DEFAULT_TOP_K,
    ) -> None:
        self._sf = session_factory
        self._top_k = top_k

    async def search(
        self, agent_id: uuid.UUID, query: str, top_k: int | None = None
    ) -> list[str]:
        k = top_k if top_k is not None else self._top_k
        try:
            async with self._sf() as session:
                chunks = await search_memory(session, agent_id, query, k)
                return [c.content for c in chunks]
        except Exception:  # noqa: BLE001
            logger.exception("P1-4 memory search failed (agent=%s)", agent_id)
            return []

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        turn: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            async with self._sf() as session:
                await upsert_memory(
                    session,
                    agent_id=agent_id,
                    session_id=session_id,
                    turn=turn,
                    content=content,
                    metadata=metadata,
                )
        except Exception:  # noqa: BLE001
            logger.exception("P1-4 memory upsert failed (agent=%s, turn=%d)", agent_id, turn)
