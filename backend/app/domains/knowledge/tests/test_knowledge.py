"""Knowledge Base — 单元测试。

覆盖 chunker 纯函数 + service（mock embedder，避免真实 API 调用）。
注意：VECTOR 类型在 SQLite 不可用，service 层测试仅覆盖 chunker 与 schema。
"""

from __future__ import annotations

import pytest

from app.domains.knowledge.chunker import chunk_text
from app.domains.knowledge.models import (
    KnowledgeBaseCreate,
    SearchQuery,
)
from app.domains.knowledge.embedder import _zero_vector


def test_chunk_text_basic() -> None:
    text = "a" * 2000
    chunks = chunk_text(text, chunk_size=800, overlap=100)
    assert len(chunks) >= 3
    assert all(len(c.content) <= 800 for c in chunks)
    assert chunks[0].index == 0
    assert chunks[1].index == 1


def test_chunk_text_overlap_boundary() -> None:
    """重叠区应使相邻 chunk 共享前 700-100 字符。"""
    text = "0123456789" * 100  # 1000 chars
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    if len(chunks) >= 2:
        # 第二块开头应出现在第一块末尾 20 字符之前
        overlap_text = chunks[0].content[-20:]
        assert chunks[1].content.startswith(overlap_text)


def test_chunk_text_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  \t ") == []


def test_chunk_text_invalid_params() -> None:
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=100, overlap=100)


def test_chunk_text_token_estimate() -> None:
    chunks = chunk_text("hello world 你好世界", chunk_size=800, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].token_count > 0


def test_zero_vector_dimension() -> None:
    from app.domains.knowledge.models import EMBEDDING_DIM

    vec = _zero_vector()
    assert len(vec) == EMBEDDING_DIM == 1536
    assert all(v == 0.0 for v in vec)


def test_kb_create_schema_defaults() -> None:
    payload = KnowledgeBaseCreate(name="test-kb")
    assert payload.chunk_size == 800
    assert payload.chunk_overlap == 100
    assert payload.embedding_model == "text-embedding-3-small"


def test_search_query_validation() -> None:
    q = SearchQuery(query="hello", top_k=3)
    assert q.top_k == 3
    assert q.score_threshold == 0.0
    with pytest.raises(Exception):
        SearchQuery(query="", top_k=0)
