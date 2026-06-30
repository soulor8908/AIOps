"""健康检查与元数据 API 契约测试。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401  (契约测试要求显式导入 app)


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert isinstance(body["version"], str)


def test_root_returns_service_info(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "aiops-console"
    assert body["docs"] == "/docs"


def test_openapi_spec_accessible(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "openapi" in spec
    assert "info" in spec
    assert spec["info"]["title"] == "AIOps Console"
    assert "paths" in spec
    # 聚合路由前缀下应包含各领域路径
    paths = spec["paths"]
    assert "/api/v1/prompts" in paths
    assert "/api/v1/agents" in paths
    assert "/api/v1/analytics/dashboard" in paths


def test_docs_accessible(client: TestClient) -> None:
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_unknown_route_returns_404(client: TestClient) -> None:
    resp = client.get("/api/v1/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    # FastAPI 默认 404 格式
    assert "detail" in body


def test_error_response_format(client: TestClient) -> None:
    """AppError 统一错误格式：{error, message, detail}。

    触发一个 NotFoundError（GET 不存在的 prompt）。
    """
    resp = client.get(f"/api/v1/prompts/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "message" in body
    assert body["error"] == "not_found"
    assert isinstance(body["message"], str) and body["message"]
