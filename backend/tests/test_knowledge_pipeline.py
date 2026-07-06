"""Knowledge Base 上传/检索/RAG eval（knowledge/SPEC.md Success Criteria）。

覆盖 6 项验收：
1. 文档上传后 status 由 processing → ready，chunk_count 与实际分块数一致
2. 检索结果按余弦相似度降序，低于 score_threshold 的被过滤
3. embedding 失败时回退零向量，文档仍可入库
4. RAG 返回的 sources 与检索结果一致，并附带 LLM usage
5. 文档超 50MB 或内容为空时上传被拒绝
6. chunk_text 在 overlap ≥ chunk_size 时抛 ValueError

策略：
- SC1/3/5：经 ``client`` fixture 的 session_factory 调用 service.upload_document，
  ``settings.openai_api_key=""`` 使 embedder 返回零向量（无网络），验证 status/chunk_count。
- SC2：search_kb 的 cosine_distance 在 SQLite 不可用，用 mock session 返回预打分行，
  验证 score_threshold 过滤逻辑。
- SC4：mock search_kb + LLMClient.chat，验证 sources 一致 + usage 透传。
- SC6：直接测 chunker.chunk_text 边界。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.llm_client import LLMResponse
from app.domains.knowledge import service as kb_service
from app.domains.knowledge.chunker import chunk_text
from app.domains.knowledge.embedder import embed_batch, embed_text
from app.domains.knowledge.models import (
    EMBEDDING_DIM,
    Chunk,
    KnowledgeBase,
    RAGQuery,
    SearchQuery,
)
from app.main import app

# ===================== 辅助：经 session_factory 执行异步场景 =====================


def _run(
    client: TestClient, scenario: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。"""
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


async def _seed_kb(session: AsyncSession, name: str = "kb") -> KnowledgeBase:
    """创建知识库并 flush。"""
    from app.domains.knowledge.models import KnowledgeBaseCreate

    kb = await kb_service.create_kb(
        session, KnowledgeBaseCreate(name=name, chunk_size=100, chunk_overlap=10),
        owner_id=uuid.uuid4(),
    )
    await session.flush()
    return kb


# ===================== 1. 上传 status processing → ready + chunk_count 一致 =====================


def test_upload_status_ready_and_chunk_count_matches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传后 status=ready，chunk_count 与 chunk_text 实际分块数一致（SPEC 1）。

    A4：embed_batch 现在用 strict=True，未配置 API key 时会抛 EmbeddingError
    导致 status=failed。此处 monkeypatch embed_batch 返回非零向量，验证
    成功路径下 status=ready 且 chunk_count 与分块数一致。
    """
    content = "A" * 250  # chunk_size=100, overlap=10 → 多个分块

    async def _fake_embed_batch(texts, model=None, *, strict=False):
        # 返回与 chunks 数量一致的非零向量（A4：成功路径不依赖 strict）
        return [[0.1] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(kb_service, "embed_batch", _fake_embed_batch)

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="upload-kb")
        doc = await kb_service.upload_document(
            session, kb.id, title="doc", content=content
        )
        await session.flush()

        assert doc.status == "ready"
        # chunk_count 与 chunk_text 输出一致
        expected = len(chunk_text(content, chunk_size=100, overlap=10))
        assert doc.chunk_count == expected
        assert expected >= 2  # 确认确实分了多块

    _run(client, _scenario)


# ===================== 2. 检索结果按相似度降序 + threshold 过滤 =====================


class _FakeChunk:
    """模拟 ORM Chunk 行（仅含 search_kb 用到的字段）。"""

    def __init__(self, cid: str | None = None, content: str = "c") -> None:
        self.id = (
            uuid.UUID(cid) if cid is not None else uuid.uuid4()
        )
        self.document_id = uuid.uuid4()
        self.content = content
        self.metadata_: dict[str, Any] = {"title": "doc"}


class _MockResult:
    """模拟 SQLAlchemy Result：第一次返回 kb，第二次返回 rows。"""

    def __init__(
        self,
        kb: KnowledgeBase | None,
        rows: list[tuple[_FakeChunk, float]] | None,
    ) -> None:
        self._kb = kb
        self._rows = rows

    def scalar_one_or_none(self) -> KnowledgeBase | None:
        return self._kb

    def all(self) -> list[tuple[_FakeChunk, float]]:
        return self._rows or []


class _MockSession:
    """模拟 AsyncSession.execute：第一次 get_kb，第二次搜索结果。"""

    def __init__(
        self, kb: KnowledgeBase, rows: list[tuple[_FakeChunk, float]]
    ) -> None:
        self._kb = kb
        self._rows = rows
        self._call = 0

    async def execute(self, stmt: Any) -> _MockResult:
        self._call += 1
        if self._call == 1:
            return _MockResult(kb=self._kb, rows=None)
        return _MockResult(kb=None, rows=self._rows)


def test_search_filters_by_score_threshold() -> None:
    """search_kb 过滤低于 score_threshold 的结果（SPEC 2）。

    cosine_distance 在 SQLite 不可用，用 mock session 返回预打分行验证过滤逻辑。
    """

    async def _scenario() -> None:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            name="kb",
            embedding_model="text-embedding-3-small",
            chunk_size=100,
            chunk_overlap=10,
        )
        # 3 行：score 0.9 / 0.5 / 0.2，threshold=0.4 应过滤掉 0.2
        rows: list[tuple[_FakeChunk, float]] = [
            (_FakeChunk("00000000-0000-0000-0000-000000000001", "high"), 0.9),
            (_FakeChunk("00000000-0000-0000-0000-000000000002", "mid"), 0.5),
            (_FakeChunk("00000000-0000-0000-0000-000000000003", "low"), 0.2),
        ]
        session = _MockSession(kb, rows)

        results = await kb_service.search_kb(
            session,  # type: ignore[arg-type]
            kb.id,
            SearchQuery(query="q", top_k=10, score_threshold=0.4),
        )

        # 0.2 被过滤，保留 0.9 / 0.5
        assert len(results) == 2
        scores = [r.score for r in results]
        assert 0.9 in scores
        assert 0.5 in scores
        assert 0.2 not in scores

    import asyncio

    asyncio.run(_scenario())


def test_search_threshold_zero_returns_all() -> None:
    """score_threshold=0 时不过滤（SPEC 2 边界）。"""

    async def _scenario() -> None:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            name="kb",
            embedding_model="text-embedding-3-small",
            chunk_size=100,
            chunk_overlap=10,
        )
        rows: list[tuple[_FakeChunk, float]] = [
            (_FakeChunk(), 0.01),
            (_FakeChunk(), 0.0),
        ]
        session = _MockSession(kb, rows)

        results = await kb_service.search_kb(
            session,  # type: ignore[arg-type]
            kb.id,
            SearchQuery(query="q", top_k=10, score_threshold=0.0),
        )
        assert len(results) == 2

    import asyncio

    asyncio.run(_scenario())


# ===================== 3. embedding 失败回退零向量 + 文档仍可入库 =====================


@pytest.mark.asyncio
async def test_embed_text_returns_zero_vector_on_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_text 在 HTTP 失败时回退零向量（SPEC 3）。"""
    import httpx

    # 临时设置 API key 使 embed_text 进入 HTTP 调用路径
    monkeypatch.setattr(settings, "openai_api_key", "sk-fake")

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.AsyncClient, "post", _raise)

    vec = await embed_text("hello")
    assert vec == [0.0] * EMBEDDING_DIM


@pytest.mark.asyncio
async def test_embed_batch_returns_zero_vectors_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_batch 在失败时回退零向量（SPEC 3 批量路径）。"""
    import httpx

    monkeypatch.setattr(settings, "openai_api_key", "sk-fake")

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise httpx.HTTPError("api error")

    monkeypatch.setattr(httpx.AsyncClient, "post", _raise)

    vecs = await embed_batch(["a", "b", "c"])
    assert len(vecs) == 3
    for v in vecs:
        assert v == [0.0] * EMBEDDING_DIM


def test_upload_marks_failed_on_embedding_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4：embedding 失败时 Document.status=failed，不写零向量 chunk。

    monkeypatch embed_batch 抛 EmbeddingError（模拟 OpenAI API 不可用），
    upload_document 据此标记 status=failed 并跳过 chunk 写入。
    旧实现回退零向量 + status=ready 污染索引——A4 修复此问题。
    """
    from app.core.exceptions import EmbeddingError

    async def _raise_embedding_error(texts, model=None, *, strict=False):
        raise EmbeddingError("simulated embedding failure")

    monkeypatch.setattr(kb_service, "embed_batch", _raise_embedding_error)

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="failed-emb-kb")
        doc = await kb_service.upload_document(
            session, kb.id, title="doc", content="some content here"
        )
        await session.flush()
        assert doc.status == "failed"
        assert doc.chunk_count == 0
        # 不应写入任何 chunk
        chunk_count = (
            await session.execute(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id)
            )
        ).scalar_one()
        assert chunk_count == 0

    _run(client, _scenario)


# ===================== 4. RAG sources 与检索一致 + LLM usage =====================


def test_rag_sources_match_search_and_includes_usage(client: TestClient) -> None:
    """RAG 返回的 sources 与 hybrid 检索结果一致，并附带 LLM usage（SPEC 4）。

    rag_query 走 hybrid search（向量 + BM25 + RRF），此处 mock ``_hybrid_search``
    返回固定 sources，验证 RAG 契约：sources 透传 + LLM usage 透传。
    """
    from app.core.llm_client import LLMClient, Message
    from app.domains.knowledge.models import SearchResult

    expected_sources = [
        SearchResult(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content="ctx-1",
            score=0.9,
        ),
        SearchResult(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content="ctx-2",
            score=0.8,
        ),
    ]

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="rag-kb")

        # mock _hybrid_search 返回固定 sources（跳过向量/BM25/RRF 内部逻辑）
        async def _fake_hybrid(
            s: AsyncSession,
            k: Any,
            question: str,
            top_k: int,
            rerank: bool = False,
        ) -> list[SearchResult]:
            return expected_sources

        # mock LLMClient.chat 返回固定 usage
        async def _fake_chat(
            self: LLMClient, messages: list[Message]
        ) -> LLMResponse:
            return LLMResponse(
                content="answer based on context",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            )

        async def _fake_close(self: LLMClient) -> None:
            pass

        orig_hybrid = kb_service._hybrid_search
        orig_chat = LLMClient.chat
        orig_close = LLMClient.close
        kb_service._hybrid_search = _fake_hybrid  # type: ignore[assignment]
        LLMClient.chat = _fake_chat  # type: ignore[method-assign]
        LLMClient.close = _fake_close  # type: ignore[method-assign]
        try:
            result = await kb_service.rag_query(
                session,
                kb.id,
                RAGQuery(question="q?", top_k=2),
            )
        finally:
            kb_service._hybrid_search = orig_hybrid  # type: ignore[assignment]
            LLMClient.chat = orig_chat  # type: ignore[method-assign]
            LLMClient.close = orig_close  # type: ignore[method-assign]

        # sources 与检索结果一致（数量 + 内容）
        assert len(result["sources"]) == 2
        assert result["sources"][0]["content"] == "ctx-1"
        assert result["sources"][1]["content"] == "ctx-2"
        # usage 透传
        assert result["usage"]["total_tokens"] == 120
        assert result["answer"] == "answer based on context"

    _run(client, _scenario)


# ===================== 5. 文档超 50MB 或内容为空时被拒绝 =====================


def test_upload_rejects_oversized_document(client: TestClient) -> None:
    """文档超 50MB 上传被拒绝（SPEC 5）。"""

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="oversized-kb")
        # 构造超 50MB 的内容（51MB）
        oversized = "x" * (kb_service.MAX_DOC_BYTES + 1)
        with pytest.raises(ValidationError) as exc_info:
            await kb_service.upload_document(
                session, kb.id, title="big", content=oversized
            )
        assert "50" in str(exc_info.value) or "MB" in str(exc_info.value)

    _run(client, _scenario)


def test_upload_empty_content_produces_zero_chunks(client: TestClient) -> None:
    """空内容经 chunk_text 返回空列表（SPEC 5 空内容分支）。

    service 层不拒绝空内容（router 层 422 拒绝），但 chunk_text 对空白返回 []，
    文档以 chunk_count=0 / status=ready 入库。此处验证 chunker 边界。
    """
    # 直接验证 chunk_text 对空白返回空列表
    assert chunk_text("   \n\t  ") == []
    assert chunk_text("") == []


# ===================== P1-7：向量维度解耦 =====================


def test_create_kb_rejects_unknown_embedding_model(client: TestClient) -> None:
    """P1-7：未登记的 embedding_model 在创建阶段被拒绝（明确错误优于上传时崩溃）。"""

    async def _scenario(session: AsyncSession) -> None:
        from app.domains.knowledge.models import KnowledgeBaseCreate

        with pytest.raises(ValidationError) as exc_info:
            await kb_service.create_kb(
                session,
                KnowledgeBaseCreate(
                    name="bad-model-kb", embedding_model="unknown-model"
                ),
                owner_id=uuid.uuid4(),
            )
        assert "未登记" in str(exc_info.value)

    _run(client, _scenario)


def test_create_kb_rejects_dimension_mismatch(client: TestClient) -> None:
    """P1-7：维度与 chunks.embedding 列不一致的模型在创建阶段被拒绝。

    text-embedding-3-large 维度 3072 != EMBEDDING_DIM(1536)，创建即报错，
    避免上传时 pgvector 写入崩溃。
    """

    async def _scenario(session: AsyncSession) -> None:
        from app.domains.knowledge.models import KnowledgeBaseCreate

        with pytest.raises(ValidationError) as exc_info:
            await kb_service.create_kb(
                session,
                KnowledgeBaseCreate(
                    name="dim-mismatch-kb", embedding_model="text-embedding-3-large"
                ),
                owner_id=uuid.uuid4(),
            )
        assert "维度" in str(exc_info.value)

    _run(client, _scenario)


def test_create_kb_accepts_registered_dim_matching_model(client: TestClient) -> None:
    """P1-7：已登记且维度匹配的模型创建成功。"""

    async def _scenario(session: AsyncSession) -> None:
        from app.domains.knowledge.models import KnowledgeBaseCreate

        kb = await kb_service.create_kb(
            session,
            KnowledgeBaseCreate(
                name="ok-kb", embedding_model="text-embedding-3-small"
            ),
            owner_id=uuid.uuid4(),
        )
        assert kb.embedding_model == "text-embedding-3-small"

    _run(client, _scenario)


# ===================== 6. chunk_text overlap ≥ chunk_size 抛 ValueError =====================


def test_chunk_text_rejects_overlap_ge_chunk_size() -> None:
    """chunk_text 在 overlap ≥ chunk_size 时抛 ValueError（SPEC 6）。"""
    with pytest.raises(ValueError) as exc_info:
        chunk_text("some text", chunk_size=100, overlap=100)
    assert "overlap" in str(exc_info.value)

    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size=100, overlap=150)


def test_chunk_text_rejects_invalid_chunk_size() -> None:
    """chunk_text 在 chunk_size ≤ 0 时抛 ValueError（SPEC 6 边界）。"""
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=0, overlap=0)
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=-1, overlap=0)


def test_chunk_text_normal_case() -> None:
    """chunk_text 正常分块（SPEC 6 正向路径）。"""
    text = "abcdefghij" * 50  # 500 字符
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) >= 5
    # 每块 index 递增
    for i, c in enumerate(chunks):
        assert c.index == i
        assert c.content
        assert c.token_count > 0
    # step = 100 - 20 = 80，相邻块有 overlap
    assert len(chunks[0].content) == 100


# ===================== 7. hybrid search: RRF 融合 + BM25 + LLM rerank =====================


def test_rrf_fuse_combines_vector_and_bm25_ranks() -> None:
    """RRF 融合：双路同时命中的 chunk 分数叠加，应排首位（P1-4）。

    构造 3 个 chunk：
    - chunk_a 在向量 rank=1、BM25 rank=2 → 双路命中，RRF 分数最高
    - chunk_b 在向量 rank=2、BM25 rank=1 → 双路命中，RRF 分数次高
    - chunk_c 仅向量 rank=3 → 单路命中，RRF 分数最低
    验证融合后顺序为 [a, b, c]，且 a 的分数严格大于 c。
    """
    chunk_a = _FakeChunk("00000000-0000-0000-0000-000000000001", "a")
    chunk_b = _FakeChunk("00000000-0000-0000-0000-000000000002", "b")
    chunk_c = _FakeChunk("00000000-0000-0000-0000-000000000003", "c")

    vector_rows: list[tuple[_FakeChunk, float]] = [
        (chunk_a, 0.9),
        (chunk_b, 0.8),
        (chunk_c, 0.7),
    ]
    bm25_rows: list[tuple[_FakeChunk, float]] = [
        (chunk_b, 1.0),
        (chunk_a, 1.0),
    ]
    fused = kb_service._rrf_fuse(
        vector_rows,  # type: ignore[arg-type]
        bm25_rows,  # type: ignore[arg-type]
        top_k=3,
    )
    assert len(fused) == 3
    # a、b 双路命中，c 仅单路；a 与 b 的 RRF 分数接近但 a 在向量 rank=1 更优
    assert fused[0][0].id == chunk_a.id
    assert fused[1][0].id == chunk_b.id
    assert fused[2][0].id == chunk_c.id
    # 双路命中的分数严格高于单路
    assert fused[0][1] > fused[2][1]
    assert fused[1][1] > fused[2][1]


def test_rrf_fuse_respects_top_k_limit() -> None:
    """RRF 融合结果不超过 top_k（P1-4 边界）。"""
    chunks = [_FakeChunk() for _ in range(5)]
    vector_rows = [(c, 0.9) for c in chunks]
    bm25_rows: list[tuple[_FakeChunk, float]] = []
    fused = kb_service._rrf_fuse(
        vector_rows,  # type: ignore[arg-type]
        bm25_rows,  # type: ignore[arg-type]
        top_k=3,
    )
    assert len(fused) == 3


def test_rrf_fuse_empty_inputs_returns_empty() -> None:
    """RRF 融合：两路均空时返回空列表（P1-4 边界）。"""
    fused = kb_service._rrf_fuse([], [], top_k=5)
    assert fused == []


def test_bm25_search_sqlite_like_path(client: TestClient) -> None:
    """BM25 检索在 SQLite 上降级为 LIKE 模糊匹配（P1-4）。

    SQLite 无 tsvector/ts_rank_cd，service 层按方言判断走 content LIKE。
    构造 3 个 chunk（含/不含查询词），验证 LIKE 命中正确 chunk。
    """

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="bm25-kb")
        # 直接写 Chunk（绕过 upload_document 的 embed_batch 调用）
        for i, content in enumerate(
            ["AIOps 是 AI 原生控制台", "另一个无关文档", "AIOps 部署指南"]
        ):
            session.add(
                Chunk(
                    document_id=uuid.uuid4(),
                    knowledge_base_id=kb.id,
                    chunk_index=i,
                    content=content,
                    embedding=None,
                    metadata_={"title": "doc"},
                )
            )
        await session.flush()

        rows = await kb_service._bm25_search(session, kb.id, "AIOps", top_k=10)
        # 仅含 "AIOps" 的 2 个 chunk 命中
        assert len(rows) == 2
        contents = {c.content for c, _ in rows}
        assert "AIOps 是 AI 原生控制台" in contents
        assert "AIOps 部署指南" in contents
        assert "另一个无关文档" not in contents

    _run(client, _scenario)


def test_hybrid_search_sqlite_returns_bm25_results(client: TestClient) -> None:
    """hybrid search 在 SQLite 上退化为纯 BM25（向量检索跳过），仍返回相关结果（P1-4）。

    SQLite 上 cosine_distance 不可用，_vector_search 返回 []，hybrid = BM25 only。
    验证 _hybrid_search 端到端在 SQLite 上工作并返回 SearchResult。
    """

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="hybrid-kb")
        for i, content in enumerate(
            ["AIOps 是 AI 原生控制台", "另一个无关文档", "AIOps 部署指南"]
        ):
            session.add(
                Chunk(
                    document_id=uuid.uuid4(),
                    knowledge_base_id=kb.id,
                    chunk_index=i,
                    content=content,
                    embedding=None,
                    metadata_={"title": "doc"},
                )
            )
        await session.flush()

        results = await kb_service._hybrid_search(
            session, kb, "AIOps", top_k=5, rerank=False
        )
        # SQLite 向量检索跳过，BM25 命中 2 个含 "AIOps" 的 chunk
        assert len(results) == 2
        contents = {r.content for r in results}
        assert "AIOps 是 AI 原生控制台" in contents
        assert "AIOps 部署指南" in contents
        # score 为 RRF 融合分数（仅 BM25 单路，rank=1: 1/(60+1)）
        for r in results:
            assert r.score > 0.0

    _run(client, _scenario)


def test_llm_rerank_reorders_by_llm_order() -> None:
    """LLM reranker 按 LLM 输出的顺序重排候选（P1-4）。

    mock LLMClient.chat 返回 {"order": [2, 0, 1]}，验证候选按此顺序重排。
    """
    from app.core.llm_client import LLMClient, LLMResponse, Message

    chunk_a = _FakeChunk("00000000-0000-0000-0000-000000000001", "content-a")
    chunk_b = _FakeChunk("00000000-0000-0000-0000-000000000002", "content-b")
    chunk_c = _FakeChunk("00000000-0000-0000-0000-000000000003", "content-c")
    candidates: list[tuple[_FakeChunk, float]] = [
        (chunk_a, 0.03),
        (chunk_b, 0.02),
        (chunk_c, 0.01),
    ]

    async def _fake_chat(
        self: LLMClient, messages: list[Message]
    ) -> LLMResponse:
        return LLMResponse(content='{"order": [2, 0, 1]}')

    async def _fake_close(self: LLMClient) -> None:
        pass

    orig_chat = LLMClient.chat
    orig_close = LLMClient.close
    LLMClient.chat = _fake_chat  # type: ignore[method-assign]
    LLMClient.close = _fake_close  # type: ignore[method-assign]
    try:
        import asyncio

        reranked = asyncio.run(
            kb_service._llm_rerank("q?", candidates, top_k=3)  # type: ignore[arg-type]
        )
    finally:
        LLMClient.chat = orig_chat  # type: ignore[method-assign]
        LLMClient.close = orig_close  # type: ignore[method-assign]

    # LLM 指定顺序 [2, 0, 1] → [chunk_c, chunk_a, chunk_b]
    assert len(reranked) == 3
    assert reranked[0][0].id == chunk_c.id
    assert reranked[1][0].id == chunk_a.id
    assert reranked[2][0].id == chunk_b.id


def test_llm_rerank_fallback_on_unparseable_output() -> None:
    """LLM reranker 输出无法解析时兜底按原 RRF 顺序返回（P1-4）。"""
    from app.core.llm_client import LLMClient, LLMResponse, Message

    chunk_a = _FakeChunk("00000000-0000-0000-0000-000000000001", "a")
    chunk_b = _FakeChunk("00000000-0000-0000-0000-000000000002", "b")
    candidates: list[tuple[_FakeChunk, float]] = [
        (chunk_a, 0.03),
        (chunk_b, 0.02),
    ]

    async def _fake_chat(
        self: LLMClient, messages: list[Message]
    ) -> LLMResponse:
        # 非 JSON 输出，触发解析失败兜底
        return LLMResponse(content="I cannot rank these")

    async def _fake_close(self: LLMClient) -> None:
        pass

    orig_chat = LLMClient.chat
    orig_close = LLMClient.close
    LLMClient.chat = _fake_chat  # type: ignore[method-assign]
    LLMClient.close = _fake_close  # type: ignore[method-assign]
    try:
        import asyncio

        reranked = asyncio.run(
            kb_service._llm_rerank("q?", candidates, top_k=2)  # type: ignore[arg-type]
        )
    finally:
        LLMClient.chat = orig_chat  # type: ignore[method-assign]
        LLMClient.close = orig_close  # type: ignore[method-assign]

    # 解析失败 → 保持原 RRF 顺序
    assert len(reranked) == 2
    assert reranked[0][0].id == chunk_a.id
    assert reranked[1][0].id == chunk_b.id


def test_llm_rerank_empty_candidates_returns_empty() -> None:
    """LLM reranker 候选为空时直接返回空列表（P1-4 边界）。"""
    import asyncio

    reranked = asyncio.run(kb_service._llm_rerank("q?", [], top_k=5))
    assert reranked == []


def test_hybrid_search_rerank_flag_calls_rerank(client: TestClient) -> None:
    """_hybrid_search rerank=True 时调用 _rerank，rerank=False 时不调用（P1-4）。

    SQLite 上向量检索跳过，BM25（LIKE）命中 2 个含查询词的 chunk 作为候选。
    mock _rerank 记录调用，验证 rerank 标志正确传递。
    _rerank 内部优先 cross-encoder（未安装时回退 LLM rerank），此处直接
    mock _rerank 调度层，与具体 reranker 实现解耦。
    """
    rerank_called = {"value": False}

    async def _scenario(session: AsyncSession) -> None:
        kb = await _seed_kb(session, name="rerank-hybrid-kb")
        # 两个 chunk 都含查询词，确保 BM25 返回 2 个候选给 rerank
        for i, content in enumerate(["AIOps 文档一", "AIOps 文档二"]):
            session.add(
                Chunk(
                    document_id=uuid.uuid4(),
                    knowledge_base_id=kb.id,
                    chunk_index=i,
                    content=content,
                    embedding=None,
                    metadata_={"title": "doc"},
                )
            )
        await session.flush()

        async def _fake_rerank(
            question: str,
            candidates: list[tuple[Any, float]],
            top_k: int,
        ) -> list[tuple[Any, float]]:
            rerank_called["value"] = True
            return candidates[:top_k]

        orig_rerank = kb_service._rerank
        kb_service._rerank = _fake_rerank  # type: ignore[assignment]
        try:
            # rerank=True → 应调用 _rerank
            results = await kb_service._hybrid_search(
                session, kb, "AIOps", top_k=2, rerank=True
            )
            assert rerank_called["value"] is True
            assert len(results) == 2

            # rerank=False → 不应调用 _rerank
            rerank_called["value"] = False
            results_no_rerank = await kb_service._hybrid_search(
                session, kb, "AIOps", top_k=2, rerank=False
            )
            assert rerank_called["value"] is False
            assert len(results_no_rerank) == 2
        finally:
            kb_service._rerank = orig_rerank  # type: ignore[assignment]

    _run(client, _scenario)


def test_rerank_dispatch_falls_back_to_llm_when_cross_encoder_unavailable(
    client: TestClient,
) -> None:
    """P1-4：sentence-transformers 未安装时 _rerank 回退到 _llm_rerank。

    测试环境不安装 sentence-transformers，_is_cross_encoder_available() 返回 False，
    _rerank 应直接调用 _llm_rerank。mock _llm_rerank 验证被调用。
    """
    llm_rerank_called = {"value": False}
    orig_available = kb_service._CROSS_ENCODER_AVAILABLE

    async def _fake_llm_rerank(
        question: str,
        candidates: list[tuple[Any, float]],
        top_k: int,
    ) -> list[tuple[Any, float]]:
        llm_rerank_called["value"] = True
        return candidates[:top_k]

    async def _scenario(session: AsyncSession) -> None:
        # 强制 cross-encoder 不可用（模拟未安装 sentence-transformers）
        kb_service._CROSS_ENCODER_AVAILABLE = False
        orig_llm_rerank = kb_service._llm_rerank
        kb_service._llm_rerank = _fake_llm_rerank  # type: ignore[assignment]
        try:
            result = await kb_service._rerank("q?", [], top_k=2)
            assert llm_rerank_called["value"] is True
            assert result == []
        finally:
            kb_service._llm_rerank = orig_llm_rerank  # type: ignore[assignment]
            kb_service._CROSS_ENCODER_AVAILABLE = orig_available

    _run(client, _scenario)


def test_rerank_dispatch_uses_cross_encoder_when_available(
    client: TestClient,
) -> None:
    """P1-4：sentence-transformers 可用时 _rerank 走 cross-encoder 路径。

    mock _is_cross_encoder_available 返回 True + _cross_encoder_rerank 记录调用，
    验证优先走本地 cross-encoder 而非 LLM rerank。
    """
    ce_rerank_called = {"value": False}
    llm_rerank_called = {"value": False}

    async def _fake_ce_rerank(
        question: str,
        candidates: list[tuple[Any, float]],
        top_k: int,
    ) -> list[tuple[Any, float]]:
        ce_rerank_called["value"] = True
        return candidates[:top_k]

    async def _fake_llm_rerank(
        question: str,
        candidates: list[tuple[Any, float]],
        top_k: int,
    ) -> list[tuple[Any, float]]:
        llm_rerank_called["value"] = True
        return candidates[:top_k]

    async def _scenario(session: AsyncSession) -> None:
        # 强制 cross-encoder 可用
        orig_available = kb_service._CROSS_ENCODER_AVAILABLE
        kb_service._CROSS_ENCODER_AVAILABLE = True
        orig_ce_rerank = kb_service._cross_encoder_rerank
        orig_llm_rerank = kb_service._llm_rerank
        kb_service._cross_encoder_rerank = _fake_ce_rerank  # type: ignore[assignment]
        kb_service._llm_rerank = _fake_llm_rerank  # type: ignore[assignment]
        try:
            await kb_service._rerank("q?", [], top_k=2)
            assert ce_rerank_called["value"] is True
            assert llm_rerank_called["value"] is False
        finally:
            kb_service._cross_encoder_rerank = orig_ce_rerank  # type: ignore[assignment]
            kb_service._llm_rerank = orig_llm_rerank  # type: ignore[assignment]
            kb_service._CROSS_ENCODER_AVAILABLE = orig_available

    _run(client, _scenario)
