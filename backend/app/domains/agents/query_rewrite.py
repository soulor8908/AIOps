"""查询改写 / HyDE（P1-5）— 检索前 LLM 改写 query + 多 query 并发检索去重。

设计要点：
- ``QueryRewriter``：用 LLM 生成 N 个查询变体 + 1 个 HyDE 假设文档作为额外查询。
  HyDE（Hypothetical Document Embeddings，Gao et al. 2022）思路：让 LLM 先
  "假装"回答问题，把假设答案作为检索 query——假设答案与真实答案的语义分布更接近
  目标文档，比直接用原始问题检索召回更高。
- ``MultiQueryMemoryBackend``：包装 ``MemoryBackend``，对改写后的多个 query 并发
  检索（``asyncio.gather``），按 content 归一化去重，保留出现频次最高的 top-k。
- 所有 LLM 调用失败时降级为原始单 query 检索（不阻塞主流程）。
- 默认关闭（``agent_query_rewrite_enabled=False``），启用后由 ``execute_agent``
  注入 ``MultiQueryMemoryBackend`` 包装 ``PgMemoryBackend``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from app.core.llm_client import LLMClient, LLMResponse, Message
from app.domains.agents.memory import _DEFAULT_TOP_K, MemoryBackend

logger = logging.getLogger("app.agents.query_rewrite")

# 默认生成 2 个查询变体 + 1 个 HyDE（共 3 个额外 query + 原始 query = 4 路并发检索）
_DEFAULT_N_VARIANTS = 2
_DEFAULT_ENABLE_HYDE = True

_REWRITE_SYSTEM = (
    "你是查询改写助手。把用户问题改写为语义等价但表述不同的检索查询，"
    "用于提升向量检索召回。返回 JSON 数组，每个元素是一个改写后的查询字符串。"
    "不要解释，只返回 JSON 数组。"
)

_HYDE_SYSTEM = (
    "你是假设文档生成器。针对用户问题，写一段假设性的答案片段（50-150 字），"
    "用于向量检索。直接返回答案文本，不要解释。"
)


class QueryRewriter:
    """LLM 驱动的查询改写器。

    生成查询变体 + 可选 HyDE 假设文档。所有 LLM 失败降级为仅返回原始 query。
    """

    def __init__(
        self,
        llm: LLMClient,
        n_variants: int = _DEFAULT_N_VARIANTS,
        enable_hyde: bool = _DEFAULT_ENABLE_HYDE,
    ) -> None:
        self._llm = llm
        self._n_variants = max(0, n_variants)
        self._enable_hyde = enable_hyde

    async def rewrite(self, query: str) -> list[str]:
        """返回改写后的查询列表（含原始 query）。

        失败时返回 [query]（原始查询），不抛异常。
        """
        queries: list[str] = [query]
        # 并发生成变体 + HyDE
        tasks: list[Any] = []
        if self._n_variants > 0:
            tasks.append(self._gen_variants(query))
        if self._enable_hyde:
            tasks.append(self._gen_hyde(query))
        if not tasks:
            return queries
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:  # noqa: BLE001
            logger.exception("P1-5 query rewrite gather failed")
            return queries
        for r in results:
            if isinstance(r, Exception):
                logger.warning("P1-5 rewrite subtask failed: %s", r)
                continue
            if isinstance(r, list):
                queries.extend(r)
            elif isinstance(r, str) and r:
                queries.append(r)
        # 去空 + 去重（保持顺序，原始 query 始终首位）
        seen: set[str] = set()
        deduped: list[str] = []
        for q in queries:
            q_norm = q.strip()
            if not q_norm or q_norm in seen:
                continue
            seen.add(q_norm)
            deduped.append(q_norm)
        return deduped

    async def _gen_variants(self, query: str) -> list[str]:
        """生成 N 个查询变体。失败返回 []。"""
        messages = [
            Message(role="system", content=_REWRITE_SYSTEM),
            Message(
                role="user",
                content=f"问题：{query}\n请生成 {self._n_variants} 个改写查询。",
            ),
        ]
        resp = await self._llm.chat(messages)
        return _parse_variants(resp, self._n_variants)

    async def _gen_hyde(self, query: str) -> str:
        """生成 HyDE 假设文档。失败返回空串。"""
        messages = [
            Message(role="system", content=_HYDE_SYSTEM),
            Message(role="user", content=query),
        ]
        resp = await self._llm.chat(messages)
        return resp.content.strip()


def _parse_variants(resp: LLMResponse, n_expected: int) -> list[str]:
    """解析 LLM 返回的 JSON 数组为变体列表。

    LLM 输出不可控（可能包裹 ```json ```、可能多余文本），宽容解析：
    1. 尝试整体 JSON parse
    2. 失败则提取首个 [ ... ] 段再 parse
    3. 仍失败返回 []（降级）
    """
    raw = resp.content.strip()
    # 去除可能的 ```json ... ``` 包裹
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 提取首个 [ ... ] 段
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data[:n_expected] if item]


class MultiQueryMemoryBackend:
    """多 query 并发检索 + 去重的记忆后端。

    包装底层 ``MemoryBackend``，对 ``QueryRewriter.rewrite`` 产出的多个 query
    并发调用 ``search``，按 content 归一化去重。每个 content 的最终 score =
    出现频次（被多少个 query 召回），按频次降序取 top-k。

    所有异常降级为底层 backend 单 query 检索，不阻塞主流程。
    """

    def __init__(
        self,
        backend: MemoryBackend,
        rewriter: QueryRewriter,
    ) -> None:
        self._backend = backend
        self._rewriter = rewriter

    async def search(
        self, agent_id: uuid.UUID, query: str, top_k: int = _DEFAULT_TOP_K
    ) -> list[str]:
        """改写 + 并发检索 + 去重。

        失败降级为底层 backend 单 query 检索。
        """
        try:
            queries = await self._rewriter.rewrite(query)
        except Exception:  # noqa: BLE001
            logger.exception("P1-5 rewrite failed, fallback to single query")
            queries = [query]
        if len(queries) <= 1:
            return await self._backend.search(agent_id, query, top_k)
        # 并发检索
        results = await asyncio.gather(
            *(self._backend.search(agent_id, q, top_k) for q in queries),
            return_exceptions=True,
        )
        # 按 content 归一化去重，统计频次
        freq: dict[str, int] = {}
        order: list[str] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("P1-5 subquery search failed: %s", r)
                continue
            for content in r:
                norm = content.strip()
                if not norm:
                    continue
                if norm not in freq:
                    freq[norm] = 0
                    order.append(norm)
                freq[norm] += 1
        # 按频次降序，同频次保持首次出现顺序
        order.sort(key=lambda c: -freq[c])
        return order[:top_k]

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        turn: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """直接转发到底层 backend。"""
        await self._backend.upsert(
            agent_id=agent_id,
            session_id=session_id,
            turn=turn,
            content=content,
            metadata=metadata,
        )


class _NoOpRewriter:
    """占位用：rewriter 为 None 时退化为单 query。

    仅供 ``MultiQueryMemoryBackend`` 内部默认值使用，避免 Optional 分支。
    """

    async def rewrite(self, query: str) -> list[str]:
        return [query]


__all__ = [
    "MultiQueryMemoryBackend",
    "QueryRewriter",
]
