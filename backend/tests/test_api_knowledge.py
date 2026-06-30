"""Knowledge Base API 契约测试 (/api/v1/knowledge-bases)。

VECTOR 列在 SQLite 上由根 conftest 渲染为 JSON，pgvector 的 bind_processor
会把向量列表序列化为字符串，因此文档上传可走真实路径（仅需 mock embedder
避免网络）。向量检索 ``cosine_distance`` (<=>) 在 SQLite 无对应算子，故
search / rag 通过 mock service 函数绕过。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401
from app.domains.knowledge import service as kb_service


def _create_kb(
    client: TestClient,
    *,
    name: str = "docs",
    description: str | None = "a kb",
) -> dict:
    resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": name, "description": description},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_knowledge_bases_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/knowledge-bases")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_knowledge_base_success(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "my-kb", "description": "知识库", "chunk_size": 1000},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["name"] == "my-kb"
    assert body["description"] == "知识库"
    assert body["embedding_model"] == "text-embedding-3-small"
    assert body["chunk_size"] == 1000
    assert body["chunk_overlap"] == 100
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_create_knowledge_base_validation_error(client: TestClient) -> None:
    # 缺少 name
    resp = client.post("/api/v1/knowledge-bases", json={"description": "no name"})
    assert resp.status_code == 422
    assert "detail" in resp.json()

    # chunk_size 超出范围 (<100)
    resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "bad", "chunk_size": 10},
    )
    assert resp.status_code == 422


def test_get_knowledge_base_by_id(client: TestClient) -> None:
    created = _create_kb(client, name="fetch-kb")
    resp = client.get(f"/api/v1/knowledge-bases/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["name"] == "fetch-kb"


def test_get_knowledge_base_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/knowledge-bases/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_upload_document(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # settings.openai_api_key="" 使 embed_text 直接返回 1536 维零向量（无网络调用），
    # pgvector Vector(1536) 维度校验通过，SQLite 上以 JSON 字符串存储。
    #
    # SQLite/aiosqlite 不支持 onupdate=func.now() 的 RETURNING，
    # upload_document 在 flush 前修改 doc.status 触发 onupdate，导致 updated_at 过期，
    # router 的 DocumentOut.model_validate(doc) 同步访问触发 MissingGreenlet。
    # PostgreSQL 生产环境通过 RETURNING 自动填充，此处仅 SQLite 测试需要 refresh 补偿。
    _original_upload = kb_service.upload_document

    async def _refreshing_upload(session, kb_id, title, content, mime_type=None, source_uri=None):  # type: ignore[no-untyped-def]
        doc = await _original_upload(session, kb_id, title, content, mime_type, source_uri)
        await session.refresh(doc)
        return doc

    monkeypatch.setattr(kb_service, "upload_document", _refreshing_upload)

    kb = _create_kb(client, name="upload-kb")
    content = "AIOps 是 AI 原生运营控制台。它聚合了 Prompt、Agent、知识库等能力。"
    resp = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        data={"title": "intro"},
        files={"file": ("intro.txt", content.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["knowledge_base_id"] == kb["id"]
    assert body["title"] == "intro"
    assert body["status"] == "ready"
    assert body["chunk_count"] >= 1
    assert body["size_bytes"] == len(content.encode("utf-8"))
    assert body["source_uri"] == "intro.txt"
    assert body["mime_type"] == "text/plain"
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_upload_document_empty_content(client: TestClient) -> None:
    """空内容应由 router 层抛 ValidationError (422)。"""
    kb = _create_kb(client, name="empty-upload-kb")
    resp = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        data={"title": "empty"},
        files={"file": ("empty.txt", b"   \n\t  ", "text/plain")},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert "message" in body


def test_search_knowledge_base(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    kb = _create_kb(client, name="search-kb")
    # mock service.search_kb（cosine_distance 在 SQLite 不可用）
    fake_results = [
        {
            "chunk_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()),
            "content": "matched chunk content",
            "score": 0.91,
            "metadata": {"title": "doc1"},
        }
    ]
    monkeypatch.setattr(kb_service, "search_kb", AsyncMock(return_value=fake_results))

    resp = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/search",
        json={"query": "AIOps", "top_k": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["content"] == "matched chunk content"
    assert item["score"] == 0.91
    assert item["metadata"]["title"] == "doc1"
    uuid.UUID(item["chunk_id"])
    uuid.UUID(item["document_id"])


def test_rag_query(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    kb = _create_kb(client, name="rag-kb")
    fake_rag = {
        "answer": "AIOps 是 AI 原生运营控制台。",
        "sources": [{"chunk_id": str(uuid.uuid4()), "content": "ctx", "score": 0.8}],
        "usage": {"total_tokens": 42},
    }
    monkeypatch.setattr(kb_service, "rag_query", AsyncMock(return_value=fake_rag))

    resp = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/rag",
        json={"question": "什么是 AIOps？", "top_k": 3},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "AIOps 是 AI 原生运营控制台。"
    assert isinstance(body["sources"], list) and len(body["sources"]) == 1
    assert body["usage"]["total_tokens"] == 42


def test_rag_query_validation_error(client: TestClient) -> None:
    kb = _create_kb(client, name="rag-validation-kb")
    # question 为空字符串应 422
    resp = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/rag",
        json={"question": ""},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()
