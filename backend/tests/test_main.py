"""app/main.py 单元测试 — FastAPI 入口端点。

使用 TestClient（由 tests/conftest.py 的 ``client`` fixture 提供 SQLite 隔离）。

覆盖：
- /health 健康检查
- / 根路径
- AppError 全局异常处理器
- 422 验证错误统一格式
- CORS 头
- /docs 和 /openapi.json 可访问
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError


# ===================== health =====================

def test_health_endpoint(client: TestClient) -> None:
    """GET /health 返回 200 和 {status: ok, version}。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert isinstance(data["version"], str)


# ===================== root =====================

def test_root_endpoint(client: TestClient) -> None:
    """GET / 返回 {service: aiops-console}。"""
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "aiops-console"
    assert "docs" in data


# ===================== AppError handler =====================

def test_app_error_handler(client: TestClient) -> None:
    """触发 AppError 时返回统一错误格式。"""
    from app.main import app

    async def raise_not_found() -> None:
        raise NotFoundError("resource missing", detail="id=abc")

    app.add_api_route("/__test_app_error__", raise_not_found, methods=["GET"])
    try:
        resp = client.get("/__test_app_error__")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "not_found"
        assert body["message"] == "resource missing"
        assert body["detail"] == "id=abc"
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_app_error__"
        ]


def test_app_error_handler_different_status(client: TestClient) -> None:
    """不同 AppError 子类返回不同 status_code。"""
    from app.core.exceptions import AuthenticationError
    from app.main import app

    async def raise_auth_error() -> None:
        raise AuthenticationError("token invalid")

    app.add_api_route("/__test_auth_err__", raise_auth_error, methods=["GET"])
    try:
        resp = client.get("/__test_auth_err__")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"] == "authentication_error"
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_auth_err__"
        ]


# ===================== validation error =====================

def test_validation_error_handler(client: TestClient) -> None:
    """发送无效数据时返回 422 统一格式。"""
    # AgentCreate 要求 name min_length=1，发送空 name 应触发 422
    # client fixture 已覆盖 get_session，body 校验失败在依赖解析之前
    resp = client.post("/api/v1/agents", json={"name": ""})
    assert resp.status_code == 422
    body = resp.json()
    # FastAPI 默认 422 格式包含 detail
    assert "detail" in body


def test_validation_error_missing_body(client: TestClient) -> None:
    """缺少必填 body 时返回 422。"""
    resp = client.post("/api/v1/agents")
    assert resp.status_code == 422


# ===================== CORS =====================

def test_cors_headers(client: TestClient) -> None:
    """验证 CORS 头存在。"""
    # 简单请求带 Origin
    resp = client.get("/health", headers={"Origin": "http://example.com"})
    assert resp.status_code == 200
    # CORS 中间件应添加 access-control-allow-origin
    assert resp.headers.get("access-control-allow-origin") is not None


def test_cors_preflight(client: TestClient) -> None:
    """CORS 预检请求返回正确头。"""
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is not None
    # 允许所有方法
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    assert "GET" in allow_methods or "*" in allow_methods


# ===================== OpenAPI docs =====================

def test_openapi_docs_available(client: TestClient) -> None:
    """/docs 和 /openapi.json 可访问。"""
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")

    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["title"] == "AIOps Console"
    # 确认路径已注册
    assert "/health" in data["paths"]
    assert "/" in data["paths"]


def test_openapi_contains_api_routes(client: TestClient) -> None:
    """OpenAPI 包含 /api/v1 路由。"""
    resp = client.get("/openapi.json")
    data = resp.json()
    api_paths = [p for p in data["paths"] if p.startswith("/api/v1")]
    assert len(api_paths) > 0
